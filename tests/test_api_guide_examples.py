"""The examples in docs/API.md work as written: the Python snippet runs against a service, the JSON request is one the service
accepts, every command line the page shows is one the command-line tool takes, and the page mentions every flag the tool has."""
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_service
import api_spec
from tools import lab_cli

GUIDE = ROOT / "docs" / "API.md"


def blocks(language):
    """The text of each fenced block of that language in the guide."""
    return re.findall(r"^```" + language + r"\n(.*?)^```", GUIDE.read_text(encoding="utf-8"), re.S | re.M)


class GuideExampleTests(unittest.TestCase):
    def setUp(self):
        self.service = api_service.Service(self)
        self.identity = self.service.add_voice("Nell", "Warm")

    def test_the_python_example_runs_as_written(self):
        [source] = [block for block in blocks("python") if "/api/speak" in block]
        shown = "http://127.0.0.1:8765"
        self.assertIn(shown, source)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            result = subprocess.run([sys.executable, "-I", "-c", source.replace(shown, self.service.base)], cwd=folder,
                                    env={**os.environ, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(folder) / "hello.wav").read_bytes()[:4], b"RIFF")
        self.assertEqual(self.service.engines.requests, [{"text": "Hello from the Lab."}])

    def test_the_json_request_in_the_guide_is_one_the_service_accepts(self):
        [source] = [block for block in blocks("json") if "identity_id" in block]
        request = json.loads(source)
        row = next(row for row in api_spec.documented() if api_spec.key(row) == "POST /api/performance")
        self.assertLessEqual(set(request), {field.name for field in row.body})
        self.assertLessEqual({field.name for field in row.body if field.required}, set(request))
        status, headers, body = self.service.request("POST", "/api/performance", {**request, "identity_id": self.identity["id"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["X-Cast-Line-Id"], request["line_id"])

    def test_every_command_line_the_guide_shows_is_one_the_tool_takes(self):
        lines = [line.strip() for block in blocks("powershell") for line in block.splitlines() if "lab_cli.py" in line]
        self.assertGreaterEqual(len(lines), 2)
        parser = lab_cli.build_parser()
        for line in lines:
            with self.subTest(line):
                parsed = parser.parse_args(shlex.split(line.split("lab_cli.py", 1)[1]))
                self.assertIn(parsed.command, ("health", "speak", "perform", "cast"))

    def test_every_flag_the_tool_has_is_in_the_guide(self):
        text = GUIDE.read_text(encoding="utf-8")
        for command in ("health", "speak", "perform", "cast"):
            shown = io.StringIO()
            with contextlib.redirect_stdout(shown), self.assertRaises(SystemExit):
                lab_cli.main([command, "--help"])
            flags = set(re.findall(r"--[a-z][a-z-]*", shown.getvalue())) - {"--help"}
            self.assertTrue(flags, command)
            for flag in sorted(flags):
                with self.subTest(f"{command} {flag}"):
                    self.assertIn(flag, text)


if __name__ == "__main__":
    unittest.main()
