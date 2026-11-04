"""Install an engine from the Models page: the plan, the running install, the routes, and the RAM each card shows.

Every install here runs a stand-in script in a private folder. Nothing is downloaded, nothing is installed, and no model is loaded.
"""

import json
import re
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import common
import engine_checks
import engine_install
import ram_needs
import reference_paths
import serve
import worker_pool
import worker_runtime
from cast_store import CastStore
from engine_install import EngineInstaller, paragraphs
from worker_pool import EnginePool, WorkerBusy

PLAN = """Fel's Voice Lab: Kokoro 82M on the CPU

This adds a small speech model that runs on an ordinary
computer, so the studio can speak.

  1. Python packages: 89 of 89 to install, among them PyTorch
     for the CPU. About 225 MB to download.
  2. Kokoro 82M, from huggingface.co.
  3. A speech check.

Licences: the Kokoro weights are Apache-2.0.
"""
PROBLEMS = "error: This computer cannot take the plan yet:\n  - About 3 GB of free disk space is needed here\n    and 1 GB is free.\n  - Run it with this folder's own Python.\n"

# Every stand-in starts by noting how it was called. wait_for() holds it until the test creates the named file.
PROLOGUE = """
import os, subprocess, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parents[1]
(HERE / "argv.txt").write_text(" ".join(sys.argv[1:]), "utf-8")


def wait_for(name):
    while not (HERE / name).exists():
        time.sleep(0.02)

"""
DRY_RUN = """
if "--dry-run" in sys.argv:
    sys.stdout.write((HERE / "plan.txt").read_text("utf-8"))
    print("Checks passed. This was a dry run, so nothing was changed.")
    sys.exit(0)
"""


def installed_probe(entry):
    return {"installed": True, "missing": [], "environment": entry["runtime"]["python"], "checkpoint": "fake"}


def missing_probe(entry):
    return {"installed": False, "missing": [entry["runtime"]["python"]], "environment": entry["runtime"]["python"],
            "checkpoint": "fake"}


class ParagraphTests(unittest.TestCase):
    def test_wrapped_lines_are_joined_and_numbered_steps_and_dashes_start_new_paragraphs(self):
        self.assertEqual(paragraphs(PLAN), [
            "Fel's Voice Lab: Kokoro 82M on the CPU",
            "This adds a small speech model that runs on an ordinary computer, so the studio can speak.",
            "1. Python packages: 89 of 89 to install, among them PyTorch for the CPU. About 225 MB to download.",
            "2. Kokoro 82M, from huggingface.co.",
            "3. A speech check.",
            "Licences: the Kokoro weights are Apache-2.0."])
        self.assertEqual(paragraphs("This computer cannot take the plan yet:\n  - first problem\n    that wraps\n  - second\n"),
                         ["This computer cannot take the plan yet:", "- first problem that wraps", "- second"])
        self.assertEqual(paragraphs("3.12 is not a step, and neither is a version such as 2.5.1\nthat wraps"),
                         ["3.12 is not a step, and neither is a version such as 2.5.1 that wraps"])
        self.assertEqual(paragraphs(""), [])


class InstallerCase(unittest.TestCase):
    """A private folder, a pool that never starts a worker, and an installer that runs the stand-in in tools/."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-engine-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "tools").mkdir()
        (self.root / "plan.txt").write_text(PLAN, "utf-8")
        for target, value in ((worker_pool, "OUT"), (worker_pool, "LAB"), (reference_paths, "LAB")):
            patcher = mock.patch.object(target, value, self.root / "out" if value == "OUT" else self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.installed = False
        self.pool = EnginePool("cpu", 0, worker_factory=lambda entry: None, idle_poll_seconds=0.1,
                               install_probe=lambda entry: (installed_probe if self.installed else missing_probe)(entry),
                               ram_needs={"kokoro": 8, "turbo": 10},
                               ram_probe=lambda: {"total_gb": 16.0, "free_gb": 9.0})
        self.addCleanup(self.pool.close)
        self.installer = EngineInstaller(self.pool, root=self.root, python=sys.executable,
                                         scripts={"kokoro": "tools/stub_installer.py"})
        self.addCleanup(self.settle)
        self.addCleanup(self.installer.close)    # runs first: no stand-in may outlive the test

    def stub(self, body):
        (self.root / "tools" / "stub_installer.py").write_text(PROLOGUE + textwrap.dedent(body), "utf-8")

    def release(self, name="go"):
        (self.root / name).write_text("go", "utf-8")

    def wait_until(self, condition, what, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"gave up waiting for {what}; the install is {self.installer.snapshot()}")

    def settle(self):
        """Let the reader of a stopped install finish, so that its log file is closed before the folder is removed."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.installer.snapshot()
            if job is None or job["state"] != "running":
                return
            time.sleep(0.05)

    def finished(self):
        job = self.installer.snapshot()
        return job if job and job["state"] != "running" else None

    def argv(self):
        return (self.root / "argv.txt").read_text("utf-8")


