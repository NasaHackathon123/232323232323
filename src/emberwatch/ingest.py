"""Ingestion: plan -> fetch -> save raw -> record coverage. No cleaning happens here."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from .config import FETCH_REGION_ID, REGIONS, Settings
from .db import iso, log_event, utcnow
from .firms_client import (
    ApiError,
    ApiFatalError,
    AuthError,
    RateLimitExceeded,
    estimate_cost,
)
from .logutil import get_logger
from .planner import Window, load_coverage, plan_windows, sp_max_date
from .raw_store import RawStore
from .sources import products_for_group


@dataclass
class IngestSummary:
    run_id: str = ""
    status: str = "ok"
    windows_planned: int = 0
    windows_ok: int = 0
    windows_failed: int = 0
    requests_made: int = 0
    est_transactions: float = 0.0
    bytes_received: int = 0
    aborted_reason: str = ""
    errors: List[str] = field(default_factory=list)
    planned: List[Dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return self.__dict__.copy()


def _remaining_from_status(status: Optional[Dict[str, object]]) -> Optional[float]:
    """Remaining transactions in FIRMS' current 10-minute interval, if the status says so."""
    if not status:
        return None
    try:
        limit = float(status.get("transaction_limit"))        # type: ignore[arg-type]
        used = float(status.get("current_transactions"))      # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return limit - used


