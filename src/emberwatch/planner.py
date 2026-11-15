"""Decide WHICH API requests are needed. Fewer requests = less quota used.

State is tracked per (source, area, UTC date) in `fetch_coverage`:

* a date marked final is never requested again (unless --force);
* a date that is not final is refreshed at most once per `min_refetch_minutes`;
* a date never fetched is requested.

Needed dates are then grouped into consecutive runs and split into windows of at
most `day_range` days, which is the unit the FIRMS area API works in.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .config import Settings
from .sources import GROUPS, Product, products_for_group

Coverage = Dict[Tuple[str, str], Tuple[datetime, bool]]     # (source_id, date_iso) -> (fetched_at, is_final)


@dataclass(frozen=True)
class Window:
    product: Product
    start: date
    days: int

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.days - 1)


def parse_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_coverage(conn: sqlite3.Connection, aoi_id: str) -> Coverage:
    out: Coverage = {}
    for r in conn.execute(
        "SELECT source_id, date_utc, fetched_at_utc, is_final FROM fetch_coverage WHERE aoi_id=?", (aoi_id,)
    ):
        out[(r["source_id"], r["date_utc"])] = (parse_utc(r["fetched_at_utc"]), bool(r["is_final"]))
    return out


def sp_max_date(product_sp: Product, availability: Dict[str, Tuple[date, date]], today: date, lag_days: int) -> date:
    """Latest date for which standard-processing data is expected to exist."""
    known = availability.get(product_sp.source_id)
    if known:
        return known[1]
    return today - timedelta(days=lag_days)       # heuristic when FIRMS did not tell us


def choose_product(
    group: str,
    d: date,
    availability: Dict[str, Tuple[date, date]],
    today: date,
    lag_days: int,
) -> Optional[Product]:
    """Pick the FIRMS source for one platform group and one date.

    Standard processing (SP) is preferred whenever it exists for that date; otherwise
    near-real-time (NRT). A product that has stopped (Suomi-NPP) is only usable for
    dates up to its last data date.
    """
    prods = products_for_group(group)
    nrt, sp = prods["NRT"], prods["SP"]

    def usable(p: Optional[Product]) -> bool:
        return p is not None and (p.last_data_date is None or d <= p.last_data_date)

    if sp is not None and usable(sp) and d <= sp_max_date(sp, availability, today, lag_days):
        return sp
    if usable(nrt):
        return nrt
    return None


def needs_fetch(
    source_id: str, d: date, coverage: Coverage, now: datetime, min_refetch_minutes: int, force: bool
) -> bool:
    if force:
        return True
    hit = coverage.get((source_id, d.isoformat()))
    if hit is None:
        return True
    fetched_at, is_final = hit
    if is_final:
        return False
    return (now - fetched_at) >= timedelta(minutes=min_refetch_minutes)


def _runs(dates: List[date]) -> Iterable[List[date]]:
    run: List[date] = []
    for d in sorted(dates):
        if run and d != run[-1] + timedelta(days=1):
            yield run
            run = []
        run.append(d)
    if run:
        yield run


def plan_windows(
    cfg: Settings,
    start: date,
    end: date,
    now: datetime,
    coverage: Coverage,
    availability: Dict[str, Tuple[date, date]],
    force: bool = False,
    groups: Optional[Iterable[str]] = None,
) -> List[Window]:
    today = now.astimezone(timezone.utc).date()
    end = min(end, today)
    if start > end:
        return []
    wanted_groups = tuple(groups) if groups else GROUPS

    needed: Dict[str, List[date]] = {}
    d = start
    while d <= end:
        for g in wanted_groups:
            p = choose_product(g, d, availability, today, cfg.sp_lag_days)
            if p is None:
                continue
            if needs_fetch(p.source_id, d, coverage, now, cfg.min_refetch_minutes, force):
                needed.setdefault(p.source_id, []).append(d)
        d += timedelta(days=1)

    windows: List[Window] = []
    from .sources import PRODUCTS

    for source_id, dates in needed.items():
        for run in _runs(dates):
            i = 0
            while i < len(run):
                chunk = run[i : i + cfg.day_range]
                windows.append(Window(PRODUCTS[source_id], chunk[0], len(chunk)))
                i += cfg.day_range
    # Most recent first: if the run is cut short, the freshest data is already stored.
    windows.sort(key=lambda w: (w.start, w.product.source_id), reverse=True)
    return windows
