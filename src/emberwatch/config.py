"""Configuration. The NASA MAP_KEY is read ONLY from the environment.

Where the key comes from:
  * on your computer: a local `.env` file (git-ignored) or an exported variable;
  * in GitHub Actions: `env: FIRMS_MAP_KEY: ${{ secrets.FIRMS_MAP_KEY }}`;
  * on a host (Render, Railway, ...): that host's environment-variable settings.

The key is never written to logs, raw files, the database or exports. If it is
missing, the system runs in DEMO mode with clearly labelled synthetic data.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .schema import DATA_MODE_DEMO, DATA_MODE_LIVE

# <repo>/src/emberwatch/config.py -> parents[2] is the repository root. Anchoring the default data folder
# there means the backend, the wrapper scripts and the dashboard agree wherever they are started from.
REPO_ROOT = Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    return REPO_ROOT / "data"


PLACEHOLDER_KEYS = {
    "",
    "your_firms_map_key_here",
    "your_key_here",
    "changeme",
    "replace_me",
    "xxxxxxxx",
}
# FIRMS MAP_KEYs are alphanumeric. Anything else is rejected before it can end up in a URL.
_MAP_KEY_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")


class ConfigError(Exception):
    """Raised for invalid configuration (never includes the key itself)."""


@dataclass(frozen=True)
class Region:
    region_id: str
    name: str
    bbox: Tuple[float, float, float, float]   # west, south, east, north
    note: str = ""


# The frontend only shows a centre point for each region. These boxes are
# PLACEHOLDER assumptions centred on those points; edit them here if needed.
# They are plain rectangles, so neighbouring regions can overlap (for example the
# southern tip of Peninsular Malaysia lies inside the Sumatra box). A detection is
# counted in every region whose box contains it.
REGIONS: Dict[str, Region] = {
    "sea": Region("sea", "Maritime Southeast Asia", (95.0, -8.0, 121.4, 11.0),
                  "Fetch domain. Centre ~ (1.5N, 108.2E)."),
    "johor": Region("johor", "Peninsular Malaysia & Johor", (102.26, 0.48, 105.26, 2.48),
                    "Centre ~ (1.48N, 103.76E)."),
    "sumatra": Region("sumatra", "Sumatra & Riau Peatlands", (95.0, -6.0, 108.0, 5.0),
                      "Centre ~ (0.5S, 101.5E)."),
    "borneo": Region("borneo", "Kalimantan & Borneo", (108.0, -4.5, 119.8, 1.3),
                     "Centre ~ (1.6S, 113.9E)."),
}
FETCH_REGION_ID = "sea"


def load_dotenv(path: Path) -> int:
    """Tiny .env reader (KEY=VALUE lines). Never overrides variables that are already set."""
    if not path.is_file():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _get_int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return value


def _get_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def valid_map_key(value: Optional[str]) -> bool:
    if value is None:
        return False
    v = value.strip()
    if v.lower() in PLACEHOLDER_KEYS:
        return False
    return bool(_MAP_KEY_RE.match(v))


@dataclass
class Settings:
    mode: str                                   # "live" | "demo"
    map_key: Optional[str] = field(default=None, repr=False)   # never printed
    base_url: str = "https://firms.modaps.eosdis.nasa.gov"
    root_dir: Path = Path("data")

    day_range: int = 5
    update_days: int = 7
    nrt_refresh_days: int = 3
    min_refetch_minutes: int = 120
    sp_lag_days: int = 90
    backfill_start: str = "2025-01-01"

    max_txn_per_window: int = 1000
    rate_window_seconds: int = 600
    min_request_interval_s: float = 0.25
    txn_reserve: int = 500
    http_timeout_s: int = 60
    max_retries: int = 4
    backoff_base_s: float = 2.0
    backoff_max_s: float = 120.0
    max_consecutive_failures: int = 3

    synthetic_seed: int = 42
    synthetic_defects: bool = True

    grid_resolutions: Tuple[float, ...] = (0.10, 0.05, 0.25)
    link_max_dt_minutes: int = 120
    link_max_pairs: int = 2_000_000
    log_level: str = "INFO"

    # ---- derived paths -------------------------------------------------
    @property
    def data_mode(self) -> str:
        return DATA_MODE_LIVE if self.mode == "live" else DATA_MODE_DEMO

    @property
    def data_dir(self) -> Path:
        # Live and demo data live in separate folders so they can never be mixed.
        return self.root_dir / self.mode

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "emberwatch.sqlite"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".run.lock"

    @property
    def key_configured(self) -> bool:
        return valid_map_key(self.map_key)

    def redact(self, text: str) -> str:
        """Remove the MAP_KEY from any text before it is logged or stored."""
        if self.map_key and self.map_key in text:
            return text.replace(self.map_key, "***")
        return text

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.raw_dir, self.export_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)


def resolve_mode(requested: str, key: Optional[str]) -> str:
    requested = (requested or "auto").strip().lower()
    if requested not in ("auto", "live", "demo"):
        raise ConfigError("EMBERWATCH_MODE must be auto, live or demo")
    if requested == "demo":
        return "demo"
    if requested == "live":
        if not valid_map_key(key):
            raise ConfigError(
                "EMBERWATCH_MODE=live but FIRMS_MAP_KEY is missing or still a placeholder. "
                "Set it as an environment variable (see .env.example)."
            )
        return "live"
    return "live" if valid_map_key(key) else "demo"


def load_settings(
    env: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Path] = Path(".env"),
    mode_override: Optional[str] = None,
) -> Settings:
    """Build Settings from the environment (and a local .env if present)."""
    if env is None:
        # EMBERWATCH_NO_DOTENV=1 turns .env loading off (the tests use it so a real .env can never leak in).
        if dotenv_path is not None and not os.environ.get("EMBERWATCH_NO_DOTENV"):
            load_dotenv(Path(dotenv_path))
            load_dotenv(REPO_ROOT / ".env")       # also find the repo-root .env when started from another folder
        env = os.environ

    key = env.get("FIRMS_MAP_KEY")
    key = key.strip() if key else None
    mode = resolve_mode(mode_override or env.get("EMBERWATCH_MODE", "auto"), key)

    resolutions = (0.10, 0.05, 0.25)
    raw_res = env.get("EMBERWATCH_GRID_RESOLUTIONS")
    if raw_res and raw_res.strip():
        try:
            resolutions = tuple(float(x) for x in raw_res.split(","))
        except ValueError:
            raise ConfigError("EMBERWATCH_GRID_RESOLUTIONS must be comma-separated numbers") from None
        if not resolutions or any(r <= 0 or r > 5 for r in resolutions):
            raise ConfigError("EMBERWATCH_GRID_RESOLUTIONS values must be >0 and <=5")

    backfill_start = env.get("EMBERWATCH_BACKFILL_START", "2025-01-01").strip() or "2025-01-01"
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", backfill_start):
        raise ConfigError("EMBERWATCH_BACKFILL_START must be YYYY-MM-DD")

    base_url = (env.get("FIRMS_BASE_URL") or "https://firms.modaps.eosdis.nasa.gov").strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ConfigError("FIRMS_BASE_URL must start with https://")

    return Settings(
        mode=mode,
        map_key=key if mode == "live" else None,
        base_url=base_url,
        root_dir=Path(env["EMBERWATCH_DATA_DIR"]) if env.get("EMBERWATCH_DATA_DIR") else default_data_dir(),
        day_range=_get_int(env, "EMBERWATCH_DAY_RANGE", 5, 1, 10),
        update_days=_get_int(env, "EMBERWATCH_UPDATE_DAYS", 7, 1, 60),
        nrt_refresh_days=_get_int(env, "EMBERWATCH_NRT_REFRESH_DAYS", 3, 1, 30),
        min_refetch_minutes=_get_int(env, "EMBERWATCH_MIN_REFETCH_MINUTES", 120, 0, 10080),
        sp_lag_days=_get_int(env, "EMBERWATCH_SP_LAG_DAYS", 90, 1, 730),
        backfill_start=backfill_start,
        max_txn_per_window=_get_int(env, "EMBERWATCH_MAX_TXN_PER_10MIN", 1000, 10, 5000),
        txn_reserve=_get_int(env, "EMBERWATCH_TXN_RESERVE", 500, 0, 4000),
        http_timeout_s=_get_int(env, "EMBERWATCH_HTTP_TIMEOUT", 60, 5, 600),
        max_retries=_get_int(env, "EMBERWATCH_MAX_RETRIES", 4, 0, 10),
        synthetic_seed=_get_int(env, "EMBERWATCH_SYNTHETIC_SEED", 42, 0, 2**31 - 1),
        synthetic_defects=_get_bool(env, "EMBERWATCH_SYNTHETIC_DEFECTS", True),
        grid_resolutions=resolutions,
        link_max_dt_minutes=_get_int(env, "EMBERWATCH_LINK_MAX_DT_MIN", 120, 1, 1440),
        log_level=(env.get("EMBERWATCH_LOG_LEVEL") or "INFO").upper(),
    )
