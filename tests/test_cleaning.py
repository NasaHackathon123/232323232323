import unittest
from datetime import date, datetime, timezone

from helpers import MODIS_HDR, NOW, VIIRS_HDR, modis_row, viirs_row  # noqa: F401  (sets sys.path)

from emberwatch.cleaning import RequestContext, clean_row
from emberwatch.config import REGIONS
from emberwatch.sources import PRODUCTS


def ctx(source_id="MODIS_NRT", start=date(2026, 11, 13), days=1):
    return RequestContext(source_id, PRODUCTS[source_id], REGIONS["sea"].bbox, start, days, NOW, "synthetic_demo",
                          1, "run")


def row(header, line):
    keys = header.split(",")
    vals = line.split(",")
    return dict(zip(keys, vals))


def clean_modis(**kw):
    return clean_row(row(MODIS_HDR, modis_row(**kw)), ctx("MODIS_NRT"))


def clean_viirs(src="VIIRS_NOAA20_NRT", **kw):
    return clean_row(row(VIIRS_HDR, viirs_row(**kw)), ctx(src))


class TestAcceptedRows(unittest.TestCase):
    def test_modis_row_preserves_original_measurements(self):
        r = clean_modis()
        self.assertFalse(r.quarantined)
        rec = r.record
        self.assertEqual(rec["frp_mw"], 52.4)
        self.assertEqual(rec["frp_raw"], "52.4")
        self.assertEqual(rec["confidence_raw"], "85")
        self.assertEqual(rec["confidence_scheme"], "modis_percent")
        self.assertEqual(rec["confidence_value"], 85.0)
        self.assertEqual(rec["confidence_class_derived"], "high")
        self.assertEqual(rec["satellite"], "Terra")
        self.assertEqual(rec["satellite_code_raw"], "Terra")
        self.assertEqual(rec["acq_time_raw"], "0642")
        self.assertEqual(rec["acquired_at_utc"], "2026-11-13T06:42:00Z")
        self.assertEqual(rec["brightness_k"], 350.1)
        self.assertEqual(rec["brightness_band"], "T21")
        self.assertEqual(rec["brightness_2_band"], "T31")
        self.assertEqual(rec["nominal_resolution_m"], 1000)
        self.assertEqual(rec["record_class"], "observed_hotspot")
        self.assertEqual(rec["qc_status"], "accepted")
        self.assertIn('"frp":"52.4"', rec["raw_json"])     # the whole original row is kept

    def test_viirs_confidence_is_categorical_and_not_converted(self):
        r = clean_viirs(conf="h")
        rec = r.record
        self.assertEqual(rec["confidence_scheme"], "viirs_category")
        self.assertIsNone(rec["confidence_value"])        # no invented percentage
        self.assertEqual(rec["confidence_class_derived"], "high")
        self.assertEqual(rec["confidence_raw"], "h")
        self.assertEqual(rec["satellite"], "NOAA-20")
        self.assertEqual(rec["brightness_band"], "I4")
        self.assertEqual(rec["nominal_resolution_m"], 375)

    def test_modis_confidence_class_boundaries(self):
        for value, cls in (("0", "low"), ("29", "low"), ("30", "nominal"), ("79", "nominal"),
                           ("80", "high"), ("100", "high")):
            self.assertEqual(clean_modis(conf=value).record["confidence_class_derived"], cls, value)

    def test_modis_and_viirs_confidence_never_cross_converted(self):
        r = clean_modis(conf="h")                         # VIIRS-style value on a MODIS row
        self.assertFalse(r.quarantined)
        self.assertIn("CONFIDENCE_SCHEME_MISMATCH", r.flags)
        self.assertIsNone(r.record["confidence_class_derived"])
        self.assertEqual(r.record["confidence_raw"], "h")
        v = clean_viirs(conf="85")                        # percentage on a VIIRS row
        self.assertIn("CONFIDENCE_SCHEME_MISMATCH", v.flags)
        self.assertIsNone(v.record["confidence_class_derived"])

    def test_short_time_is_zero_padded_and_recorded(self):
        r = clean_modis(time_="642")
        self.assertEqual(r.record["acquired_at_utc"], "2026-11-13T06:42:00Z")
        self.assertIn("T05a", r.transforms)
        self.assertEqual(r.record["acq_time_raw"], "642")


class TestMissingValuesAreNotInvented(unittest.TestCase):
    def test_missing_frp_stays_null_and_is_flagged(self):
        r = clean_modis(frp="")
        self.assertFalse(r.quarantined)
        self.assertIsNone(r.record["frp_mw"])             # NOT zero
        self.assertEqual(r.record["frp_raw"], "")
        self.assertIn("FRP_MISSING", r.flags)
        self.assertEqual(r.record["qc_status"], "accepted_with_flags")
        self.assertIn("T03", r.transforms)

    def test_negative_frp_numeric_nulled_but_raw_kept(self):
        r = clean_modis(frp="-3.0")
        self.assertIsNone(r.record["frp_mw"])
        self.assertEqual(r.record["frp_raw"], "-3.0")
        self.assertIn("FRP_NEGATIVE", r.flags)

    def test_nan_and_text_frp_invalid(self):
        for bad in ("nan", "abc", "inf"):
            r = clean_modis(frp=bad)
            if bad == "nan":
                self.assertIn("FRP_MISSING", r.flags)      # NaN is a null token
            else:
                self.assertIn("FRP_INVALID", r.flags, bad)
            self.assertIsNone(r.record["frp_mw"])

    def test_extreme_frp_kept_unchanged_but_flagged(self):
        r = clean_modis(frp="25000.5")
        self.assertEqual(r.record["frp_mw"], 25000.5)
        self.assertIn("FRP_EXTREME", r.flags)

    def test_missing_optional_fields_flagged_not_rejected(self):
        r = clean_modis(conf="", dn="", ver="", scan="", track="", bright="")
        self.assertFalse(r.quarantined)
        for flag in ("CONFIDENCE_MISSING", "DAYNIGHT_MISSING", "VERSION_MISSING", "PIXEL_SIZE_MISSING",
                     "BRIGHTNESS_MISSING"):
            self.assertIn(flag, r.flags)
        self.assertIsNone(r.record["scan_km"])
        self.assertIsNone(r.record["brightness_k"])

    def test_flags_are_listed_once(self):
        r = clean_modis(bright="", bright2="")
        self.assertEqual(r.flags.count("BRIGHTNESS_MISSING"), 1)


