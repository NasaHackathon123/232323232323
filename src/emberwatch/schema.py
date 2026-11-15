"""Constants, QC vocabularies and the transformation catalogue.

Everything the pipeline can do to a record is named here, so that every
transformation is documented in one place (see docs/TRANSFORMATIONS.md, which
is generated from TRANSFORM_CATALOG).
"""

from __future__ import annotations

PIPELINE_VERSION = "1.0.0"

# --- record classes -----------------------------------------------------------
# "observed_hotspot": one row of a NASA FIRMS file, kept as received.
# "derived": computed by EmberWatch from observed hotspots (never an observation).
RECORD_CLASS_OBSERVED = "observed_hotspot"
RECORD_CLASS_DERIVED = "derived"
RECORD_CLASS_LINK = "derived_proximity_candidate"

DATA_MODE_LIVE = "live"
DATA_MODE_DEMO = "synthetic_demo"

# Columns that MUST be present and valid, or the row is quarantined.
REQUIRED_COLUMNS = (
    "latitude",
    "longitude",
    "acq_date",
    "acq_time",
    "satellite",
    "instrument",
)

# --- quarantine reasons (row rejected from `observations`, kept in `quarantine`)
QUARANTINE_REASONS = {
    "MALFORMED_ROW": "Row has more or fewer fields than the header.",
    "MISSING_REQUIRED_COLUMN": "The file header lacks a required column.",
    "MISSING_REQUIRED_VALUE": "A required field is empty.",
    "LAT_NOT_NUMERIC": "Latitude is not a finite number.",
    "LON_NOT_NUMERIC": "Longitude is not a finite number.",
    "LAT_OUT_OF_RANGE": "Latitude is outside -90..90.",
    "LON_OUT_OF_RANGE": "Longitude is outside -180..180.",
    "COORD_ZERO_ZERO": "Latitude and longitude are both exactly 0 (common placeholder).",
    "DATE_INVALID": "acq_date is not a valid YYYY-MM-DD date.",
    "TIME_INVALID": "acq_time is not a valid HHMM UTC time.",
    "TIMESTAMP_FUTURE": "Acquisition time is later than the time of ingestion.",
    "TIMESTAMP_BEFORE_LAUNCH": "Acquisition time is earlier than the satellite's launch date.",
    "SATELLITE_UNKNOWN": "Satellite code is not in the known code table.",
    "INSTRUMENT_MISMATCH": "Instrument in the row differs from the requested FIRMS product.",
    "SATELLITE_PRODUCT_MISMATCH": "Satellite in the row is not expected for the requested FIRMS product.",
}

# --- QC flags (row kept in `observations`; value set to NULL or kept, never invented)
QC_FLAGS = {
    "FRP_MISSING": "FRP is empty in the source; stored as NULL.",
    "FRP_INVALID": "FRP is not a finite number; numeric column is NULL, raw text kept.",
    "FRP_NEGATIVE": "FRP is negative (not physical); numeric column is NULL, raw text kept.",
    "FRP_EXTREME": "FRP exceeds the review threshold; value kept unchanged.",
    "BRIGHTNESS_MISSING": "A brightness temperature is empty; stored as NULL.",
    "BRIGHTNESS_INVALID": "A brightness temperature is not numeric; stored as NULL.",
    "BRIGHTNESS_OUT_OF_RANGE": "A brightness temperature is outside 200..700 K; value kept unchanged.",
    "CONFIDENCE_MISSING": "Confidence is empty in the source.",
    "CONFIDENCE_INVALID": "Confidence is not valid for the instrument's own scale.",
    "CONFIDENCE_SCHEME_MISMATCH": "Confidence looks like the other instrument's scale; no class derived.",
    "DAYNIGHT_MISSING": "Day/night flag is empty in the source.",
    "DAYNIGHT_INVALID": "Day/night flag is not D or N.",
    "PIXEL_SIZE_MISSING": "scan and/or track (pixel size, km) is missing or invalid.",
    "VERSION_MISSING": "Product version string is empty in the source.",
    "OUTSIDE_REQUEST_AREA": "Coordinates fall outside the bounding box that was requested.",
    "OUTSIDE_REQUEST_WINDOW": "Acquisition date falls outside the requested date window.",
    "REVISION_OF_EARLIER_FETCH": "Same satellite, time and position was fetched earlier with different values "
    "(for example a preliminary real-time record replaced by near-real-time). This is the later version; "
    "the earlier one is kept with is_current=0 and superseded_by='REVISION'.",
}

# --- duplicate reasons (row NOT inserted, kept in `duplicate_log`)
DUPLICATE_REASONS = {
    "EXACT_DUPLICATE_IN_FILE": "Identical record appears twice in the same raw file.",
    "EXACT_DUPLICATE_REFETCH": "Identical record already stored from an earlier fetch (overlapping window).",
    "CONFLICT_SAME_KEY_IN_FILE": "Same satellite/time/position but different content within one file; first kept.",
}

# --- thresholds that are review aids only (they never change a value) ----------
FRP_EXTREME_MW = 10000.0
BRIGHTNESS_MIN_K = 200.0
BRIGHTNESS_MAX_K = 700.0
FUTURE_TOLERANCE_HOURS = 1

# MODIS confidence classes (percent). Derived vocabulary only.
MODIS_CONF_LOW_BELOW = 30.0      # < 30   -> low
MODIS_CONF_HIGH_FROM = 80.0      # >= 80  -> high, otherwise nominal

