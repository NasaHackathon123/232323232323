"""Raw store: every API response is saved byte-for-byte and never edited.

Each file gets a row in `raw_files` with the (key-redacted) request, the fetch
time, HTTP status, size and SHA-256 checksum. The cleaning pipeline re-checks the
checksum before reading, so silent corruption or hand-editing is detected.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple

from .config import Settings
from .db import iso

BBox = Tuple[float, float, float, float]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RawStore:
    def __init__(self, cfg: Settings, conn: sqlite3.Connection):
        self.cfg = cfg
        self.conn = conn

    def save(
        self,
        *,
        run_id: str,
        source_id: str,
        aoi_id: str,
        bbox: BBox,
        start: date,
        days: int,
        url_redacted: str,
        fetched_at: datetime,
        http_status: int,
        content: bytes,
        kind: str,
    ) -> int:
        if kind not in ("csv", "error_text"):
            raise ValueError(f"unknown content kind {kind!r}")
        ext = "csv" if kind == "csv" else "txt"
        prefix = "SYNTHETIC_" if self.cfg.mode == "demo" else ""
        stamp = fetched_at.strftime("%Y%m%dT%H%M%SZ")
        folder = self.cfg.raw_dir / source_id / f"{start:%Y}"
        folder.mkdir(parents=True, exist_ok=True)
        base = f"{prefix}{source_id}_{aoi_id}_{start:%Y%m%d}_{days}d_{stamp}"
        path = folder / f"{base}.{ext}"
        n = 1
        while path.exists():                      # never overwrite an existing raw file
            n += 1
            path = folder / f"{base}_{n}.{ext}"

        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

        rel = path.relative_to(self.cfg.data_dir).as_posix()
        cur = self.conn.execute(
            """INSERT INTO raw_files(ingest_run_id, source_id, aoi_id, bbox, window_start, window_days,
                                     request_url, fetched_at_utc, http_status, content_kind, bytes, sha256,
                                     path, data_mode)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, source_id, aoi_id, ",".join(repr(float(c)) for c in bbox), start.isoformat(), days,
             url_redacted, iso(fetched_at), http_status, kind, len(content), sha256_bytes(content),
             rel, self.cfg.data_mode),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def read(self, row: sqlite3.Row) -> Optional[bytes]:
        """Return the raw bytes if the checksum still matches, else None."""
        path = self.cfg.data_dir / row["path"]
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if sha256_bytes(data) != row["sha256"]:
            return None
        return data
