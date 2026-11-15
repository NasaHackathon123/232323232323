"""DERIVED products. These are analysis aids built from observed hotspots.

They live in their own tables, are tagged record_class = 'derived' (or
'derived_proximity_candidate'), and are rebuilt from scratch on every run, so they
can never drift from the observations they are computed from.

Nothing here edits, merges or deletes an observation.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

import sqlite3

from .config import REGIONS, Settings
from .db import log_event
from .logutil import get_logger
from .schema import HAI_METHOD, RECORD_CLASS_DERIVED, RECORD_CLASS_LINK


def cell_index(value: float, origin: float, res: float) -> int:
    """Grid index with a guard against floating-point edge cases (e.g. 1.4 / 0.1)."""
    return math.floor(round((value - origin) / res, 9))


def cell_id(res: float, i: int, j: int) -> str:
    return f"{res:g}:{i}:{j}"


def in_bbox(lat: float, lon: float, bbox: Tuple[float, float, float, float]) -> bool:
    west, south, east, north = bbox
    return west <= lon <= east and south <= lat <= north


def build_grid_and_daily(conn: sqlite3.Connection, cfg: Settings) -> Dict[str, int]:
    log = get_logger()
    conn.execute("DELETE FROM grid_cell_day")
    conn.execute("DELETE FROM daily_activity_index")

    resolutions = cfg.grid_resolutions
    # grid_acc key: (res, i, j, date) -> [n_modis, n_viirs, n_frp, sum_frp, low, nominal, high, unclassified, sats]
    grid_acc: Dict[tuple, list] = {}
    # daily_acc key: (region, res, date) -> [n_modis, n_viirs, cells, n_frp, sum_frp]
    daily_acc: Dict[tuple, list] = {}
    domain_sats: Dict[str, Set[str]] = defaultdict(set)
    obs_dates: Set[str] = set()

    cur = conn.execute(
        "SELECT satellite, instrument, acq_date_utc, latitude, longitude, frp_mw, confidence_class_derived "
        "FROM observations WHERE is_current=1"
    )
    for r in cur:
        lat, lon, dstr, inst = r["latitude"], r["longitude"], r["acq_date_utc"], r["instrument"]
        frp, cls = r["frp_mw"], r["confidence_class_derived"]
        obs_dates.add(dstr)
        domain_sats[dstr].add(r["satellite"])
        regions = [rid for rid, reg in REGIONS.items() if in_bbox(lat, lon, reg.bbox)]
        for res in resolutions:
            i, j = cell_index(lat, -90.0, res), cell_index(lon, -180.0, res)
            g = grid_acc.get((res, i, j, dstr))
            if g is None:
                g = [0, 0, 0, 0.0, 0, 0, 0, 0, set()]
                grid_acc[(res, i, j, dstr)] = g
            g[0 if inst == "MODIS" else 1] += 1
            if frp is not None:
                g[2] += 1
                g[3] += frp
            if cls == "low":
                g[4] += 1
            elif cls == "nominal":
                g[5] += 1
            elif cls == "high":
                g[6] += 1
            else:
                g[7] += 1
            g[8].add(r["satellite"])
            for rid in regions:
                d = daily_acc.get((rid, res, dstr))
                if d is None:
                    d = [0, 0, set(), 0, 0.0]
                    daily_acc[(rid, res, dstr)] = d
                d[0 if inst == "MODIS" else 1] += 1
                d[2].add((i, j))
                if frp is not None:
                    d[3] += 1
                    d[4] += frp

    grid_rows = []
    for (res, i, j, dstr), g in grid_acc.items():
        n_mod, n_vi, n_frp, s_frp, lo, no, hi, un, sats = g
        grid_rows.append((
            res, cell_id(res, i, j), round(-90.0 + i * res, 6), round(-180.0 + j * res, 6), dstr,
            n_mod, n_vi, n_mod + n_vi, n_frp, s_frp if n_frp else None, (s_frp / n_frp) if n_frp else None,
            lo, no, hi, un, ",".join(sorted(sats)), RECORD_CLASS_DERIVED,
        ))
    conn.executemany("INSERT INTO grid_cell_day VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", grid_rows)

    covered = {r["date_utc"] for r in conn.execute("SELECT DISTINCT date_utc FROM fetch_coverage")}
    all_dates = sorted(covered | obs_dates)

    def sats_7d(dstr: str) -> int:
        d0 = date.fromisoformat(dstr)
        union: Set[str] = set()
        for k in range(7):
            union |= domain_sats.get((d0 - timedelta(days=k)).isoformat(), set())
        return len(union)

    sat_cache = {d: sats_7d(d) for d in all_dates}
    daily_rows = []
    for rid in REGIONS:
        for res in resolutions:
            for dstr in all_dates:
                acc = daily_acc.get((rid, res, dstr))
                n_mod, n_vi, cells, n_frp, s_frp = (acc if acc else (0, 0, set(), 0, 0.0))
                occupied = len(cells)
                nsat = sat_cache[dstr]
                hai = 0.0 if occupied == 0 else occupied / max(1, nsat)
                daily_rows.append((
                    rid, res, dstr, n_mod, n_vi, occupied, (s_frp / n_frp) if n_frp else None, nsat, hai,
                    HAI_METHOD, 1 if dstr in covered else 0, RECORD_CLASS_DERIVED,
                ))
    conn.executemany("INSERT INTO daily_activity_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", daily_rows)
    conn.commit()
    log.info("Derived: %d grid-cell-days, %d daily index rows", len(grid_rows), len(daily_rows))
    return {"grid_cell_days": len(grid_rows), "daily_rows": len(daily_rows)}


# ---------------------------------------------------------------------------
# cross-sensor proximity links (T13) - links only, never merges
# ---------------------------------------------------------------------------
_EARTH_R_KM = 6371.0088
_BUCKET_DEG = 0.05
LINK_RULE = (
    "different satellites; same UTC date; centre distance <= mean pixel size "
    "(max(scan,track) per pixel, else nominal resolution); |time difference| <= limit"
)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


def _minutes(iso_ts: str) -> float:
    return datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").timestamp() / 60.0


def build_links(conn: sqlite3.Connection, cfg: Settings) -> int:
    log = get_logger()
    conn.execute("DELETE FROM cross_sensor_links")
    by_date: Dict[str, List[tuple]] = defaultdict(list)
    for r in conn.execute(
        "SELECT observation_id, source_product, satellite, acquired_at_utc, acq_date_utc, latitude, longitude, "
        "scan_km, track_km, nominal_resolution_m FROM observations WHERE is_current=1 "
        "ORDER BY acq_date_utc, acquired_at_utc, observation_id"
    ):
        size = (max(r["scan_km"], r["track_km"]) if r["scan_km"] and r["track_km"]
                else (r["nominal_resolution_m"] or 1000) / 1000.0)
        by_date[r["acq_date_utc"]].append((
            r["observation_id"], r["source_product"], r["satellite"], _minutes(r["acquired_at_utc"]),
            r["latitude"], r["longitude"], size,
        ))

    rows: List[tuple] = []
    capped = False
    for dstr, items in by_date.items():
        grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for idx, it in enumerate(items):
            grid[(math.floor(it[4] / _BUCKET_DEG), math.floor(it[5] / _BUCKET_DEG))].append(idx)
        for idx, a in enumerate(items):
            bi, bj = math.floor(a[4] / _BUCKET_DEG), math.floor(a[5] / _BUCKET_DEG)
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for jdx in grid.get((bi + di, bj + dj), ()):
                        if jdx <= idx:
                            continue
                        b = items[jdx]
                        if a[2] == b[2]:
                            continue                         # never link a satellite to itself
                        dt = abs(a[3] - b[3])
                        if dt > cfg.link_max_dt_minutes:
                            continue
                        dist = haversine_km(a[4], a[5], b[4], b[5])
                        if dist <= (a[6] + b[6]) / 2.0:
                            rows.append((dstr, a[0], a[1], a[2], b[0], b[1], b[2], round(dist, 4), round(dt, 1),
                                         LINK_RULE, RECORD_CLASS_LINK))
                            if len(rows) >= cfg.link_max_pairs:
                                capped = True
                                break
                    if capped:
                        break
                if capped:
                    break
            if capped:
                break
        if capped:
            break

    conn.executemany(
        "INSERT INTO cross_sensor_links(date_utc, obs_a_id, obs_a_product, satellite_a, obs_b_id, obs_b_product,"
        " satellite_b, distance_km, dt_minutes, rule, record_class) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    if capped:
        msg = f"Cross-sensor link search stopped at the cap of {cfg.link_max_pairs} links."
        log.warning(msg)
        log_event(conn, "derived", "WARNING", msg)
    log.info("Derived: %d cross-sensor proximity links", len(rows))
    return len(rows)


def build_all(conn: sqlite3.Connection, cfg: Settings) -> Dict[str, int]:
    out = build_grid_and_daily(conn, cfg)
    out["links"] = build_links(conn, cfg)
    return out
