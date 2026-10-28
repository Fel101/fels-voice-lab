"""The sweep of stale runtime folders removes only what is stale and only from the folders it owns."""
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import housekeeping
import worker_pool
from housekeeping import DAY, Item, apply, pid_may_be_alive, plan, run_startup_sweep, summarize
from tools import cleanup_runtime
from worker_pool import EnginePool
from worker_process import WorkerProcess

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


class SweepBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-sweep-")
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "out"
        for name in ("workers", "desktop", "logs"):
            (self.out / name).mkdir(parents=True)

    def folder(self, kind, name, days, *, files=()):
        path = self.out / kind / name
        path.mkdir(parents=True)
        for item in files:
            (path / item).write_text("x", "utf-8")
        self.age(path, days)
        return path

    def age(self, path, days):
        stamp = NOW - days * DAY
        os.utime(path, (stamp, stamp))

    def listed(self, **options):
        options.setdefault("pid_alive", lambda pid: False)
        return {item.path.name: item for item in plan(self.out, now=NOW, **options)}


class WorkerFolderTests(SweepBase):
    def test_an_empty_folder_of_a_stopped_worker_goes_once_it_is_an_hour_old(self):
        self.folder("workers", "kokoro-111", 2)
        self.folder("workers", "kokoro-222", 0.02)  # about half an hour: its worker may only be starting
        found = self.listed()
        self.assertEqual(set(found), {"kokoro-111"})
        self.assertEqual(found["kokoro-111"].kind, "worker")
        self.assertIn("holds nothing", found["kokoro-111"].reason)

    def test_a_folder_whose_process_may_still_be_running_stays(self):
        self.folder("workers", "kokoro-333", 5)
        self.assertEqual(self.listed(pid_alive=lambda pid: pid == 333), {})

    def test_a_folder_with_something_in_it_stays_for_a_week_as_evidence(self):
        self.folder("workers", "qwen3-tts-1.7b-base-444", 3, files=["half-written.npy"])
        self.folder("workers", "qwen3-tts-1.7b-base-555", 8, files=["half-written.npy"])
        found = self.listed()
        self.assertEqual(set(found), {"qwen3-tts-1.7b-base-555"})
        self.assertIn("exited 8 days ago", found["qwen3-tts-1.7b-base-555"].reason)
        self.assertEqual(found["qwen3-tts-1.7b-base-555"].size, 1)

    def test_names_that_are_not_a_worker_folder_are_ignored(self):
        self.folder("workers", "notes", 30)
        self.folder("workers", "kokoro-12x", 30)
        (self.out / "workers" / "kokoro-666").write_text("a file, not a folder", "utf-8")
        self.assertEqual(self.listed(), {})


class DesktopSessionTests(SweepBase):
    def make_sessions(self, ages, running=None):
        for number, days in enumerate(ages, 1):
            path = self.folder("desktop", f"session-a{number:02d}", days)
            pid = running if running is not None and number == 12 else 100 + number
            (path / "ready.json").write_text('{"pid": %d}' % pid, "utf-8")
            self.age(path, days)

    def test_the_newest_ten_stay_and_older_ones_go(self):
        self.make_sessions([2 + number for number in range(13)])
        (self.out / "desktop" / "instance.lock").write_text("lock", "utf-8")
        (self.out / "desktop" / "launcher.log").write_text("log", "utf-8")
        self.assertEqual(set(self.listed()), {"session-a11", "session-a12", "session-a13"})

    def test_a_session_whose_backend_is_still_running_stays(self):
        self.make_sessions([2 + number for number in range(13)], running=999)
        self.assertEqual(set(self.listed(pid_alive=lambda pid: pid == 999)), {"session-a11", "session-a13"})

    def test_sessions_less_than_a_day_old_stay_however_many_there_are(self):
        self.make_sessions([0.1 + number * 0.05 for number in range(13)])
        self.assertEqual(self.listed(), {})

    def test_a_session_without_a_readable_ready_file_counts_as_finished(self):
        self.make_sessions([2 + number for number in range(11)])
        (self.out / "desktop" / "session-a11" / "ready.json").write_text("{not json", "utf-8")
        self.assertEqual(set(self.listed()), {"session-a11"})