class IngestService:
    QUOTA_RECHECK_EVERY = 20      # re-read FIRMS' own transaction counter this often (live mode)

    def __init__(
        self,
        cfg: Settings,
        conn: sqlite3.Connection,
        source,
        now_fn: Callable[[], datetime] = utcnow,
    ):
        self.cfg = cfg
        self.conn = conn
        self.source = source
        self.now_fn = now_fn
        self.store = RawStore(cfg, conn)
        self.log = get_logger()
        self.aoi_id = FETCH_REGION_ID
        self.bbox = REGIONS[FETCH_REGION_ID].bbox

    # ------------------------------------------------------------------
    def plan(self, start: date, end: date, force: bool = False, use_availability: bool = True) -> List[Window]:
        now = self.now_fn()
        availability = {}
        if use_availability:
            try:
                availability = self.source.availability()
            except Exception as exc:    # best effort only
                self.log.warning("Could not read data availability: %s", self.cfg.redact(str(exc)))
        self._availability = availability
        if use_availability and not availability and self.source.mode == "live":
            self.log.info("FIRMS data availability was not usable; choosing standard vs near-real-time sources "
                          "with the %d-day lag heuristic.", self.cfg.sp_lag_days)
        coverage = load_coverage(self.conn, self.aoi_id)
        return plan_windows(self.cfg, start, end, now, coverage, availability, force=force)

    def _coverage_final(self, window: Window, d: date, fetched: datetime, has_rows: bool) -> bool:
        if window.product.processing == "NRT":
            return fetched.date() >= d + timedelta(days=self.cfg.nrt_refresh_days)
        # SP: final once it returned data, or when FIRMS says SP covers this date
        if has_rows:
            return True
        sp = products_for_group(window.product.group)["SP"]
        known = self._availability.get(sp.source_id) if sp else None
        return bool(known and d <= known[1])

    # ------------------------------------------------------------------
    def run(
        self,
        start: date,
        end: date,
        force: bool = False,
        trigger: str = "manual",
        plan_only: bool = False,
    ) -> IngestSummary:
        now = self.now_fn()
        run_id = f"ing-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        summary = IngestSummary(run_id=run_id)

        windows = self.plan(start, end, force=force, use_availability=not plan_only)
        summary.windows_planned = len(windows)
        summary.planned = [
            {"source": w.product.source_id, "start": w.start.isoformat(), "days": w.days,
             "est_cost": estimate_cost(self.bbox, w.days)}
            for w in windows
        ]
        summary.est_transactions = sum(p["est_cost"] for p in summary.planned)   # type: ignore[misc]
        if plan_only:
            summary.status = "planned_only"
            return summary

        self.conn.execute(
            "INSERT INTO ingest_runs(run_id, started_at_utc, data_mode, trigger, status, window_start, window_end,"
            " windows_planned) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, iso(now), self.cfg.data_mode, trigger, "running", start.isoformat(), end.isoformat(),
             len(windows)),
        )
        self.conn.commit()
        self.log.info("Ingest %s: %d window(s) planned (%s .. %s, mode=%s)",
                      run_id, len(windows), start, end, self.cfg.mode)

        est_spent = 0.0
        consecutive_failures = 0

        # Check FIRMS' own counter before spending anything (live mode only).
        remaining = _remaining_from_status(self.source.map_key_status())
        if remaining is not None and windows and remaining < self.cfg.txn_reserve:
            summary.status = "aborted"
            summary.aborted_reason = (
                f"Only {remaining:.0f} FIRMS transactions left in this interval (reserve {self.cfg.txn_reserve}). "
                "Try again in about 10 minutes."
            )
            windows = []
            log_event(self.conn, "ingest", "WARNING", summary.aborted_reason, run_id, redact=self.cfg.redact)

        for w in windows:
            try:
                res = self.source.fetch(w.product, self.bbox, w.start, w.days)
            except (AuthError, RateLimitExceeded) as exc:
                summary.status = "aborted"
                summary.aborted_reason = self.cfg.redact(f"{type(exc).__name__}: {exc}")
                summary.errors.append(summary.aborted_reason)
                log_event(self.conn, "ingest", "ERROR", summary.aborted_reason, run_id,
                          {"source": w.product.source_id, "start": w.start.isoformat()}, self.cfg.redact)
                break
            except ApiFatalError as exc:   # pragma: no cover - future subclasses
                summary.status = "aborted"
                summary.aborted_reason = self.cfg.redact(str(exc))
                break
            except ApiError as exc:
                msg = self.cfg.redact(str(exc))
                summary.windows_failed += 1
                summary.errors.append(msg)
                consecutive_failures += 1
                self.log.error("Window failed (%s %s +%dd): %s", w.product.source_id, w.start, w.days, msg)
                log_event(self.conn, "ingest", "ERROR", msg, run_id,
                          {"source": w.product.source_id, "start": w.start.isoformat(), "days": w.days},
                          self.cfg.redact)
                if consecutive_failures >= self.cfg.max_consecutive_failures:
                    summary.status = "aborted"
                    summary.aborted_reason = f"{consecutive_failures} consecutive failures; stopping early."
                    break
                continue

            summary.requests_made += 1
            est_spent += res.est_cost
            summary.bytes_received += len(res.content)
            fetched = self.now_fn()

            raw_id = self.store.save(
                run_id=run_id, source_id=w.product.source_id, aoi_id=self.aoi_id, bbox=self.bbox,
                start=w.start, days=w.days, url_redacted=res.url_redacted, fetched_at=fetched,
                http_status=res.http_status, content=res.content, kind=res.kind,
            )

            if res.kind != "csv":
                summary.windows_failed += 1
                consecutive_failures += 1
                msg = f"{w.product.source_id} {w.start}: {res.message or 'non-CSV response'} (kept as raw file {raw_id})"
                summary.errors.append(msg)
                log_event(self.conn, "ingest", "ERROR", msg, run_id, None, self.cfg.redact)
                if consecutive_failures >= self.cfg.max_consecutive_failures:
                    summary.status = "aborted"
                    summary.aborted_reason = f"{consecutive_failures} consecutive failures; stopping early."
                    break
                continue

            consecutive_failures = 0
            summary.windows_ok += 1
            text_lines = res.content.count(b"\n")
            has_rows = text_lines > 1 or (text_lines == 1 and not res.content.endswith(b"\n"))
            today = fetched.astimezone(timezone.utc).date()
            for i in range(w.days):
                d = w.start + timedelta(days=i)
                if d > today:
                    continue
                self.conn.execute(
                    """INSERT INTO fetch_coverage(source_id, aoi_id, date_utc, fetched_at_utc, is_final,
                                                  raw_file_id, window_rows)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(source_id, aoi_id, date_utc) DO UPDATE SET
                         fetched_at_utc=excluded.fetched_at_utc, is_final=excluded.is_final,
                         raw_file_id=excluded.raw_file_id, window_rows=excluded.window_rows""",
                    (w.product.source_id, self.aoi_id, d.isoformat(), iso(fetched),
                     1 if self._coverage_final(w, d, fetched, has_rows) else 0, raw_id,
                     max(0, text_lines - 1)),
                )
            self.conn.commit()

            # Our cost figure is an estimate: periodically trust NASA's own counter instead.
            if summary.requests_made % self.QUOTA_RECHECK_EVERY == 0:
                remaining = _remaining_from_status(self.source.map_key_status())
                if remaining is not None and remaining < self.cfg.txn_reserve:
                    summary.status = "aborted"
                    summary.aborted_reason = (
                        f"Only {remaining:.0f} FIRMS transactions left in this interval "
                        f"(reserve {self.cfg.txn_reserve}); stopped early. Progress is saved; run again later.")
                    log_event(self.conn, "ingest", "WARNING", summary.aborted_reason, run_id,
                              redact=self.cfg.redact)
                    break

        if summary.status == "ok":
            if summary.windows_failed and summary.windows_ok:
                summary.status = "partial"
            elif summary.windows_failed and not summary.windows_ok:
                summary.status = "failed"
        summary.est_transactions = est_spent
        self.conn.execute(
            """UPDATE ingest_runs SET finished_at_utc=?, status=?, windows_ok=?, windows_failed=?,
                      requests_made=?, est_transactions=?, bytes_received=?, summary_json=? WHERE run_id=?""",
            (iso(self.now_fn()), summary.status, summary.windows_ok, summary.windows_failed,
             summary.requests_made, est_spent, summary.bytes_received,
             json.dumps({"aborted_reason": summary.aborted_reason, "errors": summary.errors[:20]}), run_id),
        )
        self.conn.commit()
        self.log.info("Ingest %s finished: status=%s ok=%d failed=%d requests=%d",
                      run_id, summary.status, summary.windows_ok, summary.windows_failed, summary.requests_made)
        return summary
