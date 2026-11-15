"""NASA FIRMS product registry and sensor knowledge.

Source names are the FIRMS API `SOURCE` values. As of the last check (Nov 2026):

* MODIS_NRT / MODIS_SP        -> one source covering BOTH Terra and Aqua; the
                                 `satellite` column of each row says which.
* VIIRS_SNPP_NRT / _SP        -> Suomi-NPP. Production of new Suomi-NPP data
                                 ceased on 1-2 Nov 2026 (NASA gives both dates in
                                 different notices), so it is HISTORICAL ONLY.
* VIIRS_NOAA20_NRT / _SP      -> NOAA-20
* VIIRS_NOAA21_NRT            -> NOAA-21 (no SP source listed on the area page)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Product:
    source_id: str                      # FIRMS API SOURCE value
    group: str                          # platform group: MODIS | SNPP | NOAA20 | NOAA21
    instrument: str                     # MODIS | VIIRS
    processing: str                     # NRT | SP
    expected_satellites: Tuple[str, ...]
    nominal_resolution_m: int           # nominal pixel size at nadir
    last_data_date: Optional[date] = None   # None = still produced
    note: str = ""


# Suomi-NPP: NASA's FIRMS notice says 13:00 UTC on 2 Nov 2026, other NASA notices say
# 1 Nov 2026. We use the earlier date as the last day we expect complete data.
SNPP_LAST_DATA_DATE = date(2026, 11, 1)

_PRODUCTS: List[Product] = [
    Product("MODIS_NRT", "MODIS", "MODIS", "NRT", ("Terra", "Aqua"), 1000),
    Product("MODIS_SP", "MODIS", "MODIS", "SP", ("Terra", "Aqua"), 1000),
    Product("VIIRS_SNPP_NRT", "SNPP", "VIIRS", "NRT", ("Suomi-NPP",), 375,
            SNPP_LAST_DATA_DATE, "Retired: historical data only."),
    Product("VIIRS_SNPP_SP", "SNPP", "VIIRS", "SP", ("Suomi-NPP",), 375,
            SNPP_LAST_DATA_DATE, "Retired: historical data only."),
    Product("VIIRS_NOAA20_NRT", "NOAA20", "VIIRS", "NRT", ("NOAA-20",), 375),
    Product("VIIRS_NOAA20_SP", "NOAA20", "VIIRS", "SP", ("NOAA-20",), 375),
    Product("VIIRS_NOAA21_NRT", "NOAA21", "VIIRS", "NRT", ("NOAA-21",), 375),
]

PRODUCTS: Dict[str, Product] = {p.source_id: p for p in _PRODUCTS}
GROUPS: Tuple[str, ...] = ("MODIS", "SNPP", "NOAA20", "NOAA21")


def products_for_group(group: str) -> Dict[str, Optional[Product]]:
    """Return {'NRT': Product|None, 'SP': Product|None} for a platform group."""
    out: Dict[str, Optional[Product]] = {"NRT": None, "SP": None}
    for p in _PRODUCTS:
        if p.group == group:
            out[p.processing] = p
    return out


# Satellite launch dates: used only to reject timestamps that are impossible.
SATELLITE_LAUNCH = {
    "Terra": date(1999, 12, 18),
    "Aqua": date(2002, 5, 4),
    "Suomi-NPP": date(2011, 10, 28),
    "NOAA-20": date(2017, 11, 18),
    "NOAA-21": date(2022, 11, 10),
}

# Raw code (upper-cased) -> canonical satellite name, per instrument.
SATELLITE_CODES: Dict[str, Dict[str, str]] = {
    "MODIS": {
        "T": "Terra", "TERRA": "Terra",
        "A": "Aqua", "AQUA": "Aqua",
    },
    "VIIRS": {
        "N": "Suomi-NPP", "SNPP": "Suomi-NPP", "SUOMI-NPP": "Suomi-NPP", "SUOMI NPP": "Suomi-NPP",
        "N20": "NOAA-20", "NOAA-20": "NOAA-20", "NOAA20": "NOAA-20",
        "N21": "NOAA-21", "NOAA-21": "NOAA-21", "NOAA21": "NOAA-21",
    },
}

# Instrument-specific raw columns -> (common column, band label).
# NOTE: these bands are different physical channels; they are mapped to common
# *names* only so that tables line up, never converted into each other.
BRIGHTNESS_COLUMNS = {
    "MODIS": {
        "primary": ("brightness", "T21"),
        "secondary": ("bright_t31", "T31"),
    },
    "VIIRS": {
        "primary": ("bright_ti4", "I4"),
        "secondary": ("bright_ti5", "I5"),
    },
}

# Header row written by FIRMS area CSVs (used by the synthetic source).
FIRMS_HEADERS = {
    "MODIS": "latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,"
             "confidence,version,bright_t31,frp,daynight",
    "VIIRS": "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
             "confidence,version,bright_ti5,frp,daynight",
}

INSTRUMENT_FOR_GROUP = {"MODIS": "MODIS", "SNPP": "VIIRS", "NOAA20": "VIIRS", "NOAA21": "VIIRS"}


def canonical_satellite(instrument: str, raw_code: str) -> Optional[str]:
    return SATELLITE_CODES.get(instrument, {}).get((raw_code or "").strip().upper())