class ServiceLogTests(SweepBase):
    def make_logs(self):
        paths = {}
        for number in range(33):
            name = f"service-20260101-0000{number:02d}-1.log"
            path = self.out / "logs" / name
            path.write_text("log", "utf-8")
            paths[number] = path
            self.age(path, 20 if number < 2 else 3)
        rotated = self.out / "logs" / "service-20260101-000000-1.log.1"
        rotated.write_text("older part", "utf-8")
        self.age(rotated, 20)
        return paths

    def test_runs_beyond_the_newest_thirty_go_once_they_are_a_week_old_with_their_rotated_parts(self):
        self.make_logs()
        self.assertEqual(set(self.listed()), {"service-20260101-000000-1.log", "service-20260101-000000-1.log.1",
                                             "service-20260101-000001-1.log"})

    def test_the_log_of_the_run_that_is_asking_is_never_listed(self):
        paths = self.make_logs()
        self.assertEqual(set(self.listed(keep=[paths[1]])), {"service-20260101-000000-1.log",
                                                            "service-20260101-000000-1.log.1"})

    def test_files_that_are_not_service_logs_are_ignored(self):
        (self.out / "logs" / "notes.txt").write_text("mine", "utf-8")
        self.assertEqual(self.listed(), {})


class ApplyTests(SweepBase):
    def build(self):
        self.folder("workers", "kokoro-111", 2)
        self.folder("workers", "kokoro-222", 0.02)
        stale = self.folder("desktop", "session-old", 5)
        (stale / "backend.log").write_text("x" * 100, "utf-8")
        for number in range(10):
            self.folder("desktop", f"session-new{number}", 1.5 - number * 0.1 if number else 1.2)
        (self.out / "desktop" / "instance.lock").write_text("lock", "utf-8")
        (self.out / "desktop" / "launcher.log").write_text("log", "utf-8")
        for name in ("exports", "comparisons"):
            (self.out / name).mkdir()
            (self.out / name / "keep.txt").write_text("a person made this", "utf-8")

    def test_a_dry_run_changes_nothing_and_applying_removes_exactly_the_listed_folders(self):
        self.build()
        before = sorted(path.relative_to(self.out).as_posix() for path in self.out.rglob("*"))
        items = plan(self.out, now=NOW, pid_alive=lambda pid: False)
        self.assertEqual(sorted(path.relative_to(self.out).as_posix() for path in self.out.rglob("*")), before)
        self.assertEqual({item.path.name for item in items}, {"kokoro-111", "session-old"})
        totals = summarize(items)
        self.assertEqual((totals["count"], totals["by_kind"]["worker"]["count"], totals["by_kind"]["desktop"]["bytes"]),
                         (2, 1, 100))
        result = apply(items, self.out)
        self.assertEqual((result["removed"], result["bytes"], result["errors"]), (2, 100, []))
        after = sorted(path.relative_to(self.out).as_posix() for path in self.out.rglob("*"))
        self.assertEqual(set(before) - set(after), {"workers/kokoro-111", "desktop/session-old", "desktop/session-old/backend.log"})
        self.assertEqual(set(after) - set(before), set())
        self.assertEqual(plan(self.out, now=NOW, pid_alive=lambda pid: False), [])

    def test_a_tampered_plan_cannot_reach_outside_the_folders_it_owns(self):
        self.build()
        (self.out / "exports" / "kokoro-123").mkdir()
        wrong_name = self.folder("workers", "notes", 30)
        for item in (Item("worker", self.out / "exports" / "kokoro-123", "x", 0, 9),
                     Item("worker", self.out / "workers" / ".." / "exports" / "kokoro-123", "x", 0, 9),
                     Item("worker", wrong_name, "x", 0, 9),
                     Item("desktop", self.out / "workers" / "kokoro-111", "x", 0, 9),
                     Item("worker", self.out / "workers" / "kokoro-gone-1", "x", 0, 9),
                     Item("nonsense", self.out / "workers" / "kokoro-111", "x", 0, 9)):
            with self.subTest(path=item.path):
                result = apply([item], self.out)
                self.assertEqual(result["removed"], 0)
                self.assertEqual(len(result["errors"]), 1)
        self.assertTrue((self.out / "exports" / "kokoro-123").exists())
        self.assertTrue((self.out / "workers" / "kokoro-111").exists())
        self.assertTrue(wrong_name.exists())

    def test_a_link_is_neither_listed_nor_followed(self):
        target = Path(self.temp.name) / "precious"
        target.mkdir()
        (target / "keep.txt").write_text("keep", "utf-8")
        link = self.out / "workers" / "kokoro-999"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            # Windows lets any account make a junction, which is the other way a folder can point elsewhere.
            junction = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True) \
                if os.name == "nt" else None
            if junction is None or junction.returncode != 0:
                self.skipTest("this account cannot create symbolic links or junctions")
        self.age(link, 3)
        self.assertEqual(self.listed(), {})
        result = apply([Item("worker", link, "x", 0, 9)], self.out)
        self.assertEqual(result["removed"], 0)
        self.assertTrue((target / "keep.txt").exists())