class TestRejections(unittest.TestCase):
    def assertRejected(self, result, code):
        self.assertTrue(result.quarantined)
        self.assertIn(code, result.reasons)

    def test_bad_coordinates(self):
        self.assertRejected(clean_modis(lat="95.1"), "LAT_OUT_OF_RANGE")
        self.assertRejected(clean_modis(lon="181"), "LON_OUT_OF_RANGE")
        self.assertRejected(clean_modis(lat="abc"), "LAT_NOT_NUMERIC")
        self.assertRejected(clean_modis(lon="nan"), "MISSING_REQUIRED_VALUE")
        self.assertRejected(clean_modis(lat="0", lon="0"), "COORD_ZERO_ZERO")

    def test_bad_dates_and_times(self):
        self.assertRejected(clean_modis(date_="2026-13-01"), "DATE_INVALID")
        self.assertRejected(clean_modis(date_="2026-1-5"), "DATE_INVALID")
        self.assertRejected(clean_modis(date_="13/11/2026"), "DATE_INVALID")
        self.assertRejected(clean_modis(time_="2560"), "TIME_INVALID")
        self.assertRejected(clean_modis(time_="06:42"), "TIME_INVALID")
        self.assertRejected(clean_modis(time_="12345"), "TIME_INVALID")

    def test_future_timestamp_rejected(self):
        self.assertRejected(clean_modis(date_="2026-11-14", time_="2000"), "TIMESTAMP_FUTURE")
        self.assertFalse(clean_modis(date_="2026-11-14", time_="1130").quarantined)

    def test_timestamp_before_launch_rejected(self):
        self.assertRejected(clean_modis(date_="1999-12-01", sat="Terra"), "TIMESTAMP_BEFORE_LAUNCH")
        self.assertRejected(clean_modis(date_="2002-01-01", sat="Aqua"), "TIMESTAMP_BEFORE_LAUNCH")

    def test_satellite_and_instrument_identifiers(self):
        self.assertRejected(clean_modis(sat="Q9"), "SATELLITE_UNKNOWN")
        self.assertRejected(clean_modis(sat=""), "MISSING_REQUIRED_VALUE")
        self.assertRejected(clean_viirs(sat="N"), "SATELLITE_PRODUCT_MISMATCH")      # S-NPP in a NOAA-20 file
        self.assertRejected(clean_viirs(sat="N21"), "SATELLITE_PRODUCT_MISMATCH")
        # a MODIS row inside a VIIRS product
        bad = clean_row(row(MODIS_HDR, modis_row()), ctx("VIIRS_NOAA20_NRT"))
        self.assertRejected(bad, "INSTRUMENT_MISMATCH")

    def test_malformed_row(self):
        raw = row(MODIS_HDR, modis_row())
        raw[None] = ["extra"]
        self.assertRejected(clean_row(raw, ctx("MODIS_NRT")), "MALFORMED_ROW")
        short = dict(raw)
        del short[None]
        short["frp"] = None
        self.assertRejected(clean_row(short, ctx("MODIS_NRT")), "MALFORMED_ROW")

    def test_missing_header_column_rejects_with_reason(self):
        res = clean_row(row(MODIS_HDR, modis_row()), ctx("MODIS_NRT"), missing_columns=["acq_time"])
        self.assertRejected(res, "MISSING_REQUIRED_COLUMN")


class TestIdentity(unittest.TestCase):
    def test_different_satellites_never_share_an_identity(self):
        """Same place and minute, different satellites: must stay separate observations."""
        a = clean_viirs(sat="N20", src="VIIRS_NOAA20_NRT").record
        b = clean_row(row(VIIRS_HDR, viirs_row(sat="N21")), ctx("VIIRS_NOAA21_NRT")).record
        self.assertNotEqual(a["observation_id"], b["observation_id"])

    def test_same_satellite_same_pixel_has_same_identity_across_processing_levels(self):
        nrt = clean_viirs(src="VIIRS_NOAA20_NRT").record
        sp = clean_row(row(VIIRS_HDR, viirs_row(ver="2.0")), ctx("VIIRS_NOAA20_SP")).record
        self.assertEqual(nrt["observation_id"], sp["observation_id"])
        self.assertNotEqual(nrt["source_product"], sp["source_product"])

    def test_nearby_but_not_identical_coordinates_are_different_observations(self):
        a = clean_viirs(lat="1.48000").record
        b = clean_viirs(lat="1.48001").record
        self.assertNotEqual(a["observation_id"], b["observation_id"])      # no fuzzy matching


if __name__ == "__main__":
    unittest.main()
