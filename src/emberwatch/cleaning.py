"""Row-level validation and mapping to the common observation schema.

Principles (see docs/DATA_ARCHITECTURE.md):
  * a row is REJECTED (quarantined, with reasons) only if it cannot be located in
    space/time or attributed to a satellite;
  * every other problem becomes a QC flag and the value is left empty (NULL) -
    nothing is guessed, filled in or corrected;
  * the original row is always kept in `raw_json`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .schema import (
    BRIGHTNESS_MAX_K,
    BRIGHTNESS_MIN_K,
    FRP_EXTREME_MW,
    FUTURE_TOLERANCE_HOURS,
    MODIS_CONF_HIGH_FROM,
    MODIS_CONF_LOW_BELOW,
    RECORD_CLASS_OBSERVED,
    REQUIRED_COLUMNS,
)
from .sources import (
    BRIGHTNESS_COLUMNS,
    SATELLITE_CODES,
    SATELLITE_LAUNCH,
    Product,
)

NULL_TOKENS = {"", "na", "n/a", "nan", "null", "none", "-"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{1,4}$")
_VIIRS_CONF = {"l": "low", "low": "low", "n": "nominal", "nominal": "nominal", "h": "high", "high": "high"}


@dataclass
class RequestContext:
    source_id: str
    product: Product
    bbox: Tuple[float, float, float, float]
    start: date
    days: int
    fetched_at: datetime
    data_mode: str
    raw_file_id: int
    ingest_run_id: Optional[str]


@dataclass
class CleanResult:
    quarantined: bool
    reasons: List[str] = field(default_factory=list)
    detail: str = ""
    record: Optional[Dict[str, object]] = None
    flags: List[str] = field(default_factory=list)
    transforms: List[str] = field(default_factory=list)


def parse_float(text: Optional[str]) -> Optional[float]:
    """Decimal number or None. NaN/inf are rejected."""
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def raw_json_of(raw: Dict[object, object]) -> str:
    """The original row exactly as read, as JSON (keys keep the file's own spelling)."""
    clean = {("" if k is None else str(k)): v for k, v in raw.items()}
    return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))


def derive_confidence(
    raw_text: Optional[str], instrument: str
) -> Tuple[str, Optional[float], Optional[str], List[str]]:
    """Return (scheme, numeric value as received, derived class, flags).

    MODIS confidence is a percent; VIIRS confidence is a category. They are different
    scales, so a value that looks like the other instrument's scale is flagged and gets
    no class instead of being converted.
    """
    scheme = "modis_percent" if instrument == "MODIS" else "viirs_category"
    if raw_text is None or raw_text.strip().lower() in NULL_TOKENS:
        return scheme, None, None, ["CONFIDENCE_MISSING"]
    t = raw_text.strip()
    if instrument == "MODIS":
        f = parse_float(t)
        if f is None:
            if t.lower() in _VIIRS_CONF:
                return scheme, None, None, ["CONFIDENCE_SCHEME_MISMATCH"]
            return scheme, None, None, ["CONFIDENCE_INVALID"]
        if not 0.0 <= f <= 100.0:
            return scheme, None, None, ["CONFIDENCE_INVALID"]
        if f < MODIS_CONF_LOW_BELOW:
            cls = "low"
        elif f >= MODIS_CONF_HIGH_FROM:
            cls = "high"
        else:
            cls = "nominal"
        return scheme, f, cls, []
    cls = _VIIRS_CONF.get(t.lower())
    if cls is not None:
        return scheme, None, cls, []
    if parse_float(t) is not None:
        return scheme, None, None, ["CONFIDENCE_SCHEME_MISMATCH"]
    return scheme, None, None, ["CONFIDENCE_INVALID"]