class StartupSweepTests(SweepBase):
    class Log:
        def __init__(self):
            self.events = []

        def event(self, message):
            self.events.append(message)

    def test_the_sweep_reports_what_it_removed_and_stays_quiet_when_there_is_nothing(self):
        log = self.Log()
        with mock.patch.object(housekeeping, "pid_may_be_alive", lambda pid: False), \
                mock.patch.object(housekeeping.time, "time", lambda: NOW):
            self.assertEqual(run_startup_sweep(self.out, log)["removed"], 0)
            self.assertEqual(log.events, [])
            self.folder("workers", "kokoro-111", 2)
            result = run_startup_sweep(self.out, log, keep=[])
        self.assertEqual(result["removed"], 1)
        self.assertRegex(log.events[0], r"cleanup removed 1 stale folders and files")

    def test_a_failure_is_reported_and_never_raised(self):
        log = self.Log()
        with mock.patch.object(housekeeping, "plan", side_effect=RuntimeError("disk gone")):
            result = run_startup_sweep(self.out, log)
        self.assertEqual(result["removed"], 0)
        self.assertIn("disk gone", result["errors"][0])
        self.assertIn("could not be removed", log.events[0])


class ProcessCheckTests(unittest.TestCase):
    def test_this_process_is_alive_and_one_that_has_exited_is_not(self):
        self.assertTrue(pid_may_be_alive(os.getpid()))
        self.assertFalse(pid_may_be_alive(0))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(pid_may_be_alive(child.pid))

    @unittest.skipUnless(os.name == "nt", "os.kill only ends processes on Windows")
    def test_checking_a_process_never_signals_it(self):
        with mock.patch("os.kill", side_effect=AssertionError("os.kill ends the process on Windows")):
            self.assertTrue(pid_may_be_alive(os.getpid()))


class CommandLineTests(SweepBase):
    def run_tool(self, *arguments):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cleanup_runtime.main(["--out", str(self.out), *arguments])
        return code, buffer.getvalue()

    def test_it_lists_by_default_and_removes_only_when_asked(self):
        old = self.folder("workers", "kokoro-4000000", 5)  # a process number no machine reaches, so it is surely gone
        stamp = time.time() - 5 * DAY  # the tool reads the real clock, so age the folder against it
        os.utime(old, (stamp, stamp))
        code, text = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("worker folders: 1", text)
        self.assertIn("Nothing was removed", text)
        self.assertTrue(old.exists())
        code, text = self.run_tool("--apply")
        self.assertEqual(code, 0)
        self.assertIn("removed 1 items", text)
        self.assertFalse(old.exists())


class PoolScratchFolderTests(unittest.TestCase):
    """The worker pool takes the empty temporary-audio folder away with its worker, so none are left to sweep."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-scratch-")
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.object(worker_pool, "OUT", Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workers = Path(self.temp.name) / "workers"

    def pool(self):
        def factory(entry):
            log = Path(self.temp.name) / "logs" / f"{entry['id']}.log"
            command = [sys.executable, "-u", str(ROOT / "tests" / "fixtures" / "fake_worker.py"), "--engine", entry["id"]]
            return WorkerProcess(command, log, cwd=ROOT)

        pool = EnginePool("cpu", 0, worker_factory=factory, idle_poll_seconds=0.1)
        self.addCleanup(pool.close)
        return pool

    def folders(self):
        return sorted(path.name for path in self.workers.iterdir()) if self.workers.exists() else []

    def test_unloading_removes_the_folder_the_worker_used(self):
        pool = self.pool()
        pool.speak({"engine": "kokoro", "text": "hello"})
        (name,) = self.folders()
        self.assertRegex(name, r"kokoro-\d+\Z")
        pool.unload()
        self.assertEqual(self.folders(), [])

    def test_closing_the_pool_removes_it_too(self):
        pool = self.pool()
        pool.speak({"engine": "kokoro", "text": "hello"})
        self.assertEqual(len(self.folders()), 1)
        pool.close()
        self.assertEqual(self.folders(), [])

    def test_a_folder_with_something_left_in_it_is_kept_for_the_sweep(self):
        pool = self.pool()
        pool.speak({"engine": "kokoro", "text": "hello"})
        (name,) = self.folders()
        (self.workers / name / "left-by-a-crash.npy").write_bytes(b"x")
        pool.unload()
        self.assertEqual(self.folders(), [name])
        self.assertTrue(re.fullmatch(r"kokoro-\d+", name))


if __name__ == "__main__":
    unittest.main()