class PlanTests(InstallerCase):
    def test_the_plan_is_the_installers_own_dry_run_without_its_command_line_ending(self):
        self.stub(DRY_RUN + "sys.exit(9)\n")
        plan = self.installer.plan("kokoro")
        self.assertEqual(plan, {"engine": "kokoro", "ok": True, "problems": [], "paragraphs": paragraphs(PLAN)})
        self.assertEqual(self.argv(), "--dry-run")
        self.assertIsNone(self.installer.snapshot())
        self.assertFalse(self.pool.busy)
        self.assertFalse((self.root / "out").exists())

    def test_a_plan_that_cannot_go_ahead_gives_the_reasons_and_the_plan_it_refused(self):
        self.stub(f"""
            sys.stdout.write((HERE / "plan.txt").read_text("utf-8"))
            sys.stderr.write({PROBLEMS!r})
            sys.exit(1)
        """)
        plan = self.installer.plan("kokoro")
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["paragraphs"], paragraphs(PLAN))
        self.assertEqual(plan["problems"], ["This computer cannot take the plan yet:",
                                            "- About 3 GB of free disk space is needed here and 1 GB is free.",
                                            "- Run it with this folder's own Python."])

    def test_an_installer_that_dies_without_a_word_is_still_explained(self):
        self.stub("sys.exit(7)\n")
        plan = self.installer.plan("kokoro")
        self.assertEqual((plan["ok"], plan["problems"]), (False, ["The installer stopped without saying why (exit code 7)."]))

    def test_an_installer_that_does_not_answer_in_time_is_an_error(self):
        self.stub("wait_for('never')\n")
        with mock.patch.object(engine_install, "PLAN_TIMEOUT", 0.5), self.assertRaisesRegex(TimeoutError, "did not answer"):
            self.installer.plan("kokoro")

    def test_unknown_engines_and_engines_without_an_installer_are_refused(self):
        self.stub(DRY_RUN)
        for action in (self.installer.plan, self.installer.start):
            with self.subTest(action=action.__name__):
                with self.assertRaisesRegex(ValueError, "Unknown engine"):
                    action("not-an-engine")
                with self.assertRaisesRegex(ValueError, "Chatterbox Turbo 350M cannot be installed from the app yet"):
                    action("turbo")
        self.assertFalse((self.root / "argv.txt").exists())
        self.assertFalse((self.root / "out").exists())

    def test_a_missing_python_is_told_to_run_setup_first(self):
        installer = EngineInstaller(self.pool, root=self.root, scripts={"kokoro": "tools/stub_installer.py"})
        with self.assertRaisesRegex(ValueError, "Run setup.bat first"):
            installer.plan("kokoro")

    def test_no_plan_is_read_while_an_install_runs(self):
        self.stub(DRY_RUN + "print('working', flush=True)\nwait_for('go')\n")
        self.installer.start("kokoro")
        with self.assertRaisesRegex(WorkerBusy, "Kokoro 82M is being installed now"):
            self.installer.plan("kokoro")
        self.release()
        self.wait_until(self.finished, "the install to end")


