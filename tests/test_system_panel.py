"""The System panel's service routes: the report, the storage table, and backups that run in the background."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ffmpeg_tools  # noqa: E402
import housekeeping  # noqa: E402
import library_backup  # noqa: E402
import serve  # noqa: E402
from app_version import VERSION  # noqa: E402
from evaluation_store import EvaluationStore  # noqa: E402
from request_log import RequestLog  # noqa: E402
from scene_store import SceneStore  # noqa: E402
from tests.library_fixture import PRIVATE_LINE, PRIVATE_NAME, PRIVATE_VOICE, build_library  # noqa: E402

DEAD_PID = 2_147_483_646
FFMPEG_STATUS = {"found": True, "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "version": "7.1", "decoders": {"aac": True},
                 "encoders": {"mp3": True, "aac": True, "vorbis": True, "flac": True}, "message": None}


class FakeEngines:
    device = "cpu"
    entries = {}
    api_entries = {}

    def engine_status(self):
        return {"device": "cpu", "engines": [{"id": "kokoro", "name": "Kokoro", "state": "usable"},
                                             {"id": "moss", "name": "MOSS", "state": "not_integrated"}]}


class SystemPanelBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-system-")
        self.base = Path(self.temp.name).resolve()
        self.root, self.app, self.out = self.base / "lab", self.base / "program", self.base / "program" / "out"
        self.app.mkdir()
        self.cast = build_library(self.root)
        self.backups = self.root / library_backup.BACKUP_FOLDER
        scenes = SceneStore(self.cast, {}, root=self.root)
        evaluations = EvaluationStore(self.root / library_backup.DATABASES["evaluations"], lab_root=self.root)
        self.log = RequestLog(self.out / "logs")
        self.ffmpeg = mock.patch.object(ffmpeg_tools, "status", return_value=dict(FFMPEG_STATUS))
        self.ffmpeg_status = self.ffmpeg.start()
        patches = [mock.patch.object(serve, "OUT", self.out), mock.patch.object(serve, "APP_ROOT", self.app)]
        for patch in patches:
            patch.start()
        self.patches = [self.ffmpeg, *patches]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        self.server.RequestHandlerClass = serve.make_handler(
            FakeEngines(), self.server.server_address[1], self.cast, scenes, None, evaluations, self.log)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        try:
            self.wait_task()
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            for patch in self.patches:
                patch.stop()
            self.log.close()
            self.temp.cleanup()

    def call(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def task(self):
        return self.call("/api/system/task")[1]["task"]

    def wait_task(self, timeout=60):
        """The state of the job once it is no longer running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self.task()
            if task["status"] != "running":
                return task
            time.sleep(0.02)
        self.fail("the job never finished")

    def run_job(self, path, body):
        status, answer = self.call(path, body)
        self.assertEqual(status, 202, answer)
        self.assertEqual(answer["task"]["status"], "running")
        return self.wait_task()

    def make_backup(self, **options):
        return library_backup.create(self.root, self.backups, app="Fixture", version="9.9.9", **options)

    def log_text(self, needle, timeout=3.0):
        """A line reaches the log when the job ends or the answer has gone out; give the server thread a moment."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.log.path.read_text("utf-8")
            if needle in text:
                return text
            time.sleep(0.02)
        self.fail(f"{needle!r} never reached the log:\n{self.log.path.read_text('utf-8')}")


class ReportRouteTests(SystemPanelBase):
    def test_the_report_names_the_installation_and_carries_the_text_to_copy(self):
        status, report = self.call("/api/system")
        self.assertEqual(status, 200)
        self.assertEqual(report["app"]["version"], VERSION)
        self.assertEqual(report["library"]["path"], str(self.root))
        self.assertEqual(report["library"]["counts"]["takes"], 2)
        self.assertEqual(report["system"]["device"], "cpu")
        self.assertTrue(report["ffmpeg"]["ready"])
        self.assertEqual(report["engines"]["counts"]["usable"], 1)
        self.assertEqual([row["id"] for row in report["engines"]["rows"]], ["kokoro"])
        self.assertEqual(report["log"]["file"], str(self.log.path))
        self.assertEqual(report["backups"]["count"], 0)
        text = report["diagnostics"]
        self.assertIn(f"{report['app']['name']} {VERSION} diagnostics", text)
        self.assertIn("kokoro: usable", text)
        for private in (PRIVATE_NAME, PRIVATE_VOICE, PRIVATE_LINE):
            self.assertNotIn(private, text)

    def test_checking_ffmpeg_again_is_asked_for_and_otherwise_the_remembered_answer_is_used(self):
        self.call("/api/system")
        self.ffmpeg_status.assert_called_with(refresh=False)
        self.call("/api/system?recheck=1")
        self.ffmpeg_status.assert_called_with(refresh=True)

    def test_the_report_changes_nothing(self):
        before = sorted(path.relative_to(self.base).as_posix() for path in self.base.rglob("*"))
        self.call("/api/system")
        self.call("/api/system/backups")
        self.assertEqual(sorted(path.relative_to(self.base).as_posix() for path in self.base.rglob("*")), before)

    def test_storage_is_measured_on_request_from_the_library_and_the_program_folders(self):
        (self.out / "conversions").mkdir(parents=True)
        (self.out / "conversions" / "a.wav").write_bytes(b"x" * 3000)
        self.backups.mkdir()
        (self.backups / "voice-lab-library-20261025-120000.zip").write_bytes(b"x" * 500)
        status, report = self.call("/api/system/storage")
        self.assertEqual(status, 200)
        self.assertEqual([row["key"] for row in report["library"]][:2], ["cast", "scenes"])
        self.assertGreater(report["library"][0]["bytes"], 0)
        self.assertEqual({row["key"]: row["bytes"] for row in report["other"]}["conversions"], 3000, "the program's own out folder")
        self.assertEqual({row["key"]: row["bytes"] for row in report["other"]}["backups"], 500, "the library's backup folder")
        self.assertEqual(report["cleanup"]["count"], 0)

    def test_every_system_route_answers_this_computer_only(self):
        stranger = {"Origin": "http://evil.example"}
        for path in ("/api/system", "/api/system/storage", "/api/system/backups", "/api/system/task"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, headers=stranger)[0], 403)
        for path in ("/api/system/backups", "/api/system/backups/verify", "/api/system/backups/restore",
                     "/api/system/use-library", "/api/system/cleanup"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, {}, headers=stranger)[0], 403)
        self.assertEqual(self.task()["status"], "idle")
        self.assertFalse(self.backups.exists())
        self.assertFalse((self.app / "desktop.local.json").exists())


class BackupJobTests(SystemPanelBase):
    def test_nothing_is_listed_until_a_backup_exists_and_the_plan_names_every_section(self):
        status, answer = self.call("/api/system/backups")
        self.assertEqual(status, 200)
        self.assertEqual(answer["backups"], [])
        self.assertEqual(answer["folder"], str(self.backups))
        self.assertEqual(answer["task"], {"status": "idle"})
        self.assertEqual([row["key"] for row in answer["plan"]["sections"]],
                         ["cast", "scenes", "evaluations", "settings", "voices", "exports", "drama"])
        self.assertRegex(answer["suggested_folder"], r"^lab-restored-\d{8}$")

    def test_a_backup_runs_in_the_background_and_then_shows_in_the_list(self):
        task = self.run_job("/api/system/backups", {})
        self.assertEqual((task["status"], task["kind"]), ("done", "backup"))
        result = task["result"]
        self.assertRegex(result["name"], library_backup.NAME)
        self.assertGreaterEqual(result["files"], 5)
        self.assertTrue((self.backups / result["name"]).is_file())
        self.assertTrue(library_backup.verify(self.backups / result["name"])["ok"])
        self.assertEqual(task["files_done"], task["files_total"])
        listing = self.call("/api/system/backups")[1]
        self.assertEqual([row["name"] for row in listing["backups"]], [result["name"]])
        row = listing["backups"][0]
        self.assertEqual((row["app_version"], row["files"]), (VERSION, result["files"]))
        self.assertGreater(row["bytes"], 0)
        text = self.log_text("backup finished")
        self.assertIn(f"backup finished: {result['name']}, {result['files']} files", text)
        for private in (PRIVATE_NAME, PRIVATE_VOICE, PRIVATE_LINE):
            self.assertNotIn(private, text)

    def test_only_the_sections_asked_for_go_in(self):
        self.root.joinpath("data", "voices", "narrator").mkdir(parents=True)
        self.root.joinpath("data", "voices", "narrator", "voice.wav").write_bytes(b"RIFF")
        self.root.joinpath("out", "exports", "package-1").mkdir(parents=True)
        self.root.joinpath("out", "exports", "package-1", "manifest.json").write_text("{}", "utf-8")
        task = self.run_job("/api/system/backups", {"include": ["voices"]})
        with zipfile.ZipFile(self.backups / task["result"]["name"]) as archive:
            names = set(archive.namelist())
        self.assertIn("library/data/voices/narrator/voice.wav", names)
        self.assertNotIn("library/out/exports/package-1/manifest.json", names)

    def test_a_request_that_names_no_sections_means_all_of_them(self):
        self.root.joinpath("out", "exports", "package-1").mkdir(parents=True)
        self.root.joinpath("out", "exports", "package-1", "manifest.json").write_text("{}", "utf-8")
        task = self.run_job("/api/system/backups", {})
        with zipfile.ZipFile(self.backups / task["result"]["name"]) as archive:
            self.assertIn("library/out/exports/package-1/manifest.json", archive.namelist())

    def test_a_bad_request_is_refused_before_anything_starts(self):
        for body, words in (({"include": "voices"}, "include must be a list"), ({"include": [1]}, "include must be a list"),
                            ({"include": ["nonsense"]}, "Unknown backup section: nonsense"),
                            ({"include": ["cast"]}, "Unknown backup section: cast")):
            with self.subTest(body=body):
                status, answer = self.call("/api/system/backups", body)
                self.assertEqual(status, 400)
                self.assertIn(words, answer["error"])
        self.assertEqual(self.task(), {"status": "idle"})
        self.assertFalse(self.backups.exists())

    def test_a_second_job_while_one_runs_is_refused_and_the_first_is_not_disturbed(self):
        existing = self.make_backup()["name"]
        started, release = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(30))
            return {"name": "voice-lab-library-20261025-120000.zip", "files": 1, "bytes": 1, "warnings": []}

        with mock.patch.object(library_backup, "create", slow):
            status, answer = self.call("/api/system/backups", {})
            self.assertEqual(status, 202)
            self.assertTrue(started.wait(10))
            try:
                for path, body in (("/api/system/backups", {}), ("/api/system/backups/verify", {"name": existing}),
                                   ("/api/system/backups/restore", {"name": existing, "folder": "second"})):
                    with self.subTest(path=path):
                        status, refused = self.call(path, body)
                        self.assertEqual(status, 409)
                        self.assertIn("already running", refused["error"])
                self.assertEqual(self.task()["status"], "running")
            finally:
                release.set()
            task = self.wait_task()
        self.assertEqual((task["status"], task["result"]["name"]), ("done", "voice-lab-library-20261025-120000.zip"))
        self.assertFalse((self.root / "restored").exists(), "the refused restore did nothing")

    def test_a_backup_that_cannot_be_made_ends_failed_with_the_reason_and_the_next_job_can_start(self):
        (self.root / library_backup.DATABASES["cast"]).unlink()
        task = self.run_job("/api/system/backups", {})
        self.assertEqual(task["status"], "failed")
        self.assertIn("nothing to back up", task["error"])
        self.assertIn("backup failed: BackupError", self.log_text("backup failed"))
        self.assertEqual(self.task()["status"], "failed", "the page can still read why")
        self.assertEqual(self.call("/api/system/backups", {})[0], 202, "a failed job does not block the next one")

    def test_an_unexpected_error_in_a_job_is_reported_not_swallowed(self):
        with mock.patch.object(library_backup, "create", side_effect=RuntimeError("the disk exploded")):
            task = self.run_job("/api/system/backups", {})
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error"], "It stopped unexpectedly: RuntimeError: the disk exploded")
        self.log_text("backup failed: RuntimeError: the disk exploded")


class CheckJobTests(SystemPanelBase):
    def test_check_reads_a_backup_in_full_and_says_it_is_fine(self):
        name = self.make_backup()["name"]
        task = self.run_job("/api/system/backups/verify", {"name": name})
        self.assertEqual((task["status"], task["kind"]), ("done", "check"))
        self.assertEqual((task["result"]["ok"], task["result"]["problems"], task["result"]["name"]), (True, [], name))
        self.log_text(f"check finished: {name} passed")

    def test_a_damaged_backup_is_a_finished_check_with_problems_not_a_failed_job(self):
        source = self.make_backup()
        damaged = self.backups / "voice-lab-library-20261025-130000.zip"
        with zipfile.ZipFile(source["path"]) as archive, zipfile.ZipFile(damaged, "w") as out:
            for info in archive.infolist():
                data = archive.read(info)
                if info.filename.endswith("preferences.json") or info.filename.startswith("library/data/cast/media/takes/"):
                    data = b"damaged" + data[7:]
                out.writestr(info.filename, data)
        task = self.run_job("/api/system/backups/verify", {"name": damaged.name})
        self.assertEqual(task["status"], "done")
        self.assertFalse(task["result"]["ok"])
        self.assertTrue(any("does not match its checksum" in problem for problem in task["result"]["problems"]), task["result"])
        self.log_text(f"check finished: {damaged.name} did not pass")

    def test_only_the_name_of_a_backup_in_the_backup_folder_is_accepted(self):
        self.make_backup()
        outside = self.base / "voice-lab-library-20260101-000000.zip"
        outside.write_bytes(b"not here")
        cases = ((None, "That is not the name of a backup."), ("", "That is not the name of a backup."),
                 ("../voice-lab-library-20260101-000000.zip", "That is not the name of a backup."),
                 ("voice-lab-library-1.zip", "That is not the name of a backup."), (7, "That is not the name of a backup."),
                 ("voice-lab-library-20200101-000000.zip", "There is no backup called voice-lab-library-20200101-000000.zip."))
        for name, words in cases:
            for path in ("/api/system/backups/verify", "/api/system/backups/restore"):
                with self.subTest(name=name, path=path):
                    body = {"name": name, "folder": "copy"} if path.endswith("restore") else {"name": name}
                    status, answer = self.call(path, body)
                    self.assertEqual((status, answer["error"]), (400, words))
        self.assertEqual(self.task(), {"status": "idle"})


class RestoreJobTests(SystemPanelBase):
    def snapshot(self, root):
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in Path(root).rglob("*")
                if path.is_file() and "backups" not in path.relative_to(root).parts and "restored" not in path.relative_to(root).parts}

    def test_a_restore_builds_a_checked_copy_in_a_new_folder_and_leaves_the_library_alone(self):
        name = self.make_backup()["name"]
        before = self.snapshot(self.root)
        task = self.run_job("/api/system/backups/restore", {"name": name, "folder": "second copy"})
        self.assertEqual(task["status"], "done", task)
        result = task["result"]
        target = (self.root / "restored" / "second copy").resolve()
        self.assertEqual((result["folder"], Path(result["state_root"])), ("second copy", target))
        self.assertEqual(library_backup.library_counts(target)["takes"], 2)
        self.assertEqual(result["counts"]["takes"], 2)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.snapshot(self.root), before, "the library in use was not touched")
        self.assertEqual(sorted(path.name for path in (self.root / "restored").iterdir()), ["second copy"], "no staging folder is left")
        self.log_text(f"restore finished: second copy, {result['files']} files")

    def test_a_folder_name_that_is_not_plain_words_is_refused_and_nothing_is_created(self):
        name = self.make_backup()["name"]
        for folder in ("", "..", ".", "a/b", "a\\b", "C:\\x", "con", "x" * 61, "trailing.", " leading", None, 5):
            with self.subTest(folder=folder):
                status, answer = self.call("/api/system/backups/restore", {"name": name, "folder": folder})
                self.assertEqual(status, 400)
                self.assertIn("letters, digits, spaces", answer["error"])
        self.assertFalse((self.root / "restored").exists())
        self.assertEqual(self.task(), {"status": "idle"})

    def test_a_restore_that_cannot_go_ahead_ends_failed_and_the_existing_folder_is_untouched(self):
        name = self.make_backup()["name"]
        taken = self.root / "restored" / "taken"
        taken.mkdir(parents=True)
        (taken / "mine.txt").write_text("keep me", "utf-8")
        task = self.run_job("/api/system/backups/restore", {"name": name, "folder": "taken"})
        self.assertEqual(task["status"], "failed")
        self.assertIn("already exists and is not empty", task["error"])
        self.assertEqual([path.name for path in taken.iterdir()], ["mine.txt"])
        self.assertEqual([path.name for path in (self.root / "restored").iterdir()], ["taken"])
        self.log_text("restore failed: BackupError")

    def test_a_damaged_backup_is_not_restored_and_leaves_nothing_behind(self):
        source = self.make_backup()
        damaged = self.backups / "voice-lab-library-20261025-130000.zip"
        with zipfile.ZipFile(source["path"]) as archive, zipfile.ZipFile(damaged, "w") as out:
            for info in archive.infolist():
                data = archive.read(info)
                out.writestr(info.filename, b"damaged" + data[7:] if info.filename.startswith("library/data/cast/media/takes/") else data)
        task = self.run_job("/api/system/backups/restore", {"name": damaged.name, "folder": "copy"})
        self.assertEqual(task["status"], "failed")
        self.assertIn("does not check out", task["error"])
        self.assertEqual(list((self.root / "restored").iterdir()), [])


class UseLibraryTests(SystemPanelBase):
    def restored(self, name):
        target = self.root / "restored" / name
        library_backup.restore(self.make_backup()["path"], target)
        return target.resolve()

    def settings(self):
        return json.loads((self.app / "desktop.local.json").read_text("utf-8"))

    def test_the_desktop_app_is_pointed_at_a_restored_folder_and_the_old_choice_is_remembered(self):
        first = self.restored("copy one")
        status, answer = self.call("/api/system/use-library", {"folder": "copy one"})
        self.assertEqual(status, 200)
        self.assertEqual((Path(answer["state_root"]), answer["previous"], Path(answer["settings_file"])),
                         (first, None, self.app / "desktop.local.json"))
        self.assertEqual(Path(self.settings()["state_root"]), first)
        second = self.restored("copy two")
        status, answer = self.call("/api/system/use-library", {"folder": "copy two"})
        self.assertEqual((status, Path(answer["state_root"]), Path(answer["previous"])), (200, second, first))
        self.assertEqual(Path(self.settings()["state_root"]), second)

    def test_other_settings_in_the_file_survive(self):
        self.restored("copy one")
        (self.app / "desktop.local.json").write_text(json.dumps({"window": {"width": 1200}}), "utf-8")
        self.call("/api/system/use-library", {"folder": "copy one"})
        self.assertEqual(self.settings()["window"], {"width": 1200})

    def test_a_folder_that_is_not_a_library_or_not_a_plain_name_is_refused_and_nothing_is_written(self):
        (self.root / "restored" / "empty").mkdir(parents=True)
        for folder, words in (("empty", "is not a library"), ("missing", "is not a library"), (str(self.root), "letters, digits, spaces"),
                              ("..", "letters, digits, spaces"), ("", "letters, digits, spaces")):
            with self.subTest(folder=folder):
                status, answer = self.call("/api/system/use-library", {"folder": folder})
                self.assertEqual(status, 400)
                self.assertIn(words, answer["error"])
        self.assertFalse((self.app / "desktop.local.json").exists())

    def test_a_settings_file_that_cannot_be_read_is_left_exactly_as_it_is(self):
        self.restored("copy one")
        broken = self.app / "desktop.local.json"
        broken.write_text("{not json", "utf-8")
        status, answer = self.call("/api/system/use-library", {"folder": "copy one"})
        self.assertEqual(status, 400)
        self.assertIn("cannot be read", answer["error"])
        self.assertEqual(broken.read_text("utf-8"), "{not json")


class CleanupRouteTests(SystemPanelBase):
    def setUp(self):
        super().setUp()
        if housekeeping.pid_may_be_alive(DEAD_PID):
            self.skipTest("this platform cannot say that the fixture's process number is unused")

    def stale_worker(self):
        folder = self.out / "workers" / f"kokoro-{DEAD_PID}"
        folder.mkdir(parents=True)
        old = time.time() - 2 * 86400
        os.utime(folder, (old, old))
        return folder

    def test_cleanup_removes_what_the_sweep_lists_says_so_and_touches_nothing_else(self):
        stale = self.stale_worker()
        status, result = self.call("/api/system/cleanup", {})
        self.assertEqual((status, result["removed"], result["errors"]), (200, 1, []))
        self.assertFalse(stale.exists())
        self.assertEqual(self.cast.list_cast()[0]["name"], PRIVATE_NAME, "the library is not part of cleanup")
        self.log_text("cleanup on request removed 1 items")

    def test_with_nothing_to_remove_it_removes_nothing(self):
        status, result = self.call("/api/system/cleanup", {})
        self.assertEqual((status, result), (200, {"removed": 0, "bytes": 0, "errors": []}))

    def test_the_log_of_the_service_that_is_running_is_never_removed(self):
        folder = self.out / "logs"
        for number in range(housekeeping.LOG_KEEP):  # thirty newer runs push this service's own log out of the kept ones
            (folder / f"service-29990101-0000{number:02d}-1.log").write_text("later run", "utf-8")
        old = time.time() - 30 * 86400
        os.utime(self.log.path, (old, old))
        plan = housekeeping.plan(self.out)
        self.assertEqual([item.path for item in plan], [self.log.path], "the fixture makes this log a candidate for removal")
        status, result = self.call("/api/system/cleanup", {})
        self.assertEqual((status, result["removed"], result["errors"]), (200, 0, []))
        self.assertTrue(self.log.path.is_file())


if __name__ == "__main__":
    unittest.main()