# --- transformation catalogue ---------------------------------------------------
TRANSFORM_CATALOG = [
    {
        "id": "T01",
        "name": "decode_and_parse_csv",
        "scope": "file",
        "kind": "technical",
        "description": "Decode bytes as UTF-8 (BOM tolerated, undecodable bytes replaced) and split into rows. "
        "The raw file on disk is never modified.",
    },
    {
        "id": "T02",
        "name": "normalise_header_names",
        "scope": "file",
        "kind": "technical",
        "description": "Header names are trimmed and lower-cased for lookup only. The original header spelling is "
        "preserved in raw_json.",
    },
    {
        "id": "T03",
        "name": "empty_tokens_to_null",
        "scope": "row",
        "kind": "technical",
        "description": "Empty strings and the tokens NA, N/A, NAN, NULL, NONE and '-' become NULL. No value is "
        "substituted or estimated. Applied only when such a token was present.",
    },
    {
        "id": "T04",
        "name": "parse_numeric_fields",
        "scope": "row",
        "kind": "technical",
        "description": "Latitude, longitude, scan, track, FRP, brightness values and numeric confidence are parsed "
        "as decimal numbers. Units are unchanged (degrees, km, MW, K). The original text is kept "
        "for FRP and confidence (frp_raw, confidence_raw) and for every field in raw_json.",
    },
    {
        "id": "T05",
        "name": "build_utc_timestamp",
        "scope": "row",
        "kind": "technical",
        "description": "acq_date (YYYY-MM-DD) and acq_time (HHMM, UTC) are combined into acquired_at_utc "
        "(ISO 8601, minute resolution, as delivered by FIRMS). The raw strings are kept as "
        "acq_date_raw and acq_time_raw.",
    },
    {
        "id": "T05a",
        "name": "zero_pad_acq_time",
        "scope": "row",
        "kind": "technical",
        "description": "An acq_time with fewer than 4 digits (for example 642) is left-padded with zeros (0642). "
        "Applied only when needed; acq_time_raw keeps the original text.",
    },
    {
        "id": "T06",
        "name": "canonicalise_satellite",
        "scope": "row",
        "kind": "naming",
        "description": "Satellite codes (T, A, N, N20, N21, Terra, Aqua, ...) are mapped to canonical names "
        "(Terra, Aqua, Suomi-NPP, NOAA-20, NOAA-21). The original code is kept as satellite_code_raw.",
    },
    {
        "id": "T07",
        "name": "map_instrument_specific_columns",
        "scope": "row",
        "kind": "naming",
        "description": "Instrument-specific column names are mapped to common names WITHOUT changing values: "
        "MODIS brightness -> brightness_k (band T21), bright_t31 -> brightness_2_k (band T31); "
        "VIIRS bright_ti4 -> brightness_k (band I4), bright_ti5 -> brightness_2_k (band I5). "
        "The band is stored in brightness_band / brightness_2_band because the bands are not "
        "interchangeable between instruments.",
    },
    {
        "id": "T08",
        "name": "derive_confidence_class",
        "scope": "row",
        "kind": "derived_label",
        "description": "A shared low/nominal/high LABEL is derived from each instrument's own confidence scale "
        "(MODIS percent: <30 low, 30-79 nominal, >=80 high; VIIRS: l/n/h). The label is a "
        "convenience for filtering only. MODIS and VIIRS confidence are NOT statistically "
        "equivalent, so the original value and its scheme are always kept alongside.",
    },
    {
        "id": "T09",
        "name": "compute_observation_identity",
        "scope": "row",
        "kind": "technical",
        "description": "observation_id = SHA-256 of satellite | instrument | acquired_at_utc | latitude(5 dp) | "
        "longitude(5 dp). The satellite is part of the identity, so detections from different "
        "satellites can never collide. Matching is exact; there is no fuzzy coordinate matching.",
    },
    {
        "id": "T10",
        "name": "version_precedence",
        "scope": "dataset",
        "kind": "selection",
        "description": "Chooses which stored version of an observation is current, without deleting or editing any. "
        "(a) When standard-processing (SP) data exists for a satellite and UTC date, near-real-time (NRT) "
        "rows of that same satellite and date get is_current=0, superseded_by='SP'. "
        "(b) When a later fetch returns the same satellite/time/position with different values (FIRMS NRT "
        "sources first carry real-time/ultra-real-time records that are later replaced), the later "
        "version is current and the earlier gets superseded_by='REVISION'. "
        "Different satellites are never compared.",
    },
    {
        "id": "T11",
        "name": "assign_common_grid_cell",
        "scope": "dataset",
        "kind": "derived_product",
        "description": "Each current observation is assigned to a regular latitude/longitude grid cell "
        "(0.05, 0.10 and 0.25 degrees) to build grid_cell_day. Derived product, not an observation.",
    },
    {
        "id": "T12",
        "name": "daily_activity_index",
        "scope": "dataset",
        "kind": "derived_product",
        "description": "Per region and UTC day: raw MODIS and VIIRS counts, occupied grid cells, mean FRP and "
        "the Harmonised Activity Index HAI = occupied_cells / satellites_contributing_7d "
        "(0 when no cell is occupied). hai_method is recorded on every row. Derived product.",
    },
    {
        "id": "T13",
        "name": "cross_sensor_proximity_links",
        "scope": "dataset",
        "kind": "derived_product",
        "description": "Detections from DIFFERENT satellites that are close in space and time are LINKED in "
        "cross_sensor_links for analysis. Nothing is merged, removed or altered.",
    },
    {
        "id": "T14",
        "name": "frontend_export_projection",
        "scope": "export",
        "kind": "presentation",
        "description": "Read-only projection into the JSON/CSV shapes used by the EmberWatch frontend. For display "
        "VIIRS l/n/h is spelled Low/Nominal/High and MODIS confidence is shown with a percent sign. "
        "Originals remain in the extra *_raw columns.",
    },
]

TRANSFORM_IDS = {t["id"] for t in TRANSFORM_CATALOG}

HAI_METHOD = "v0-proposed: occupied_cells / satellites_contributing_7d"
