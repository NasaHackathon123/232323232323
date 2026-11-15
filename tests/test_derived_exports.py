import csv
import json
import time
import unittest
from datetime import date, datetime, timedelta, timezone

from helpers import (MODIS_HDR, NOW, TempDirMixin, VIIRS_HDR, add_raw, csv_text, make_cfg, modis_row, open_conn,
                     viirs_row)

from emberwatch.derived import (build_all, build_grid_and_daily, build_links, cell_index, haversine_km)
from emberwatch.exports import CSV_HEADER, _baseline_departure, _csv_safe, export_all
from emberwatch.logutil import setup_logging
from emberwatch.pipeline import Harmonizer
from emberwatch.quality import build_report
from emberwatch.runner import LockBusy, run_lock, run_pipeline
from emberwatch.synthetic import SyntheticSource
from emberwatch.sources import PRODUCTS


def process(cfg, conn):
    return Harmonizer(cfg, conn, lambda: NOW + timedelta(minutes=5)).run()


class TestGridMath(unittest.TestCase):
    def test_cell_index_is_stable_at_edges(self):
        self.assertEqual(cell_index(1.4, -90.0, 0.1), 914)
        self.assertEqual(cell_index(1.5, -90.0, 0.1), 915)
        self.assertEqual(cell_index(-0.1, -90.0, 0.1), 899)
        self.assertEqual(cell_index(103.7, -180.0, 0.1), 2837)
        self.assertEqual(cell_index(103.75, -180.0, 0.25), 1135)

    def test_haversine(self):
        self.assertAlmostEqual(haversine_km(0, 0, 0, 1), 111.19, places=1)
        self.assertEqual(haversine_km(1, 1, 1, 1), 0.0)