class InstallTests(InstallerCase):
    def test_the_install_runs_with_yes_keeps_its_output_and_holds_the_service_until_it_ends(self):
        self.stub("""
            print("first line", flush=True)
            wait_for("go")
            print("last line", flush=True)
        """)
        started = self.installer.start("kokoro")
        self.assertEqual((started["engine"], started["name"], started["state"]), ("kokoro", "Kokoro 82M", "running"))
        self.assertIsNone(started["exit_code"])
        self.wait_until(lambda: "first line" in (self.installer.snapshot() or {}).get("lines", []), "the first line")
        self.assertTrue(self.pool.busy)
        status = self.pool.engine_status()
        self.assertTrue(status["busy"])
        self.assertEqual(self.argv(), "--yes")
        with self.assertRaisesRegex(WorkerBusy, r"^The GPU worker is busy while Kokoro 82M is being installed\.$"):
            self.pool.load("kokoro")
        self.release()
        done = self.wait_until(self.finished, "the install to end")
        self.assertEqual((done["state"], done["exit_code"], done["lines"]), ("finished", 0, ["first line", "last line"]))
        self.assertIsNotNone(done["ended_at"])
        self.assertFalse(self.pool.busy)
        self.assertIsNone(self.pool._held_for)

    def test_a_failed_install_keeps_the_end_of_its_output_and_frees_the_service(self):
        self.stub("""
            print("Installing the Python packages", flush=True)
            print("error: pip stopped with exit code 1.", file=sys.stderr, flush=True)
            sys.exit(3)
        """)
        self.installer.start("kokoro")
        done = self.wait_until(self.finished, "the install to end")
        self.assertEqual((done["state"], done["exit_code"]), ("failed", 3))
        self.assertEqual(done["lines"], ["Installing the Python packages", "error: pip stopped with exit code 1."])
        self.assertFalse(self.pool.busy)
        self.assertEqual(self.installer.start("kokoro")["state"], "running")    # and it can be started again

    def test_only_one_install_runs_at_a_time(self):
        self.stub("print('working', flush=True)\nwait_for('go')\n")
        self.installer.start("kokoro")
        with self.assertRaisesRegex(WorkerBusy, "Kokoro 82M is already being installed"):
            self.installer.start("kokoro")
        self.release()
        self.wait_until(self.finished, "the install to end")

    def test_a_service_that_is_busy_will_not_start_an_install(self):
        self.stub("print('working', flush=True)\n")
        self.pool.hold("a clip is made")
        try:
            with self.assertRaisesRegex(WorkerBusy, r"^The GPU worker is busy while a clip is made\.$"):
                self.installer.start("kokoro")
        finally:
            self.pool.release_hold()
        self.assertIsNone(self.installer.snapshot())
        self.assertFalse((self.root / "argv.txt").exists())
        self.assertFalse((self.root / "out").exists())

    def test_an_engine_that_is_installed_is_not_installed_again(self):
        self.stub("print('working', flush=True)\n")
        self.installed = True
        with self.assertRaisesRegex(ValueError, "Kokoro 82M is already installed"):
            self.installer.start("kokoro")
        self.assertFalse((self.root / "argv.txt").exists())
        self.assertFalse(self.pool.busy)

    def test_a_script_that_cannot_start_frees_the_service(self):
        self.stub("print('working', flush=True)\n")
        with mock.patch.object(engine_install.subprocess, "Popen", side_effect=OSError("no such program")):
            with self.assertRaisesRegex(OSError, "no such program"):
                self.installer.start("kokoro")
        self.assertFalse(self.pool.busy)
        self.assertIsNone(self.installer.snapshot())

    def test_a_progress_line_that_is_rewritten_shows_only_its_latest_state(self):
        self.stub("""
            sys.stdout.buffer.write(b"10%\\r20%\\r30%\\nnext\\n\\n\\n\\nlast")
            sys.stdout.buffer.flush()
        """)
        self.installer.start("kokoro")
        done = self.wait_until(self.finished, "the install to end")
        self.assertEqual(done["lines"], ["30%", "next", "", "last"])    # one blank line stays; the last line had no newline

    def test_output_is_read_as_utf8_even_when_a_character_arrives_in_pieces(self):
        self.stub("""
            data = "D\\u00e9j\\u00e0 vu \\u2713\\n".encode("utf-8")
            sys.stdout.buffer.write(data[:2])    # "D" and the first of the two bytes of the e with the accent
            sys.stdout.buffer.flush()
            time.sleep(0.2)
            sys.stdout.buffer.write(data[2:])
            sys.stdout.buffer.flush()
        """)
        self.installer.start("kokoro")
        self.assertEqual(self.wait_until(self.finished, "the install to end")["lines"], ["Déjà vu ✓"])

    def test_the_whole_output_goes_to_a_log_and_only_the_newest_logs_are_kept(self):
        folder = self.root / "out" / "install"
        folder.mkdir(parents=True)
        for number in range(12):
            (folder / f"kokoro-2020010{number % 10}-00000{number // 10}.log").write_text("old", "utf-8")
        (folder / "turbo-20200101-000000.log").write_text("another engine's log", "utf-8")
        self.stub("""
            for number in range(300):
                print(f"line {number}", flush=True)
        """)
        self.installer.start("kokoro")
        done = self.wait_until(self.finished, "the install to end")
        log = self.root / done["log"]
        self.assertTrue(done["log"].startswith("out/install/kokoro-"))
        self.assertEqual(log.read_text("utf-8").splitlines(), [f"line {number}" for number in range(300)])
        self.assertEqual(len(done["lines"]), engine_install.TAIL_LINES)
        self.assertEqual(done["lines"][-1], "line 299")
        self.assertEqual(len(list(folder.glob("kokoro-*.log"))), engine_install.LOGS_KEPT)
        self.assertTrue((folder / "turbo-20200101-000000.log").is_file())

    def test_a_log_that_cannot_be_written_stops_the_install_and_still_ends_it(self):
        class FullDisk:
            def write(self, data):
                raise OSError(28, "No space left on device")

            def flush(self):
                pass

            def close(self):
                raise OSError(28, "No space left on device")

        class FullDiskLog:
            name = "kokoro-full.log"

            def open(self, mode):
                return FullDisk()

        self.stub("""
            (HERE / "pid.txt").write_text(str(os.getpid()), "utf-8")
            print("working", flush=True)
            wait_for("never")
        """)
        with mock.patch.object(EngineInstaller, "_new_log", return_value=FullDiskLog()):
            self.installer.start("kokoro")
        done = self.wait_until(self.finished, "the install to end")
        self.assertEqual(done["state"], "failed")
        self.assertRegex("\n".join(done["lines"]), "The service stopped reading the installer's output: OSError")
        self.assertFalse(self.pool.busy)
        pid = int((self.root / "pid.txt").read_text("utf-8"))
        self.wait_until(lambda: not worker_pool.process_is_running(pid), "the stand-in to be stopped")

    @unittest.skipUnless(os.name == "nt", "the process tree is stopped with taskkill")
    def test_closing_stops_the_install_and_the_processes_it_started(self):
        self.stub("""
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            (HERE / "pids.txt").write_text(f"{os.getpid()} {child.pid}", "utf-8")
            print("working", flush=True)
            wait_for("never")
        """)
        self.installer.start("kokoro")
        pids = self.wait_until(lambda: (self.root / "pids.txt").is_file() and (self.root / "pids.txt").read_text("utf-8").split(),
                               "the stand-in to start its child")
        script, child = map(int, pids)
        self.assertTrue(worker_pool.process_is_running(script) and worker_pool.process_is_running(child))
        self.installer.close()
        done = self.wait_until(self.finished, "the install to stop")
        self.assertEqual(done["state"], "failed")
        self.wait_until(lambda: not worker_pool.process_is_running(script) and not worker_pool.process_is_running(child),
                        "both processes to be gone")
        self.assertFalse(self.pool.busy)

    def test_closing_with_nothing_running_does_nothing(self):
        self.installer.close()
        self.assertIsNone(self.installer.snapshot())


