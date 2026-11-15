"""Command line interface.

    python -m emberwatch status
    python -m emberwatch fetch --plan-only
    python -m emberwatch run
    python -m emberwatch backfill --start 2025-01-01
    python -m emberwatch harmonize --rebuild
    python -m emberwatch serve --every-minutes 180
    python -m emberwatch catalog --write docs/TRANSFORMATIONS.md

(from the `src` folder, or with PYTHONPATH=src). Nothing here ever prints the MAP_KEY.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from .config import ConfigError, Settings, load_settings
from .db import connect, init_db, utcnow
from .ingest import IngestService
from .logutil import setup_logging
from .quality import build_report
from .runner import LockBusy, make_source, open_db, run_lock, run_pipeline, serve
from .schema import (
    DUPLICATE_REASONS,
    HAI_METHOD,
    PIPELINE_VERSION,
    QC_FLAGS,
    QUARANTINE_REASONS,
    TRANSFORM_CATALOG,
)
from .sources import PRODUCTS


def _date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {s!r}") from None


def render_catalog_markdown() -> str:
    lines = [
        "# EmberWatch transformation catalogue",
        "",
        f"_Generated from `schema.py` (pipeline version {PIPELINE_VERSION}). Do not edit by hand._",
        "",
        "Every change the pipeline can make to a record is listed here. Raw files are never modified.",
        "",
        "## Transformations",
        "",
        "| ID | Name | Scope | Kind | What it does |",
        "|---|---|---|---|---|",
    ]
    for t in TRANSFORM_CATALOG:
        lines.append(f"| {t['id']} | `{t['name']}` | {t['scope']} | {t['kind']} | {t['description']} |")
    lines += ["", f"HAI method label stored on every derived row: `{HAI_METHOD}`", "",
              "## Rejection reasons (row goes to `quarantine`)", "", "| Code | Meaning |", "|---|---|"]
    lines += [f"| `{k}` | {v} |" for k, v in QUARANTINE_REASONS.items()]
    lines += ["", "## QC flags (row kept in `observations`; values left empty, never invented)", "",
              "| Code | Meaning |", "|---|---|"]
    lines += [f"| `{k}` | {v} |" for k, v in QC_FLAGS.items()]
    lines += ["", "## Duplicate reasons (row goes to `duplicate_log`)", "", "| Code | Meaning |", "|---|---|"]
    lines += [f"| `{k}` | {v} |" for k, v in DUPLICATE_REASONS.items()]
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="emberwatch", description="EmberWatch FIRMS backend")
    p.add_argument("--mode", choices=["auto", "live", "demo"], help="override EMBERWATCH_MODE")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show mode, configuration and data counts (never shows the key)")

    f = sub.add_parser("fetch", help="download raw FIRMS data only (no cleaning)")
    f.add_argument("--start", type=_date)
    f.add_argument("--end", type=_date)
    f.add_argument("--force", action="store_true", help="refetch even dates already marked final")
    f.add_argument("--plan-only", action="store_true", help="show the requests that would be made; make none")

    h = sub.add_parser("harmonize", help="clean raw files into observations, rebuild derived data and exports")
    h.add_argument("--rebuild", action="store_true", help="reprocess ALL raw files from scratch")

    r = sub.add_parser("run", help="fetch + harmonize + derive + export (what the scheduler runs)")
    r.add_argument("--start", type=_date)
    r.add_argument("--end", type=_date)
    r.add_argument("--force", action="store_true")

    b = sub.add_parser("backfill", help="historical retrieval over a date range, then process it")
    b.add_argument("--start", type=_date, help="default: EMBERWATCH_BACKFILL_START")
    b.add_argument("--end", type=_date, help="default: today")
    b.add_argument("--force", action="store_true")

    s = sub.add_parser("serve", help="run the pipeline repeatedly on a schedule")
    s.add_argument("--every-minutes", type=int, default=180)

    c = sub.add_parser("catalog", help="print or write the transformation catalogue (Markdown)")
    c.add_argument("--write", type=Path, help="write to this file instead of printing")

    sub.add_parser("quality", help="print the data-quality report as JSON")
    return p


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_status(cfg: Settings) -> int:
    info = {
        "mode": cfg.mode,
        "data_mode": cfg.data_mode,
        "map_key_configured": cfg.key_configured,        # yes/no only - the key is never printed
        "data_dir": str(cfg.data_dir),
        "database_exists": cfg.db_path.exists(),
        "day_range": cfg.day_range,
        "rate_budget_per_10min": cfg.max_txn_per_window,
    }
    if cfg.mode == "demo":
        info["notice"] = "DEMO MODE: all data is clearly labelled synthetic. Set FIRMS_MAP_KEY to use real data."
    if cfg.db_path.exists():
        conn = connect(cfg.db_path, read_only=True)
        try:
            info["counts"] = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                              for t in ("raw_files", "observations", "quarantine", "duplicate_log")}
        finally:
            conn.close()
    if cfg.mode == "live":
        try:
            st = make_source(cfg).map_key_status()
            info["firms_transaction_status"] = st if st is not None else "unavailable"
        except Exception as exc:
            info["firms_transaction_status"] = f"unavailable ({cfg.redact(str(exc))})"
    _print(info)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_settings(mode_override=args.mode)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    # Read-only commands do not create log files or folders.
    setup_logging(cfg, to_file=args.cmd in ("fetch", "harmonize", "run", "backfill", "serve"))

    try:
        if args.cmd == "status":
            return cmd_status(cfg)

        if args.cmd == "catalog":
            text = render_catalog_markdown()
            if args.write:
                args.write.parent.mkdir(parents=True, exist_ok=True)
                args.write.write_text(text, encoding="utf-8")
                print(f"Wrote {args.write}")
            else:
                print(text)
            return 0

        if args.cmd == "quality":
            conn = open_db(cfg)
            try:
                _print(build_report(conn, cfg.data_mode))
            finally:
                conn.close()
            return 0

        today = utcnow().astimezone(timezone.utc).date()

        if args.cmd == "fetch":
            start = args.start or (today - timedelta(days=cfg.update_days - 1))
            end = args.end or today
            conn = open_db(cfg)
            try:
                with run_lock(cfg):
                    res = IngestService(cfg, conn, make_source(cfg)).run(
                        start, end, force=args.force, trigger="cli-fetch", plan_only=args.plan_only)
            finally:
                conn.close()
            out = res.as_dict()
            if args.plan_only:
                out["note"] = "Plan only: no requests were made. Availability was not queried, so a heuristic " \
                              "decides standard vs near-real-time sources."
            _print(out)
            return 0 if res.status in ("ok", "planned_only", "partial") else 1

        if args.cmd == "harmonize":
            with run_lock(cfg):
                _print(run_pipeline(cfg, rebuild=args.rebuild, skip_fetch=True, trigger="cli-harmonize"))
            return 0

        if args.cmd == "run":
            with run_lock(cfg):
                out = run_pipeline(cfg, start=args.start, end=args.end, force=args.force, trigger="cli-run")
            _print(out)
            return 0 if out["ingest"]["status"] in ("ok", "partial") else 1

        if args.cmd == "backfill":
            start = args.start or date.fromisoformat(cfg.backfill_start)
            with run_lock(cfg):
                out = run_pipeline(cfg, start=start, end=args.end, force=args.force, trigger="cli-backfill")
            _print(out)
            return 0 if out["ingest"]["status"] in ("ok", "partial") else 1

        if args.cmd == "serve":
            serve(cfg, args.every_minutes)
            return 0
    except LockBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"Error: {cfg.redact(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
