"""The version number lives in one file and shows up where people and tools look for it."""
import subprocess
import sys
import unittest
from pathlib import Path

import serve
from app_version import APP_NAME, VERSION

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_the_version_is_three_plain_numbers(self):
        self.assertRegex(VERSION, r"\d+\.\d+\.\d+\Z")

    def test_both_pages_show_it_and_no_placeholder_is_left_behind(self):
        for name, page in (("serve.html", serve.PAGE), ("compare.html", serve.COMPARE_PAGE)):
            with self.subTest(page=name):
                self.assertIn(f'<span id="appVersion">{VERSION}</span>', page)
                self.assertNotIn("__APP_VERSION__", page)

    def test_the_command_line_reports_it(self):
        done = subprocess.run([sys.executable, str(ROOT / "serve.py"), "--version"], capture_output=True,
                              text=True, timeout=120, cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), f"{APP_NAME} {VERSION}")


if __name__ == "__main__":
    unittest.main()
