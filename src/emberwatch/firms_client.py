"""Live NASA FIRMS client: URL building, rate-limit protection and retries.

The MAP_KEY appears only in the request URL path (that is how FIRMS works). It is
redacted from everything we store or log: `FetchResult.url_redacted` is what
goes into the raw-file record.
"""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Deque, Dict, Optional, Tuple

from .config import Settings
from .logutil import get_logger
from .sources import Product

BBox = Tuple[float, float, float, float]


# ----------------------------------------------------------------------------
# errors
# ----------------------------------------------------------------------------
class ApiFatalError(Exception):
    """Stop the whole run (bad key, rate limit exhausted)."""


class AuthError(ApiFatalError):
    pass


class RateLimitExceeded(ApiFatalError):
    pass


class ApiError(Exception):
    """This one request failed; other requests may still work."""


class TransportError(Exception):
    """Network-level failure (timeout, DNS, connection reset)."""


# ----------------------------------------------------------------------------
# result / transport
# ----------------------------------------------------------------------------
@dataclass
class HttpResponse:
    status: int
    content: bytes
    headers: Dict[str, str]


@dataclass
class FetchResult:
    url_redacted: str
    http_status: int
    content: bytes
    kind: str                 # "csv" (header present, maybe zero rows) | "error_text"
    attempts: int = 1
    est_cost: float = 0.0
    message: str = ""


class RequestsTransport:
    """Default transport using the `requests` library."""

    def __init__(self, user_agent: str = "EmberWatch/1.0 (NASA Space Apps hackathon)"):
        import requests  # imported lazily so tests/demo mode do not need the network stack

        self._requests = requests
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})

    def get(self, url: str, timeout: int) -> HttpResponse:
        try:
            r = self._session.get(url, timeout=timeout)
        except self._requests.exceptions.RequestException as exc:
            # str(exc) may contain the URL (and key); callers redact before logging.
            raise TransportError(type(exc).__name__) from None
        return HttpResponse(r.status_code, r.content, {k: v for k, v in r.headers.items()})