class CatalogTests(unittest.TestCase):
    def test_every_launcher_installer_is_also_one_the_page_can_run(self):
        self.assertEqual(set(engine_install.INSTALLERS), set(engine_checks.GUIDED_INSTALLERS))
        for key, script in engine_install.INSTALLERS.items():
            with self.subTest(engine=key):
                self.assertTrue((ROOT / script).is_file(), script)
                launcher = (ROOT / engine_checks.GUIDED_INSTALLERS[key]).read_text("utf-8")
                self.assertIn(script.replace("/", "\\"), launcher)

    def test_every_engine_the_service_offers_has_a_free_ram_need_in_the_one_table(self):
        offered = {entry["id"] for entry in worker_pool.read_catalog()["engines"]
                   if entry.get("integration", {}).get("service_api") is True}
        self.assertEqual(set(ram_needs.DEFAULT_MIN_FREE_RAM_GB), offered)
        self.assertIs(serve.MIN_FREE_RAM_GB, ram_needs.MIN_FREE_RAM_GB)
        self.assertIs(worker_runtime.MIN_FREE_RAM_GB, ram_needs.MIN_FREE_RAM_GB)
        self.assertTrue(all(isinstance(need, int) and need > 0 for need in ram_needs.DEFAULT_MIN_FREE_RAM_GB.values()))

    def test_the_hardware_table_in_troubleshooting_gives_each_model_the_ram_the_one_table_does(self):
        rows = {}
        for line in (ROOT / "docs" / "TROUBLESHOOTING.md").read_text("utf-8").splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            need = re.match(r"(\d+) GB", cells[1]) if line.startswith("|") and len(cells) == 7 else None
            if need:
                rows[cells[0]] = int(need.group(1))
        names = {entry["id"]: entry["name"] for entry in worker_pool.read_catalog()["engines"]}
        expected = {"Phonon 31M English": ["phonon-31m-en"], "Kokoro 82M": ["kokoro"],
                    "Chatterbox Turbo 350M (cloning)": ["turbo"], "Chatterbox 500M and Voice Conversion": ["orig", "chatterbox-vc"],
                    "Qwen3-TTS (four checkpoints)": [key for key in names if key.startswith("qwen3-tts")],
                    "VoxCPM2 2B": ["voxcpm2"], "Breeze TTS 2": ["breeze-tts-2"], "Drama 3 (35B, Q4)": ["drama-3"]}
        self.assertEqual(set(rows), set(expected))
        self.assertEqual(sorted(key for keys in expected.values() for key in keys), sorted(ram_needs.DEFAULT_MIN_FREE_RAM_GB))
        for row, keys in expected.items():
            for key in keys:
                with self.subTest(row=row, engine=key):
                    self.assertEqual(rows[row], ram_needs.DEFAULT_MIN_FREE_RAM_GB[key])

    def test_runtime_local_json_can_change_a_free_ram_need_for_one_computer(self):
        defaults = {"kokoro": 3, "turbo": 10}
        self.assertEqual(ram_needs.with_overrides(defaults, None), defaults)
        self.assertEqual(ram_needs.with_overrides(defaults, {"kokoro": 1.5}), {"kokoro": 1.5, "turbo": 10})
        self.assertEqual(ram_needs.with_overrides(defaults, {"turbo": 0}), {"kokoro": 3, "turbo": 0})
        self.assertEqual(defaults, {"kokoro": 3, "turbo": 10})

    def test_a_free_ram_override_that_cannot_be_used_is_refused_with_its_name(self):
        defaults = {"kokoro": 3}
        for overrides, words in (([1], "must map engines to GB"), ({"kokoro-2": 1}, "'kokoro-2', which is not one"),
                                 ({"kokoro": -1}, "kokoro must be a number"), ({"kokoro": "2"}, "kokoro must be a number"),
                                 ({"kokoro": True}, "kokoro must be a number"), ({"kokoro": float("nan")}, "kokoro must be")):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, words):
                ram_needs.with_overrides(defaults, overrides)

    def test_this_computers_ram_is_reported_in_gigabytes(self):
        report = common.ram_report()
        self.assertEqual(set(report), {"total_gb", "free_gb"})
        self.assertTrue(0 < report["free_gb"] <= report["total_gb"], report)
        self.assertAlmostEqual(report["free_gb"], common.free_ram_gb(), delta=1.0)

    def test_the_status_rows_carry_the_ram_need_and_the_payload_carries_this_computers_ram(self):
        entries = {entry["id"]: entry for entry in worker_pool.read_catalog()["engines"] if entry["id"] in {"kokoro", "turbo"}}
        payload = engine_checks.status_payload(entries, device="cpu", busy=False, store=None, probe=missing_probe,
                                               fingerprint_of=lambda entry: "pin", ram_needs={"kokoro": 8},
                                               ram={"total_gb": 16.0, "free_gb": 9.0})
        self.assertEqual({row["id"]: row["min_free_ram_gb"] for row in payload["engines"]}, {"kokoro": 8, "turbo": None})
        self.assertEqual(payload["ram"], {"total_gb": 16.0, "free_gb": 9.0})
        bare = engine_checks.status_payload(entries, device="cpu", busy=False, store=None, probe=missing_probe)
        self.assertIsNone(bare["ram"])
        self.assertEqual([row["min_free_ram_gb"] for row in bare["engines"]], [None, None])