def observation_identity(satellite: str, instrument: str, acquired_at_utc: str, lat: float, lon: float) -> str:
    key = f"{satellite}|{instrument}|{acquired_at_utc}|{lat:.5f}|{lon:.5f}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def content_hash_of(norm: Dict[str, Optional[str]]) -> str:
    payload = "\x1f".join(f"{k}={'' if v is None else v}" for k, v in sorted(norm.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clean_row(
    raw: Dict[object, object],
    ctx: RequestContext,
    missing_columns: Optional[List[str]] = None,
) -> CleanResult:
    """Validate one raw CSV row and map it to the common schema."""
    # ---- structural problems -------------------------------------------------
    if missing_columns:
        return CleanResult(True, ["MISSING_REQUIRED_COLUMN"], ",".join(missing_columns))
    if None in raw or any(v is None for v in raw.values()):
        return CleanResult(True, ["MALFORMED_ROW"], "field count differs from header")

    transforms: List[str] = []
    norm: Dict[str, Optional[str]] = {}
    nulled = False
    for k, v in raw.items():
        key = str(k).strip().lower()
        val = str(v).strip()
        norm[key] = val

    def get(name: str) -> Optional[str]:
        """Value or None for empty / null tokens (T03)."""
        nonlocal nulled
        if name not in norm:
            return None
        v = norm[name]
        if v is None or v.strip().lower() in NULL_TOKENS:
            nulled = True
            return None
        return v

    product = ctx.product
    reasons: List[str] = []
    details: List[str] = []

    def reject(code: str, detail: str) -> None:
        reasons.append(code)
        details.append(detail)

    # ---- required: location, time, platform ------------------------------------
    req: Dict[str, Optional[str]] = {c: get(c) for c in REQUIRED_COLUMNS}
    for c in REQUIRED_COLUMNS:
        if req[c] is None:
            reject("MISSING_REQUIRED_VALUE", c)

    lat = lon = None
    if req["latitude"] is not None:
        lat = parse_float(req["latitude"])
        if lat is None:
            reject("LAT_NOT_NUMERIC", f"latitude={req['latitude']!r}")
        elif not -90.0 <= lat <= 90.0:
            reject("LAT_OUT_OF_RANGE", f"latitude={req['latitude']}")
    if req["longitude"] is not None:
        lon = parse_float(req["longitude"])
        if lon is None:
            reject("LON_NOT_NUMERIC", f"longitude={req['longitude']!r}")
        elif not -180.0 <= lon <= 180.0:
            reject("LON_OUT_OF_RANGE", f"longitude={req['longitude']}")
    if lat is not None and lon is not None and not reasons and lat == 0.0 and lon == 0.0:
        reject("COORD_ZERO_ZERO", "latitude=0,longitude=0")

    acq_date: Optional[date] = None
    if req["acq_date"] is not None:
        if _DATE_RE.match(req["acq_date"]):
            try:
                acq_date = datetime.strptime(req["acq_date"], "%Y-%m-%d").date()
            except ValueError:
                acq_date = None
        if acq_date is None:
            reject("DATE_INVALID", f"acq_date={req['acq_date']!r}")

    hh = mm = None
    padded = False
    if req["acq_time"] is not None:
        t = req["acq_time"]
        if _TIME_RE.match(t):
            if len(t) < 4:
                t = t.zfill(4)
                padded = True
            hh, mm = int(t[:2]), int(t[2:])
            if hh > 23 or mm > 59:
                hh = mm = None
        if hh is None:
            reject("TIME_INVALID", f"acq_time={req['acq_time']!r}")

    satellite = None
    instrument_raw = (req["instrument"] or "").upper()
    if req["instrument"] is not None and instrument_raw != product.instrument:
        reject("INSTRUMENT_MISMATCH", f"instrument={req['instrument']!r} expected {product.instrument}")
    elif req["satellite"] is not None and req["instrument"] is not None:
        satellite = SATELLITE_CODES[product.instrument].get(req["satellite"].strip().upper())
        if satellite is None:
            reject("SATELLITE_UNKNOWN", f"satellite={req['satellite']!r}")
        elif satellite not in product.expected_satellites:
            reject("SATELLITE_PRODUCT_MISMATCH", f"{satellite} not expected in {product.source_id}")

    acquired: Optional[datetime] = None
    if acq_date is not None and hh is not None and mm is not None:
        acquired = datetime(acq_date.year, acq_date.month, acq_date.day, hh, mm, tzinfo=timezone.utc)
        if acquired > ctx.fetched_at + timedelta(hours=FUTURE_TOLERANCE_HOURS):
            reject("TIMESTAMP_FUTURE", f"acquired={acquired.isoformat()}")
        if satellite is not None and acq_date < SATELLITE_LAUNCH[satellite]:
            reject("TIMESTAMP_BEFORE_LAUNCH", f"{satellite} launched {SATELLITE_LAUNCH[satellite]}")

    if reasons:
        return CleanResult(True, list(dict.fromkeys(reasons)), "; ".join(details))

    assert lat is not None and lon is not None and acquired is not None and satellite is not None
    assert acq_date is not None
    flags: List[str] = []
    if padded:
        transforms.append("T05a")

    # ---- optional measurements: keep what is valid, flag the rest ---------------
    frp_raw = norm.get("frp")                       # exactly as received ('' if empty)
    frp_val: Optional[float] = None
    frp_txt = get("frp")
    if frp_txt is None:
        flags.append("FRP_MISSING")
    else:
        parsed = parse_float(frp_txt)
        if parsed is None:
            flags.append("FRP_INVALID")
        elif parsed < 0:
            flags.append("FRP_NEGATIVE")
        else:
            frp_val = parsed
            if parsed > FRP_EXTREME_MW:
                flags.append("FRP_EXTREME")

    cols = BRIGHTNESS_COLUMNS[product.instrument]
    bvals: Dict[str, Optional[float]] = {}
    for slot in ("primary", "secondary"):
        col, _band = cols[slot]
        txt = get(col)
        if txt is None:
            flags.append("BRIGHTNESS_MISSING")
            bvals[slot] = None
            continue
        v = parse_float(txt)
        if v is None:
            flags.append("BRIGHTNESS_INVALID")
        else:
            bvals[slot] = v
            if not BRIGHTNESS_MIN_K <= v <= BRIGHTNESS_MAX_K:
                flags.append("BRIGHTNESS_OUT_OF_RANGE")
        bvals.setdefault(slot, None)

    scan_v = parse_float(get("scan"))
    track_v = parse_float(get("track"))
    if scan_v is None or track_v is None or scan_v <= 0 or track_v <= 0:
        flags.append("PIXEL_SIZE_MISSING")
        if scan_v is not None and scan_v <= 0:
            scan_v = None
        if track_v is not None and track_v <= 0:
            track_v = None

    conf_text = get("confidence")
    scheme, conf_value, conf_class, conf_flags = derive_confidence(conf_text, product.instrument)
    flags.extend(conf_flags)

    dn_text = get("daynight")
    daynight: Optional[str] = None
    if dn_text is None:
        flags.append("DAYNIGHT_MISSING")
    elif dn_text.upper() in ("D", "N"):
        daynight = dn_text.upper()
    else:
        flags.append("DAYNIGHT_INVALID")

    version = get("version")
    if version is None:
        flags.append("VERSION_MISSING")

    west, south, east, north = ctx.bbox
    eps = 0.001
    if not (west - eps <= lon <= east + eps and south - eps <= lat <= north + eps):
        flags.append("OUTSIDE_REQUEST_AREA")
    window_end = ctx.start + timedelta(days=ctx.days - 1)
    if not ctx.start <= acq_date <= window_end:
        flags.append("OUTSIDE_REQUEST_WINDOW")

    if nulled:
        transforms.append("T03")
    flags = list(dict.fromkeys(flags))      # each flag once per row

    acquired_iso = acquired.strftime("%Y-%m-%dT%H:%M:%SZ")
    record: Dict[str, object] = {
        "observation_id": observation_identity(satellite, product.instrument, acquired_iso, lat, lon),
        "source_product": product.source_id,
        "processing_level": product.processing,
        "record_class": RECORD_CLASS_OBSERVED,
        "data_mode": ctx.data_mode,
        "satellite": satellite,
        "satellite_code_raw": raw_value(raw, "satellite"),
        "instrument": product.instrument,
        "nominal_resolution_m": product.nominal_resolution_m,
        "acquired_at_utc": acquired_iso,
        "acq_date_utc": acq_date.isoformat(),
        "acq_date_raw": raw_value(raw, "acq_date"),
        "acq_time_raw": raw_value(raw, "acq_time"),
        "daynight": daynight,
        "latitude": lat,
        "longitude": lon,
        "scan_km": scan_v,
        "track_km": track_v,
        "frp_mw": frp_val,
        "frp_raw": frp_raw,
        "brightness_k": bvals.get("primary"),
        "brightness_band": cols["primary"][1],
        "brightness_2_k": bvals.get("secondary"),
        "brightness_2_band": cols["secondary"][1],
        "confidence_raw": raw_value(raw, "confidence"),
        "confidence_scheme": scheme,
        "confidence_value": conf_value,
        "confidence_class_derived": conf_class,
        "product_version": version,
        "qc_status": "accepted_with_flags" if flags else "accepted",
        "qc_flags": ",".join(flags) if flags else None,
        "transforms_applied": ",".join(transforms) if transforms else None,
        "content_hash": content_hash_of(norm),
        "raw_json": raw_json_of(raw),
    }
    return CleanResult(False, [], "", record, flags, transforms)


def raw_value(raw: Dict[object, object], name: str) -> Optional[str]:
    """Original (un-trimmed) text of a column, matched case-insensitively on the header."""
    for k, v in raw.items():
        if k is not None and str(k).strip().lower() == name:
            return None if v is None else str(v)
    return None