class TestDerived(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp, EMBERWATCH_GRID_RESOLUTIONS="0.1")
        self.conn = open_conn(self.cfg)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def load(self, product, *rows, header=None, **kw):
        header = header or (MODIS_HDR if product.startswith("MODIS") else VIIRS_HDR)
        add_raw(self.cfg, self.conn, product, csv_text(header, *rows), **kw)

    def test_grid_and_daily_counts(self):
        self.load("VIIRS_NOAA20_NRT",
                  viirs_row(lat="1.41", lon="103.71", frp="10"), viirs_row(lat="1.42", lon="103.72", frp="20"),
                  viirs_row(lat="1.56", lon="103.76", frp=""), viirs_row(lat="2.01", lon="103.01"))
        self.load("MODIS_NRT", modis_row(lat="1.405", lon="103.705", frp="30"))
        process(self.cfg, self.conn)
        build_grid_and_daily(self.conn, self.cfg)
        g = {r["cell_id"]: dict(r) for r in self.conn.execute("SELECT * FROM grid_cell_day")}
        self.assertEqual(len(g), 3)                           # one cell holds 3 detections, two hold one each
        big = max(g.values(), key=lambda r: r["n_total"])
        self.assertEqual((big["n_viirs"], big["n_modis"], big["n_total"]), (2, 1, 3))
        self.assertAlmostEqual(big["mean_frp_mw"], 20.0)      # (10+20+30)/3
        self.assertEqual(big["n_frp_valid"], 3)
        self.assertEqual(big["record_class"], "derived")
        d = self.conn.execute("SELECT * FROM daily_activity_index WHERE region_id='sea' AND date_utc='2026-11-13'").fetchone()
        self.assertEqual((d["n_modis"], d["n_viirs"]), (1, 4))
        self.assertEqual(d["occupied_cells"], 3)
        self.assertEqual(d["satellites_contributing_7d"], 2)
        self.assertAlmostEqual(d["hai"], 1.5)
        self.assertIn("v0-proposed", d["hai_method"])
        # missing FRP is excluded from the mean, never counted as zero
        self.assertAlmostEqual(d["mean_frp_mw"], (10 + 20 + 30 + 12.3) / 4)

    def test_region_membership_by_bounding_box(self):
        self.load("VIIRS_NOAA20_NRT", viirs_row(lat="1.5", lon="103.7"), viirs_row(lat="-2.0", lon="114.0"))
        process(self.cfg, self.conn)
        build_grid_and_daily(self.conn, self.cfg)
        occ = {r["region_id"]: r["occupied_cells"] for r in self.conn.execute(
            "SELECT region_id, occupied_cells FROM daily_activity_index WHERE date_utc='2026-11-13'")}
        # the Johor and Sumatra boxes are rectangles and overlap around (1.5N, 103.7E): by design
        self.assertEqual(occ, {"sea": 2, "johor": 1, "sumatra": 1, "borneo": 1})

    def test_hai_is_zero_not_null_on_covered_day_without_detections(self):
        self.load("VIIRS_NOAA20_NRT", viirs_row())
        process(self.cfg, self.conn)
        self.conn.execute("INSERT INTO fetch_coverage VALUES('VIIRS_NOAA20_NRT','sea','2026-11-12','2026-11-14T12:00:00Z',1,1,0)")
        self.conn.commit()
        build_grid_and_daily(self.conn, self.cfg)
        r = self.conn.execute("SELECT * FROM daily_activity_index WHERE region_id='sea' AND date_utc='2026-11-12'").fetchone()
        self.assertEqual((r["occupied_cells"], r["hai"], r["data_covered"]), (0, 0.0, 1))

    def test_derived_tables_do_not_touch_observations(self):
        self.load("VIIRS_NOAA20_NRT", viirs_row(), viirs_row(lat="3.0"))
        process(self.cfg, self.conn)
        before = [tuple(r) for r in self.conn.execute("SELECT * FROM observations ORDER BY observation_id")]
        build_all(self.conn, self.cfg)
        after = [tuple(r) for r in self.conn.execute("SELECT * FROM observations ORDER BY observation_id")]
        self.assertEqual(before, after)

    def test_cross_sensor_links_connect_different_satellites_only_and_merge_nothing(self):
        self.load("VIIRS_NOAA20_NRT",
                  viirs_row(lat="1.50000", lon="103.50000", sat="N20", time_="0600"),
                  viirs_row(lat="1.50001", lon="103.50001", sat="N20", time_="0600"),    # same satellite nearby
                  viirs_row(lat="5.0", lon="100.0", sat="N20"))
        self.load("VIIRS_NOAA21_NRT",
                  viirs_row(lat="1.50020", lon="103.50020", sat="N21", time_="0640"),    # ~30 m away, 40 min later
                  viirs_row(lat="1.60000", lon="103.50000", sat="N21", time_="0640"))    # ~11 km away: no link
        self.load("MODIS_NRT", modis_row(lat="1.5035", lon="103.5035", sat="Terra", time_="0630"))
        process(self.cfg, self.conn)
        n_obs = self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        build_links(self.conn, self.cfg)
        links = self.conn.execute("SELECT * FROM cross_sensor_links").fetchall()
        self.assertTrue(links)
        for l in links:
            self.assertNotEqual(l["satellite_a"], l["satellite_b"])
            self.assertEqual(l["record_class"], "derived_proximity_candidate")
            self.assertLessEqual(l["dt_minutes"], self.cfg.link_max_dt_minutes)
        pairs = {frozenset((l["satellite_a"], l["satellite_b"])) for l in links}
        self.assertIn(frozenset(("NOAA-20", "NOAA-21")), pairs)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], n_obs)

    def test_links_respect_time_window(self):
        self.load("VIIRS_NOAA20_NRT", viirs_row(lat="1.5", lon="103.5", sat="N20", time_="0100"))
        self.load("VIIRS_NOAA21_NRT", viirs_row(lat="1.5", lon="103.5", sat="N21", time_="0900"))
        process(self.cfg, self.conn)
        build_links(self.conn, self.cfg)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cross_sensor_links").fetchone()[0], 0)

    def test_superseded_nrt_rows_are_excluded_from_derived_counts(self):
        self.load("VIIRS_NOAA20_NRT", viirs_row())
        self.load("VIIRS_NOAA20_SP", viirs_row(ver="2.0"), fetched_at=NOW + timedelta(days=60))
        process(self.cfg, self.conn)
        build_grid_and_daily(self.conn, self.cfg)
        d = self.conn.execute("SELECT n_viirs FROM daily_activity_index WHERE region_id='sea' AND date_utc='2026-11-13'").fetchone()
        self.assertEqual(d["n_viirs"], 1)             # counted once, not twice

    def test_baseline_needs_a_prior_year(self):
        occ = {"2026-10-10": 10, "2025-10-10": 20, "2025-10-12": 30}
        cov = {k: 1 for k in occ}
        b = _baseline_departure(occ, cov)
        self.assertIsNone(b["2025-10-10"]["departure_pct"])
        self.assertEqual(b["2025-10-10"]["baseline_years"], 0)
        self.assertEqual(b["2026-10-10"]["baseline_years"], 1)
        self.assertEqual(b["2026-10-10"]["departure_pct"], -60.0)     # mean of 20 and 30 = 25 -> (10-25)/25


