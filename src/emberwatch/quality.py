"""Data-quality metrics. The dashboard only DISPLAYS what this module computes.

Everything is calculated from the database, so the numbers can always be
reproduced and checked with plain SQL.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import iso, utcnow
from .schema import DATA_MODE_DEMO, DUPLICATE_REASONS, PIPELINE_VERSION, QC_FLAGS, QUARANTINE_REASONS

STALE_HOURS = 12     # a satellite with no new acquisition for this long is shown as stale (live mode only)


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_report(conn: sqlite3.Connection, data_mode: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or utcnow()
    rep: Dict[str, Any] = {
        "generated_at_utc": iso(now),
        "data_mode": data_mode,
        "is_synthetic": data_mode == DATA_MODE_DEMO,
        "pipeline_version": PIPELINE_VERSION,
    }

    # ---- accounting / reconciliation ------------------------------------------------
    raw_rows = _one(conn, "SELECT COALESCE(SUM(data_rows),0) FROM raw_files WHERE process_status='processed'") or 0
    accepted = _one(conn, "SELECT COUNT(*) FROM observations") or 0
    accepted_flagged = _one(conn, "SELECT COUNT(*) FROM observations WHERE qc_status='accepted_with_flags'") or 0
    quarantined = _one(conn, "SELECT COUNT(*) FROM quarantine") or 0
    duplicates = _one(conn, "SELECT COUNT(*) FROM duplicate_log") or 0
    superseded_sp = _one(conn, "SELECT COUNT(*) FROM observations WHERE superseded_by='SP'") or 0
    superseded_rev = _one(conn, "SELECT COUNT(*) FROM observations WHERE superseded_by='REVISION'") or 0
    rep["accounting"] = {
        "raw_rows_read": raw_rows,
        "accepted": accepted,
        "accepted_with_flags": accepted_flagged,
        "quarantined": quarantined,
        "duplicates_not_inserted": duplicates,
        "superseded_by_sp": superseded_sp,
        "superseded_by_later_fetch": superseded_rev,
        "silent_discards": raw_rows - (accepted + quarantined + duplicates),
        "reconciled": raw_rows == accepted + quarantined + duplicates,
    }

    # ---- why rows were rejected / flagged ------------------------------------------
    q_counter: Counter = Counter()
    for r in conn.execute("SELECT reasons FROM quarantine"):
        for code in r["reasons"].split(","):
            q_counter[code] += 1
    rep["quarantine_reasons"] = [
        {"code": c, "rows": n, "meaning": QUARANTINE_REASONS.get(c, "")} for c, n in q_counter.most_common()
    ]
    f_counter: Counter = Counter()
    for r in conn.execute("SELECT qc_flags FROM observations WHERE qc_flags IS NOT NULL"):
        for code in r["qc_flags"].split(","):
            f_counter[code] += 1
    rep["qc_flags"] = [{"code": c, "rows": n, "meaning": QC_FLAGS.get(c, "")} for c, n in f_counter.most_common()]
    d_counter = Counter({r["reason"]: r["n"] for r in conn.execute(
        "SELECT reason, COUNT(*) AS n FROM duplicate_log GROUP BY reason")})
    rep["duplicate_reasons"] = [
        {"code": c, "rows": n, "meaning": DUPLICATE_REASONS.get(c, "")} for c, n in d_counter.most_common()
    ]

    # ---- completeness of key measurements ------------------------------------------
    def pct(numer_sql: str) -> Optional[float]:
        if not accepted:
            return None
        return round(100.0 * (_one(conn, numer_sql) or 0) / accepted, 2)

    rep["completeness_pct_missing"] = {
        "frp": pct("SELECT COUNT(*) FROM observations WHERE frp_mw IS NULL"),
        "confidence": pct("SELECT COUNT(*) FROM observations WHERE confidence_raw IS NULL OR confidence_raw='' "
                          "OR (confidence_value IS NULL AND confidence_class_derived IS NULL)"),
        "brightness": pct("SELECT COUNT(*) FROM observations WHERE brightness_k IS NULL"),
        "pixel_size": pct("SELECT COUNT(*) FROM observations WHERE scan_km IS NULL OR track_km IS NULL"),
        "daynight": pct("SELECT COUNT(*) FROM observations WHERE daynight IS NULL"),
    }

    # ---- per satellite: volume, freshness, ingestion lag ----------------------------
    sats: List[Dict[str, Any]] = []
    for r in conn.execute(
        """SELECT satellite, instrument, COUNT(*) AS n, MIN(acquired_at_utc) AS first_acq,
                  MAX(acquired_at_utc) AS last_acq, MAX(ingested_at_utc) AS last_ingest
           FROM observations GROUP BY satellite, instrument ORDER BY instrument, satellite"""
    ):
        last_acq = _parse(r["last_acq"])
        age_h = round((now - last_acq).total_seconds() / 3600.0, 1) if last_acq else None
        lags = [
            (_parse(x["ingested_at_utc"]) - _parse(x["acquired_at_utc"])).total_seconds() / 60.0
            for x in conn.execute(
                "SELECT acquired_at_utc, ingested_at_utc FROM observations WHERE satellite=? "
                "ORDER BY acquired_at_utc DESC LIMIT 2000", (r["satellite"],))
            if x["ingested_at_utc"]
        ]
        lags.sort()
        median_lag = round(lags[len(lags) // 2], 1) if lags else None
        sats.append({
            "satellite": r["satellite"], "instrument": r["instrument"], "observations": r["n"],
            "first_acquired_utc": r["first_acq"], "last_acquired_utc": r["last_acq"],
            "last_ingested_utc": r["last_ingest"], "hours_since_last_acquisition": age_h,
            "median_ingestion_lag_min": median_lag,
            "stale": bool(age_h is not None and age_h > STALE_HOURS and data_mode != DATA_MODE_DEMO),
        })
    rep["satellites"] = sats

    # ---- ingestion health -----------------------------------------------------------
    since = iso(now - timedelta(hours=24))
    rep["ingestion_24h"] = {
        "runs": _one(conn, "SELECT COUNT(*) FROM ingest_runs WHERE started_at_utc>=?", (since,)) or 0,
        "runs_failed_or_aborted": _one(
            conn, "SELECT COUNT(*) FROM ingest_runs WHERE started_at_utc>=? AND status IN ('failed','aborted')",
            (since,)) or 0,
        "requests": _one(conn, "SELECT COALESCE(SUM(requests_made),0) FROM ingest_runs WHERE started_at_utc>=?",
                         (since,)) or 0,
        "est_transactions": _one(
            conn, "SELECT COALESCE(SUM(est_transactions),0) FROM ingest_runs WHERE started_at_utc>=?",
            (since,)) or 0,
        "errors": _one(conn, "SELECT COUNT(*) FROM error_log WHERE ts_utc>=? AND severity='ERROR'", (since,)) or 0,
    }
    rep["last_ingest_run"] = _row_dict(conn.execute(
        "SELECT run_id, started_at_utc, finished_at_utc, status, windows_planned, windows_ok, windows_failed, "
        "requests_made, trigger FROM ingest_runs ORDER BY started_at_utc DESC LIMIT 1").fetchone())
    rep["last_processing_run"] = _row_dict(conn.execute(
        "SELECT run_id, started_at_utc, finished_at_utc, status, rows_read, rows_accepted, rows_quarantined, "
        "rows_duplicate, reconciled FROM processing_runs ORDER BY started_at_utc DESC LIMIT 1").fetchone())

    # ---- coverage: dates in the last 30 days with no successful fetch ---------------
    today = now.date()
    covered = {r["date_utc"] for r in conn.execute("SELECT DISTINCT date_utc FROM fetch_coverage")}
    missing = [(today - timedelta(days=k)).isoformat() for k in range(0, 30)
               if (today - timedelta(days=k)).isoformat() not in covered]
    rep["coverage_gaps_last_30d"] = sorted(missing)
    rep["latest_covered_date"] = max(covered) if covered else None

    # ---- recent errors --------------------------------------------------------------
    rep["recent_errors"] = [dict(r) for r in conn.execute(
        "SELECT ts_utc, component, severity, message FROM error_log ORDER BY error_id DESC LIMIT 20")]

    rep["observations_by_day"] = [dict(r) for r in conn.execute(
        """SELECT acq_date_utc AS date, instrument, COUNT(*) AS n FROM observations WHERE is_current=1
           GROUP BY acq_date_utc, instrument ORDER BY acq_date_utc DESC, instrument LIMIT 400""")]
    rep["run_history"] = [dict(r) for r in conn.execute(
        """SELECT started_at_utc, status, rows_read, rows_accepted, rows_quarantined, rows_duplicate, reconciled
           FROM processing_runs ORDER BY started_at_utc DESC LIMIT 30""")]
    return rep


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def write_report(conn: sqlite3.Connection, data_mode: str, export_dir: Path) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    rep = build_report(conn, data_mode)
    path = export_dir / "quality_report.json"
    path.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    return path
