"""Real owned desktop service lifecycle, independent of models and existing services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from desktop import DesktopBackend
from worker_pool import process_is_running

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows desktop ownership")
class DesktopTests(unittest.TestCase):
    def test_owned_backend_is_ready_isolated_and_stops_on_close(self):
        with tempfile.TemporaryDirectory() as temp:
            # The session folder and the service log go in the temporary folder, and the service skips its sweep of out/, so
            # the run leaves the real out/ folder as it found it.
            backend = DesktopBackend(root=ROOT, state_root=temp, python=sys.executable, device="cpu",
                                     sessions=Path(temp, "desktop"),
                                     service_args=("--no-cleanup", "--log-dir", Path(temp, "logs")))
            try:
                url = backend.start(timeout=45)
                ready = json.loads((backend.session / "ready.json").read_text("utf-8"))
                with urllib.request.urlopen(url + "/api/health", timeout=10) as response:
                    health = json.load(response)
                self.assertEqual(health["workers"], [])
                self.assertTrue(Path(temp, "data/evaluations/evaluations.sqlite3").is_file())
                with urllib.request.urlopen(url + "/api/cast", timeout=10) as response:
                    self.assertEqual(json.load(response)["characters"], [])
                with urllib.request.urlopen(url + "/api/preferences", timeout=10) as response:
                    self.assertEqual(json.load(response), {"theme": "system", "check_updates": False})
                request = urllib.request.Request(url + "/api/preferences", data=b'{"theme":"dark"}',
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=10) as response:
                    self.assertEqual(json.load(response), {"theme": "dark", "check_updates": False})
                self.assertEqual(json.loads(Path(temp, "data/ui/preferences.json").read_text()),
                                 {"theme": "dark", "check_updates": False})
                for page in ("/", "/compare"):
                    with urllib.request.urlopen(url + page, timeout=10) as response:
                        html = response.read().decode()
                    self.assertIn('data-theme="dark"', html)
                    self.assertIn("Fel's Voice Lab", html)
                    self.assertIn('src="/theme.js"', html)
                request = urllib.request.Request(url + "/api/preferences", data=b'{"theme":"light"}',
                    headers={"Content-Type": "application/json", "Origin": "http://foreign.invalid"}, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(error.exception.code, 403)
                with urllib.request.urlopen(url + "/theme.js", timeout=10) as response:
                    self.assertIn("javascript", response.headers["Content-Type"])
                    self.assertIn(b"/api/preferences", response.read())
                for origin in ("http://foreign.invalid", "http://localhost:invalid", "null", "https://127.0.0.1"):
                    with self.subTest(origin=origin):
                        request = urllib.request.Request(url + "/api/health", headers={"Origin": origin})
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            urllib.request.urlopen(request, timeout=5)
                        self.assertEqual(error.exception.code, 403)
                self.assertTrue(process_is_running(ready["pid"]))
            finally:
                backend.close()
            self.assertFalse(process_is_running(ready["pid"]))
            self.assertIsNotNone(backend.process.returncode)
            backend.close()  # Repeated close is harmless.

    def test_crashed_launcher_releases_owned_descendants(self):
        script = """import subprocess,sys,time
sys.path.insert(0,sys.argv[1])
from desktop_process import OwnedJob
job=OwnedJob()
child=subprocess.Popen([sys.executable,'-u','-c',"import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']); print(p.pid,flush=True);time.sleep(120)"],stdout=subprocess.PIPE,text=True)
job.add(child)
print(str(child.pid)+','+child.stdout.readline().strip(),flush=True)
time.sleep(120)
"""
        process = subprocess.Popen([sys.executable, "-u", "-c", script, str(ROOT)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        children = []
        try:
            line = process.stdout.readline().strip()
            self.assertTrue(line)
            children = [int(item) for item in line.split(",")]
            self.assertTrue(all(process_is_running(pid) for pid in children))
            process.terminate()
            process.wait(timeout=5)
            deadline = time.monotonic() + 5
            while any(process_is_running(pid) for pid in children) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(any(process_is_running(pid) for pid in children))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
