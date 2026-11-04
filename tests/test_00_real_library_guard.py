"""The guard that keeps tests off the real library.

This file is named to sort first: unittest imports test files in name order and importing real_library_guard installs the
guard, so it is in place before any other test runs. Every probe below lies in a folder that does not exist, so that even a
broken guard could only meet a harmless "no such folder" error and create nothing.
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import real_library_guard
from real_library_guard import LAB, RealLibraryError, protected

import library_backup
import serve
from cast_store import CastStore
from evaluation_store import EvaluationStore
from scene_store import SceneStore

DATA = LAB / "data"
MISSING = DATA / "guard-probe" / "not-a-folder"


class WhatIsGuardedTests(unittest.TestCase):
    def test_the_guard_is_in_place_before_any_test_runs(self):
        self.assertFalse(real_library_guard.install())

    def test_everything_under_data_and_the_library_outputs_is_guarded(self):
        for name in ("data", "data/cast/cast.sqlite3", "data/voices/a.wav", "out/scenes/x/y.wav",
                     "out/comparisons/run.json", "out/m5/run.json", "out/exports/package-1/manifest.json",
                     "out/drama3/studio/job/audio.wav", "desktop.local.json", "desktop.local.json.tmp"):
            with self.subTest(name):
                self.assertIsNotNone(protected(LAB / name))

    def test_scratch_folders_and_look_alikes_are_not_guarded(self):
        for name in ("out", "out/workers/kokoro-1/a.npy", "out/desktop/session-1", "out/logs/a.log", "data2/x", "database/x",
                     "out/scenes-old/x", "desktop.json", "serve.py", "tests/a.py", "docs/a.md"):
            with self.subTest(name):
                self.assertIsNone(protected(LAB / name))
        self.assertIsNone(protected(Path(tempfile.gettempdir()) / "a" / "data" / "x"))

    def test_the_library_backups_own_folders_are_all_guarded(self):
        for section in library_backup.SECTIONS:
            for folder in section.folders:
                with self.subTest(folder):
                    self.assertIsNotNone(protected(LAB / folder / "probe"), f"{folder} is in the backup but not guarded")

    def test_odd_values_are_not_paths_and_are_left_alone(self):
        for value in (None, 3, "", ":memory:", b"", object()):
            with self.subTest(repr(value)):
                self.assertIsNone(protected(value))

    def test_paths_in_every_spelling_are_recognised(self):
        self.assertIsNotNone(protected(str(DATA / "x")))
        self.assertIsNotNone(protected(os.fsencode(str(DATA / "x"))))
        self.assertIsNotNone(protected(os.path.relpath(DATA / "x")))
        self.assertIsNotNone(protected(str(DATA / "sub" / ".." / "x")))
        self.assertIsNotNone(protected(DATA.as_uri() + "/x.sqlite3?mode=ro"))
        self.assertIsNone(protected(Path(tempfile.gettempdir()).as_uri() + "/x.sqlite3?mode=ro"))

    @unittest.skipUnless(os.name == "nt", "Windows folder names ignore case")
    def test_windows_ignores_the_case_of_the_folder_name(self):
        self.assertIsNotNone(protected(str(DATA / "x").upper()))


class RefusalTests(unittest.TestCase):
    def refused(self, action):
        with self.assertRaises(RealLibraryError) as caught:
            action()
        return str(caught.exception)

    def test_writing_a_file_is_refused(self):
        for mode in ("w", "wb", "a", "x", "r+", "w+"):
            with self.subTest(mode):
                self.refused(lambda: open(MISSING / "a.txt", mode))
        self.refused(lambda: (MISSING / "a.txt").write_text("x"))
        self.refused(lambda: (MISSING / "a.bin").write_bytes(b"x"))
        self.refused(lambda: os.open(MISSING / "a.bin", os.O_WRONLY))
        self.refused(lambda: os.open(MISSING / "a.bin", os.O_WRONLY | os.O_CREAT))
        self.refused(lambda: os.open(MISSING / "a.bin", os.O_RDWR))
        self.refused(lambda: os.open(MISSING / "a.bin", os.O_CREAT | os.O_RDONLY))      # this creates the file too
        self.refused(lambda: os.open(MISSING / "a.bin", os.O_WRONLY | os.O_APPEND))

    def test_reading_is_allowed(self):
        with open(LAB / "serve.py", "rb") as handle:
            self.assertTrue(handle.read(1))
        with self.assertRaises(FileNotFoundError):                  # a plain missing-file error, not the guard's
            open(MISSING / "a.txt", "rb")
        with self.assertRaises(FileNotFoundError):
            os.open(MISSING / "a.bin", os.O_RDONLY)
        self.assertFalse(MISSING.exists())

    def test_creating_removing_and_renaming_are_refused(self):
        self.refused(lambda: MISSING.mkdir())
        self.refused(lambda: os.mkdir(MISSING))
        self.refused(lambda: os.rmdir(MISSING))
        self.refused(lambda: os.remove(MISSING / "a.txt"))
        self.refused(lambda: (MISSING / "a.txt").unlink())
        self.refused(lambda: os.rename(MISSING / "a.txt", MISSING / "b.txt"))
        self.refused(lambda: os.replace(MISSING / "a.txt", MISSING / "b.txt"))

    def test_moving_a_file_into_or_out_of_the_library_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            outside = Path(temp) / "a.txt"
            self.refused(lambda: os.rename(outside, MISSING / "a.txt"))
            self.refused(lambda: os.rename(MISSING / "a.txt", outside))

    def test_opening_a_database_is_refused_even_to_read(self):
        self.refused(lambda: sqlite3.connect(MISSING / "a.sqlite3"))
        self.refused(lambda: sqlite3.connect(str(MISSING / "a.sqlite3")))
        self.refused(lambda: sqlite3.connect(MISSING.as_uri() + "/a.sqlite3?mode=ro", uri=True))
        with tempfile.TemporaryDirectory() as temp:                 # a database anywhere else, and one in memory, are fine
            sqlite3.connect(Path(temp) / "a.sqlite3").close()
        sqlite3.connect(":memory:").close()

    def test_the_message_says_what_to_use_instead(self):
        message = self.refused(lambda: (MISSING / "a.txt").write_text("x"))
        self.assertIn("write to", message)
        self.assertIn("guard-probe", message)
        self.assertIn("temporary library", message)
        self.assertIn("library_fixture.temp_cast", message)

    def test_a_catch_all_handler_cannot_hide_it(self):
        self.assertFalse(issubclass(RealLibraryError, Exception))

        def answers_500():
            try:
                (MISSING / "a.txt").write_text("x")
            except Exception:
                return 500

        self.refused(answers_500)

    def test_nothing_was_created_by_the_probes(self):
        self.assertFalse((DATA / "guard-probe").exists())


class StoresAndHandlerTests(unittest.TestCase):
    def test_a_store_with_no_root_is_refused_before_it_creates_anything(self):
        existed = {name: (DATA / name).exists() for name in ("cast", "scenes", "evaluations")}
        with self.assertRaises(RealLibraryError):
            CastStore().list_cast()                        # a Cast store opens its database when first used
        with self.assertRaises(RealLibraryError):
            SceneStore(CastStore(), {})                    # the other two open theirs when they are built
        with self.assertRaises(RealLibraryError):
            EvaluationStore()
        self.assertEqual({name: (DATA / name).exists() for name in existed}, existed)

    def test_a_handler_with_no_stores_is_refused(self):
        with self.assertRaises(RealLibraryError):
            serve.make_handler(types.SimpleNamespace(entries={}), 0)

    def test_a_handler_on_a_temporary_cast_store_keeps_everything_in_that_library(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            cast = CastStore(root)
            serve.make_handler(types.SimpleNamespace(entries={}), 0, cast)
            cast.list_cast()                               # a Cast store opens its database when first used
            for database in ("cast/cast.sqlite3", "scenes/scenes.sqlite3", "evaluations/evaluations.sqlite3"):
                self.assertTrue((root / "data" / database).is_file(), database)

    def test_the_appearance_setting_is_saved_in_that_library_too(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
            server.RequestHandlerClass = serve.make_handler(types.SimpleNamespace(entries={}), server.server_address[1],
                                                            CastStore(root))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/preferences",
                                             data=json.dumps({"theme": "dark"}).encode(),
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(json.loads((root / "data" / "ui" / "preferences.json").read_text(encoding="utf-8")),
                             {"theme": "dark", "check_updates": False})


class ShutdownTests(unittest.TestCase):
    def test_a_server_that_never_served_can_be_shut_down_without_waiting(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)
        done = threading.Event()
        threading.Thread(target=lambda: (server.shutdown(), done.set()), daemon=True).start()
        self.assertTrue(done.wait(5), "shutdown() waited for a server that was never serving")

    def test_a_serving_server_still_stops(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        server.shutdown()
        thread.join(5)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
