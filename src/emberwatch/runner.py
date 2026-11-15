"""End-to-end runs (fetch -> harmonise -> derive -> export) and scheduling."""

from __future__ import annotations

import json
import os
import random
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, Optional

from .config import Settings
from .db import connect, init_db, iso, log_event, utcnow
from .derived import build_all
from .exports import export_all
from .firms_client import FirmsClient
from .ingest import IngestService
from .logutil import get_logger, setup_logging
from .pipeline import Harmonizer
from .quality import write_report
from .synthetic import SyntheticSource

LOCK_STALE_MINUTES = 180


def make_source(cfg: Settings):
    """The ONLY place that decides live vs demo. Everything downstream is identical."""
    if cfg.mode == "live":
        return FirmsClient(cfg)
    return SyntheticSource(cfg)


class LockBusy(Exception):
    pass


@contextmanager
def run_lock(cfg: Settings) -> Iterator[None]:
    """Prevent two runs at once (a slow run must not be overlapped by the next scheduled one)."""
    cfg.ensure_dirs()
    path = cfg.lock_path
    if path.exists():
        age_min = (time.time() - path.stat().st_mtime) / 60.0
        if age_min < LOCK_STALE_MINUTES:
            raise LockBusy(f"Another run is in progress (lock is {age_min:.0f} min old).")
        get_logger().warning("Removing stale lock file (%.0f min old).", age_min)
        path.unlink()
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, f"pid={os.getpid()} started={iso(utcnow())}\n".encode())
        os.close(fd)
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def open_db(cfg: Settings):
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    return conn


def run_pipeline(
    cfg: Settings,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    force: bool = False,
    rebuild: bool = False,
    trigger: str = "manual",
    source=None,
    now_fn: Callable[[], datetime] = utcnow,
    skip_fetch: bool = False,
) -> Dict[str, Any]:
    log = get_logger()
    now = now_fn()
    today = now.astimezone(timezone.utc).date()
    end = end or today
    start = start or (today - timedelta(days=cfg.update_days - 1))
    conn = open_db(cfg)
    out: Dict[str, Any] = {"mode": cfg.mode, "data_mode": cfg.data_mode, "start": start.isoformat(),
                           "end": end.isoformat()}
    try:
        if not skip_fetch:
            src = source or make_source(cfg)
            ingest = IngestService(cfg, conn, src, now_fn).run(start, end, force=force, trigger=trigger)
            out["ingest"] = ingest.as_dict()
        proc = Harmonizer(cfg, conn, now_fn).run(rebuild=rebuild)
        out["harmonize"] = proc.as_dict()
        out["derived"] = build_all(conn, cfg)
        out["exports"] = [p.name for p in export_all(conn, cfg, now_fn)]
        out["quality_report"] = write_report(conn, cfg.data_mode, cfg.export_dir).name
    except Exception as exc:
        msg = cfg.redact(f"{type(exc).__name__}: {exc}")
        log.exception("Pipeline run failed")
        try:
            log_event(conn, "runner", "ERROR", msg, None, None, cfg.redact)
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return out


def serve(cfg: Settings, every_minutes: int, sleep: Callable[[float], None] = time.sleep,
          max_runs: Optional[int] = None) -> None:
    """Simple in-process scheduler: run, wait, repeat. Errors never stop the loop."""
    log = get_logger()
    runs = 0
    while True:
        try:
            with run_lock(cfg):
                summary = run_pipeline(cfg, trigger="scheduler")
                ing = summary.get("ingest", {})
                log.info("Scheduled run finished: ingest=%s windows_ok=%s", ing.get("status"), ing.get("windows_ok"))
        except LockBusy as exc:
            log.warning("%s Skipping this cycle.", exc)
        except Exception as exc:
            log.error("Scheduled run failed: %s", cfg.redact(str(exc)))
        runs += 1
        if max_runs is not None and runs >= max_runs:
            return
        wait = every_minutes * 60 + random.uniform(0, 30)      # small jitter
        log.info("Next run in %.0f minutes.", wait / 60)
        sleep(wait)
