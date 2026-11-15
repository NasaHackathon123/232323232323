"""Turn raw files into observations, with full accounting.

Every data row of every raw file ends in EXACTLY ONE of three places:

    observations  (accepted, possibly with QC flags)
    quarantine    (rejected, with reason codes)
    duplicate_log (not inserted because the same observation is already stored)

so  rows_read == accepted + quarantined + duplicates  always holds. The run fails
loudly (and rolls the file back) if it ever does not: there are no silent discards.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

from .cleaning import CleanResult, RequestContext, clean_row, raw_json_of
from .config import Settings
from .db import iso, log_event, utcnow
from .logutil import get_logger
from .planner import parse_utc
from .raw_store import RawStore
from .schema import PIPELINE_VERSION, REQUIRED_COLUMNS
from .sources import PRODUCTS

OBS_COLUMNS = [
    "observation_id", "source_product", "processing_level", "record_class", "data_mode", "satellite",
    "satellite_code_raw", "instrument", "nominal_resolution_m", "acquired_at_utc", "acq_date_utc",
    "acq_date_raw", "acq_time_raw", "daynight", "latitude", "longitude", "scan_km", "track_km", "frp_mw",
    "frp_raw", "brightness_k", "brightness_band", "brightness_2_k", "brightness_2_band", "confidence_raw",
    "confidence_scheme", "confidence_value", "confidence_class_derived", "product_version", "qc_status",
    "qc_flags", "transforms_applied", "raw_file_id", "raw_row", "content_hash", "raw_json", "ingest_run_id",
    "ingested_at_utc", "processed_at_utc", "pipeline_version",
]
_INSERT_OBS = (
    f"INSERT INTO observations({','.join(OBS_COLUMNS)}) VALUES({','.join('?' for _ in OBS_COLUMNS)})"
)


@dataclass
class ProcessingSummary:
    run_id: str = ""
    status: str = "ok"
    raw_files_processed: int = 0
    raw_files_skipped: int = 0
    rows_read: int = 0
    rows_accepted: int = 0
    rows_accepted_with_flags: int = 0
    rows_quarantined: int = 0
    rows_duplicate: int = 0
    rows_superseded: int = 0
    rows_revised: int = 0
    reconciled: bool = True
    quarantine_reasons: Dict[str, int] = field(default_factory=dict)
    flag_counts: Dict[str, int] = field(default_factory=dict)
    duplicate_reasons: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return self.__dict__.copy()


def _chunks(seq: List[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


class Harmonizer:
    def __init__(self, cfg: Settings, conn: sqlite3.Connection, now_fn: Callable[[], datetime] = utcnow):
        self.cfg = cfg
        self.conn = conn
        self.now_fn = now_fn
        self.store = RawStore(cfg, conn)
        self.log = get_logger()

    # ------------------------------------------------------------------
    def reset_processed_data(self) -> None:
        """--rebuild: drop everything derived from raw files; raw files are untouched."""
        for t in ("observations", "quarantine", "duplicate_log", "grid_cell_day", "daily_activity_index",
                  "cross_sensor_links", "transformation_log"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.execute("UPDATE raw_files SET processed_at_utc=NULL, process_status=NULL, data_rows=NULL, "
                          "pipeline_version=NULL")
        self.conn.commit()

    # ------------------------------------------------------------------
    def run(self, rebuild: bool = False) -> ProcessingSummary:
        started = self.now_fn()
        run_id = f"proc-{started:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        summary = ProcessingSummary(run_id=run_id)
        if rebuild:
            self.reset_processed_data()
        self.conn.execute(
            "INSERT INTO processing_runs(run_id, started_at_utc, data_mode, status, pipeline_version)"
            " VALUES(?,?,?,?,?)", (run_id, iso(started), self.cfg.data_mode, "running", PIPELINE_VERSION))
        self.conn.commit()

        pending = self.conn.execute(
            "SELECT * FROM raw_files WHERE processed_at_utc IS NULL ORDER BY fetched_at_utc, raw_file_id"
        ).fetchall()
        quarantine_counter: Counter = Counter()
        flag_counter: Counter = Counter()
        dup_counter: Counter = Counter()

        for rf in pending:
            try:
                fs = self._process_file(rf, run_id, quarantine_counter, flag_counter, dup_counter)
            except Exception as exc:        # one bad file must not stop the others
                self.conn.rollback()
                msg = self.cfg.redact(f"raw file {rf['raw_file_id']} ({rf['path']}): {type(exc).__name__}: {exc}")
                summary.errors.append(msg)
                summary.status = "partial"
                self.log.exception("Processing failed for raw file %s", rf["raw_file_id"])
                log_event(self.conn, "pipeline", "ERROR", msg, run_id, None, self.cfg.redact)
                continue
            if fs is None:
                summary.raw_files_skipped += 1
                continue
            summary.raw_files_processed += 1
            summary.rows_read += fs["rows_read"]
            summary.rows_accepted += fs["accepted"]
            summary.rows_accepted_with_flags += fs["accepted_with_flags"]
            summary.rows_quarantined += fs["quarantined"]
            summary.rows_duplicate += fs["duplicates"]
            summary.rows_revised += fs["revisions"]

        summary.rows_superseded = self.apply_precedence()
        summary.quarantine_reasons = dict(quarantine_counter)
        summary.flag_counts = dict(flag_counter)
        summary.duplicate_reasons = dict(dup_counter)
        summary.reconciled = (
            summary.rows_read == summary.rows_accepted + summary.rows_quarantined + summary.rows_duplicate
        )
        if not summary.reconciled:
            summary.status = "partial"
            msg = "Row accounting does not reconcile (rows_read != accepted + quarantined + duplicates)."
            summary.errors.append(msg)
            log_event(self.conn, "pipeline", "ERROR", msg, run_id)

        self.conn.execute(
            """UPDATE processing_runs SET finished_at_utc=?, status=?, raw_files_processed=?, rows_read=?,
                   rows_accepted=?, rows_accepted_with_flags=?, rows_quarantined=?, rows_duplicate=?,
                   rows_superseded=?, reconciled=?, summary_json=? WHERE run_id=?""",
            (iso(self.now_fn()), summary.status, summary.raw_files_processed, summary.rows_read,
             summary.rows_accepted, summary.rows_accepted_with_flags, summary.rows_quarantined,
             summary.rows_duplicate, summary.rows_superseded, 1 if summary.reconciled else 0,
             json.dumps({"quarantine_reasons": summary.quarantine_reasons, "flags": summary.flag_counts,
                         "duplicate_reasons": summary.duplicate_reasons, "rows_revised": summary.rows_revised,
                         "errors": summary.errors[:20]}),
             run_id),
        )
        self.conn.commit()
        self.log.info(
            "Harmonise %s: files=%d rows=%d accepted=%d (flagged %d) quarantined=%d duplicates=%d superseded=%d",
            run_id, summary.raw_files_processed, summary.rows_read, summary.rows_accepted,
            summary.rows_accepted_with_flags, summary.rows_quarantined, summary.rows_duplicate,
            summary.rows_superseded)
        return summary

    # ------------------------------------------------------------------
    def _process_file(self, rf: sqlite3.Row, run_id: str, qc: Counter, fc: Counter, dc: Counter):
        now_iso = iso(self.now_fn())
        if rf["content_kind"] != "csv":
            self.conn.execute(
                "UPDATE raw_files SET processed_at_utc=?, process_status='skipped_non_csv', data_rows=0,"
                " pipeline_version=? WHERE raw_file_id=?", (now_iso, PIPELINE_VERSION, rf["raw_file_id"]))
            self.conn.commit()
            return None

        data = self.store.read(rf)
        if data is None:
            msg = f"Raw file {rf['path']} is missing or its checksum no longer matches; not processed."
            log_event(self.conn, "pipeline", "ERROR", msg, run_id)
            self.conn.execute(
                "UPDATE raw_files SET processed_at_utc=?, process_status='integrity_error', pipeline_version=?"
                " WHERE raw_file_id=?", (now_iso, PIPELINE_VERSION, rf["raw_file_id"]))
            self.conn.commit()
            return None

        product = PRODUCTS[rf["source_id"]]
        bbox = tuple(float(x) for x in rf["bbox"].split(","))
        ctx = RequestContext(
            source_id=rf["source_id"], product=product, bbox=bbox,                      # type: ignore[arg-type]
            start=date.fromisoformat(rf["window_start"]), days=int(rf["window_days"]),
            fetched_at=parse_utc(rf["fetched_at_utc"]), data_mode=rf["data_mode"],
            raw_file_id=rf["raw_file_id"], ingest_run_id=rf["ingest_run_id"],
        )

        # T01 decode/parse, T02 header normalisation
        text = data.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]

        cleaned: List[Tuple[int, Dict[object, object], CleanResult]] = []
        for raw in reader:
            cleaned.append((reader.line_num, raw, clean_row(raw, ctx, missing)))
        rows_read = len(cleaned)

        # look up what is already stored for these observation ids
        ids = sorted({r.record["observation_id"] for _, _, r in cleaned if not r.quarantined})   # type: ignore[index]
        existing: Dict[Tuple[str, str], str] = {}
        for chunk in _chunks(ids, 400):
            q = ",".join("?" for _ in chunk)
            for row in self.conn.execute(
                f"SELECT observation_id, source_product, content_hash FROM observations "
                f"WHERE observation_id IN ({q}) ORDER BY raw_file_id",
                chunk,
            ):
                # ordered by raw_file_id, so the LATEST stored version of each record is what remains
                existing[(row["observation_id"], row["source_product"])] = row["content_hash"]

        obs_rows: List[Tuple] = []
        quarantine_rows: List[Tuple] = []
        dup_rows: List[Tuple] = []
        seen_in_file: Dict[Tuple[str, str], str] = {}
        revised_keys: List[Tuple[str, str]] = []
        accepted = accepted_flagged = 0
        transform_rows: Counter = Counter()

        for line_no, raw, res in cleaned:
            if res.quarantined:
                quarantine_rows.append(
                    (rf["raw_file_id"], line_no, product.source_id, rf["data_mode"], ",".join(res.reasons),
                     res.detail, raw_json_of(raw), now_iso))
                for code in res.reasons:
                    qc[code] += 1
                continue
            rec = res.record
            assert rec is not None
            key = (rec["observation_id"], rec["source_product"])          # type: ignore[assignment]
            chash = rec["content_hash"]
            if key in seen_in_file:
                same = seen_in_file[key] == chash
                reason = "EXACT_DUPLICATE_IN_FILE" if same else "CONFLICT_SAME_KEY_IN_FILE"
                dup_rows.append((rf["raw_file_id"], line_no, product.source_id, rec["observation_id"], reason,
                                 chash, seen_in_file[key], rec["raw_json"], now_iso))
                dc[reason] += 1
                continue
            revised = False
            if key in existing:
                if existing[key] == chash:
                    dup_rows.append((rf["raw_file_id"], line_no, product.source_id, rec["observation_id"],
                                     "EXACT_DUPLICATE_REFETCH", chash, existing[key], rec["raw_json"], now_iso))
                    dc["EXACT_DUPLICATE_REFETCH"] += 1
                    continue
                # Same satellite/time/position, different values, from a LATER fetch: FIRMS NRT sources first
                # carry real-time records that are later replaced. The later version becomes current; the
                # earlier one is kept (superseded_by='REVISION'). Nothing is deleted.
                revised = True
                revised_keys.append(key)

            seen_in_file[key] = chash                                   # type: ignore[index]
            rec.update({
                "raw_file_id": rf["raw_file_id"], "raw_row": line_no, "ingest_run_id": rf["ingest_run_id"],
                "ingested_at_utc": rf["fetched_at_utc"], "processed_at_utc": now_iso,
                "pipeline_version": PIPELINE_VERSION,
            })
            row_flags = list(res.flags)
            if revised:
                row_flags.append("REVISION_OF_EARLIER_FETCH")
                rec["qc_flags"] = ",".join(row_flags)
                rec["qc_status"] = "accepted_with_flags"
            obs_rows.append(tuple(rec[c] for c in OBS_COLUMNS))
            accepted += 1
            if row_flags:
                accepted_flagged += 1
                for f in row_flags:
                    fc[f] += 1
            for t in res.transforms:
                transform_rows[t] += 1

        duplicates = len(dup_rows)
        quarantined = len(quarantine_rows)
        if rows_read != len(obs_rows) + quarantined + duplicates:
            raise RuntimeError(
                f"row accounting mismatch: read={rows_read} accepted={len(obs_rows)} "
                f"quarantined={quarantined} duplicates={duplicates}")

        c = self.conn
        if revised_keys:
            c.executemany(
                "UPDATE observations SET is_current=0, superseded_by='REVISION' "
                "WHERE observation_id=? AND source_product=? AND (superseded_by IS NULL OR superseded_by!='REVISION')",
                revised_keys)
        c.executemany(_INSERT_OBS, obs_rows)
        c.executemany(
            "INSERT INTO quarantine(raw_file_id, raw_row, source_product, data_mode, reasons, detail, raw_json,"
            " quarantined_at_utc) VALUES(?,?,?,?,?,?,?,?)", quarantine_rows)
        c.executemany(
            "INSERT INTO duplicate_log(raw_file_id, raw_row, source_product, observation_id, reason, content_hash,"
            " existing_hash, raw_json, logged_at_utc) VALUES(?,?,?,?,?,?,?,?,?)", dup_rows)

        # transformation log: what was applied to how many rows of this file
        tl = [
            ("T01", rows_read, rows_read, "decoded UTF-8 and split into rows"),
            ("T02", rows_read, rows_read, "header names normalised for lookup"),
            ("T04", rows_read, len(obs_rows), "numeric fields parsed on accepted rows"),
            ("T05", rows_read, len(obs_rows), "UTC timestamp built on accepted rows"),
            ("T06", rows_read, len(obs_rows), "satellite names canonicalised on accepted rows"),
            ("T07", rows_read, len(obs_rows), "instrument-specific columns mapped to common names"),
            ("T08", rows_read, len(obs_rows), "confidence class derived on accepted rows"),
            ("T09", rows_read, len(obs_rows) + duplicates, "observation identity computed"),
        ]
        for tid in ("T03", "T05a"):
            if transform_rows.get(tid):
                tl.append((tid, rows_read, transform_rows[tid], "applied only where needed"))
        c.executemany(
            "INSERT INTO transformation_log(run_id, raw_file_id, transform_id, rows_in, rows_affected, detail)"
            " VALUES(?,?,?,?,?,?)", [(run_id, rf["raw_file_id"], *t) for t in tl])

        c.execute(
            "UPDATE raw_files SET processed_at_utc=?, process_status='processed', data_rows=?, pipeline_version=?"
            " WHERE raw_file_id=?", (now_iso, rows_read, PIPELINE_VERSION, rf["raw_file_id"]))
        c.commit()
        return {"rows_read": rows_read, "accepted": len(obs_rows), "accepted_with_flags": accepted_flagged,
                "quarantined": quarantined, "duplicates": duplicates, "revisions": len(revised_keys)}

    # ------------------------------------------------------------------
    def apply_precedence(self) -> int:
        """T10(a): if SP data exists for a satellite+UTC date, NRT rows of that satellite+date are superseded.

        Only rows previously marked superseded_by='SP' are reset first, so superseded_by='REVISION'
        (an older version replaced by a later fetch) is never undone.
        """
        c = self.conn
        c.execute("UPDATE observations SET is_current=1, superseded_by=NULL WHERE superseded_by='SP'")
        cur = c.execute(
            """UPDATE observations SET is_current=0, superseded_by='SP'
               WHERE processing_level='NRT' AND is_current=1 AND EXISTS (
                   SELECT 1 FROM observations s
                   WHERE s.processing_level='SP' AND s.is_current=1 AND s.satellite=observations.satellite
                     AND s.acq_date_utc=observations.acq_date_utc)"""
        )
        c.commit()
        return cur.rowcount if cur.rowcount is not None else 0
