"""setup.bat's own Python: tools/get_python.ps1 fetches the package catalog/python_runtime.json names, checks its SHA-256, and
leaves python\\ whole or not at all. The package is a stand-in served from this computer."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "get_python.ps1"
POWERSHELL = shutil.which("powershell.exe")


def package(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


GOOD = package(
    {
        "_rels/.rels": b"<Relationships/>",
        "python.nuspec": b"<package/>",
        "tools/python.exe": b"stand-in python",
        "tools/Lib/venv/__init__.py": b"# venv\n",
        "tools/DLLs/_ctypes.pyd": b"pyd",
    }
)


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "get_python.ps1 runs in Windows PowerShell")
class GetPythonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-getpython-")
        self.addCleanup(self.temp.cleanup)
        self.lab = Path(self.temp.name)
        self.served = {"/python.nupkg": GOOD}
        self.requests = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append(self.path)
                body = test.served.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

    def catalog(self, sha256=None, path="/python.nupkg"):
        catalog = self.lab / "python_runtime.json"
        catalog.write_text(
            json.dumps(
                {
                    "version": "3.12.10",
                    "url": self.url + path,
                    "bytes": len(GOOD),
                    "sha256": sha256 or hashlib.sha256(GOOD).hexdigest(),
                    "folder": "tools",
                }
            ),
            encoding="utf-8",
        )
        return catalog

    def run_script(self, catalog):
        done = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-Catalog", str(catalog),
             "-Destination", str(self.lab / "python")],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )  # fmt: skip
        return done.returncode, done.stdout + done.stderr

    def left_behind(self):
        return sorted(path.name for path in self.lab.iterdir() if path.name != "python_runtime.json")

    def test_the_package_is_unpacked_into_python_and_nothing_else_is_left(self):
        code, output = self.run_script(self.catalog())
        self.assertEqual(code, 0, output)
        self.assertIn("downloading the Lab's own Python 3.12.10", output)
        self.assertEqual((self.lab / "python" / "python.exe").read_bytes(), b"stand-in python")
        self.assertTrue((self.lab / "python" / "Lib" / "venv" / "__init__.py").is_file())
        self.assertFalse((self.lab / "python" / "python.nuspec").exists(), "only the package's Python folder is unpacked")
        self.assertEqual(self.left_behind(), ["python"])

    def test_a_python_that_is_already_there_is_not_fetched_again(self):
        (self.lab / "python").mkdir()
        (self.lab / "python" / "python.exe").write_bytes(b"mine")
        code, output = self.run_script(self.catalog())
        self.assertEqual((code, self.requests), (0, []), output)
        self.assertEqual((self.lab / "python" / "python.exe").read_bytes(), b"mine")

    def test_a_download_that_does_not_match_its_sha256_is_deleted_and_nothing_is_installed(self):
        code, output = self.run_script(self.catalog(sha256="0" * 64))
        self.assertEqual(code, 1, output)
        self.assertIn("does not match its published SHA-256", output)
        self.assertEqual(self.left_behind(), [])

    def test_a_download_that_fails_says_so_and_leaves_nothing(self):
        code, output = self.run_script(self.catalog(path="/missing.nupkg"))
        self.assertEqual(code, 1, output)
        self.assertIn("Check the internet connection", output)
        self.assertEqual(self.left_behind(), [])

    def test_what_an_interrupted_run_left_is_cleared_first(self):
        (self.lab / "python.partial" / "python").mkdir(parents=True)
        (self.lab / "python.partial" / "package.zip").write_bytes(b"half")
        code, output = self.run_script(self.catalog())
        self.assertEqual(code, 0, output)
        self.assertEqual(self.left_behind(), ["python"])

    def test_a_python_folder_setup_did_not_make_is_left_alone(self):
        (self.lab / "python").mkdir()
        (self.lab / "python" / "notes.txt").write_text("mine", encoding="utf-8")
        code, output = self.run_script(self.catalog())
        self.assertEqual((code, self.requests), (1, []), output)
        self.assertIn("without python.exe", output)
        self.assertEqual(os.listdir(self.lab / "python"), ["notes.txt"])

    def test_a_file_that_would_land_outside_the_folder_stops_the_install(self):
        evil = package({"tools/python.exe": b"x", "tools/../../escaped.txt": b"x"})
        self.served["/python.nupkg"] = evil
        code, output = self.run_script(self.catalog(sha256=hashlib.sha256(evil).hexdigest()))
        self.assertEqual(code, 1, output)
        self.assertIn("outside its folder", output)
        self.assertEqual(self.left_behind(), [])
        self.assertFalse((self.lab.parent / "escaped.txt").exists())


class CatalogTests(unittest.TestCase):
    def test_the_catalog_names_python_3_12_from_nuget_by_its_sha256(self):
        runtime = json.loads((ROOT / "catalog" / "python_runtime.json").read_text(encoding="utf-8"))
        self.assertTrue(runtime["version"].startswith("3.12."))
        self.assertTrue(runtime["url"].startswith("https://api.nuget.org/v3-flatcontainer/python/"), runtime["url"])
        self.assertIn(runtime["version"], runtime["url"])
        self.assertRegex(runtime["sha256"], r"\A[0-9a-f]{64}\Z")
        self.assertGreater(runtime["bytes"], 1_000_000)


class SetupBatchTests(unittest.TestCase):
    def test_setup_bat_takes_the_labs_python_then_this_computers_then_fetches_one(self):
        data = (ROOT / "setup.bat").read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "batch files keep CRLF line ends")
        lines = [line.strip() for line in data.decode("ascii").splitlines()]
        own = lines.index('if exist "python\\python.exe" set "PYTHON=python\\python.exe"')
        computer = lines.index('py -3.12 -c "import sys" <nul >nul 2>&1')
        fetch = lines.index("powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\\get_python.ps1")
        core = lines.index("%PYTHON% tools\\setup_core.py")
        self.assertLess(own, computer)
        self.assertLess(computer, fetch)
        self.assertLess(fetch, core)
        self.assertEqual(lines[fetch + 1], "if errorlevel 1 goto no_python")
        # Windows PowerShell's own module path, not one inherited from PowerShell 7 (the next test says why).
        self.assertLess(lines.index('set "PSModulePath="'), fetch)

    def test_the_script_hashes_with_dotnet_not_get_filehash(self):
        # Windows PowerShell loads Get-FileHash from a module. Started by way of PowerShell 7 (cmd /c setup.bat on GitHub's
        # runners, or setup.bat from a PowerShell 7 window) it inherits PowerShell 7's module path, and on GitHub's runner
        # Get-FileHash was then "not recognized" while the other commands worked. The SHA-256 tests above check the hash.
        script = SCRIPT.read_text(encoding="ascii")
        self.assertNotIn("Get-FileHash -", script)
        self.assertIn("[Security.Cryptography.SHA256]::Create()", script)

    def test_the_script_keeps_crlf_line_ends_and_plain_ascii(self):
        data = SCRIPT.read_bytes()
        self.assertTrue(data.isascii())
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))


if __name__ == "__main__":
    unittest.main()