# ----------------------------------------------------------------------------
# rate limiter
# ----------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window budget of 'transactions' plus a minimum gap between requests.

    FIRMS allows 5,000 transactions per 10 minutes per MAP_KEY. We stay far below
    that (default 1,000) because the real cost of one area request is not
    documented precisely; see estimate_cost().
    """

    def __init__(
        self,
        max_cost: float,
        window_seconds: float,
        min_interval_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_wait_s: Optional[float] = None,
    ):
        self.max_cost = float(max_cost)
        self.window = float(window_seconds)
        self.min_interval = float(min_interval_s)
        self._clock = clock
        self._sleep = sleep
        self.max_wait = float(max_wait_s) if max_wait_s is not None else self.window + 30.0
        self._events: Deque[Tuple[float, float]] = deque()
        self._last_request: Optional[float] = None
        self.total_cost = 0.0
        self.total_requests = 0

    def _purge(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= self.window:
            self._events.popleft()

    def used(self) -> float:
        self._purge(self._clock())
        return sum(c for _, c in self._events)

    def acquire(self, cost: float) -> None:
        cost = min(float(cost), self.max_cost)  # a single request can never exceed the whole budget
        waited = 0.0
        while True:
            now = self._clock()
            self._purge(now)
            used = sum(c for _, c in self._events)
            gap_wait = 0.0
            if self._last_request is not None:
                gap_wait = max(0.0, self.min_interval - (now - self._last_request))
            if used + cost <= self.max_cost:
                if gap_wait > 0:
                    self._sleep(gap_wait)
                    now = self._clock()
                self._events.append((now, cost))
                self._last_request = now
                self.total_cost += cost
                self.total_requests += 1
                return
            oldest = self._events[0][0]
            wait = max(0.05, oldest + self.window - now)
            if waited + wait > self.max_wait:
                raise RateLimitExceeded(
                    f"Local rate budget exhausted ({used:.0f}/{self.max_cost:.0f} per {self.window:.0f}s)."
                )
            self._sleep(wait)
            waited += wait


def estimate_cost(bbox: BBox, days: int) -> float:
    """Conservative estimate of FIRMS transactions for one area request.

    FIRMS charges by area size, number of days and sensor. The exact formula is not
    guaranteed, so we assume 2 transactions per 60x60-degree cell per day (the high
    end of what third-party notes describe). The real counter is read from the
    status endpoint at the start of each run (see FirmsClient.map_key_status).
    """
    west, south, east, north = bbox
    cells = max(1, math.ceil(max(east - west, 0.0001) / 60.0)) * max(1, math.ceil(max(north - south, 0.0001) / 60.0))
    return float(2 * cells * max(1, days))


# ----------------------------------------------------------------------------
# response classification
# ----------------------------------------------------------------------------
def classify_body(content: bytes) -> str:
    """Return one of: csv, empty, auth_error, rate_limited, error_text."""
    text = content.decode("utf-8", errors="replace").lstrip("\ufeff")
    stripped = text.strip()
    if not stripped:
        return "empty"
    first = stripped.splitlines()[0].lower()
    if "latitude" in first and "longitude" in first and "acq_date" in first:
        return "csv"
    low = stripped.lower()
    if "invalid map_key" in low or "invalid mapkey" in low or "invalid map key" in low:
        return "auth_error"
    if "transaction limit" in low or "exceeding" in low or "too many requests" in low:
        return "rate_limited"
    return "error_text"


def _fmt_coord(x: float) -> str:
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


# ----------------------------------------------------------------------------
# client
# ----------------------------------------------------------------------------
class FirmsClient:
    mode = "live"

    def __init__(
        self,
        cfg: Settings,
        transport=None,
        limiter: Optional[RateLimiter] = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ):
        if not cfg.key_configured:
            raise AuthError("FIRMS_MAP_KEY is not configured.")
        self.cfg = cfg
        self.transport = transport or RequestsTransport()
        self._sleep = sleep
        self._rng = rng
        self.limiter = limiter or RateLimiter(
            cfg.max_txn_per_window, cfg.rate_window_seconds, cfg.min_request_interval_s, sleep=sleep
        )
        self.log = get_logger()

    # ---- URLs ----------------------------------------------------------
    def build_area_url(self, source_id: str, bbox: BBox, start: date, days: int, redact: bool = False) -> str:
        key = "***" if redact else self.cfg.map_key
        area = ",".join(_fmt_coord(c) for c in bbox)
        return f"{self.cfg.base_url}/api/area/csv/{key}/{source_id}/{area}/{days}/{start.isoformat()}"

    # ---- main fetch ----------------------------------------------------
    def fetch(self, product: Product, bbox: BBox, start: date, days: int) -> FetchResult:
        if not 1 <= days <= 10:
            raise ValueError("days must be between 1 and 10")
        url = self.build_area_url(product.source_id, bbox, start, days)
        url_red = self.build_area_url(product.source_id, bbox, start, days, redact=True)
        cost = estimate_cost(bbox, days)
        last_msg = "unknown error"
        attempts = 0

        for attempt in range(1, self.cfg.max_retries + 2):
            attempts = attempt
            self.limiter.acquire(cost)
            try:
                resp = self.transport.get(url, self.cfg.http_timeout_s)
            except TransportError as exc:
                last_msg = f"network error ({self.cfg.redact(str(exc))})"
                self._backoff(attempt, None, last_msg, url_red)
                continue

            status = resp.status
            if status in (401, 403):
                raise AuthError(f"HTTP {status}: the MAP_KEY was rejected.")
            if status == 429 or status >= 500:
                last_msg = f"HTTP {status}"
                self._backoff(attempt, resp.headers.get("Retry-After"), last_msg, url_red)
                continue
            if status != 200:
                raise ApiError(f"HTTP {status} for {url_red}")

            kind = classify_body(resp.content)
            if kind == "csv":
                return FetchResult(url_red, status, resp.content, "csv", attempts, cost)
            if kind == "auth_error":
                raise AuthError("FIRMS reported an invalid MAP_KEY.")
            if kind == "rate_limited":
                last_msg = "FIRMS reported the transaction limit was exceeded"
                # Force a long wait: the server-side window is 10 minutes.
                self._backoff(attempt, str(self.cfg.rate_window_seconds), last_msg, url_red)
                continue
            if kind == "empty":
                last_msg = "empty response body"
                self._backoff(attempt, None, last_msg, url_red)
                continue
            # unknown text: keep it as evidence but do not retry
            return FetchResult(url_red, status, resp.content, "error_text", attempts, cost,
                               message="Unrecognised response body (not a FIRMS CSV).")

        if "transaction limit" in last_msg:
            raise RateLimitExceeded(last_msg)
        raise ApiError(f"Giving up after {attempts} attempts: {last_msg}")

    def _backoff(self, attempt: int, retry_after: Optional[str], reason: str, url_red: str) -> None:
        if attempt > self.cfg.max_retries:
            return  # the loop is about to end; no point sleeping
        delay = min(self.cfg.backoff_max_s, self.cfg.backoff_base_s * (2 ** (attempt - 1)))
        delay += self._rng() * 0.5 * delay          # jitter
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), self.cfg.backoff_max_s))
            except ValueError:
                pass
        self.log.warning("Request failed (%s); retry %d/%d in %.1fs", reason, attempt, self.cfg.max_retries, delay)
        self._sleep(delay)

    # ---- helper endpoints (best effort; failures never stop a run) ------
    def map_key_status(self) -> Optional[Dict[str, object]]:
        """Return FIRMS' own transaction counters, or None if unavailable."""
        import json

        url = f"{self.cfg.base_url}/mapserver/mapkey_status/?MAP_KEY={self.cfg.map_key}"
        try:
            resp = self.transport.get(url, self.cfg.http_timeout_s)
            if resp.status != 200:
                return None
            data = json.loads(resp.content.decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
        except (TransportError, ValueError):
            return None

    def availability(self) -> Dict[str, Tuple[date, date]]:
        """{SP SOURCE: (first_date, last_date)} from the data_availability endpoint, or {} if unusable.

        The response layout is not something we can rely on, so two layouts are understood:
          A) data_id,min_date,max_date                  (one row per source)
          B) sensor,date,sp,nrt                         (one row per date; sp/nrt are true/false)
        Anything else returns {} and the planner falls back to its configured lag heuristic.
        """
        import csv
        import io

        url = f"{self.cfg.base_url}/api/data_availability/csv/{self.cfg.map_key}/ALL"
        try:
            resp = self.transport.get(url, self.cfg.http_timeout_s)
        except TransportError:
            return {}
        if resp.status != 200:
            return {}
        text = resp.content.decode("utf-8", errors="replace").lstrip("\ufeff")
        try:
            rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
                    for r in csv.DictReader(io.StringIO(text))]
        except csv.Error:
            return {}
        return parse_availability(rows)


def _to_date(text: str) -> date:
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def parse_availability(rows: list) -> Dict[str, Tuple[date, date]]:
    out: Dict[str, Tuple[date, date]] = {}
    if not rows:
        return out
    try:
        first = rows[0]
        if "data_id" in first and "min_date" in first and "max_date" in first:           # layout A
            for r in rows:
                if r.get("data_id") and r.get("min_date") and r.get("max_date"):
                    out[r["data_id"]] = (_to_date(r["min_date"]), _to_date(r["max_date"]))
        elif "sensor" in first and "date" in first and "sp" in first:                     # layout B
            span: Dict[str, list] = {}
            for r in rows:
                if r.get("sp", "").lower() in ("true", "1", "yes", "t") and r.get("sensor") and r.get("date"):
                    sensor = r["sensor"]
                    key = sensor[:-4] + "_SP" if sensor.endswith("_NRT") else sensor
                    d = _to_date(r["date"])
                    lo_hi = span.setdefault(key, [d, d])
                    lo_hi[0], lo_hi[1] = min(lo_hi[0], d), max(lo_hi[1], d)
            out = {k: (v[0], v[1]) for k, v in span.items()}
    except ValueError:
        return {}
    return out
