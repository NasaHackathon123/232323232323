import contextlib
import io
import json
import os
import unittest
from unittest import mock

from helpers import FAKE_KEY, TempDirMixin, make_cfg  # noqa: F401  (sets sys.path)

from emberwatch import cli
from emberwatch.runner import serve


class TestCli(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._cwd = os.getcwd()
        os.chdir(self.tmp)                         # so no .env file from the real repo is picked up
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FIRMS_", "EMBERWATCH_"))}
        env["EMBERWATCH_DATA_DIR"] = str(self.tmp / "data")
        env["EMBERWATCH_NO_DOTENV"] = "1"         # a real .env must never leak into tests
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        os.chdir(self._cwd)
        import logging
        lg = logging.getLogger("emberwatch")
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()
        super().tearDown()

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_status_in_demo_mode_never_shows_a_key(self):
        code, out, _ = self.call("status")
        self.assertEqual(code, 0)
        info = json.loads(out)
        self.assertEqual(info["mode"], "demo")
        self.assertFalse(info["map_key_configured"])
        self.assertIn("DEMO MODE", info["notice"])

    def test_status_with_key_reports_yes_but_not_the_value(self):
        with mock.patch.dict(os.environ, {"FIRMS_MAP_KEY": FAKE_KEY}):
            with mock.patch("emberwatch.cli.make_source") as ms:
                ms.return_value.map_key_status.return_value = {"transaction_limit": 5000, "current_transactions": 3}
                code, out, err = self.call("status")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["map_key_configured"])
        self.assertNotIn(FAKE_KEY, out + err)

    def test_live_mode_without_key_is_a_clear_error(self):
        code, _, err = self.call("--mode", "live", "status")
        self.assertEqual(code, 2)
        self.assertIn("FIRMS_MAP_KEY", err)

    def test_plan_only_then_run_then_quality(self):
        code, out, _ = self.call("fetch", "--plan-only")
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["status"], "planned_only")
        self.assertGreater(plan["windows_planned"], 0)
        self.assertEqual(plan["requests_made"], 0)
        self.assertFalse((self.tmp / "data" / "demo" / "raw").exists() and any(
            (self.tmp / "data" / "demo" / "raw").rglob("*.csv")))

        code, out, _ = self.call("run")
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual(res["ingest"]["status"], "ok")
        self.assertTrue(res["harmonize"]["reconciled"])
        self.assertTrue((self.tmp / "data" / "demo" / "exports" / "audit.json").exists())

        code, out, _ = self.call("quality")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["is_synthetic"])

        code, out, _ = self.call("fetch")                       # second fetch right away: nothing to do
        self.assertEqual(json.loads(out)["windows_planned"], 0)

    def test_harmonize_rebuild_command(self):
        self.call("run")
        code, out, _ = self.call("harmonize", "--rebuild")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["harmonize"]["reconciled"])

    def test_read_only_commands_create_no_files_or_folders(self):
        self.call("status")
        self.call("catalog")
        self.assertFalse((self.tmp / "data").exists())

    def test_default_data_folder_is_anchored_to_the_repository_root(self):
        from emberwatch.config import REPO_ROOT, load_settings
        cfg = load_settings(env={}, dotenv_path=None)
        self.assertEqual(cfg.root_dir, REPO_ROOT / "data")
        self.assertTrue((REPO_ROOT / "src" / "emberwatch").is_dir())

    def test_catalog_command_writes_markdown(self):
        target = self.tmp / "docs" / "T.md"
        code, out, _ = self.call("catalog", "--write", str(target))
        self.assertEqual(code, 0)
        text = target.read_text()
        for tid in ("T01", "T05a", "T10", "T13", "TIMESTAMP_FUTURE", "FRP_MISSING", "REVISION_OF_EARLIER_FETCH"):
            self.assertIn(tid, text)

    def test_bad_date_argument_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.call("fetch", "--start", "not-a-date")


class TestServe(TempDirMixin, unittest.TestCase):
    def test_serve_runs_waits_and_survives_failures(self):
        cfg = make_cfg(self.tmp)
        sleeps = []
        calls = {"n": 0}

        def flaky(cfg_, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return {"ingest": {"status": "ok", "windows_ok": 1}}

        with mock.patch("emberwatch.runner.run_pipeline", flaky):
            serve(cfg, every_minutes=60, sleep=sleeps.append, max_runs=2)
        self.assertEqual(calls["n"], 2)                         # the failure did not stop the loop
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(3600 <= sleeps[0] <= 3630)


if __name__ == "__main__":
    unittest.main()
