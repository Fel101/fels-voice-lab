"""The system report: what a person sees about this installation, and the text they can paste into a bug report."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine_checks  # noqa: E402
import housekeeping  # noqa: E402
import library_backup  # noqa: E402
import system_report  # noqa: E402
from cast_store import CAST_SCHEMA_VERSION  # noqa: E402
from tests.library_fixture import PRIVATE_LINE, PRIVATE_NAME, PRIVATE_VOICE, build_library  # noqa: E402
from worker_pool import read_catalog  # noqa: E402

GB = 1024 ** 3
DEAD_PID = 2_147_483_646
Usage = namedtuple("Usage", "total used free")
FFMPEG = {"found": True, "ready": True, "version": "7.1", "message": None,
          "formats": {"wav": True, "flac": True, "ogg": True, "mp3": False, "m4a": True}}
ENGINES = {"device": "cuda", "engines": [
    {"id": "kokoro", "name": "Kokoro", "state": "usable", "check": {"status": "passed", "ran_on": "cpu", "real_time_factor": 0.276}},
    {"id": "turbo", "name": "Chatterbox Turbo", "state": "not_installed", "check": None},
    {"id": "moss", "name": "MOSS", "state": "not_integrated"}],
    "speed": [{"engine": "kokoro", "device": "cpu", "clips": 5, "median_real_time_factor": 0.25, "last_total_s": 1.0,
               "last_audio_seconds": 4.0, "last_measured_at": "t"}]}
HARDWARE = {"processor": "AMD Ryzen 7 PRO 8845HS w/ Radeon 780M Graphics",
            "graphics_cards": [{"name": "NVIDIA GeForce RTX 4070", "driver": "32.0.15.6094"}, {"name": "Card", "driver": None}]}


class TempBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-sysreport-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def home(self, folder=None):
        return mock.patch.object(system_report.Path, "home", return_value=folder or self.base)

    def report(self, **changes):
        report = {
            "generated_at": "2026-10-25T12:00:00+00:00", "app": {"name": "Fixture", "version": "9.9.9"},
            "system": {"python": "3.12.4", "platform": "Windows-11", "device": "cuda", "free_ram_gb": 21.5, "total_ram_gb": 31.7,
                       **HARDWARE},
            "library": {"path": str(self.base / "VoiceLab"), "schemas": {"evaluations": 2, "scenes": 1, "cast": 8},
                        "counts": {"characters": 3, "identities": 5, "references": 4, "takes": 120, "scenes": 2}},
            "ffmpeg": FFMPEG,
            "disk": {"library": {"free_bytes": 200 * GB, "total_bytes": 500 * GB, "low": False}, "install": None},
            "engines": system_report.engines_summary(ENGINES),
            "log": {"file": str(self.base / "lab" / "out" / "logs" / "service-1.log"), "folder": str(self.base / "lab"), "bytes": 1234},
            "backups": {"folder": str(self.base / "VoiceLab" / "backups"), "count": 2,
                        "latest": {"name": "voice-lab-library-20261025-120000.zip", "bytes": 5 * 1024 * 1024}}}
        report.update(changes)
        return report


class RedactTests(TempBase):
    def test_the_home_folder_reads_as_a_tilde_in_either_slash_style(self):
        home = self.base / "sam"
        with self.home(home):
            text = f"{home}{os.sep}Documents{os.sep}x.log and {home.as_posix()}/data/y.json, in {home}."
            self.assertEqual(system_report.redact(text), f"~{os.sep}Documents{os.sep}x.log and ~/data/y.json, in ~.")

    @unittest.skipUnless(os.name == "nt", "Windows paths ignore letter case")
    def test_on_windows_the_letter_case_does_not_matter(self):
        home = self.base / "Sam"
        with self.home(home):
            self.assertEqual(system_report.redact(str(home).upper() + "\\X"), "~\\X")
            self.assertEqual(system_report.redact(str(home).lower() + "/x"), "~/x")

    def test_a_longer_name_that_only_starts_like_the_home_folder_is_left_alone(self):
        home = self.base / "sam"
        with self.home(home):
            for other in (f"{home}2{os.sep}x", f"{home}-old{os.sep}x", f"{home}.bak{os.sep}x", f"{home}_x{os.sep}x"):
                with self.subTest(other=other):
                    self.assertEqual(system_report.redact(other), other)

    def test_text_that_holds_no_home_folder_and_a_backslash_in_the_replacement_are_both_safe(self):
        with self.home(self.base / "sam"):
            self.assertEqual(system_report.redact("C:\\Program Files\\FFmpeg"), "C:\\Program Files\\FFmpeg")
            self.assertEqual(system_report.redact(None), "None")
        with self.home(Path(self.base / "a" / "b")):  # a path with regex metacharacters elsewhere in the text
            self.assertEqual(system_report.redact("(a+b)*[x] " + str(self.base / "a" / "b") + os.sep + "z"), "(a+b)*[x] ~" + os.sep + "z")


class DiskTests(TempBase):
    def test_the_free_space_of_a_folder_that_does_not_exist_yet_is_that_of_the_nearest_folder_that_does(self):
        row = system_report.disk(self.base / "not" / "made" / "yet")
        self.assertGreater(row["free_bytes"], 0)
        self.assertGreaterEqual(row["total_bytes"], row["free_bytes"])
        self.assertEqual(row["low"], row["free_bytes"] < system_report.LOW_DISK_BYTES)

    def test_low_means_under_five_gigabytes(self):
        for free, low in ((system_report.LOW_DISK_BYTES - 1, True), (system_report.LOW_DISK_BYTES, False)):
            with self.subTest(free=free), mock.patch.object(shutil, "disk_usage", return_value=Usage(500 * GB, 0, free)):
                self.assertEqual(system_report.disk(self.base), {"free_bytes": free, "total_bytes": 500 * GB, "low": low})
        self.assertEqual(system_report.LOW_DISK_BYTES, 5 * GB)

    def test_a_drive_that_cannot_be_read_is_none(self):
        with mock.patch.object(shutil, "disk_usage", side_effect=OSError("not ready")):
            self.assertIsNone(system_report.disk(self.base))


class EnginesAndLogTests(TempBase):
    def test_models_are_counted_by_state_and_one_that_is_not_integrated_is_left_out(self):
        summary = system_report.engines_summary(ENGINES)
        self.assertEqual(summary["device"], "cuda")
        self.assertEqual([row["id"] for row in summary["rows"]], ["kokoro", "turbo"])
        self.assertEqual({key: count for key, count in summary["counts"].items() if count}, {"usable": 1, "not_installed": 1})
        self.assertEqual(list(summary["counts"])[:6], list(system_report.ENGINE_STATES))

    def test_each_model_says_where_its_last_test_ran_and_how_fast_and_the_speed_table_comes_along(self):
        summary = system_report.engines_summary(ENGINES)
        self.assertEqual(summary["rows"][0], {"id": "kokoro", "name": "Kokoro", "state": "usable", "ran_on": "cpu",
                                              "real_time_factor": 0.276})
        self.assertEqual(summary["rows"][1], {"id": "turbo", "name": "Chatterbox Turbo", "state": "not_installed"})
        self.assertEqual(summary["speed"], [{"engine": "kokoro", "device": "cpu", "clips": 5, "median_real_time_factor": 0.25}])

    def test_a_state_added_later_is_counted_and_named_not_dropped_or_crashed_on(self):
        payload = {"device": "cpu", "engines": [{"id": "x", "name": "X", "state": "brand_new"}]}
        summary = system_report.engines_summary(payload)
        self.assertEqual(summary["counts"]["brand_new"], 1)
        report = self.report(engines=summary)
        text = system_report.diagnostics_text(report)
        self.assertIn("Models (device cpu): 1 brand_new", text)
        self.assertIn("  x: brand_new", text)

    def test_the_real_status_payload_summarises_without_loading_anything(self):
        entries = {row["id"]: row for row in read_catalog()["engines"]}
        payload = engine_checks.status_payload(entries, device="cpu", busy=False, store=None,
                                               probe=lambda entry: {"installed": False, "missing": [], "environment": None, "checkpoint": None},
                                               fingerprint_of=lambda entry: "fingerprint")
        summary = system_report.engines_summary(payload)
        integrated = [row for row in payload["engines"] if row["integrated"]]
        self.assertEqual(len(summary["rows"]), len(integrated))
        self.assertEqual(sum(summary["counts"].values()), len(integrated))
        self.assertTrue(all(row["state"] in system_report.STATE_WORDS for row in summary["rows"]))

    def test_the_log_is_named_with_its_size_and_a_missing_file_still_names_itself(self):
        self.assertIsNone(system_report.log_info(None))
        log = self.base / "logs" / "service-1.log"
        log.parent.mkdir()
        log.write_bytes(b"x" * 321)
        self.assertEqual(system_report.log_info(log), {"file": str(log), "folder": str(log.parent), "bytes": 321})
        log.unlink()
        self.assertEqual(system_report.log_info(log), {"file": str(log), "folder": str(log.parent), "bytes": None})


class BuildTests(TempBase):
    def test_the_report_names_the_installation_the_library_and_its_backups(self):
        root, out = self.base / "lab", self.base / "program-out"
        build_library(root)
        made = library_backup.create(root, root / library_backup.BACKUP_FOLDER, app="Fixture", version="9.9.9")
        log = out / "logs" / "service-1.log"
        log.parent.mkdir(parents=True)
        log.write_text("start\n", encoding="utf-8")
        report = system_report.build(app="Fixture", version="9.9.9", state_root=root, out_dir=out, device="cpu", ffmpeg=FFMPEG,
                                     engine_status=ENGINES, log_path=log, free_ram=12.34, total_ram=31.66, hardware=HARDWARE)
        self.assertEqual(report["app"], {"name": "Fixture", "version": "9.9.9"})
        self.assertEqual(report["system"]["free_ram_gb"], 12.3)
        self.assertEqual(report["system"]["total_ram_gb"], 31.7)
        self.assertEqual((report["system"]["processor"], report["system"]["graphics_cards"]),
                         (HARDWARE["processor"], HARDWARE["graphics_cards"]))
        self.assertEqual(report["system"]["device"], "cpu")
        self.assertEqual(report["library"]["path"], str(root))
        self.assertEqual(report["library"]["schemas"]["cast"], CAST_SCHEMA_VERSION)
        self.assertEqual(set(report["library"]["schemas"]), {"cast", "scenes", "evaluations"})
        self.assertEqual({key: report["library"]["counts"][key] for key in ("characters", "identities", "takes", "scenes")},
                         {"characters": 1, "identities": 1, "takes": 2, "scenes": 0})
        self.assertEqual(report["ffmpeg"], FFMPEG)
        self.assertTrue(set(report["disk"]) == {"library", "install"} and all(row["free_bytes"] > 0 for row in report["disk"].values()))
        self.assertEqual(report["log"]["file"], str(log))
        self.assertEqual(report["backups"]["count"], 1)
        self.assertEqual(report["backups"]["latest"]["name"], made["name"])
        self.assertEqual(report["backups"]["folder"], str(root / "backups"))
        self.assertRegex(report["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertEqual(report["engines"]["counts"]["usable"], 1)

    def test_a_library_that_does_not_exist_yet_reports_nothing_in_it_and_does_not_create_it(self):
        root = self.base / "new-lab"
        report = system_report.build(app="Fixture", version="1", state_root=root, out_dir=self.base, device="cpu", ffmpeg=FFMPEG,
                                     engine_status=ENGINES, log_path=None, free_ram=1.0, hardware={})
        self.assertEqual(report["library"]["schemas"], {})
        self.assertEqual((report["system"]["total_ram_gb"], report["system"]["processor"], report["system"]["graphics_cards"]),
                         (None, None, []))
        self.assertEqual(report["library"]["counts"]["takes"], 0)
        self.assertEqual((report["backups"]["count"], report["backups"]["latest"], report["log"]), (0, None, None))
        self.assertFalse(root.exists(), "reading the report must not create the library")


class DiagnosticsTests(TempBase):
    def text(self, report=None, home=None):
        with self.home(home or self.base):
            return system_report.diagnostics_text(report or self.report())

    def test_the_text_is_the_facts_and_nothing_else_with_the_home_folder_as_a_tilde(self):
        sep = os.sep
        self.assertEqual(self.text(), "\n".join([
            "Fixture 9.9.9 diagnostics, 2026-10-25T12:00:00+00:00",
            "System: Python 3.12.4, Windows-11, device cuda, 21.5 GB RAM free of 31.7 GB",
            "Processor: AMD Ryzen 7 PRO 8845HS w/ Radeon 780M Graphics",
            "Graphics: NVIDIA GeForce RTX 4070 (driver 32.0.15.6094); Card",
            f"Library: ~{sep}VoiceLab",
            "  schemas: evaluations 2, scenes 1, cast 8",
            "  contents: 3 characters, 5 voice identities, 4 references, 120 takes, 2 scenes",
            "FFmpeg 7.1 found; ffprobe found; can write: WAV yes, FLAC yes, Ogg yes, MP3 no, AAC yes",
            "Disk: library drive 200.0 GB free of 500.0 GB; install drive unreadable",
            "Models (device cuda): 1 usable, 1 not installed",
            "  kokoro: usable (tested on cpu, 0.28 s per second of audio)",
            "  turbo: not installed",
            "Speed of recent clips: kokoro on cpu, 0.25 s per second of audio over 5 clips",
            f"Service log: ~{sep}lab{sep}out{sep}logs{sep}service-1.log (1234 bytes)",
            f"Backups: 2 in ~{sep}VoiceLab{sep}backups; newest voice-lab-library-20261025-120000.zip (5.0 MB)",
            "Not included: scripts, transcripts, names of characters or voices, and recordings. The home folder is shown as ~."]))

    def test_nothing_that_was_made_in_the_library_reaches_the_text(self):
        root = self.base / "lab"
        build_library(root)
        report = system_report.build(app="Fixture", version="9.9.9", state_root=root, out_dir=self.base, device="cpu", ffmpeg=FFMPEG,
                                     engine_status=ENGINES, log_path=None, free_ram=8.0, hardware=HARDWARE)
        text = self.text(report)
        for private in (PRIVATE_NAME, PRIVATE_VOICE, PRIVATE_LINE, "line-0"):
            self.assertNotIn(private, text)
        self.assertIn("2 takes", text)
        self.assertNotIn(str(self.base), text, "the folder the library sits in reads as ~")

    def test_what_is_missing_is_said_not_left_out(self):
        no_probe = {"found": True, "ready": False, "version": None, "message": "ffprobe missing",
                    "formats": {"wav": False, "flac": False, "ogg": False, "mp3": False, "m4a": False}}
        cases = {
            "FFmpeg (version unknown) found; ffprobe missing; can write: WAV no, FLAC no, Ogg no, MP3 no, AAC no": {"ffmpeg": no_probe},
            "FFmpeg not found": {"ffmpeg": {"found": False, "ready": False, "version": None, "formats": {}, "message": "gone"}},
            "Service log: none": {"log": None},
            "Models (device cpu): none integrated": {"engines": system_report.engines_summary({"device": "cpu", "engines": []})},
            "  schemas: no databases yet": {"library": {"path": "x", "schemas": {}, "counts": {}}},
            "Backups: 0 in ": {"backups": {"folder": "x", "count": 0, "latest": None}},
        }
        for expected, change in cases.items():
            with self.subTest(expected=expected):
                self.assertIn(expected, self.text(self.report(**change)))
        text = self.text(self.report(disk={"library": None, "install": None}))
        self.assertIn("Disk: library drive unreadable; install drive unreadable", text)

    def test_one_of_a_thing_is_said_in_the_singular(self):
        counts = {"characters": 1, "identities": 1, "references": 1, "takes": 1, "scenes": 1}
        text = self.text(self.report(library={"path": "x", "schemas": {}, "counts": counts}))
        self.assertIn("  contents: 1 character, 1 voice identity, 1 reference, 1 take, 1 scene", text)
        counts = dict.fromkeys(counts, 0)
        self.assertIn("  contents: 0 characters, 0 voice identities, 0 references, 0 takes, 0 scenes",
                      self.text(self.report(library={"path": "x", "schemas": {}, "counts": counts})))

    def test_a_library_that_cannot_be_counted_leaves_the_count_out_rather_than_saying_zero(self):
        counts = {"characters": 3, "identities": None, "references": 4, "takes": None, "scenes": 2}
        text = self.text(self.report(library={"path": "x", "schemas": {"cast": None}, "counts": counts}))
        self.assertIn("  contents: 3 characters, 4 references, 2 scenes", text)
        self.assertIn("  schemas: cast unreadable", text)


class StorageTests(TempBase):
    def setUp(self):
        super().setUp()
        self.root, self.out = self.base / "lab", self.base / "program-out"
        build_library(self.root)

    def put(self, folder, name, size):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_the_panel_lists_the_library_folders_the_run_folders_and_what_cleanup_would_remove(self):
        if housekeeping.pid_may_be_alive(DEAD_PID):
            self.skipTest("this platform cannot say that the fixture's process number is unused")
        self.put(self.out / "conversions", "a.wav", 3000)
        self.put(self.root / "backups", "voice-lab-library-20261025-120000.zip", 500)
        self.put(self.out / "logs", "service-20261025-120000-1.log", 70)
        stale = self.out / "workers" / f"kokoro-{DEAD_PID}"
        stale.mkdir(parents=True)
        old = time.time() - 2 * 86400
        os.utime(stale, (old, old))
        report = system_report.storage(self.root, self.out)
        self.assertEqual([row["key"] for row in report["library"]],
                         ["cast", "scenes", "evaluations", "settings", "voices", "exports", "drama"])
        self.assertEqual([row["optional"] for row in report["library"]], [False] * 4 + [True] * 3)
        cast = report["library"][0]
        self.assertGreater(cast["bytes"], 0)
        self.assertGreater(cast["files"], 2)
        other = {row["key"]: row["bytes"] for row in report["other"]}
        self.assertEqual(list(other), ["backups", "conversions", "logs", "workers", "desktop"])
        self.assertEqual((other["backups"], other["conversions"], other["logs"], other["desktop"]), (500, 3000, 70, 0))
        self.assertEqual(report["cleanup"]["count"], 1)
        self.assertEqual(report["cleanup"]["by_kind"]["worker"]["count"], 1)
        self.assertTrue(stale.exists(), "measuring removes nothing")

    def test_nothing_is_reported_for_folders_that_are_not_there(self):
        report = system_report.storage(self.base / "empty-lab", self.base / "no-out")
        self.assertEqual(sum(row["bytes"] for row in report["library"]), 0)
        self.assertEqual({row["bytes"] for row in report["other"]}, {0})
        self.assertEqual(report["cleanup"]["count"], 0)

    def test_the_drama_model_folder_is_never_walked(self):
        self.put(self.root / "out" / "drama3" / "models", "huge-model.gguf", 10)
        self.put(self.root / "out" / "drama3" / "studio" / "job-1", "job.json", 20)
        walked = []
        real = os.walk

        def spy(top, *args, **kwargs):
            walked.append(Path(top))
            return real(top, *args, **kwargs)

        with mock.patch.object(os, "walk", spy):
            report = system_report.storage(self.root, self.out)
        self.assertTrue(walked)
        self.assertFalse([path for path in walked if "models" in path.parts], walked)
        self.assertEqual(next(row for row in report["library"] if row["key"] == "drama")["bytes"], 20)


if __name__ == "__main__":
    unittest.main()