class TestExportsAndQuality(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp)
        self.conn = open_conn(self.cfg)
        add_raw(self.cfg, self.conn, "MODIS_NRT", csv_text(
            MODIS_HDR, modis_row(), modis_row(lat="95"), modis_row(lat="1.7", frp=""), modis_row()))
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT", csv_text(VIIRS_HDR, viirs_row(conf="n")))
        process(self.cfg, self.conn)
        build_all(self.conn, self.cfg)
        self.paths = export_all(self.conn, self.cfg, lambda: NOW)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def load(self, name):
        return json.loads((self.cfg.export_dir / name).read_text())

    def test_all_exports_carry_the_synthetic_banner(self):
        for p in self.paths:
            if p.suffix == ".json":
                meta = json.loads(p.read_text())["meta"]
                self.assertTrue(meta["is_synthetic"], p.name)
                self.assertIn("SYNTHETIC", meta["banner"], p.name)
                self.assertEqual(meta["data_mode"], "synthetic_demo")

    def test_audit_numbers_reconcile_and_show_zero_silent_discards(self):
        a = self.load("audit.json")
        self.assertEqual(a["raw_records_loaded"], 5)
        self.assertEqual(a["cleaned_records_retained"], 3)
        self.assertEqual(a["quarantined_records"], 1)
        self.assertEqual(a["invalid_coordinate_records"], 1)
        self.assertEqual(a["exact_duplicates_not_inserted"], 1)
        self.assertEqual(a["silent_discards"], 0)
        self.assertEqual(a["observations_merged"], 0)
        self.assertEqual(a["records_revised_by_later_fetch"], 0)
        self.assertTrue(a["reconciled"])

    def test_hotspot_shape_matches_what_the_frontend_displays(self):
        h = self.load("hotspots_sea.json")["hotspots"]
        self.assertEqual(len(h), 3)
        for p in h:
            for key in ("lat", "lng", "sensor", "date", "confidence", "frp", "brightness"):
                self.assertIn(key, p)
        viirs = [p for p in h if p["sensor"] == "VIIRS"][0]
        self.assertEqual(viirs["confidence"], "Nominal")
        self.assertEqual(viirs["confidence_raw"], "n")              # original preserved
        self.assertEqual(viirs["date"], "2026-11-13 06:42")
        modis = [p for p in h if p["sensor"] == "MODIS" and p["frp"] is not None][0]
        self.assertEqual(modis["confidence"], "85%")
        missing = [p for p in h if p["frp"] is None]
        self.assertEqual(len(missing), 1)                           # FRP stays null, not 0

    def test_csv_has_the_frontends_eight_columns_first(self):
        with open(self.cfg.export_dir / "harmonised_fires.csv", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0][:8], ["latitude", "longitude", "acq_date", "acq_time", "satellite", "instrument",
                                       "confidence", "frp"])
        self.assertEqual(rows[0], CSV_HEADER)
        self.assertEqual(len(rows) - 1, 3)
        first = rows[1]
        self.assertEqual(first[3], "0642")

    def test_calendar_and_overview_shapes(self):
        cal = self.load("calendar_sea_0.10.json")["days"]["2026-11-13"]
        for key in ("hai", "grid", "viirs", "modis"):
            self.assertIn(key, cal)
        # the MODIS and VIIRS detections at (1.48, 103.76) fall in the same 0.1 degree cell
        self.assertEqual((cal["modis"], cal["viirs"], cal["grid"]), (2, 1, 2))
        ov = self.load("overview_sea.json")
        self.assertEqual((ov["modis_detections"], ov["viirs_detections"]), (2, 1))

    def test_exports_are_valid_json_without_nan(self):
        for p in self.paths:
            if p.suffix == ".json":
                json.loads(p.read_text(), parse_constant=lambda c: self.fail(f"{c} in {p.name}"))

    def test_quality_report(self):
        rep = build_report(self.conn, "synthetic_demo", NOW)
        self.assertTrue(rep["accounting"]["reconciled"])
        self.assertEqual(rep["accounting"]["silent_discards"], 0)
        self.assertEqual(rep["quarantine_reasons"][0]["code"], "LAT_OUT_OF_RANGE")
        self.assertEqual({f["code"] for f in rep["qc_flags"]}, {"FRP_MISSING"})
        self.assertEqual(rep["completeness_pct_missing"]["frp"], round(100 / 3, 2))
        self.assertEqual({s["satellite"] for s in rep["satellites"]}, {"Terra", "NOAA-20"})
        self.assertFalse(any(s["stale"] for s in rep["satellites"]))      # staleness is a live-mode check

    def test_csv_formula_guard(self):
        self.assertEqual(_csv_safe("=1+1"), "'=1+1")
        self.assertEqual(_csv_safe("@x"), "'@x")
        self.assertEqual(_csv_safe("-3.0"), "-3.0")
        self.assertEqual(_csv_safe("-cmd"), "'-cmd")
        self.assertEqual(_csv_safe(None), "")


