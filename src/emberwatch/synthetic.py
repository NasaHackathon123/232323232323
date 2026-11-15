"""SYNTHETIC DEMONSTRATION DATA. NOT real NASA observations.

Used only while no real MAP_KEY is configured. It produces CSV text in exactly the
same column layout as the FIRMS area API, so the cleaning pipeline cannot tell the
difference and nothing has to be rewritten to switch to real data.

Everything it creates is labelled:
  * stored under data/demo/ (never data/live/),
  * file names start with SYNTHETIC_,
  * every database row carries data_mode = 'synthetic_demo',
  * every export carries a banner.

The generator is deterministic (seeded) and illustrative only: fire locations are
random draws around a few Southeast Asian hotspot regions, with an invented
seasonal pattern. It says nothing about real fires.

When `defects` is on, a small share of rows is deliberately damaged (impossible
latitude, missing FRP, invalid time, unknown satellite code, repeated rows) so the
cleaning pipeline and the quality dashboard have something to show.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .config import Settings
from .firms_client import BBox, FetchResult, estimate_cost
from .sources import FIRMS_HEADERS, Product

# (lat, lon, weight, sigma_deg) - invented cluster centres around SE Asia
_CLUSTERS = [
    (0.5, 101.8, 3.0, 0.45),    # Riau-like
    (-3.0, 104.5, 2.5, 0.50),   # South Sumatra-like
    (-2.3, 113.9, 3.0, 0.55),   # Central Kalimantan-like
    (-0.5, 110.5, 2.0, 0.45),   # West Kalimantan-like
    (1.9, 103.3, 1.0, 0.30),    # Johor-like
    (4.5, 98.0, 0.8, 0.35),
]

# Illustrative overpass times (UTC, minutes after midnight) per satellite: (day pass, night pass)
_PASS_MINUTES = {
    "Terra": (3 * 60 + 20, 15 * 60 + 20),
    "Aqua": (6 * 60 + 20, 18 * 60 + 20),
    "Suomi-NPP": (6 * 60 + 25, 18 * 60 + 25),
    "NOAA-20": (7 * 60 + 15, 19 * 60 + 15),
    "NOAA-21": (5 * 60 + 35, 17 * 60 + 35),
}
_RAW_CODE = {"Terra": "Terra", "Aqua": "Aqua", "Suomi-NPP": "N", "NOAA-20": "N20", "NOAA-21": "N21"}
# mean detections per satellite per day at seasonal peak
_PEAK_MEAN = {"Terra": 14.0, "Aqua": 16.0, "Suomi-NPP": 70.0, "NOAA-20": 70.0, "NOAA-21": 70.0}


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= limit:
            return k - 1


def _seasonal(d: date) -> float:
    """Invented seasonal curve: low in the wet season, peak around early September."""
    doy = d.timetuple().tm_yday
    return 0.08 + 0.92 * math.exp(-(((doy - 250) / 55.0) ** 2))


class SyntheticSource:
    mode = "demo"

    def __init__(self, cfg: Settings, now: Optional[datetime] = None):
        self.cfg = cfg
        self._now = now          # tests can pin the clock

    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def today(self) -> date:
        return self.now().astimezone(timezone.utc).date()

    # --- same surface as FirmsClient ---------------------------------
    def map_key_status(self) -> Optional[Dict[str, object]]:
        return None

    def availability(self) -> Dict[str, Tuple[date, date]]:
        """Mimic FIRMS: standard processing lags about 60 days behind."""
        sp_max = self.today() - timedelta(days=60)
        return {
            "MODIS_SP": (date(2000, 11, 1), sp_max),
            "VIIRS_SNPP_SP": (date(2012, 1, 20), sp_max),
            "VIIRS_NOAA20_SP": (date(2018, 4, 1), sp_max),
        }

    def fetch(self, product: Product, bbox: BBox, start: date, days: int) -> FetchResult:
        text = self.generate_csv(product, bbox, start, days)
        url = f"synthetic://demo/{product.source_id}/{start.isoformat()}/{days}"
        return FetchResult(url, 200, text.encode("utf-8"), "csv", 1, estimate_cost(bbox, days),
                           message="SYNTHETIC DEMONSTRATION DATA")

    # --- generation ----------------------------------------------------
    def generate_csv(self, product: Product, bbox: BBox, start: date, days: int) -> str:
        lines: List[str] = [FIRMS_HEADERS[product.instrument]]
        sp_max = self.availability().get("MODIS_SP", (None, None))[1]
        for i in range(days):
            d = start + timedelta(days=i)
            if d > self.today():
                continue
            if product.last_data_date and d > product.last_data_date:
                continue
            if product.processing == "SP" and sp_max and d > sp_max:
                continue   # SP data does not exist yet for recent days
            lines.extend(self._day_rows(product, bbox, d))
        return "\n".join(lines) + "\n"

    def _day_rows(self, product: Product, bbox: BBox, d: date) -> List[str]:
        west, south, east, north = bbox
        rows: List[Tuple[int, str]] = []
        for sat in product.expected_satellites:
            rng = random.Random(f"{self.cfg.synthetic_seed}|{product.source_id}|{sat}|{d.isoformat()}")
            lam = _PEAK_MEAN[sat] * _seasonal(d) * (1.0 + 0.25 * math.sin(d.toordinal() / 3.0))
            n = _poisson(rng, lam)
            for _ in range(n):
                cl = rng.choices(_CLUSTERS, weights=[c[2] for c in _CLUSTERS])[0]
                lat = min(max(rng.gauss(cl[0], cl[3]), south + 1e-4), north - 1e-4)
                lon = min(max(rng.gauss(cl[1], cl[3]), west + 1e-4), east - 1e-4)
                day_pass = rng.random() < 0.75
                minute = _PASS_MINUTES[sat][0 if day_pass else 1] + rng.randint(-6, 6)
                minute = max(0, min(23 * 60 + 59, minute))
                if d == self.today():
                    now = self.now()
                    if minute > now.hour * 60 + now.minute - 5:
                        continue                    # a satellite pass cannot be in the future
                acq_time = f"{minute // 60:02d}{minute % 60:02d}"
                frp_mu = 3.0 if product.instrument == "MODIS" else 2.0
                frp = round(rng.lognormvariate(frp_mu, 1.0), 1)
                if product.instrument == "MODIS":
                    conf = str(int(min(100, max(0, rng.gauss(70, 22)))))
                    bright = round(rng.uniform(305, 400), 1)
                    bright2 = round(rng.uniform(288, 315), 1)
                    scan = round(rng.uniform(1.0, 4.8), 1)
                    track = round(rng.uniform(1.0, 2.0), 1)
                    lat_s, lon_s = f"{lat:.3f}", f"{lon:.3f}"
                    version = "6.1NRT" if product.processing == "NRT" else "6.1"
                else:
                    conf = rng.choices(["l", "n", "h"], weights=[8, 80, 12])[0]
                    bright = round(rng.uniform(300, 367), 1)
                    bright2 = round(rng.uniform(280, 300), 1)
                    scan = round(rng.uniform(0.36, 0.80), 2)
                    track = round(rng.uniform(0.36, 0.60), 2)
                    lat_s, lon_s = f"{lat:.5f}", f"{lon:.5f}"
                    version = "2.0NRT" if product.processing == "NRT" else "2.0"
                code = _RAW_CODE[sat]
                fields = {
                    "latitude": lat_s, "longitude": lon_s, "bright": f"{bright}", "scan": f"{scan}",
                    "track": f"{track}", "acq_date": d.isoformat(), "acq_time": acq_time, "satellite": code,
                    "instrument": product.instrument, "confidence": conf, "version": version,
                    "bright2": f"{bright2}", "frp": f"{frp}", "daynight": "D" if day_pass else "N",
                }
                if self.cfg.synthetic_defects:
                    self._damage(rng, fields)
                line = ",".join([fields["latitude"], fields["longitude"], fields["bright"], fields["scan"],
                                 fields["track"], fields["acq_date"], fields["acq_time"], fields["satellite"],
                                 fields["instrument"], fields["confidence"], fields["version"],
                                 fields["bright2"], fields["frp"], fields["daynight"]])
                rows.append((minute, line))
                if self.cfg.synthetic_defects and rng.random() < 0.004:
                    rows.append((minute, line))            # exact repeated row
        rows.sort(key=lambda t: (t[0], t[1]))
        return [r[1] for r in rows]

    @staticmethod
    def _damage(rng: random.Random, f: Dict[str, str]) -> None:
        r = rng.random()
        if r < 0.003:
            f["latitude"] = "95.12345"           # impossible latitude
        elif r < 0.006:
            f["acq_time"] = "2560"               # impossible time
        elif r < 0.0075:
            f["satellite"] = "Q9"                # unknown satellite code
        r2 = rng.random()
        if r2 < 0.006:
            f["frp"] = ""                        # missing FRP
        elif r2 < 0.008:
            f["frp"] = "-3.0"                    # non-physical FRP
        if rng.random() < 0.004:
            f["confidence"] = ""                 # missing confidence
        if rng.random() < 0.003:
            f["daynight"] = "X"
