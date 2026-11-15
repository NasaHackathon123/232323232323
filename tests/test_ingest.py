import json
import logging
import os
import unittest
from datetime import date, datetime, timedelta, timezone

from helpers import (FAKE_KEY, MODIS_HDR, NOW, TempDirMixin, VIIRS_HDR, csv_text, make_cfg, modis_row, open_conn,
                     viirs_row)

from emberwatch.config import ConfigError, load_settings, resolve_mode, valid_map_key
from emberwatch.firms_client import (ApiError, AuthError, FirmsClient, HttpResponse, RateLimiter,
                                     RateLimitExceeded, TransportError, classify_body, estimate_cost)
from emberwatch.ingest import IngestService
from emberwatch.logutil import setup_logging
from emberwatch.planner import choose_product, load_coverage, plan_windows
from emberwatch.sources import PRODUCTS


class FakeTransport:
    """Returns scripted responses in order; records every URL requested."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok(text):
    return HttpResponse(200, text.encode(), {})


class TestConfigAndSecrets(TempDirMixin, unittest.TestCase):
    def test_missing_or_placeholder_key_means_demo_mode(self):
        for k in (None, "", "your_firms_map_key_here", "short", "has space in it 1234567890abcdef"):
            self.assertEqual(resolve_mode("auto", k), "demo", k)

    def test_valid_key_means_live_and_live_without_key_fails(self):
        self.assertEqual(resolve_mode("auto", FAKE_KEY), "live")
        with self.assertRaises(ConfigError):
            resolve_mode("live", "your_firms_map_key_here")
        self.assertTrue(valid_map_key(FAKE_KEY))

    def test_key_is_never_in_repr_or_demo_settings(self):
        cfg = make_cfg(self.tmp, "live")
        self.assertNotIn(FAKE_KEY, repr(cfg))
        self.assertNotIn(FAKE_KEY, str(cfg))
        self.assertEqual(cfg.redact(f"url/{FAKE_KEY}/x"), "url/***/x")
        demo = load_settings(env={"EMBERWATCH_DATA_DIR": str(self.tmp), "FIRMS_MAP_KEY": FAKE_KEY,
                                  "EMBERWATCH_MODE": "demo"}, dotenv_path=None)
        self.assertIsNone(demo.map_key)           # demo mode never carries a key

    def test_data_folders_are_separate_by_mode(self):
        self.assertTrue(str(make_cfg(self.tmp, "live").data_dir).endswith("live"))
        self.assertTrue(str(make_cfg(self.tmp, "demo").data_dir).endswith("demo"))

    def test_bad_settings_are_rejected(self):
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, EMBERWATCH_DAY_RANGE="99")
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, EMBERWATCH_DAY_RANGE="abc")

    def test_dotenv_loader_does_not_override_environment(self):
        from emberwatch.config import load_dotenv
        p = self.tmp / ".env"
        p.write_text("# comment\nEW_TEST_A=from_file\nEW_TEST_B='quoted'\n")
        os.environ["EW_TEST_A"] = "already"
        try:
            load_dotenv(p)
            self.assertEqual(os.environ["EW_TEST_A"], "already")
            self.assertEqual(os.environ["EW_TEST_B"], "quoted")
        finally:
            os.environ.pop("EW_TEST_A", None)
            os.environ.pop("EW_TEST_B", None)


class TestClient(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp, "live", EMBERWATCH_MAX_RETRIES=3)
        self.sleeps = []
        self.sleep = self.sleeps.append
        self.product = PRODUCTS["VIIRS_NOAA20_NRT"]
        self.bbox = (95.0, -8.0, 121.4, 11.0)

    def client(self, responses):
        t = FakeTransport(responses)
        limiter = RateLimiter(1000, 600, 0, clock=lambda: 0.0, sleep=self.sleep)
        return FirmsClient(self.cfg, t, limiter, sleep=self.sleep, rng=lambda: 0.0), t

    def test_url_uses_key_but_redacted_url_does_not(self):
        c, t = self.client([ok(csv_text(VIIRS_HDR, viirs_row()))])
        res = c.fetch(self.product, self.bbox, date(2026, 11, 10), 3)
        self.assertIn(FAKE_KEY, t.urls[0])
        self.assertNotIn(FAKE_KEY, res.url_redacted)
        self.assertTrue(t.urls[0].endswith("/VIIRS_NOAA20_NRT/95,-8,121.4,11/3/2026-11-10"))
        self.assertEqual(res.kind, "csv")

    def test_header_only_response_is_a_valid_empty_result(self):
        c, _ = self.client([ok(VIIRS_HDR + "\n")])
        res = c.fetch(self.product, self.bbox, date(2026, 11, 10), 1)
        self.assertEqual(res.kind, "csv")

    def test_server_errors_are_retried_with_growing_waits(self):
        c, t = self.client([HttpResponse(500, b"", {}), HttpResponse(503, b"", {}), ok(VIIRS_HDR + "\n")])
        res = c.fetch(self.product, self.bbox, date(2026, 11, 10), 1)
        self.assertEqual(res.attempts, 3)
        self.assertEqual(len(t.urls), 3)
        self.assertEqual(len(self.sleeps), 2)
        self.assertGreater(self.sleeps[1], self.sleeps[0])

    def test_429_honours_retry_after(self):
        c, _ = self.client([HttpResponse(429, b"", {"Retry-After": "30"}), ok(VIIRS_HDR + "\n")])
        c.fetch(self.product, self.bbox, date(2026, 11, 10), 1)
        self.assertGreaterEqual(self.sleeps[0], 30)

    def test_network_errors_are_retried_then_give_up(self):
        c, t = self.client([TransportError("ConnectTimeout")])
        with self.assertRaises(ApiError) as cm:
            c.fetch(self.product, self.bbox, date(2026, 11, 10), 1)
        self.assertEqual(len(t.urls), self.cfg.max_retries + 1)
        self.assertNotIn(FAKE_KEY, str(cm.exception))

    def test_invalid_key_is_fatal_and_not_retried(self):
        c, t = self.client([ok("Invalid MAP_KEY.")])
        with self.assertRaises(AuthError):
            c.fetch(self.product, self.bbox, date(2026, 11, 10), 1)
        self.assertEqual(len(t.urls), 1)
        c2, _ = self.client([HttpResponse(403, b"", {})])
        with self.assertRaises(AuthError):
            c2.fetch(self.product, self.bbox, date(2026, 11, 10), 1)

    def test_server_side_transaction_limit_waits_then_stops(self):
        c, t = self.client([ok("Exceeding allowed transaction limit")])
        with self.assertRaises(RateLimitExceeded):
            c.fetch(self.product, self.bbox, date(2026, 11, 10), 1)
        self.assertEqual(len(t.urls), self.cfg.max_retries + 1)
        self.assertTrue(all(s >= 1 for s in self.sleeps))

    def test_unrecognised_text_is_returned_as_error_text_not_parsed(self):
        c, _ = self.client([ok("<html>Maintenance</html>")])
        res = c.fetch(self.product, self.bbox, date(2026, 11, 10), 1)
        self.assertEqual(res.kind, "error_text")

    def test_other_http_errors_fail_the_request_without_retry(self):
        c, t = self.client([HttpResponse(400, b"bad", {})])
        with self.assertRaises(ApiError):
            c.fetch(self.product, self.bbox, date(2026, 11, 10), 1)
        self.assertEqual(len(t.urls), 1)

    def test_day_range_validated(self):
        c, _ = self.client([ok(VIIRS_HDR + "\n")])
        with self.assertRaises(ValueError):
            c.fetch(self.product, self.bbox, date(2026, 11, 10), 11)

    def test_body_classification(self):
        self.assertEqual(classify_body(b""), "empty")
        self.assertEqual(classify_body(b"\xef\xbb\xbf" + VIIRS_HDR.encode()), "csv")
        self.assertEqual(classify_body(b"Invalid MAP_KEY."), "auth_error")
        self.assertEqual(classify_body(b"Exceeding allowed transaction limit"), "rate_limited")
        self.assertEqual(classify_body(b"whatever"), "error_text")

    def test_availability_parsing_is_best_effort(self):
        text = "data_id,min_date,max_date\nMODIS_SP,2000-11-01,2026-08-01\nVIIRS_NOAA20_SP,2018-04-01,2026-08-15\n"
        c, _ = self.client([ok(text)])
        av = c.availability()
        self.assertEqual(av["MODIS_SP"], (date(2000, 11, 1), date(2026, 8, 1)))
        c2, _ = self.client([ok("garbage")])
        self.assertEqual(c2.availability(), {})
        # second known layout: one row per date with sp / nrt flags
        text_b = ("sensor,date,sp,nrt\nMODIS_NRT,2026-06-12,false,true\nMODIS_NRT,2026-06-10,true,true\n"
                  "MODIS_NRT,2026-06-11,true,true\nVIIRS_NOAA21_NRT,2026-06-12,false,true\n")
        c4, _ = self.client([ok(text_b)])
        self.assertEqual(c4.availability(), {"MODIS_SP": (date(2026, 6, 10), date(2026, 6, 11))})
        c3, _ = self.client([TransportError("x")])
        self.assertEqual(c3.availability(), {})


class TestRateLimiter(unittest.TestCase):
    def test_blocks_when_budget_used_then_resumes_after_window(self):
        clock = {"t": 0.0}
        slept = []

        def sleep(s):
            slept.append(s)
            clock["t"] += s

        rl = RateLimiter(10, 600, 0, clock=lambda: clock["t"], sleep=sleep)
        for _ in range(5):
            rl.acquire(2)                      # budget exactly used
        self.assertEqual(slept, [])
        rl.acquire(2)                          # must wait for the window to roll over
        self.assertGreaterEqual(sum(slept), 599)
        self.assertEqual(rl.total_requests, 6)

    def test_gives_up_instead_of_waiting_forever(self):
        clock = {"t": 0.0}
        rl = RateLimiter(4, 600, 0, clock=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                         max_wait_s=10)
        rl.acquire(4)
        with self.assertRaises(RateLimitExceeded):
            rl.acquire(4)

    def test_minimum_gap_between_requests(self):
        clock = {"t": 0.0}
        slept = []
        rl = RateLimiter(100, 600, 1.0, clock=lambda: clock["t"],
                         sleep=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)))
        rl.acquire(1)
        rl.acquire(1)
        self.assertAlmostEqual(sum(slept), 1.0)

    def test_estimated_cost_is_conservative_and_scales_with_days(self):
        self.assertEqual(estimate_cost((95, -8, 121.4, 11), 1), 2.0)
        self.assertEqual(estimate_cost((95, -8, 121.4, 11), 5), 10.0)
        self.assertEqual(estimate_cost((-180, -90, 180, 90), 1), 2.0 * 6 * 3)


class TestPlanner(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp)
        self.av = {}

    def plan(self, start, end, coverage=None, now=NOW, **kw):
        return plan_windows(self.cfg, start, end, now, coverage or {}, self.av, **kw)

    def test_windows_never_exceed_day_range_and_cover_every_needed_day(self):
        ws = self.plan(date(2026, 11, 1), date(2026, 11, 12), groups=["NOAA20"])
        self.assertTrue(all(w.days <= self.cfg.day_range for w in ws))
        days = sorted(w.start + timedelta(days=i) for w in ws for i in range(w.days))
        self.assertEqual(days[0], date(2026, 11, 1))
        self.assertEqual(len(days), 12)
        self.assertEqual(len(set(days)), 12)

    def test_never_plans_future_dates(self):
        ws = self.plan(date(2026, 11, 10), date(2026, 12, 30), groups=["NOAA20"])
        self.assertTrue(all(w.end <= date(2026, 11, 14) for w in ws))

    def test_suomi_npp_is_not_requested_after_it_stopped(self):
        ws = self.plan(date(2026, 10, 28), date(2026, 11, 8), groups=["SNPP"])
        days = {w.start + timedelta(days=i) for w in ws for i in range(w.days)}
        self.assertEqual(max(days), date(2026, 11, 1))
        self.assertTrue(all(w.product.source_id.startswith("VIIRS_SNPP") for w in ws))

    def test_sp_preferred_when_available_else_nrt(self):
        self.av = {"VIIRS_NOAA20_SP": (date(2018, 4, 1), date(2026, 8, 15))}
        p = lambda d: choose_product("NOAA20", d, self.av, date(2026, 11, 14), 90).source_id
        self.assertEqual(p(date(2026, 8, 15)), "VIIRS_NOAA20_SP")
        self.assertEqual(p(date(2026, 8, 16)), "VIIRS_NOAA20_NRT")
        # NOAA-21 has no SP source at all
        self.assertEqual(choose_product("NOAA21", date(2025, 1, 1), self.av, date(2026, 11, 14), 90).source_id,
                         "VIIRS_NOAA21_NRT")

    def test_heuristic_lag_used_when_availability_unknown(self):
        p = lambda d: choose_product("MODIS", d, {}, date(2026, 11, 14), 90).source_id
        self.assertEqual(p(date(2026, 1, 1)), "MODIS_SP")
        self.assertEqual(p(date(2026, 11, 10)), "MODIS_NRT")

    def test_final_dates_are_never_requested_again(self):
        cov = {("VIIRS_NOAA20_NRT", d.isoformat()): (NOW, True)
               for d in (date(2026, 11, 10) + timedelta(days=i) for i in range(5))}
        ws = self.plan(date(2026, 11, 10), date(2026, 11, 14), cov, groups=["NOAA20"], now=NOW + timedelta(days=30))
        self.assertEqual(ws, [])

    def test_non_final_dates_refresh_only_after_the_minimum_interval(self):
        cov = {("VIIRS_NOAA20_NRT", "2026-11-14"): (NOW - timedelta(minutes=30), False)}
        self.assertEqual(self.plan(date(2026, 11, 14), date(2026, 11, 14), cov, groups=["NOAA20"]), [])
        cov = {("VIIRS_NOAA20_NRT", "2026-11-14"): (NOW - timedelta(hours=5), False)}
        self.assertEqual(len(self.plan(date(2026, 11, 14), date(2026, 11, 14), cov, groups=["NOAA20"])), 1)

    def test_force_overrides_coverage(self):
        cov = {("VIIRS_NOAA20_NRT", "2026-11-12"): (NOW, True)}
        ws = self.plan(date(2026, 11, 12), date(2026, 11, 12), cov, groups=["NOAA20"], force=True)
        self.assertEqual(len(ws), 1)

    def test_gap_in_the_middle_is_filled_without_refetching_neighbours(self):
        cov = {("VIIRS_NOAA20_NRT", d): (NOW, True) for d in ("2026-11-10", "2026-11-11", "2026-11-13", "2026-11-14")}
        ws = self.plan(date(2026, 11, 10), date(2026, 11, 14), cov, groups=["NOAA20"])
        self.assertEqual([(w.start, w.days) for w in ws], [(date(2026, 11, 12), 1)])


class TestIngestService(TempDirMixin, unittest.TestCase):
    @staticmethod
    def _reset_logging():
        lg = logging.getLogger("emberwatch")
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()

    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp, "live", EMBERWATCH_MAX_RETRIES=1, EMBERWATCH_MIN_REFETCH_MINUTES=120)
        self.conn = open_conn(self.cfg)
        self.sleeps = []

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def svc(self, area_responses, now=NOW, status="{}", availability="data_id,min_date,max_date\n"):
        """Script: availability call, then key-status call, then the area requests (last one repeats)."""
        t = FakeTransport([ok(availability), ok(status)] + list(area_responses))
        limiter = RateLimiter(1000, 600, 0, clock=lambda: 0.0, sleep=self.sleeps.append)
        client = FirmsClient(self.cfg, t, limiter, sleep=self.sleeps.append, rng=lambda: 0.0)
        return IngestService(self.cfg, self.conn, client, lambda: now), t

    def test_second_run_makes_no_new_requests_for_final_data(self):
        text = csv_text(VIIRS_HDR, viirs_row())
        s1, t1 = self.svc([ok(text)])
        a = s1.run(date(2026, 11, 1), date(2026, 11, 5))
        self.assertGreater(a.requests_made, 0)
        self.assertEqual(a.status, "ok")
        s2, t2 = self.svc([ok(text)])
        b = s2.run(date(2026, 11, 1), date(2026, 11, 5))
        self.assertEqual(b.windows_planned, 0)
        self.assertEqual(b.requests_made, 0)
        self.assertEqual(len([u for u in t2.urls if "/api/area/" in u]), 0)

    def test_raw_response_is_stored_unchanged_with_redacted_request(self):
        text = csv_text(VIIRS_HDR, viirs_row(), viirs_row(lat="2.5"))
        s, _ = self.svc([ok(text)])
        s.run(date(2026, 11, 13), date(2026, 11, 13))
        r = self.conn.execute("SELECT * FROM raw_files WHERE source_id='VIIRS_NOAA20_NRT'").fetchone()
        self.assertEqual((self.cfg.data_dir / r["path"]).read_bytes(), text.encode())
        self.assertNotIn(FAKE_KEY, r["request_url"])
        self.assertEqual(r["data_mode"], "live")
        self.assertEqual(r["http_status"], 200)
        self.assertEqual(r["fetched_at_utc"], "2026-11-14T12:00:00Z")

    def test_recent_nrt_dates_are_not_final_but_old_ones_are(self):
        s, _ = self.svc([ok(VIIRS_HDR + "\n")])
        s.run(date(2026, 11, 8), date(2026, 11, 14))
        fin = {r["date_utc"]: r["is_final"] for r in self.conn.execute(
            "SELECT date_utc, is_final FROM fetch_coverage WHERE source_id='VIIRS_NOAA20_NRT'")}
        self.assertEqual(fin["2026-11-08"], 1)       # fetched >= 3 days after that date
        self.assertEqual(fin["2026-11-14"], 0)       # still within the refresh window

    def test_non_csv_response_is_kept_as_evidence_and_counted_as_failure(self):
        s, _ = self.svc([ok("<html>down</html>")])
        r = s.run(date(2026, 11, 13), date(2026, 11, 13))
        self.assertEqual(r.windows_ok, 0)
        self.assertGreater(r.windows_failed, 0)
        kinds = {x[0] for x in self.conn.execute("SELECT content_kind FROM raw_files")}
        self.assertEqual(kinds, {"error_text"})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fetch_coverage").fetchone()[0], 0)

    def test_bad_key_aborts_the_run_and_records_it(self):
        s, t = self.svc([ok("Invalid MAP_KEY.")])
        r = s.run(date(2026, 11, 1), date(2026, 11, 14))
        self.assertEqual(r.status, "aborted")
        self.assertIn("AuthError", r.aborted_reason)
        self.assertEqual(len([u for u in t.urls if "/api/area/" in u]), 1)    # stopped at the first request
        st = self.conn.execute("SELECT status FROM ingest_runs").fetchone()[0]
        self.assertEqual(st, "aborted")

    def test_repeated_failures_trip_the_circuit_breaker(self):
        s, t = self.svc([HttpResponse(400, b"no", {})])
        r = s.run(date(2025, 1, 1), date(2025, 6, 30))
        self.assertEqual(r.status, "aborted")
        self.assertEqual(r.windows_failed, self.cfg.max_consecutive_failures)

    def test_low_remaining_quota_stops_before_any_area_request(self):
        status = json.dumps({"transaction_limit": 5000, "current_transactions": 4900, "transaction_interval": "10 minutes"})
        s, t = self.svc([ok("unused")], status=status)
        r = s.run(date(2026, 11, 1), date(2026, 11, 14))
        self.assertEqual(r.status, "aborted")
        self.assertEqual(len([u for u in t.urls if "/api/area/" in u]), 0)

    def test_quota_is_rechecked_during_a_long_run_and_the_run_stops_cleanly(self):
        ok_status = json.dumps({"transaction_limit": 5000, "current_transactions": 10})
        low_status = json.dumps({"transaction_limit": 5000, "current_transactions": 4950})
        text = csv_text(VIIRS_HDR, viirs_row())
        # calls: availability, initial status, 20 area requests, mid-run status (low), then anything else
        script = [ok(text)] * 20 + [ok(low_status)]
        s, t = self.svc(script, status=ok_status)
        r = s.run(date(2025, 1, 1), date(2025, 4, 30))
        self.assertEqual(r.status, "aborted")
        self.assertIn("stopped early", r.aborted_reason)
        self.assertEqual(r.requests_made, 20)
        self.assertEqual(len([u for u in t.urls if "/api/area/" in u]), 20)
        # what was fetched before stopping is kept and recorded as covered
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM raw_files").fetchone()[0], 20)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM fetch_coverage").fetchone()[0], 0)

    def test_plan_only_makes_no_requests_and_writes_nothing(self):
        s, t = self.svc([ok("x")])
        r = s.run(date(2026, 11, 1), date(2026, 11, 14), plan_only=True)
        self.assertEqual(t.urls, [])
        self.assertGreater(r.windows_planned, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0], 0)

    def test_the_key_never_reaches_the_database_raw_files_or_logs(self):
        setup_logging(self.cfg)
        self.addCleanup(self._reset_logging)
        s, _ = self.svc([HttpResponse(500, b"", {}), ok(csv_text(VIIRS_HDR, viirs_row()))])
        s.run(date(2026, 11, 12), date(2026, 11, 13))
        s2, _ = self.svc([ok("Invalid MAP_KEY.")])
        s2.run(date(2026, 10, 1), date(2026, 10, 3), force=True)
        logging.shutdown()
        self.conn.commit()
        needle = FAKE_KEY.encode()
        scanned = 0
        for path in self.cfg.data_dir.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                scanned += 1
                self.assertNotIn(needle, data, f"key found in {path}")
        self.assertGreater(scanned, 3)
        # error_log rows too
        for row in self.conn.execute("SELECT message, context_json FROM error_log"):
            self.assertNotIn(FAKE_KEY, (row[0] or "") + (row[1] or ""))


if __name__ == "__main__":
    unittest.main()