class TestSynthetic(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp)
        self.src = SyntheticSource(self.cfg, now=NOW)
        self.bbox = (95.0, -8.0, 121.4, 11.0)

    def test_deterministic_and_in_firms_layout(self):
        p = PRODUCTS["VIIRS_NOAA20_NRT"]
        a = self.src.generate_csv(p, self.bbox, date(2026, 11, 1), 3)
        b = self.src.generate_csv(p, self.bbox, date(2026, 11, 1), 3)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"))
        self.assertGreater(len(a.splitlines()), 10)

    def test_no_data_from_the_future_or_from_retired_snpp(self):
        p = PRODUCTS["VIIRS_SNPP_NRT"]
        text = self.src.generate_csv(p, self.bbox, date(2026, 10, 30), 10)
        dates = {l.split(",")[5] for l in text.splitlines()[1:]}
        self.assertTrue(dates)
        self.assertLessEqual(max(dates), "2026-11-01")
        n = PRODUCTS["VIIRS_NOAA20_NRT"]
        text = self.src.generate_csv(n, self.bbox, date(2026, 11, 14), 1)
        for l in text.splitlines()[1:]:
            f = l.split(",")
            self.assertLessEqual(f[5] + f[6], "202611141155")           # not later than 'now' (12:00)

    def test_defects_can_be_switched_off(self):
        clean_cfg = make_cfg(self.tmp, EMBERWATCH_SYNTHETIC_DEFECTS="false")
        src = SyntheticSource(clean_cfg, now=NOW)
        text = src.generate_csv(PRODUCTS["MODIS_NRT"], self.bbox, date(2025, 8, 1), 30)
        conn = open_conn(clean_cfg)
        add_raw(clean_cfg, conn, "MODIS_NRT", text, start=date(2025, 8, 1), days=30)
        s = Harmonizer(clean_cfg, conn, lambda: NOW).run()
        conn.close()
        self.assertGreater(s.rows_read, 50)
        self.assertEqual(s.rows_quarantined, 0)
        self.assertEqual(s.rows_duplicate, 0)

    def test_raw_files_are_labelled_synthetic_and_stored_under_demo(self):
        conn = open_conn(self.cfg)
        add_raw(self.cfg, conn, "MODIS_NRT", csv_text(MODIS_HDR, modis_row()))
        r = conn.execute("SELECT path, data_mode FROM raw_files").fetchone()
        conn.close()
        self.assertIn("SYNTHETIC_", r["path"])
        self.assertEqual(r["data_mode"], "synthetic_demo")
        self.assertTrue(str(self.cfg.raw_dir).endswith("demo/raw"))