class InstallRoutesTests(InstallerCase):
    def setUp(self):
        super().setUp()
        self.cast = CastStore(self.root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        evaluations = serve.EvaluationStore(self.root / "evaluations.sqlite3", lab_root=self.root)
        self.server.RequestHandlerClass = serve.make_handler(self.pool, self.server.server_address[1], self.cast,
                                                             evaluation_store=evaluations, engine_installer=self.installer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def test_the_status_carries_the_ram_needs_this_computers_ram_and_no_install_at_first(self):
        status, body = self.call("GET", "/api/engines/status")
        self.assertEqual(status, 200)
        rows = {row["id"]: row for row in body["engines"]}
        self.assertEqual((rows["kokoro"]["min_free_ram_gb"], rows["turbo"]["min_free_ram_gb"], rows["orig"]["min_free_ram_gb"]),
                         (8, 10, None))
        self.assertEqual(body["ram"], {"total_gb": 16.0, "free_gb": 9.0})
        self.assertIsNone(body["install"])

    def test_the_plan_then_the_install_run_through_the_routes(self):
        self.stub(DRY_RUN + "print('working', flush=True)\nwait_for('go')\nprint('all done', flush=True)\n")
        status, body = self.call("POST", "/api/engines/kokoro/install", {"dry_run": True})
        self.assertEqual((status, body["plan"]["ok"], body["plan"]["engine"]), (200, True, "kokoro"))
        self.assertEqual(body["plan"]["paragraphs"], paragraphs(PLAN))
        self.assertEqual(self.call("GET", "/api/engines/status")[1]["install"], None)
        status, body = self.call("POST", "/api/engines/kokoro/install", {})
        self.assertEqual((status, body["install"]["state"], body["install"]["engine"]), (200, "running", "kokoro"))
        self.wait_until(lambda: "working" in (self.call("GET", "/api/engines/status")[1]["install"] or {}).get("lines", []),
                        "the installer's first line to reach the status")
        status, body = self.call("GET", "/api/engines/status")
        self.assertTrue(body["busy"])
        self.assertEqual(self.call("POST", "/api/load", {"engine": "kokoro"})[0], 409)
        self.assertEqual(self.call("POST", "/api/engines/kokoro/install", {})[0], 409)
        self.assertEqual(self.call("POST", "/api/engines/kokoro/install", {"dry_run": True})[0], 409)
        self.release()
        self.wait_until(lambda: self.call("GET", "/api/engines/status")[1]["install"]["state"] != "running", "the install to end")
        status, body = self.call("GET", "/api/engines/status")
        self.assertEqual((body["busy"], body["install"]["state"], body["install"]["exit_code"]), (False, "finished", 0))
        self.assertEqual(body["install"]["lines"], ["working", "all done"])

    def test_bad_requests_are_answered_before_anything_runs(self):
        self.stub(DRY_RUN + "wait_for('go')\n")
        self.assertEqual(self.call("POST", "/api/engines/kokoro/install", {"dry_run": "yes"})[0], 400)
        self.assertEqual(self.call("POST", "/api/engines/turbo/install", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/engines/not-an-engine/install", {"dry_run": True})[0], 400)
        self.installed = True
        status, body = self.call("POST", "/api/engines/kokoro/install", {})
        self.assertEqual(status, 400)
        self.assertIn("already installed", body["error"])
        self.assertFalse((self.root / "argv.txt").exists())
        self.assertIsNone(self.installer.snapshot())

    def test_a_page_from_another_website_cannot_start_an_install(self):
        self.stub(DRY_RUN + "wait_for('go')\n")
        for body in ({}, {"dry_run": True}):
            status, answer = self.call("POST", "/api/engines/kokoro/install", body, headers={"Origin": "http://evil.example"})
            self.assertEqual((status, answer), (403, {"error": "local requests only"}))
        self.assertFalse((self.root / "argv.txt").exists())
        self.assertIsNone(self.installer.snapshot())
        self.assertFalse(self.pool.busy)


    @unittest.skipUnless(os.name == "nt", "the process tree is stopped with taskkill")
    def test_the_service_stops_the_install_it_serves_when_it_shuts_down(self):
        self.stub("print('working', flush=True)\nwait_for('never')\n")
        self.call("POST", "/api/engines/kokoro/install", {})
        self.wait_until(lambda: "working" in (self.installer.snapshot() or {}).get("lines", []), "the installer's first line")
        self.server.RequestHandlerClass.close_installer()
        self.assertEqual(self.wait_until(self.finished, "the install to stop")["state"], "failed")
        self.assertFalse(self.pool.busy)


if __name__ == "__main__":
    unittest.main()
