"""Read-only exports in the shapes the EmberWatch frontend already displays (T14).

The frontend file is not modified. These files are the data contract: when the
frontend is later wired to real data it can load these JSON/CSV files (or an API
that serves them) without any change to the cleaning pipeline.

Every export carries a `meta` block with the data mode, so synthetic demonstration
data can never be mistaken for real NASA observations.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import REGIONS, Settings
from .db import iso, utcnow
from .logutil import get_logger
from .quality import build_report
from .schema import DATA_MODE_DEMO, HAI_METHOD, PIPELINE_VERSION

SYNTHETIC_BANNER = "SYNTHETIC DEMONSTRATION DATA - not real NASA observations"
MAX_HOTSPOTS = 5000
CSV_DAYS = 30

COORDINATE_REASONS = ("LAT_NOT_NUMERIC", "LON_NOT_NUMERIC", "LAT_OUT_OF_RANGE", "LON_OUT_OF_RANGE",
                      "COORD_ZERO_ZERO")


def _meta(cfg: Settings, now: datetime, kind: str) -> Dict[str, Any]:
    synthetic = cfg.data_mode == DATA_MODE_DEMO
    m: Dict[str, Any] = {
        "kind": kind,
        "data_mode": cfg.data_mode,
        "is_synthetic": synthetic,
        "generated_at_utc": iso(now),
        "pipeline_version": PIPELINE_VERSION,
    }
    if synthetic:
        m["banner"] = SYNTHETIC_BANNER
    return m


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, allow_nan=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def display_confidence(row: sqlite3.Row) -> str:
    """Presentation label only; the original value stays in confidence_raw."""
    if row["confidence_scheme"] == "modis_percent":
        v = row["confidence_value"]
        return f"{v:g}%" if v is not None else "n/a"
    cls = row["confidence_class_derived"]
    return cls.capitalize() if cls else "n/a"


def _csv_safe(text: Optional[str]) -> str:
    """Guard text cells against spreadsheet formula injection."""
    if not text:
        return ""
    if text[0] in "=+@" or (text[0] == "-" and not _is_number(text)):
        return "'" + text
    return text


def _is_number(text: str) -> bool:
    try:
        return math.isfinite(float(text))
    except ValueError:
        return False


def _latest_date(conn: sqlite3.Connection) -> Optional[date]:
    v = conn.execute("SELECT MAX(acq_date_utc) FROM observations WHERE is_current=1").fetchone()[0]
    return date.fromisoformat(v) if v else None


# ---------------------------------------------------------------------------
def export_hotspots(conn: sqlite3.Connection, cfg: Settings, now: datetime, region_id: str) -> Path:
    reg = REGIONS[region_id]
    west, south, east, north = reg.bbox
    latest = _latest_date(conn)
    points: List[Dict[str, Any]] = []
    if latest:
        since = (latest - timedelta(days=2)).isoformat()
        for r in conn.execute(
            """SELECT * FROM observations WHERE is_current=1 AND acq_date_utc>=?
               AND latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?
               ORDER BY acquired_at_utc DESC LIMIT ?""",
            (since, south, north, west, east, MAX_HOTSPOTS),
        ):
            points.append({
                "lat": r["latitude"], "lng": r["longitude"], "sensor": r["instrument"],
                "date": r["acquired_at_utc"][:16].replace("T", " "),
                "confidence": display_confidence(r), "frp": r["frp_mw"], "brightness": r["brightness_k"],
                # extra provenance (the frontend ignores unknown keys)
                "satellite": r["satellite"], "confidence_raw": r["confidence_raw"],
                "confidence_scheme": r["confidence_scheme"],
                "confidence_class_derived": r["confidence_class_derived"],
                "brightness_band": r["brightness_band"], "daynight": r["daynight"],
                "source_product": r["source_product"], "processing_level": r["processing_level"],
                "qc_flags": r["qc_flags"], "observation_id": r["observation_id"],
                "record_class": r["record_class"], "data_mode": r["data_mode"],
            })
    payload = {
        "meta": {**_meta(cfg, now, "hotspots"), "region": region_id,
                 "period": {"from": (latest - timedelta(days=2)).isoformat() if latest else None,
                            "to": latest.isoformat() if latest else None},
                 "truncated_at": MAX_HOTSPOTS if len(points) >= MAX_HOTSPOTS else None,
                 "record_class": "observed_hotspot"},
        "hotspots": points,
    }
    path = cfg.export_dir / f"hotspots_{region_id}.json"
    _write_json(path, payload)
    return path


def export_overview(conn: sqlite3.Connection, cfg: Settings, now: datetime, region_id: str) -> Path:
    reg = REGIONS[region_id]
    west, south, east, north = reg.bbox
    latest = _latest_date(conn)
    modis = viirs = 0
    cells = set()
    latest_acq = None
    if latest:
        since = (latest - timedelta(days=29)).isoformat()
        for r in conn.execute(
            """SELECT instrument, latitude, longitude, acquired_at_utc FROM observations
               WHERE is_current=1 AND acq_date_utc>=? AND latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?""",
            (since, south, north, west, east),
        ):
            if r["instrument"] == "MODIS":
                modis += 1
            else:
                viirs += 1
            cells.add((math.floor(round((r["latitude"] + 90.0) / 0.10, 9)),
                       math.floor(round((r["longitude"] + 180.0) / 0.10, 9))))
            if latest_acq is None or r["acquired_at_utc"] > latest_acq:
                latest_acq = r["acquired_at_utc"]
    payload = {
        "meta": {**_meta(cfg, now, "overview"), "region": region_id,
                 "period": {"from": (latest - timedelta(days=29)).isoformat() if latest else None,
                            "to": latest.isoformat() if latest else None, "days": 30}},
        "modis_detections": modis,
        "viirs_detections": viirs,
        "occupied_cells_0_10deg": len(cells),
        "latest_acquisition_utc": latest_acq,
        "notes": {"occupied_cells_0_10deg": "derived (grid product), not an observation count"},
    }
    path = cfg.export_dir / f"overview_{region_id}.json"
    _write_json(path, payload)
    return path


def _baseline_departure(occ: Dict[str, int], covered: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
    """Departure of occupied cells from the mean of the same day-of-year (+-7 days) in PRIOR years.

    Only years that have data coverage for that date window are used. Returns {} entries with
    departure None when there is no prior year to compare with (nothing is invented).
    """
    out: Dict[str, Dict[str, Any]] = {}
    for dstr in occ:
        d = date.fromisoformat(dstr)
        values: List[float] = []
        years_used = set()
        for y in range(d.year - 10, d.year):
            for off in range(-7, 8):
                try:
                    dd = d.replace(year=y) + timedelta(days=off)
                except ValueError:       # 29 Feb
                    continue
                k = dd.isoformat()
                if k in occ and covered.get(k):
                    values.append(float(occ[k]))
                    years_used.add(y)
        if values:
            mean = sum(values) / len(values)
            dep = round(100.0 * (occ[dstr] - mean) / mean, 1) if mean > 0 else None
            out[dstr] = {"departure_pct": dep, "baseline_mean_cells": round(mean, 2),
                         "baseline_years": len(years_used)}
        else:
            out[dstr] = {"departure_pct": None, "baseline_mean_cells": None, "baseline_years": 0}
    return out


def export_calendar(conn: sqlite3.Connection, cfg: Settings, now: datetime, region_id: str, res: float) -> Path:
    rows = conn.execute(
        """SELECT date_utc, n_modis, n_viirs, occupied_cells, mean_frp_mw, hai, satellites_contributing_7d,
                  data_covered FROM daily_activity_index WHERE region_id=? AND grid_res_deg=? ORDER BY date_utc""",
        (region_id, res),
    ).fetchall()
    occ = {r["date_utc"]: r["occupied_cells"] for r in rows}
    cov = {r["date_utc"]: r["data_covered"] for r in rows}
    base = _baseline_departure(occ, cov)
    days: Dict[str, Any] = {}
    for r in rows:
        b = base[r["date_utc"]]
        days[r["date_utc"]] = {
            "hai": round(r["hai"], 3) if r["hai"] is not None else None,
            "grid": r["occupied_cells"], "viirs": r["n_viirs"], "modis": r["n_modis"],
            "mean_frp_mw": round(r["mean_frp_mw"], 2) if r["mean_frp_mw"] is not None else None,
            "satellites_contributing_7d": r["satellites_contributing_7d"],
            "data_covered": bool(r["data_covered"]),
            "baseline_departure_pct": b["departure_pct"], "baseline_years": b["baseline_years"],
        }
    payload = {
        "meta": {**_meta(cfg, now, "calendar"), "region": region_id, "grid_res_deg": res,
                 "hai_method": HAI_METHOD, "record_class": "derived",
                 "notes": "grid, hai and baseline_departure_pct are derived products; viirs and modis are raw counts"},
        "days": days,
    }
    path = cfg.export_dir / f"calendar_{region_id}_{res:.2f}.json"
    _write_json(path, payload)
    return path


def export_analytics(conn: sqlite3.Connection, cfg: Settings, now: datetime, region_id: str) -> Path:
    reg = REGIONS[region_id]
    west, south, east, north = reg.bbox
    res = cfg.grid_resolutions[0]
    daily = conn.execute(
        """SELECT date_utc, n_modis, n_viirs, occupied_cells, hai FROM daily_activity_index
           WHERE region_id=? AND grid_res_deg=? ORDER BY date_utc""", (region_id, res)).fetchall()

    monthly: Dict[str, Dict[str, int]] = defaultdict(lambda: {"modis": 0, "viirs": 0})
    for r in daily:
        m = r["date_utc"][:7]
        monthly[m]["modis"] += r["n_modis"]
        monthly[m]["viirs"] += r["n_viirs"]

    bins = [("0-10", 0, 10), ("10-25", 10, 25), ("25-50", 25, 50), ("50-100", 50, 100), ("100+", 100, None)]
    frp_hist: List[Dict[str, Any]] = []
    for label, lo, hi in bins:
        cond = "frp_mw>=? " + ("AND frp_mw<? " if hi is not None else "")
        params: List[Any] = [south, north, west, east, lo] + ([hi] if hi is not None else [])
        row = {"bin_mw": label}
        for inst in ("MODIS", "VIIRS"):
            row[inst.lower()] = conn.execute(
                f"SELECT COUNT(*) FROM observations WHERE is_current=1 AND instrument=? AND frp_mw IS NOT NULL "
                f"AND latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ? AND {cond}",
                [inst] + params).fetchone()[0]
        frp_hist.append(row)

    # weekly mean HAI and z-score against the same ISO week of prior years (needs >=3 prior years)
    weekly: Dict[tuple, List[float]] = defaultdict(list)
    for r in daily:
        if r["hai"] is not None:
            y, w, _ = date.fromisoformat(r["date_utc"]).isocalendar()
            weekly[(y, w)].append(r["hai"])
    weekly_mean = {k: sum(v) / len(v) for k, v in weekly.items()}
    weekly_out = []
    for (y, w), val in sorted(weekly_mean.items()):
        prior = [weekly_mean[(py, w)] for py in range(y - 10, y) if (py, w) in weekly_mean]
        z = None
        if len(prior) >= 3:
            sd = statistics.pstdev(prior)
            z = round((val - statistics.mean(prior)) / sd, 2) if sd > 0 else None
        weekly_out.append({"iso_year": y, "iso_week": w, "mean_hai": round(val, 3), "baseline_years": len(prior),
                           "z_score": z, "anomaly_gt_2sigma": (abs(z) > 2) if z is not None else None})

    payload = {
        "meta": {**_meta(cfg, now, "analytics"), "region": region_id, "grid_res_deg": res,
                 "hai_method": HAI_METHOD,
                 "notes": "mean_hai and z_score are derived; z_score needs 3+ prior years of the same ISO week"},
        "daily_series_last_90d": [
            {"date": r["date_utc"], "hai": round(r["hai"], 3) if r["hai"] is not None else None,
             "modis": r["n_modis"], "viirs": r["n_viirs"]} for r in daily[-90:]],
        "monthly_sensor_comparison": [{"month": m, **v} for m, v in sorted(monthly.items())],
        "frp_distribution": frp_hist,
        "weekly_anomalies": weekly_out,
    }
    path = cfg.export_dir / f"analytics_{region_id}.json"
    _write_json(path, payload)
    return path


def export_audit(conn: sqlite3.Connection, cfg: Settings, now: datetime) -> Path:
    rep = build_report(conn, cfg.data_mode, now)
    acc = rep["accounting"]
    invalid_coord = 0
    for r in conn.execute("SELECT reasons FROM quarantine"):
        if any(c in COORDINATE_REASONS for c in r["reasons"].split(",")):
            invalid_coord += 1
    links = conn.execute("SELECT COUNT(*) FROM cross_sensor_links").fetchone()[0]
    payload = {
        "meta": _meta(cfg, now, "audit"),
        "raw_records_loaded": acc["raw_rows_read"],
        "cleaned_records_retained": acc["accepted"],
        "retained_with_qc_flags": acc["accepted_with_flags"],
        "quarantined_records": acc["quarantined"],
        "invalid_coordinate_records": invalid_coord,
        "exact_duplicates_not_inserted": acc["duplicates_not_inserted"],
        "nrt_rows_superseded_by_sp": acc["superseded_by_sp"],
        "records_revised_by_later_fetch": acc["superseded_by_later_fetch"],
        "cross_sensor_proximity_links": links,
        "observations_merged": 0,
        "silent_discards": acc["silent_discards"],
        "reconciled": acc["reconciled"],
        "notes": "cross_sensor_proximity_links are analysis links between DIFFERENT satellites; "
                 "no observation is merged, removed or edited because of them",
    }
    path = cfg.export_dir / "audit.json"
    _write_json(path, payload)
    return path


CSV_HEADER = [
    "latitude", "longitude", "acq_date", "acq_time", "satellite", "instrument", "confidence", "frp",
    # provenance / original values (extra columns after the frontend's eight)
    "observation_id", "satellite_name", "source_product", "processing_level", "confidence_raw",
    "confidence_scheme", "confidence_class_derived", "frp_raw", "brightness_k", "brightness_band",
    "qc_flags", "data_mode", "record_class",
]


def export_csv(conn: sqlite3.Connection, cfg: Settings, now: datetime) -> Path:
    latest = _latest_date(conn)
    path = cfg.export_dir / "harmonised_fires.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        if latest:
            since = (latest - timedelta(days=CSV_DAYS - 1)).isoformat()
            for r in conn.execute(
                "SELECT * FROM observations WHERE is_current=1 AND acq_date_utc>=? ORDER BY acquired_at_utc",
                (since,),
            ):
                ts = r["acquired_at_utc"]
                w.writerow([
                    r["latitude"], r["longitude"], r["acq_date_utc"], ts[11:13] + ts[14:16],
                    r["satellite_code_raw"], r["instrument"], display_confidence(r),
                    "" if r["frp_mw"] is None else r["frp_mw"],
                    r["observation_id"], r["satellite"], r["source_product"], r["processing_level"],
                    _csv_safe(r["confidence_raw"]), r["confidence_scheme"], r["confidence_class_derived"] or "",
                    _csv_safe(r["frp_raw"]), "" if r["brightness_k"] is None else r["brightness_k"],
                    r["brightness_band"], r["qc_flags"] or "", r["data_mode"], r["record_class"],
                ])
    os.replace(tmp, path)
    return path


def export_all(conn: sqlite3.Connection, cfg: Settings, now_fn: Callable[[], datetime] = utcnow) -> List[Path]:
    log = get_logger()
    now = now_fn()
    cfg.export_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for rid in REGIONS:
        paths.append(export_hotspots(conn, cfg, now, rid))
        paths.append(export_overview(conn, cfg, now, rid))
        paths.append(export_analytics(conn, cfg, now, rid))
        for res in cfg.grid_resolutions:
            paths.append(export_calendar(conn, cfg, now, rid, res))
    paths.append(export_audit(conn, cfg, now))
    paths.append(export_csv(conn, cfg, now))
    manifest = {
        "meta": _meta(cfg, now, "manifest"),
        "files": [{"name": p.name, "bytes": p.stat().st_size} for p in paths],
    }
    mpath = cfg.export_dir / "manifest.json"
    _write_json(mpath, manifest)
    paths.append(mpath)
    log.info("Exported %d files to %s", len(paths), cfg.export_dir)
    return paths