class TestRunner(TempDirMixin, unittest.TestCase):
    def test_demo_pipeline_end_to_end_and_second_run_is_incremental(self):
        cfg = make_cfg(self.tmp, EMBERWATCH_SYNTHETIC_SEED=7)
        src = SyntheticSource(cfg, now=NOW)
        out = run_pipeline(cfg, start=date(2026, 9, 1), end=date(2026, 11, 14), source=src, now_fn=lambda: NOW)
        self.assertEqual(out["ingest"]["status"], "ok")
        self.assertTrue(out["harmonize"]["reconciled"])
        self.assertGreater(out["harmonize"]["rows_accepted"], 100)
        self.assertGreater(out["harmonize"]["rows_quarantined"], 0)          # demo defects are caught
        self.assertTrue((cfg.export_dir / "quality_report.json").exists())
        # a second run a little later fetches only what is not final / fresh
        later = NOW + timedelta(minutes=30)
        out2 = run_pipeline(cfg, start=date(2026, 9, 1), end=date(2026, 11, 14),
                            source=SyntheticSource(cfg, now=later), now_fn=lambda: later)
        self.assertEqual(out2["ingest"]["windows_planned"], 0)
        self.assertEqual(out2["harmonize"]["rows_read"], 0)
        # much later: only the recent, non-final window is refreshed, and it adds no duplicates to the totals
        later2 = NOW + timedelta(hours=6)
        out3 = run_pipeline(cfg, start=date(2026, 9, 1), end=date(2026, 11, 14),
                            source=SyntheticSource(cfg, now=later2), now_fn=lambda: later2)
        self.assertGreater(out3["ingest"]["windows_planned"], 0)
        self.assertLess(out3["ingest"]["windows_planned"], 20)
        self.assertTrue(out3["harmonize"]["reconciled"])

    def test_switching_modes_uses_separate_folders(self):
        demo = make_cfg(self.tmp, "demo")
        live = make_cfg(self.tmp, "live")
        self.assertNotEqual(demo.db_path, live.db_path)
        self.assertNotEqual(demo.raw_dir, live.raw_dir)

    def test_lock_prevents_overlapping_runs_and_is_released(self):
        cfg = make_cfg(self.tmp)
        with run_lock(cfg):
            with self.assertRaises(LockBusy):
                with run_lock(cfg):
                    pass
        with run_lock(cfg):          # released after the first block
            pass

    def test_stale_lock_is_replaced(self):
        import os
        cfg = make_cfg(self.tmp)
        cfg.ensure_dirs()
        cfg.lock_path.write_text("old")
        old = time.time() - 5 * 3600
        os.utime(cfg.lock_path, (old, old))
        with run_lock(cfg):
            pass


if __name__ == "__main__":
    unittest.main()
