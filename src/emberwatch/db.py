"""SQLite storage. One database per mode (live / demo), plus a raw-file folder.

Tables are grouped by trust level:

  RAW (never edited)   raw_files, fetch_coverage (files themselves are on disk)
  OBSERVED             observations, quarantine, duplicate_log
  DERIVED              grid_cell_day, daily_activity_index, cross_sensor_links
  OPERATIONS           ingest_runs, processing_runs, transformation_catalog,
                       transformation_log, error_log, meta
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .schema import TRANSFORM_CATALOG

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id          TEXT PRIMARY KEY,
    started_at_utc  TEXT NOT NULL,
    finished_at_utc TEXT,
    data_mode       TEXT NOT NULL,
    trigger         TEXT,
    status          TEXT,            -- running | ok | partial | failed | aborted
    window_start    TEXT,
    window_end      TEXT,
    windows_planned INTEGER DEFAULT 0,
    windows_ok      INTEGER DEFAULT 0,
    windows_failed  INTEGER DEFAULT 0,
    requests_made   INTEGER DEFAULT 0,
    est_transactions REAL DEFAULT 0,
    bytes_received  INTEGER DEFAULT 0,
    summary_json    TEXT
);

CREATE TABLE IF NOT EXISTS raw_files (
    raw_file_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ingest_run_id   TEXT,
    source_id       TEXT NOT NULL,
    aoi_id          TEXT NOT NULL,
    bbox            TEXT NOT NULL,        -- "west,south,east,north"
    window_start    TEXT NOT NULL,
    window_days     INTEGER NOT NULL,
    request_url     TEXT NOT NULL,        -- MAP_KEY redacted
    fetched_at_utc  TEXT NOT NULL,
    http_status     INTEGER,
    content_kind    TEXT NOT NULL,        -- csv | error_text
    bytes           INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    path            TEXT NOT NULL,        -- relative to the mode's data folder
    data_mode       TEXT NOT NULL,
    data_rows       INTEGER,              -- filled in by the harmonise step
    processed_at_utc TEXT,
    process_status  TEXT,                 -- processed | skipped_non_csv | integrity_error | empty
    pipeline_version TEXT
);
CREATE INDEX IF NOT EXISTS ix_raw_files_pending ON raw_files(processed_at_utc, fetched_at_utc);

CREATE TABLE IF NOT EXISTS fetch_coverage (
    source_id      TEXT NOT NULL,
    aoi_id         TEXT NOT NULL,
    date_utc       TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    is_final       INTEGER NOT NULL DEFAULT 0,
    raw_file_id    INTEGER,
    window_rows    INTEGER,
    PRIMARY KEY (source_id, aoi_id, date_utc)
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id   TEXT NOT NULL,
    source_product   TEXT NOT NULL,
    processing_level TEXT NOT NULL,           -- NRT | SP
    record_class     TEXT NOT NULL DEFAULT 'observed_hotspot',
    data_mode        TEXT NOT NULL,
    satellite        TEXT NOT NULL,
    satellite_code_raw TEXT,
    instrument       TEXT NOT NULL,
    nominal_resolution_m INTEGER,
    acquired_at_utc  TEXT NOT NULL,
    acq_date_utc     TEXT NOT NULL,
    acq_date_raw     TEXT,
    acq_time_raw     TEXT,
    daynight         TEXT,
    latitude         REAL NOT NULL,
    longitude        REAL NOT NULL,
    scan_km          REAL,
    track_km         REAL,
    frp_mw           REAL,
    frp_raw          TEXT,
    brightness_k     REAL,
    brightness_band  TEXT,
    brightness_2_k   REAL,
    brightness_2_band TEXT,
    confidence_raw   TEXT,
    confidence_scheme TEXT,                    -- modis_percent | viirs_category
    confidence_value REAL,                     -- MODIS percent as received, else NULL
    confidence_class_derived TEXT,             -- low | nominal | high | NULL (derived label)
    product_version  TEXT,
    qc_status        TEXT NOT NULL,            -- accepted | accepted_with_flags
    qc_flags         TEXT,
    transforms_applied TEXT,
    is_current       INTEGER NOT NULL DEFAULT 1,
    superseded_by    TEXT,                     -- SP (standard processing exists) | REVISION (later fetch)
    raw_file_id      INTEGER NOT NULL,
    raw_row          INTEGER NOT NULL,
    content_hash     TEXT NOT NULL,
    raw_json         TEXT NOT NULL,
    ingest_run_id    TEXT,
    ingested_at_utc  TEXT,
    processed_at_utc TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    PRIMARY KEY (observation_id, source_product, raw_file_id)
);
CREATE INDEX IF NOT EXISTS ix_obs_sat_date ON observations(satellite, acq_date_utc, processing_level);
CREATE INDEX IF NOT EXISTS ix_obs_date ON observations(acq_date_utc);
CREATE INDEX IF NOT EXISTS ix_obs_current ON observations(is_current, acq_date_utc);
CREATE INDEX IF NOT EXISTS ix_obs_raw ON observations(raw_file_id);

CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_file_id   INTEGER NOT NULL,
    raw_row       INTEGER NOT NULL,
    source_product TEXT NOT NULL,
    data_mode     TEXT NOT NULL,
    reasons       TEXT NOT NULL,        -- comma separated codes
    detail        TEXT,
    raw_json      TEXT NOT NULL,
    quarantined_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quarantine_raw ON quarantine(raw_file_id);

CREATE TABLE IF NOT EXISTS duplicate_log (
    duplicate_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_file_id   INTEGER NOT NULL,
    raw_row       INTEGER NOT NULL,
    source_product TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    reason        TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    existing_hash TEXT,
    raw_json      TEXT NOT NULL,
    logged_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dup_raw ON duplicate_log(raw_file_id);

CREATE TABLE IF NOT EXISTS grid_cell_day (
    grid_res_deg REAL NOT NULL,
    cell_id      TEXT NOT NULL,
    cell_lat_min REAL NOT NULL,
    cell_lon_min REAL NOT NULL,
    date_utc     TEXT NOT NULL,
    n_modis      INTEGER NOT NULL,
    n_viirs      INTEGER NOT NULL,
    n_total      INTEGER NOT NULL,
    n_frp_valid  INTEGER NOT NULL,
    sum_frp_mw   REAL,
    mean_frp_mw  REAL,
    n_conf_low   INTEGER NOT NULL,
    n_conf_nominal INTEGER NOT NULL,
    n_conf_high  INTEGER NOT NULL,
    n_conf_unclassified INTEGER NOT NULL,
    satellites   TEXT NOT NULL,
    record_class TEXT NOT NULL DEFAULT 'derived',
    PRIMARY KEY (grid_res_deg, cell_id, date_utc)
);

CREATE TABLE IF NOT EXISTS daily_activity_index (
    region_id    TEXT NOT NULL,
    grid_res_deg REAL NOT NULL,
    date_utc     TEXT NOT NULL,
    n_modis      INTEGER NOT NULL,
    n_viirs      INTEGER NOT NULL,
    occupied_cells INTEGER NOT NULL,
    mean_frp_mw  REAL,
    satellites_contributing_7d INTEGER NOT NULL,
    hai          REAL,
    hai_method   TEXT NOT NULL,
    data_covered INTEGER NOT NULL,           -- 1 if a successful fetch covers this date
    record_class TEXT NOT NULL DEFAULT 'derived',
    PRIMARY KEY (region_id, grid_res_deg, date_utc)
);

CREATE TABLE IF NOT EXISTS cross_sensor_links (
    link_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    date_utc     TEXT NOT NULL,
    obs_a_id     TEXT NOT NULL,
    obs_a_product TEXT NOT NULL,
    satellite_a  TEXT NOT NULL,
    obs_b_id     TEXT NOT NULL,
    obs_b_product TEXT NOT NULL,
    satellite_b  TEXT NOT NULL,
    distance_km  REAL NOT NULL,
    dt_minutes   REAL NOT NULL,
    rule         TEXT NOT NULL,
    record_class TEXT NOT NULL DEFAULT 'derived_proximity_candidate'
);
CREATE INDEX IF NOT EXISTS ix_links_date ON cross_sensor_links(date_utc);

CREATE TABLE IF NOT EXISTS processing_runs (
    run_id          TEXT PRIMARY KEY,
    started_at_utc  TEXT NOT NULL,
    finished_at_utc TEXT,
    data_mode       TEXT NOT NULL,
    status          TEXT,
    raw_files_processed INTEGER DEFAULT 0,
    rows_read       INTEGER DEFAULT 0,
    rows_accepted   INTEGER DEFAULT 0,
    rows_accepted_with_flags INTEGER DEFAULT 0,
    rows_quarantined INTEGER DEFAULT 0,
    rows_duplicate  INTEGER DEFAULT 0,
    rows_superseded INTEGER DEFAULT 0,
    reconciled      INTEGER,
    summary_json    TEXT,
    pipeline_version TEXT
);

CREATE TABLE IF NOT EXISTS transformation_catalog (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    scope       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transformation_log (
    log_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    raw_file_id  INTEGER,
    transform_id TEXT NOT NULL,
    rows_in      INTEGER NOT NULL,
    rows_affected INTEGER NOT NULL,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS error_log (
    error_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT NOT NULL,
    run_id     TEXT,
    component  TEXT NOT NULL,
    severity   TEXT NOT NULL,
    message    TEXT NOT NULL,
    context_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_error_ts ON error_log(ts_utc);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """UTC ISO-8601 with seconds, e.g. 2026-11-14T06:42:00Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path, read_only: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if read_only:
        if not path.exists():
            raise FileNotFoundError(str(path))
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    elif int(row["value"]) > SCHEMA_VERSION:
        raise RuntimeError("Database was created by a newer EmberWatch version.")
    for t in TRANSFORM_CATALOG:
        conn.execute(
            "INSERT OR REPLACE INTO transformation_catalog(id,name,scope,kind,description) VALUES(?,?,?,?,?)",
            (t["id"], t["name"], t["scope"], t["kind"], t["description"]),
        )
    conn.commit()


def log_event(
    conn: sqlite3.Connection,
    component: str,
    severity: str,
    message: str,
    run_id: Optional[str] = None,
    context: Optional[dict] = None,
    redact=None,
) -> None:
    """Write a row to error_log. `redact` (callable) scrubs secrets from text first."""
    if redact is not None:
        message = redact(message)
    ctx: Any = None
    if context:
        ctx = json.dumps(context, default=str)
        if redact is not None:
            ctx = redact(ctx)
    conn.execute(
        "INSERT INTO error_log(ts_utc, run_id, component, severity, message, context_json) VALUES(?,?,?,?,?,?)",
        (iso(utcnow()), run_id, component, severity, message, ctx),
    )
    conn.commit()
