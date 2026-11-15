"""Shared helpers for the test-suite (standard library only)."""

import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emberwatch.config import REGIONS, load_settings          # noqa: E402
from emberwatch.db import connect, init_db                      # noqa: E402
from emberwatch.raw_store import RawStore                       # noqa: E402
from emberwatch.sources import FIRMS_HEADERS                    # noqa: E402

FAKE_KEY = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"      # obviously fake, 32 chars
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)

MODIS_HDR = FIRMS_HEADERS["MODIS"]
VIIRS_HDR = FIRMS_HEADERS["VIIRS"]


def make_cfg(tmp, mode="demo", **extra):
    env = {"EMBERWATCH_DATA_DIR": str(tmp), "EMBERWATCH_MODE": mode}
    if mode == "live":
        env["FIRMS_MAP_KEY"] = FAKE_KEY
    env.update({k: str(v) for k, v in extra.items()})
    return load_settings(env=env, dotenv_path=None)


def open_conn(cfg):
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    return conn


def modis_row(lat="1.480", lon="103.760", date_="2026-11-13", time_="0642", sat="Terra", conf="85",
              frp="52.4", bright="350.1", bright2="300.2", scan="1.0", track="1.0", dn="D", ver="6.1NRT"):
    return f"{lat},{lon},{bright},{scan},{track},{date_},{time_},{sat},MODIS,{conf},{ver},{bright2},{frp},{dn}"


def viirs_row(lat="1.48000", lon="103.76000", date_="2026-11-13", time_="0642", sat="N20", conf="h",
              frp="12.3", bright="340.5", bright2="290.1", scan="0.38", track="0.36", dn="D", ver="2.0NRT"):
    return f"{lat},{lon},{bright},{scan},{track},{date_},{time_},{sat},VIIRS,{conf},{ver},{bright2},{frp},{dn}"


def csv_text(header, *rows):
    return header + "\n" + "\n".join(rows) + ("\n" if rows else "")


def add_raw(cfg, conn, source_id, text, start=date(2026, 11, 13), days=1, fetched_at=NOW, kind="csv"):
    return RawStore(cfg, conn).save(
        run_id="test", source_id=source_id, aoi_id="sea", bbox=REGIONS["sea"].bbox, start=start, days=days,
        url_redacted="https://example.invalid/***", fetched_at=fetched_at, http_status=200,
        content=text.encode("utf-8"), kind=kind)


class TempDirMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
