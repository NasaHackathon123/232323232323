"""Guards for the files that touch secrets: nothing that looks like a real key may be committed."""

import os
import re
import unittest
from pathlib import Path

from helpers import TempDirMixin  # noqa: F401  (sets sys.path)

from emberwatch.config import load_dotenv, load_settings

ROOT = Path(__file__).resolve().parents[1]
KEY_ASSIGNMENT = re.compile(r"FIRMS_MAP_KEY\s*[=:]\s*['\"]?([A-Za-z0-9]{20,})")


class TestRepoFiles(TempDirMixin, unittest.TestCase):
    def test_env_example_has_only_placeholders_and_gives_demo_mode(self):
        text = (ROOT / ".env.example").read_text()
        self.assertIn("FIRMS_MAP_KEY=your_firms_map_key_here", text)
        self.assertIsNone(KEY_ASSIGNMENT.search(text))
        names_before = set(os.environ)
        env = {}
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k] = v
        env["EMBERWATCH_DATA_DIR"] = str(self.tmp)
        cfg = load_settings(env=env, dotenv_path=None)
        self.assertEqual(cfg.mode, "demo")
        self.assertFalse(cfg.key_configured)
        self.assertEqual(set(os.environ), names_before)

    def test_gitignore_additions_exclude_env_and_generated_data(self):
        lines = {l.strip() for l in (ROOT / ".gitignore.emberwatch").read_text().splitlines()}
        for needed in (".env", ".env.*", "!.env.example", "/data/", "*.sqlite"):
            self.assertIn(needed, lines)

    def test_workflow_reads_the_key_only_from_secrets(self):
        text = (ROOT / ".github" / "workflows" / "emberwatch-ingest.yml").read_text()
        self.assertIn("${{ secrets.FIRMS_MAP_KEY }}", text)
        self.assertIsNone(KEY_ASSIGNMENT.search(text))
        self.assertNotIn("github.event.inputs.backfill_start }}\"", text.split("run: |")[1] if "run: |" in text else "")

    def test_no_key_like_assignment_anywhere_in_source_docs_or_config(self):
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix in (".pyc", ".sqlite", ".zip") or "__pycache__" in path.parts:
                continue
            if "tests" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if KEY_ASSIGNMENT.search(text):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_key_is_only_ever_read_from_the_environment_in_source(self):
        src = "\n".join(p.read_text() for p in (ROOT / "src").rglob("*.py"))
        self.assertEqual(len(re.findall(r'FIRMS_MAP_KEY"\)', src)), 1)       # single read site (config.py)
        self.assertNotRegex(src, r"map_key\s*=\s*[\"'][A-Za-z0-9]{16,}[\"']")


if __name__ == "__main__":
    unittest.main()
