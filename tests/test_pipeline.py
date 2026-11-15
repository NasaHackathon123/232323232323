import unittest
from datetime import date, datetime, timedelta, timezone

from helpers import (MODIS_HDR, NOW, VIIRS_HDR, TempDirMixin, add_raw, csv_text, make_cfg, modis_row, open_conn,
                     viirs_row)

from emberwatch.pipeline import Harmonizer


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("observations", "quarantine", "duplicate_log")}


class TestPipeline(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp)
        self.conn = open_conn(self.cfg)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def run_h(self, **kw):
        return Harmonizer(self.cfg, self.conn, lambda: NOW + timedelta(minutes=5)).run(**kw)

    def test_every_row_has_exactly_one_outcome(self):
        text = csv_text(
            MODIS_HDR,
            modis_row(),                                # accepted
            modis_row(lat="1.5", frp=""),               # accepted with flag
            modis_row(lat="95"),                        # quarantined
            modis_row(),                                # exact duplicate of the first
            modis_row(sat="Q9"),                        # quarantined
        )
        add_raw(self.cfg, self.conn, "MODIS_NRT", text)
        s = self.run_h()
        self.assertEqual(s.rows_read, 5)
        self.assertEqual((s.rows_accepted, s.rows_quarantined, s.rows_duplicate), (2, 2, 1))
        self.assertEqual(s.rows_accepted_with_flags, 1)
        self.assertTrue(s.reconciled)
        c = counts(self.conn)
        self.assertEqual(c, {"observations": 2, "quarantine": 2, "duplicate_log": 1})
        raw_rows = self.conn.execute("SELECT data_rows FROM raw_files").fetchone()[0]
        self.assertEqual(raw_rows, 5)

    def test_quarantine_keeps_original_row_and_reasons(self):
        add_raw(self.cfg, self.conn, "MODIS_NRT", csv_text(MODIS_HDR, modis_row(lat="95.5")))
        self.run_h()
        q = self.conn.execute("SELECT * FROM quarantine").fetchone()
        self.assertEqual(q["reasons"], "LAT_OUT_OF_RANGE")
        self.assertIn("95.5", q["raw_json"])
        self.assertEqual(q["data_mode"], "synthetic_demo")

    def test_refetching_the_same_window_adds_nothing(self):
        text = csv_text(VIIRS_HDR, viirs_row(), viirs_row(lat="1.6"))
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT", text)
        self.run_h()
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT", text, fetched_at=NOW + timedelta(hours=3))
        s = self.run_h()
        self.assertEqual(counts(self.conn)["observations"], 2)
        self.assertEqual(s.rows_duplicate, 2)
        self.assertEqual(s.duplicate_reasons, {"EXACT_DUPLICATE_REFETCH": 2})
        self.assertTrue(s.reconciled)

    def test_later_fetch_with_changed_values_becomes_current_and_keeps_the_earlier_version(self):
        """FIRMS NRT sources first carry real-time records that are later replaced."""
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT", csv_text(VIIRS_HDR, viirs_row(frp="12.3", ver="2.0URT")))
        self.run_h()
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT", csv_text(VIIRS_HDR, viirs_row(frp="99.9", ver="2.0NRT")),
                fetched_at=NOW + timedelta(hours=3))
        s = self.run_h()
        self.assertEqual(s.rows_revised, 1)
        self.assertEqual((s.rows_accepted, s.rows_duplicate, s.rows_quarantined), (1, 0, 0))
        self.assertTrue(s.reconciled)
        rows = self.conn.execute("SELECT frp_mw, product_version, is_current, superseded_by, qc_flags, qc_status "
                                 "FROM observations ORDER BY raw_file_id").fetchall()
        self.assertEqual(len(rows), 2)                          # nothing deleted
        old, new = rows
        self.assertEqual((old["frp_mw"], old["is_current"], old["superseded_by"]), (12.3, 0, "REVISION"))
        self.assertEqual((new["frp_mw"], new["is_current"], new["superseded_by"]), (99.9, 1, None))
        self.assertIn("REVISION_OF_EARLIER_FETCH", new["qc_flags"])
        self.assertEqual(new["qc_status"], "accepted_with_flags")
        # re-fetching the identical later version again is now a plain duplicate
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT", csv_text(VIIRS_HDR, viirs_row(frp="99.9", ver="2.0NRT")),
                fetched_at=NOW + timedelta(hours=6))
        s2 = self.run_h()
        self.assertEqual((s2.rows_duplicate, s2.rows_revised), (1, 0))
        self.assertEqual(counts(self.conn)["observations"], 2)

    def test_revision_does_not_undo_sp_precedence_and_vice_versa(self):
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT", csv_text(VIIRS_HDR, viirs_row(frp="1.0")))
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT", csv_text(VIIRS_HDR, viirs_row(frp="2.0")),
                fetched_at=NOW + timedelta(hours=3))
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_SP", csv_text(VIIRS_HDR, viirs_row(ver="2.0", frp="3.0")),
                fetched_at=NOW + timedelta(days=60))
        self.run_h()
        got = {(r["source_product"], r["frp_mw"]): (r["is_current"], r["superseded_by"])
               for r in self.conn.execute("SELECT * FROM observations")}
        self.assertEqual(got[("VIIRS_NOAA20_NRT", 1.0)], (0, "REVISION"))
        self.assertEqual(got[("VIIRS_NOAA20_NRT", 2.0)], (0, "SP"))
        self.assertEqual(got[("VIIRS_NOAA20_SP", 3.0)], (1, None))
        self.run_h(rebuild=True)                                # same outcome when rebuilt from raw
        got2 = {(r["source_product"], r["frp_mw"]): (r["is_current"], r["superseded_by"])
                for r in self.conn.execute("SELECT * FROM observations")}
        self.assertEqual(got, got2)

    def test_conflicting_rows_inside_one_file_keep_the_first(self):
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT",
                csv_text(VIIRS_HDR, viirs_row(frp="12.3"), viirs_row(frp="99.9")))
        s = self.run_h()
        self.assertEqual(s.duplicate_reasons, {"CONFLICT_SAME_KEY_IN_FILE": 1})
        self.assertEqual(self.conn.execute("SELECT frp_mw FROM observations").fetchone()[0], 12.3)

    def test_different_satellites_at_same_place_and_time_are_both_kept(self):
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT", csv_text(VIIRS_HDR, viirs_row(sat="N20")))
        add_raw(self.cfg, self.conn, "VIIRS_NOAA21_NRT", csv_text(VIIRS_HDR, viirs_row(sat="N21")))
        s = self.run_h()
        self.assertEqual(s.rows_accepted, 2)
        self.assertEqual(s.rows_duplicate, 0)
        sats = {r[0] for r in self.conn.execute("SELECT satellite FROM observations")}
        self.assertEqual(sats, {"NOAA-20", "NOAA-21"})

    def test_modis_terra_and_aqua_are_separate(self):
        add_raw(self.cfg, self.conn, "MODIS_NRT",
                csv_text(MODIS_HDR, modis_row(sat="Terra"), modis_row(sat="Aqua")))
        s = self.run_h()
        self.assertEqual(s.rows_accepted, 2)

    def test_sp_supersedes_nrt_of_the_same_satellite_and_date_only(self):
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT",
                csv_text(VIIRS_HDR, viirs_row(), viirs_row(lat="2.0", date_="2026-11-12")))
        add_raw(self.cfg, self.conn, "VIIRS_NOAA20_SP", csv_text(VIIRS_HDR, viirs_row(ver="2.0")),
                fetched_at=NOW + timedelta(days=60))
        # NOAA-21 NRT on the same date must NOT be touched by NOAA-20 SP
        add_raw(self.cfg, self.conn, "VIIRS_NOAA21_NRT", csv_text(VIIRS_HDR, viirs_row(sat="N21")))
        s = self.run_h()
        self.assertEqual(s.rows_superseded, 1)
        cur = {(r["satellite"], r["source_product"], r["acq_date_utc"]): (r["is_current"], r["superseded_by"])
               for r in self.conn.execute("SELECT * FROM observations")}
        self.assertEqual(cur[("NOAA-20", "VIIRS_NOAA20_NRT", "2026-11-13")], (0, "SP"))
        self.assertEqual(cur[("NOAA-20", "VIIRS_NOAA20_SP", "2026-11-13")], (1, None))
        self.assertEqual(cur[("NOAA-20", "VIIRS_NOAA20_NRT", "2026-11-12")], (1, None))
        self.assertEqual(cur[("NOAA-21", "VIIRS_NOAA21_NRT", "2026-11-13")], (1, None))
        # nothing was deleted: both versions are stored
        self.assertEqual(counts(self.conn)["observations"], 4)

    def test_header_without_required_column_quarantines_every_row(self):
        text = "latitude,longitude,acq_date,satellite,instrument,confidence,frp\n" \
               "1.4,103.7,2026-11-13,Terra,MODIS,85,10\n1.5,103.8,2026-11-13,Terra,MODIS,85,11\n"
        add_raw(self.cfg, self.conn, "MODIS_NRT", text)
        s = self.run_h()
        self.assertEqual((s.rows_read, s.rows_quarantined, s.rows_accepted), (2, 2, 0))
        self.assertTrue(s.reconciled)
        self.assertEqual(self.conn.execute("SELECT DISTINCT reasons FROM quarantine").fetchone()[0],
                         "MISSING_REQUIRED_COLUMN")

    def test_header_only_file_is_processed_with_zero_rows(self):
        add_raw(self.cfg, self.conn, "MODIS_NRT", csv_text(MODIS_HDR))
        s = self.run_h()
        self.assertEqual((s.raw_files_processed, s.rows_read), (1, 0))

    def test_non_csv_raw_file_is_kept_but_not_processed(self):
        add_raw(self.cfg, self.conn, "MODIS_NRT", "Some unexpected message", kind="error_text")
        s = self.run_h()
        self.assertEqual(s.raw_files_skipped, 1)
        st = self.conn.execute("SELECT process_status FROM raw_files").fetchone()[0]
        self.assertEqual(st, "skipped_non_csv")

    def test_tampered_raw_file_is_detected_and_not_processed(self):
        rid = add_raw(self.cfg, self.conn, "MODIS_NRT", csv_text(MODIS_HDR, modis_row()))
        path = self.cfg.data_dir / self.conn.execute(
            "SELECT path FROM raw_files WHERE raw_file_id=?", (rid,)).fetchone()[0]
        path.write_text(path.read_text() + "9.9,9.9,1,1,1,2026-11-13,0100,Terra,MODIS,50,6.1NRT,300,5,D\n")
        s = self.run_h()
        self.assertEqual(s.rows_read, 0)
        self.assertEqual(counts(self.conn)["observations"], 0)
        st = self.conn.execute("SELECT process_status FROM raw_files").fetchone()[0]
        self.assertEqual(st, "integrity_error")
        self.assertGreaterEqual(self.conn.execute(
            "SELECT COUNT(*) FROM error_log WHERE component='pipeline'").fetchone()[0], 1)

    def test_rebuild_reproduces_identical_results(self):
        add_raw(self.cfg, self.conn, "MODIS_NRT",
                csv_text(MODIS_HDR, modis_row(), modis_row(lat="95"), modis_row(), modis_row(frp="")))
        first = self.run_h()
        before = counts(self.conn)
        again = self.run_h()                        # nothing pending: no change
        self.assertEqual(again.rows_read, 0)
        self.assertEqual(counts(self.conn), before)
        rebuilt = self.run_h(rebuild=True)
        self.assertEqual(counts(self.conn), before)
        self.assertEqual((rebuilt.rows_read, rebuilt.rows_accepted), (first.rows_read, first.rows_accepted))

    def test_provenance_columns_filled(self):
        rid = add_raw(self.cfg, self.conn, "VIIRS_NOAA20_NRT", csv_text(VIIRS_HDR, viirs_row()))
        self.run_h()
        r = self.conn.execute("SELECT * FROM observations").fetchone()
        self.assertEqual(r["raw_file_id"], rid)
        self.assertEqual(r["raw_row"], 2)                      # line 2 of the file (line 1 is the header)
        self.assertEqual(r["ingested_at_utc"], "2026-11-14T12:00:00Z")
        self.assertEqual(r["data_mode"], "synthetic_demo")
        self.assertEqual(r["pipeline_version"], "1.0.0")

    def test_transformation_log_written(self):
        add_raw(self.cfg, self.conn, "MODIS_NRT", csv_text(MODIS_HDR, modis_row(), modis_row(lat="1.5", frp="")))
        self.run_h()
        ids = {r[0] for r in self.conn.execute("SELECT transform_id FROM transformation_log")}
        self.assertTrue({"T01", "T02", "T04", "T05", "T06", "T07", "T08", "T09", "T03"} <= ids)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transformation_catalog").fetchone()[0], 15)


if __name__ == "__main__":
    unittest.main()
