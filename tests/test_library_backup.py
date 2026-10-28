"""Library backup and restore: one checked ZIP, a restore that builds a new folder and never touches the old one."""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_backup  # noqa: E402
from cast_store import CAST_SCHEMA_VERSION, CastStore  # noqa: E402
from evaluation_cast import import_cast_takes  # noqa: E402
from evaluation_store import SCHEMA_VERSION as EVALUATION_SCHEMA, EvaluationStore  # noqa: E402
from library_backup import BackupBusy, BackupError  # noqa: E402
from library_fixture import use_old_comparison_folder  # noqa: E402
from scene_store import SceneStore  # noqa: E402

RATE = 24_000
TERMS = {"transcript": "A clear reference line.", "origin": "backup fixture", "allowed_use": "tests only",
         "edits": {"target_sample_rate": RATE}}


def tone(seconds=1.5, hz=220.0):
    time_axis = np.arange(round(seconds * RATE)) / RATE
    return (0.1 * np.sin(2 * np.pi * hz * time_axis)).astype(np.float32)


def wav_bytes(seconds=1.5):
    buffer = io.BytesIO()
    sf.write(buffer, tone(seconds), RATE, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def tree(root):
    """Every file under root as {posix path: sha256}."""
    found = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def rewrite_zip(source, target, change=None, drop=(), add=None, manifest_change=None):
    """A copy of a backup with one thing changed: bytes of a member, a member removed, a member added, or the manifest."""
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as out:
        for info in archive.infolist():
            data = archive.read(info)
            if info.filename in drop:
                continue
            if info.filename == library_backup.MANIFEST and manifest_change:
                manifest = json.loads(data)
                manifest_change(manifest)
                data = json.dumps(manifest).encode()
            if change and info.filename in change:
                data = change[info.filename](data)
            out.writestr(info.filename, data)
        for name, data in (add or {}).items():
            out.writestr(name, data)


class LibraryBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-backup-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "lab"
        self.dest = self.base / "backups"
        self.cast = CastStore(self.root)
        character = self.cast.create_character("Backed up voice", "")
        cloned = self.cast.create_identity(character["id"], "qwen3-tts-1.7b-base", "Cloned")
        self.reference = self.cast.add_reference(cloned["id"], "ref.wav", wav_bytes(), **TERMS)
        preset = self.cast.create_identity(character["id"], "kokoro", "Preset")
        self.generation = self.cast.get_generation_identity(preset["id"])
        self.takes = [self.save_take(f"line-{index}", 1.0 + index * 0.25) for index in range(3)]
        SceneStore(self.cast, {}, root=self.root)
        EvaluationStore(self.root / library_backup.DATABASES["evaluations"], lab_root=self.root)
        self.put("out/comparisons/run-1/comparison.json", b'{"clips": 2}')
        self.put("out/scenes/scene-1/jobs/job-1.json", b'{"rows": 1}')
        self.put("out/exports/package-1/manifest.json", b'{"files": 1}')
        self.put("out/exports/package-1/line.flac", b"fLaC" + os.urandom(4096))
        self.put("data/voices/narrator/20260928-210223/voice.wav", wav_bytes(1.0))
        self.put("data/ui/preferences.json", b'{"theme": "dark"}')
        self.put("out/drama3/studio/job-1/job.json", b'{"status": "done"}')
        self.put("out/drama3/studio/runner.lock", b"locked")
        self.put("out/drama3/models/huge-model.gguf", b"not part of a backup")
        self.put("data/cast/migration-backups/cast-schema7-old.sqlite3", b"an old safety copy")
        self.put("data/cast/notes.tmp", b"scratch")
        self.put("data/unicode/ünï cödé.txt", b"outside the folders a backup copies")

    def save_take(self, line_id, seconds):
        return self.cast.create_take(self.generation, line_id=line_id, text="Hello there.", spoken_text="Hello there.",
                                     language="English", direction="", settings={},
                                     model={"backend": "fixture", "checkpoint_revision": "fixture"},
                                     wav=tone(seconds), sample_rate=RATE, generation_info={})

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def make(self, **options):
        options.setdefault("app", "Fixture")
        options.setdefault("version", "9.9.9")
        return library_backup.create(self.root, self.dest, **options)

    def members(self, path):
        with zipfile.ZipFile(path) as archive:
            return {name[len(library_backup.MEMBER_ROOT):] for name in archive.namelist()
                    if name.startswith(library_backup.MEMBER_ROOT)}

    def manifest(self, path):
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read(library_backup.MANIFEST))


class CreateTests(LibraryBase):
    def test_a_backup_holds_what_the_guide_says_to_keep_together_and_nothing_else(self):
        result = self.make()
        members = self.members(result["path"])
        self.assertTrue({"data/cast/cast.sqlite3", "data/scenes/scenes.sqlite3", "data/evaluations/evaluations.sqlite3",
                         "out/comparisons/run-1/comparison.json", "out/scenes/scene-1/jobs/job-1.json",
                         "out/exports/package-1/manifest.json", "out/exports/package-1/line.flac",
                         "data/voices/narrator/20260928-210223/voice.wav", "data/ui/preferences.json",
                         "out/drama3/studio/job-1/job.json"} <= members)
        self.assertTrue(any(name.startswith("data/cast/media/takes/") for name in members))
        self.assertTrue(any(name.startswith("data/cast/media/references/") for name in members))
        for left_out in ("out/drama3/studio/runner.lock", "out/drama3/models/huge-model.gguf", "data/cast/notes.tmp",
                         "data/cast/migration-backups/cast-schema7-old.sqlite3", "data/unicode/ünï cödé.txt"):
            self.assertNotIn(left_out, members)
        self.assertEqual(result["files"], len(members))
        self.assertEqual(sorted(path.name for path in self.dest.iterdir()), [result["name"]])
        outcome = library_backup.verify(result["path"])
        self.assertEqual((outcome["ok"], outcome["problems"]), (True, []))
        self.assertEqual(outcome["files"], len(members))

    def test_a_library_that_still_has_the_old_comparison_folder_is_backed_up_with_it(self):
        self.put("out/m5/old-run/comparison.json", b'{"clips": 1}')
        members = self.members(self.make()["path"])
        self.assertTrue({"out/comparisons/run-1/comparison.json", "out/m5/old-run/comparison.json"} <= members)

    def test_the_optional_sections_can_be_left_out(self):
        result = self.make(include=["voices"])
        members = self.members(result["path"])
        self.assertIn("data/voices/narrator/20260928-210223/voice.wav", members)
        self.assertFalse(any(name.startswith(("out/exports/", "out/drama3/")) for name in members))
        self.assertEqual(set(self.manifest(result["path"])["included"]), {"cast", "scenes", "evaluations", "settings", "voices"})
        with self.assertRaisesRegex(BackupError, "Unknown backup section: nonsense"):
            self.make(include=["nonsense"])

    def test_the_manifest_lists_every_file_with_its_size_and_checksum_and_the_schema_of_each_database(self):
        result = self.make()
        manifest = self.manifest(result["path"])
        self.assertEqual((manifest["format"], manifest["format_version"], manifest["app_version"]),
                         (library_backup.FORMAT, 1, "9.9.9"))
        self.assertEqual(manifest["databases"]["cast"], {"path": "data/cast/cast.sqlite3", "schema_version": CAST_SCHEMA_VERSION})
        by_path = {entry["path"]: entry for entry in manifest["files"]}
        take = self.takes[0]
        relative = "data/cast/media/takes/" + Path(self.cast.get_media_path("take", take["id"])).relative_to(
            self.root / "data/cast/media/takes").as_posix()
        audio = (self.root / relative).read_bytes()
        self.assertEqual(by_path[relative], {"path": relative, "size": len(audio), "sha256": hashlib.sha256(audio).hexdigest()})
        self.assertEqual(sum(row["files"] for row in manifest["sections"].values()), len(manifest["files"]))
        self.assertNotIn(str(self.root), json.dumps(manifest), "a backup file must not carry the folder it came from")

    def test_the_databases_are_copied_scenes_and_comparisons_first_and_cast_last(self):
        order = []
        real = library_backup._snapshot

        def spy(source, target, label):
            order.append(label)
            return real(source, target, label)

        with mock.patch.object(library_backup, "_snapshot", spy):
            self.make()
        self.assertEqual(order, ["Evaluations", "Scenes", "Cast"])

    def test_a_take_saved_while_the_files_are_copied_leaves_the_backup_consistent(self):
        real_add = library_backup._add
        added = []

        def add_and_save(archive, source, member, relative, report):
            if relative.startswith("data/cast/media/") and not added:
                added.append(self.save_take("late", 1.0))  # the databases were copied before the first media file
            return real_add(archive, source, member, relative, report)

        with mock.patch.object(library_backup, "_add", add_and_save):
            result = self.make()
        self.assertEqual(len(added), 1)
        outcome = library_backup.verify(result["path"])
        self.assertTrue(outcome["ok"], outcome["problems"])
        self.assertEqual(self.manifest(result["path"])["missing_media_total"], 0)
        restored = library_backup.restore(result["path"], self.base / "restored")
        self.assertEqual(restored["counts"]["takes"], 3, "the database copy was made before the late take existed")
        self.assertEqual(restored["warnings"], [])
        self.assertEqual(self.cast.list_takes(line_id="late")[0]["id"], added[0]["id"], "the library in use has it")

    def test_progress_counts_every_file_once_and_ends_at_the_total(self):
        calls = []
        self.make(progress=lambda *args: calls.append(args))
        self.assertTrue(calls)
        files = [call[0] for call in calls]
        self.assertEqual(files, sorted(files))
        self.assertEqual(calls[-1][0], calls[-1][1])
        self.assertLessEqual(calls[-1][2], calls[-1][3])

    def test_a_library_with_no_cast_database_has_nothing_to_back_up(self):
        with self.assertRaisesRegex(BackupError, "nothing to back up"):
            library_backup.create(self.base / "empty", self.dest)
        self.assertFalse(self.dest.exists())

    def test_the_folder_may_not_be_inside_what_is_copied(self):
        with self.assertRaisesRegex(BackupError, "inside data/cast"):
            library_backup.create(self.root, self.root / "data" / "cast" / "backups")
        with self.assertRaisesRegex(BackupError, "inside out/exports"):
            library_backup.create(self.root, self.root / "out" / "exports")
        library_backup.create(self.root, self.root / "backups")  # the default place, beside the folders it copies
        self.assertEqual(len(list((self.root / "backups").glob("*.zip"))), 1)

    def test_not_enough_free_space_is_refused_before_anything_is_written(self):
        usage = shutil.disk_usage(self.base)
        with mock.patch.object(library_backup.shutil, "disk_usage", return_value=usage._replace(free=1024)):
            with self.assertRaisesRegex(BackupError, "not enough free space"):
                self.make()
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_a_database_that_fails_sqlites_integrity_check_is_not_backed_up(self):
        with mock.patch.object(library_backup, "_snapshot", side_effect=BackupError("The Cast database fails SQLite's integrity check")):
            with self.assertRaisesRegex(BackupError, "integrity check"):
                self.make()
        self.assertEqual(list(self.dest.iterdir()), [], "no half-written zip and no scratch folder are left")

    def test_a_backup_that_does_not_verify_is_discarded(self):
        with mock.patch.object(library_backup, "verify", return_value={"ok": False, "problems": ["x does not match its checksum."]}):
            with self.assertRaisesRegex(BackupError, "did not check out, so it was discarded: x does not match"):
                self.make()
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_files_that_vanish_during_the_backup_are_noted_not_fatal(self):
        real_add = library_backup._add

        def add(archive, source, member, relative, report):
            if relative == "out/comparisons/run-1/comparison.json":
                raise FileNotFoundError(relative)
            return real_add(archive, source, member, relative, report)

        with mock.patch.object(library_backup, "_add", add):
            result = self.make()
        manifest = self.manifest(result["path"])
        self.assertEqual(manifest["skipped"], [{"path": "out/comparisons/run-1/comparison.json", "reason": "it was removed while the backup ran"}])
        self.assertTrue(any("left out" in warning for warning in result["warnings"]))
        self.assertTrue(library_backup.verify(result["path"])["ok"])

    def test_links_are_not_followed_out_of_the_library(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("not part of the library")
        link = self.root / "out" / "exports" / "linked"
        try:
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        except (ImportError, OSError):
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("this account cannot create links")
        result = self.make()
        self.assertFalse(any("secret.txt" in name for name in self.members(result["path"])))
        self.assertEqual([item["path"] for item in self.manifest(result["path"])["skipped"]], ["out/exports/linked"])

    def test_a_linked_file_is_not_followed_either(self):
        outside = self.base / "outside.txt"
        outside.write_text("not part of the library")
        try:
            os.symlink(outside, self.root / "out" / "exports" / "package-1" / "linked.txt")
        except (OSError, NotImplementedError):
            self.skipTest("this account cannot create file links")
        result = self.make()
        self.assertNotIn("out/exports/package-1/linked.txt", self.members(result["path"]))
        self.assertEqual([item["path"] for item in self.manifest(result["path"])["skipped"]],
                         ["out/exports/package-1/linked.txt"])

    def test_leftovers_of_a_cut_short_backup_go_once_a_day_old_and_nothing_else_does(self):
        self.dest.mkdir()
        old_part = self.dest / "voice-lab-library-20260101-000000.zip.part"
        fresh_part = self.dest / "voice-lab-library-20260102-000000.zip.part"
        old_scratch, mine = self.dest / ".backup-abc123", self.dest / "notes.txt"
        for path in (old_part, fresh_part, mine):
            path.write_bytes(b"x")
        old_scratch.mkdir()
        (old_scratch / "cast.sqlite3").write_bytes(b"x")
        stale = time.time() - 3 * 86400
        for path in (old_part, old_scratch):
            os.utime(path, (stale, stale))
        self.make()
        self.assertFalse(old_part.exists() or old_scratch.exists())
        self.assertTrue(fresh_part.exists() and mine.exists())

    def test_two_backups_in_one_second_get_different_names(self):
        folder = self.base / "names"
        folder.mkdir()
        self.assertEqual(library_backup._free_name(folder, "20261025-120000"), "voice-lab-library-20261025-120000.zip")
        (folder / "voice-lab-library-20261025-120000.zip").write_bytes(b"x")
        self.assertEqual(library_backup._free_name(folder, "20261025-120000"), "voice-lab-library-20261025-120000-2.zip")
        (folder / "voice-lab-library-20261025-120000-2.zip.part").write_bytes(b"x")  # one still being written counts
        self.assertEqual(library_backup._free_name(folder, "20261025-120000"), "voice-lab-library-20261025-120000-3.zip")
        self.assertTrue(all(library_backup.NAME.fullmatch(name) for name in (
            "voice-lab-library-20261025-120000.zip", "voice-lab-library-20261025-120000-3.zip")))

    def test_the_plan_adds_up_what_a_backup_will_hold(self):
        plan = library_backup.plan(self.root)
        rows = {row["key"]: row for row in plan["sections"]}
        self.assertTrue(all(rows[key]["included"] for key in ("cast", "scenes", "evaluations", "settings", "voices", "exports", "drama")))
        self.assertEqual(plan["files"], sum(row["files"] for row in plan["sections"]))
        self.assertEqual(self.make()["files"], plan["files"])
        smaller = library_backup.plan(self.root, include=[])
        self.assertFalse(any(row["included"] for row in smaller["sections"] if row["optional"]))
        self.assertLess(smaller["bytes"], plan["bytes"])
        self.assertGreater(rows["exports"]["bytes"], 4096)


class VerifyTests(LibraryBase):
    def setUp(self):
        super().setUp()
        self.good = Path(self.make()["path"])

    def broken(self, name, **changes):
        target = self.base / name
        rewrite_zip(self.good, target, **changes)
        return target

    def test_a_flipped_byte_in_a_media_file_is_found_and_named(self):
        take_member = next(name for name in zipfile.ZipFile(self.good).namelist() if "/media/takes/" in name)
        bad = self.broken("flipped.zip", change={take_member: lambda data: data[:-1] + bytes([data[-1] ^ 1])})
        outcome = library_backup.verify(bad)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["problems"], [f"{take_member[len('library/'):]} does not match its checksum."])

    def test_a_missing_member_an_unlisted_member_and_a_wrong_size_are_each_reported(self):
        outcome = library_backup.verify(self.broken("gone.zip", drop={"library/data/ui/preferences.json"}))
        self.assertEqual(outcome["problems"], ["data/ui/preferences.json is listed but missing from the backup."])
        outcome = library_backup.verify(self.broken("extra.zip", add={"library/data/cast/extra.bin": b"x"}))
        self.assertEqual(outcome["problems"], ["library/data/cast/extra.bin is in the backup file but is not listed in its manifest."])

        def lie(manifest):
            manifest["files"][0]["size"] += 1
        outcome = library_backup.verify(self.broken("size.zip", manifest_change=lie))
        self.assertRegex(outcome["problems"][0], "has the wrong size")

    def test_a_database_that_is_damaged_inside_a_consistent_backup_fails_the_integrity_check(self):
        with zipfile.ZipFile(self.good) as archive:
            damaged = bytearray(archive.read("library/data/cast/cast.sqlite3"))
        for page in range(2, len(damaged) // 4096, 2):  # zero every other page and keep the header readable
            damaged[page * 4096:(page + 1) * 4096] = b"\0" * 4096
        damaged = bytes(damaged)

        def fix(manifest):
            for entry in manifest["files"]:
                if entry["path"] == "data/cast/cast.sqlite3":
                    entry["sha256"] = hashlib.sha256(damaged).hexdigest()

        broken = self.broken("damaged.zip", change={"library/data/cast/cast.sqlite3": lambda _data: damaged}, manifest_change=fix)
        outcome = library_backup.verify(broken)
        self.assertFalse(outcome["ok"], "every checksum matches, but the database inside is not a sound database")
        self.assertRegex(" ".join(outcome["problems"]), "Cast database in the backup (cannot be opened|fails SQLite)")
        self.assertTrue(library_backup.verify(broken, deep=False)["ok"], "only the deep check reads inside the databases")
        with self.assertRaisesRegex(BackupError, "does not check out, so nothing was restored"):
            library_backup.restore(broken, self.base / "never")
        self.assertEqual([path.name for path in self.base.iterdir() if "restoring" in path.name or path.name == "never"], [])

    def test_names_that_could_escape_the_library_are_refused_even_with_a_matching_checksum(self):
        for bad in ("../outside.txt", "/etc/passwd", "C:/Windows/x.dll", "data\\cast\\x", "data/cast/../../x", "data/cast/CON",
                    "data/cast/aux.txt", "data/cast/trailing.", "data/cast/ spaced", "serve.py", "data/engines/checks.json",
                    "data/cast/" + "x" * 400, "out/drama3/models/Drama-35B.gguf"):
            with self.subTest(bad=bad):
                def plant(manifest, bad=bad):
                    manifest["files"].append({"path": bad, "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()})
                slip = self.broken("slip.zip", manifest_change=plant, add={"library/" + bad: b"x"})
                outcome = library_backup.verify(slip)
                self.assertFalse(outcome["ok"])
                self.assertRegex(outcome["problems"][0], "not allowed|outside the library folders")
                with self.assertRaisesRegex(BackupError, "not allowed|outside the library folders"):
                    library_backup.restore(slip, self.base / "slipped")
                self.assertFalse((self.base / "slipped").exists())
                self.assertEqual([path.name for path in self.base.iterdir() if "restoring" in path.name], [])
                self.assertFalse((self.base / "outside.txt").exists())

    def test_files_that_are_not_backups_are_refused_in_plain_words(self):
        garbage = self.base / "garbage.zip"
        garbage.write_bytes(b"this is not a zip file at all" * 10)
        empty = self.base / "empty.zip"
        empty.write_bytes(b"")
        plain = self.base / "plain.zip"
        with zipfile.ZipFile(plain, "w") as archive:
            archive.writestr("readme.txt", "just a zip")
        truncated = self.base / "truncated.zip"
        truncated.write_bytes(self.good.read_bytes()[:-2000])
        for path, words in ((garbage, "cannot be read as a backup"), (empty, "cannot be read as a backup"),
                            (plain, "has no BACKUP_MANIFEST.json"), (truncated, "cannot be read as a backup"),
                            (self.base / "missing.zip", "cannot be read as a backup")):
            with self.subTest(path.name):
                outcome = library_backup.verify(path)
                self.assertFalse(outcome["ok"])
                self.assertIn(words, outcome["problems"][0])
                with self.assertRaisesRegex(BackupError, words):
                    library_backup.restore(path, self.base / "nope")
        self.assertFalse((self.base / "nope").exists())

    def test_a_backup_from_a_newer_format_or_schema_says_to_update_the_app(self):
        def newer_format(manifest):
            manifest["format_version"] = 2
        outcome = library_backup.verify(self.broken("format.zip", manifest_change=newer_format))
        self.assertRegex(outcome["problems"][0], "newer version of the app \\(backup format 2\\)")

        def foreign(manifest):
            manifest["format"] = "something-else"
        self.assertRegex(library_backup.verify(self.broken("foreign.zip", manifest_change=foreign))["problems"][0], "not a Fel's Voice Lab")

        with zipfile.ZipFile(self.good) as archive:
            scratch = self.base / "newer.sqlite3"
            scratch.write_bytes(archive.read("library/data/cast/cast.sqlite3"))
        newer = sqlite3.connect(scratch)
        newer.execute(f"PRAGMA user_version={CAST_SCHEMA_VERSION + 1}")
        newer.commit()
        newer.close()
        data = scratch.read_bytes()

        def declare(manifest):
            manifest["databases"]["cast"]["schema_version"] = CAST_SCHEMA_VERSION + 1
            for entry in manifest["files"]:
                if entry["path"] == "data/cast/cast.sqlite3":
                    entry["size"], entry["sha256"] = len(data), hashlib.sha256(data).hexdigest()
        target = self.broken("schema.zip", change={"library/data/cast/cast.sqlite3": lambda _data: data}, manifest_change=declare)
        with self.assertRaisesRegex(BackupError, f"Cast database at schema {CAST_SCHEMA_VERSION + 1}, newer than"):
            library_backup.restore(target, self.base / "too-new")
        self.assertFalse((self.base / "too-new").exists())

    def test_duplicate_members_and_duplicate_manifest_entries_are_refused(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # zipfile warns when a name is written twice, which is the point here
            doubled = self.broken("dup-member.zip", add={"library/data/ui/preferences.json": b'{"theme": "light"}'})
        self.assertIn("appears more than once", " ".join(library_backup.verify(doubled)["problems"]))
        with self.assertRaisesRegex(BackupError, "nothing was restored"):
            library_backup.restore(doubled, self.base / "doubled")

        def twice(manifest):
            manifest["files"].append(dict(manifest["files"][0]))
        self.assertRegex(library_backup.verify(self.broken("dup-entry.zip", manifest_change=twice))["problems"][0], "lists .* twice")

    def test_a_manifest_that_describes_the_databases_wrongly_is_refused(self):
        def drop_cast(manifest):
            del manifest["databases"]["cast"]
        self.assertEqual(library_backup.verify(self.broken("no-cast.zip", manifest_change=drop_cast))["problems"],
                         ["This backup holds no Cast database."])

        def elsewhere(manifest):
            manifest["databases"]["cast"]["path"] = "data/scenes/scenes.sqlite3"
        self.assertEqual(library_backup.verify(self.broken("elsewhere.zip", manifest_change=elsewhere))["problems"],
                         ["The backup's manifest describes the cast database wrongly."])

        def unknown(manifest):
            manifest["databases"]["engines"] = {"path": "data/engines/checks.json", "schema_version": 1}
        self.assertEqual(library_backup.verify(self.broken("unknown.zip", manifest_change=unknown))["problems"],
                         ["The backup's manifest describes the engines database wrongly."])

    def test_folder_entries_in_the_zip_are_harmless(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            padded = self.broken("folders.zip", add={"library/data/": b"", "library/data/cast/": b""})
        self.assertTrue(library_backup.verify(padded)["ok"])

    def test_only_the_first_problems_are_listed(self):
        def many(manifest):
            for entry in manifest["files"]:
                entry["sha256"] = "1" * 64
        outcome = library_backup.verify(self.broken("many.zip", manifest_change=many))
        self.assertEqual(len(outcome["problems"]), library_backup.MAX_PROBLEMS + 1)
        self.assertRegex(outcome["problems"][-1], r"\.\.\. and \d+ more")

    def test_a_member_far_larger_than_the_manifest_says_is_refused_without_being_unpacked(self):
        bomb = b"\0" * (3 * 1024 * 1024)
        target = self.broken("bomb.zip", change={"library/data/ui/preferences.json": lambda _data: bomb})
        outcome = library_backup.verify(target)      # the manifest still says the file is 17 bytes
        self.assertEqual(outcome["problems"], ["data/ui/preferences.json has the wrong size (3145728 bytes, expected 17)."])
        with self.assertRaisesRegex(BackupError, "has the wrong size"):
            library_backup.restore(target, self.base / "bombed")
        self.assertFalse((self.base / "bombed").exists())

    def test_listing_shows_newest_first_with_a_summary_and_marks_a_file_that_does_not_read(self):
        second = Path(self.make()["path"])
        (self.dest / "voice-lab-library-20200101-000000.zip").write_bytes(b"not a zip")
        (self.dest / "unrelated.zip").write_bytes(b"x")
        rows = library_backup.list_backups(self.dest)
        self.assertEqual([row["name"] for row in rows], sorted([self.good.name, second.name, "voice-lab-library-20200101-000000.zip"], reverse=True))
        good = next(row for row in rows if row["name"] == self.good.name)
        self.assertEqual((good["app_version"], good["databases"]["cast"], good["bytes"]), ("9.9.9", CAST_SCHEMA_VERSION, self.good.stat().st_size))
        self.assertGreater(good["content_bytes"], good["bytes"] // 2)
        self.assertIn("problem", next(row for row in rows if row["name"].startswith("voice-lab-library-2020")))
        self.assertEqual(library_backup.list_backups(self.base / "no-such-folder"), [])

    def test_a_listing_shows_the_sections_in_their_usual_order(self):
        row = library_backup.list_backups(self.dest)[0]
        self.assertEqual(list(row["sections"]), ["cast", "scenes", "evaluations", "settings", "voices", "exports", "drama"])
        self.assertEqual(row["included"], list(row["sections"]))
        self.assertEqual(row["sections"]["cast"]["label"], "Cast voices, references and takes")
        self.assertEqual(sum(part["files"] for part in row["sections"].values()), row["files"])

    def test_a_listing_never_fails_on_a_manifest_with_odd_values(self):
        def odd(manifest):
            manifest.update(created_at={"not": "text"}, app_version="v" * 500, library_name=7, skipped_total="many",
                            missing_media_total=-3, included="cast", sections=[1, 2])
            manifest["files"] = [1, "a", {"size": "big"}, {"size": True}, {"size": 7}, {"size": -5}]
            manifest["databases"] = {"cast": 5, "scenes": {"schema_version": "x"}, "evaluations": {"schema_version": 2}, "bogus": {}}

        rewrite_zip(self.good, self.dest / "voice-lab-library-20300101-000000.zip", manifest_change=odd)
        row = next(item for item in library_backup.list_backups(self.dest) if item["name"].startswith("voice-lab-library-2030"))
        self.assertNotIn("problem", row)
        self.assertEqual((row["files"], row["content_bytes"]), (6, 7))
        self.assertEqual(row["databases"], {"scenes": None, "evaluations": 2})
        self.assertEqual((row["sections"], row["included"]), ({}, []))
        self.assertEqual((row["created_at"], row["library_name"], len(row["app_version"])), (None, None, 100))
        self.assertEqual((row["skipped"], row["missing_media"]), (0, 0))


class RestoreTests(LibraryBase):
    def setUp(self):
        super().setUp()
        self.backup = Path(self.make()["path"])

    def test_a_restore_builds_a_working_copy_and_leaves_the_library_in_use_untouched(self):
        before = tree(self.root)
        result = library_backup.restore(self.backup, self.base / "restored")
        self.assertEqual(tree(self.root), before)
        restored = self.base / "restored"
        self.assertEqual(result["state_root"], str(restored.resolve()))
        self.assertEqual(result["counts"], {"characters": 1, "identities": 2, "references": 1, "takes": 3, "scenes": 0})
        self.assertEqual(result["warnings"], [])
        original, copy = tree(self.root), tree(restored)
        for relative, digest in original.items():
            if relative in library_backup.DATABASES.values() or relative.startswith(("data/cast/migration-backups", "out/drama3/models",
                                                                                     "data/unicode")) or relative.endswith((".lock", ".tmp")):
                continue
            self.assertEqual(copy.get(relative), digest, relative)
        cast = CastStore(restored)
        self.assertEqual([item["name"] for item in cast.list_cast()], ["Backed up voice"])
        for take in self.takes:
            self.assertEqual(cast.get_media_path("take", take["id"]).read_bytes(), self.cast.get_media_path("take", take["id"]).read_bytes())
            self.assertEqual(cast.get_take(take["id"])["loudness"], take["loudness"])
        self.assertEqual(library_backup.schema_versions(restored)["cast"], CAST_SCHEMA_VERSION)
        self.assertEqual([path.name for path in self.base.iterdir() if "restoring" in path.name], [])

    def test_a_folder_that_has_anything_in_it_is_never_written_into(self):
        target = self.base / "occupied"
        target.mkdir()
        (target / "mine.txt").write_text("keep")
        with self.assertRaisesRegex(BackupError, "already exists and is not empty"):
            library_backup.restore(self.backup, target)
        self.assertEqual([path.name for path in target.iterdir()], ["mine.txt"])
        (self.base / "a-file").write_text("x")
        with self.assertRaisesRegex(BackupError, "already exists and is not empty"):
            library_backup.restore(self.backup, self.base / "a-file")

    def test_an_empty_folder_is_filled_and_a_missing_parent_is_made(self):
        empty = self.base / "empty"
        empty.mkdir()
        library_backup.restore(self.backup, empty)
        self.assertTrue((empty / "data" / "cast" / "cast.sqlite3").is_file())
        library_backup.restore(self.backup, self.base / "new" / "deeper" / "library")
        self.assertTrue((self.base / "new" / "deeper" / "library" / "data" / "cast" / "cast.sqlite3").is_file())

    def test_an_older_schema_is_moved_forward_in_the_copy_with_a_safety_copy_beside_it(self):
        db = sqlite3.connect(self.root / library_backup.DATABASES["cast"])
        db.execute("ALTER TABLE takes DROP COLUMN loudness_json")
        db.execute("DROP INDEX takes_parent")
        db.execute("ALTER TABLE takes DROP COLUMN edit_json")
        db.execute("ALTER TABLE takes DROP COLUMN parent_take_id")
        db.execute("PRAGMA user_version=7")
        db.commit()
        db.close()
        old = Path(self.make()["path"])
        self.assertEqual(self.manifest(old)["databases"]["cast"]["schema_version"], 7)
        result = library_backup.restore(old, self.base / "migrated")
        self.assertEqual(result["schemas"]["cast"], CAST_SCHEMA_VERSION)
        copies = list((self.base / "migrated" / "data" / "cast" / "migration-backups").glob("cast-schema7-*.sqlite3"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(CastStore(self.base / "migrated").get_take(self.takes[0]["id"])["loudness"], {"status": "not_measured", "meter": None})
        self.assertEqual(library_backup.schema_versions(self.root)["cast"], 7, "the library in use is still at its own schema")

    def test_a_backup_made_before_the_comparison_folder_was_renamed_restores_with_the_folder_and_database_moved(self):
        db_path = self.root / library_backup.DATABASES["evaluations"]
        run = import_cast_takes(self.cast, EvaluationStore(db_path, lab_root=self.root),
                                [take["id"] for take in self.takes[:2]], title="Old comparison")
        use_old_comparison_folder(db_path, self.root / "out")
        old = Path(self.make()["path"])
        self.assertEqual(self.manifest(old)["databases"]["evaluations"]["schema_version"], 2)
        members = self.members(old)
        self.assertIn(f"out/m5/{run['id']}/results.json", members)
        self.assertIn("out/m5/run-1/comparison.json", members)
        migrated = self.base / "migrated"
        result = library_backup.restore(old, migrated)
        self.assertEqual(result["schemas"]["evaluations"], EVALUATION_SCHEMA)
        self.assertEqual(result["warnings"], [])
        self.assertFalse((migrated / "out" / "m5").exists())
        self.assertTrue((migrated / "out" / "comparisons" / run["id"] / "results.json").is_file())
        self.assertTrue((migrated / "out" / "comparisons" / "run-1" / "comparison.json").is_file())
        restored = EvaluationStore(migrated / library_backup.DATABASES["evaluations"], lab_root=migrated)
        self.assertEqual(restored.migration_notes, [])
        samples = restored.get_run(run["id"])["samples"]
        self.assertEqual(len(samples), 2)
        self.assertTrue(all(restored.get_blind_audio_path(sample["id"]).is_file() for sample in samples))
        copies = list((migrated / "data" / "evaluations" / "migration-backups").glob("evaluations-schema2-*.sqlite3"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(library_backup.schema_versions(self.root)["evaluations"], 2, "the library in use is untouched")

    def test_media_the_library_was_already_missing_is_a_warning_and_a_loss_in_the_backup_is_a_failure(self):
        self.cast.get_media_path("take", self.takes[0]["id"]).unlink()
        gone = Path(self.make()["path"])
        manifest = self.manifest(gone)
        self.assertEqual(manifest["missing_media_total"], 1)
        result = library_backup.restore(gone, self.base / "with-warning")
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("already missing", result["warnings"][0])

        # A backup that lost a file the database needs, with its manifest rewritten to match, still fails.
        take_member = next(name for name in zipfile.ZipFile(self.backup).namelist()
                           if "/media/takes/" in name and self.takes[1]["id"] in name)
        lost = self.base / "lost.zip"

        def forget(data):
            data["files"] = [entry for entry in data["files"] if "library/" + entry["path"] != take_member]
        rewrite_zip(self.backup, lost, drop={take_member}, manifest_change=forget)
        self.assertTrue(library_backup.verify(lost)["ok"], "the zip itself is consistent")
        with self.assertRaisesRegex(BackupError, "missing from the backup, for example data/cast/media/takes/"):
            library_backup.restore(lost, self.base / "lost-library")
        self.assertFalse((self.base / "lost-library").exists())

    def test_restore_checks_free_space_first(self):
        usage = shutil.disk_usage(self.base)
        with mock.patch.object(library_backup.shutil, "disk_usage", return_value=usage._replace(free=1024)):
            with self.assertRaisesRegex(BackupError, "not enough free space to restore"):
                library_backup.restore(self.backup, self.base / "no-room")
        self.assertEqual([path.name for path in self.base.iterdir() if "no-room" in path.name], [])

    def test_a_restore_that_fails_late_leaves_no_folder_and_no_staging_folder(self):
        with mock.patch.object(library_backup, "_open_stores", side_effect=BackupError("This build cannot open the restored library: boom")):
            with self.assertRaisesRegex(BackupError, "cannot open the restored library"):
                library_backup.restore(self.backup, self.base / "late")
        self.assertEqual([path.name for path in self.base.iterdir() if "late" in path.name], [])

    def test_progress_runs_to_the_end(self):
        calls = []
        library_backup.restore(self.backup, self.base / "progress", progress=lambda *args: calls.append(args))
        self.assertEqual(calls[-1][0], calls[-1][1])
        self.assertEqual(calls[-1][2], calls[-1][3])

    def test_a_restore_never_writes_outside_its_own_folder(self):
        before = {path for path in self.base.rglob("*")}
        library_backup.restore(self.backup, self.base / "inside")
        added = {path for path in self.base.rglob("*")} - before
        self.assertTrue(all((self.base / "inside") in path.parents or path == self.base / "inside" for path in added))


class SmallLibraryTests(unittest.TestCase):
    def test_a_library_with_only_a_cast_database_backs_up_and_restores(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-small-") as temp:
            base = Path(temp)
            cast = CastStore(base / "lab")
            cast.create_character("Only voice", "")
            plan = library_backup.plan(base / "lab")
            self.assertEqual({row["key"]: row["files"] for row in plan["sections"]},
                             {"cast": 1, "scenes": 0, "evaluations": 0, "settings": 0, "voices": 0, "exports": 0, "drama": 0})
            result = library_backup.create(base / "lab", base / "backups", app="Fixture", version="1")
            self.assertEqual((result["files"], result["warnings"]), (1, []))
            self.assertEqual(list(library_backup.verify(result["path"])["databases"]), ["cast"])
            restored = library_backup.restore(result["path"], base / "back")
            self.assertEqual(restored["counts"]["characters"], 1)
            self.assertEqual(sorted(restored["schemas"]), ["cast", "evaluations", "scenes"],
                             "opening the restored library gave it the two stores the backup did not have yet")

    def test_a_backup_is_the_same_whichever_way_the_optional_sections_are_named(self):
        with self.assertRaisesRegex(BackupError, "Unknown backup section: cast"):
            library_backup.chosen_sections(["cast"])  # the required sections are always in, so naming one is a mistake
        self.assertEqual(library_backup.chosen_sections(None), library_backup.chosen_sections([]))
        self.assertEqual(library_backup.chosen_sections(library_backup.OPTIONAL) - library_backup.chosen_sections([]),
                         set(library_backup.OPTIONAL))


class NameTests(unittest.TestCase):
    def test_a_restore_folder_name_is_plain_and_lands_inside_the_library(self):
        library = Path("C:/library") if os.name == "nt" else Path("/library")
        for good in ("restored", "my library 2", "lab-2026.10_25", "A" * 60):
            self.assertEqual(library_backup.restore_target(library, good), library / "restored" / good)
        for bad in ("", "../x", "a/b", "a\\b", ".hidden", "name.", " lead", "trail ", "CON", "con.txt", "x" * 61, "naïve",
                    "a:b", None, 5):
            with self.subTest(bad=bad), self.assertRaisesRegex(BackupError, "letters, digits"):
                library_backup.restore_target(library, bad)

    def test_a_backup_name_must_be_a_backup_file_that_is_there(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-names-") as temp:
            folder = Path(temp)
            (folder / "voice-lab-library-20261025-120000.zip").write_bytes(b"x")
            (folder / "other.zip").write_bytes(b"x")
            self.assertEqual(library_backup.backup_path(folder, "voice-lab-library-20261025-120000.zip").name,
                             "voice-lab-library-20261025-120000.zip")
            for bad in ("other.zip", "../voice-lab-library-20261025-120000.zip", "voice-lab-library-20261025-120000.zip.part",
                        "voice-lab-library-1.zip", "", None):
                with self.subTest(bad=bad), self.assertRaisesRegex(BackupError, "not the name of a backup"):
                    library_backup.backup_path(folder, bad)
            with self.assertRaisesRegex(BackupError, "There is no backup called voice-lab-library-20261025-130000.zip"):
                library_backup.backup_path(folder, "voice-lab-library-20261025-130000.zip")

    def test_the_suggested_folder_name_is_dated_and_skips_one_that_exists(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-suggest-") as temp:
            library = Path(temp) / "lab"
            first = library_backup.suggested_folder(library)
            self.assertRegex(first, r"lab-restored-\d{8}\Z")
            (library / "restored" / first).mkdir(parents=True)
            self.assertEqual(library_backup.suggested_folder(library), first + "-2")
            self.assertEqual(library_backup.restore_target(library, first + "-2").parent, library / "restored")


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-switch-")
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name) / "app"
        self.app.mkdir()
        self.library = Path(self.temp.name) / "library"
        CastStore(self.library).list_cast()

    def test_the_launcher_setting_is_written_keeping_what_else_it_holds_and_the_old_value_is_reported(self):
        settings = self.app / "desktop.local.json"
        settings.write_text(json.dumps({"state_root": "old", "other": [1, 2]}), "utf-8")
        result = library_backup.use_library(self.app, self.library)
        self.assertEqual((result["previous"], result["state_root"]), ("old", str(self.library.resolve())))
        self.assertEqual(json.loads(settings.read_text("utf-8")), {"state_root": str(self.library.resolve()), "other": [1, 2]})
        self.assertFalse((self.app / "desktop.local.json.tmp").exists())
        self.assertEqual(library_backup.configured_state_root(self.app), self.library.resolve())

    def test_a_missing_settings_file_is_created_and_the_default_library_is_the_app_folder(self):
        self.assertEqual(library_backup.configured_state_root(self.app), self.app.resolve())
        self.assertIsNone(library_backup.use_library(self.app, self.library)["previous"])
        self.assertTrue((self.app / "desktop.local.json").is_file())

    def test_a_relative_setting_is_read_against_the_app_folder_like_the_launcher_does(self):
        (self.app / "desktop.local.json").write_text(json.dumps({"state_root": "data-here"}), "utf-8")
        self.assertEqual(library_backup.configured_state_root(self.app), (self.app / "data-here").resolve())

    def test_a_settings_file_that_cannot_be_read_is_never_overwritten(self):
        settings = self.app / "desktop.local.json"
        for text in ("{not json", "[1, 2]"):
            settings.write_text(text, "utf-8")
            with self.assertRaisesRegex(BackupError, "fix or delete it first"):
                library_backup.use_library(self.app, self.library)
            self.assertEqual(settings.read_text("utf-8"), text)

    def test_a_folder_without_a_cast_database_is_not_a_library(self):
        with self.assertRaisesRegex(BackupError, "is not a library"):
            library_backup.use_library(self.app, Path(self.temp.name) / "nothing")
        self.assertFalse((self.app / "desktop.local.json").exists())


class TaskTests(unittest.TestCase):
    def test_one_job_runs_at_a_time_with_progress_and_a_result(self):
        task = library_backup.Task()
        self.assertEqual(task.status(), {"status": "idle"})
        release, started = threading.Event(), threading.Event()

        def work(progress):
            progress(1, 4, 10, 40)
            started.set()
            release.wait(5)
            return {"ok": True}

        first = task.start("backup", "Backing up", work)
        self.assertEqual((first["status"], first["kind"]), ("running", "backup"))
        self.assertTrue(started.wait(5))
        self.assertEqual((task.status()["files_done"], task.status()["bytes_total"]), (1, 40))
        with self.assertRaisesRegex(BackupBusy, "backup is already running"):
            task.start("check", "Checking", work)
        release.set()
        deadline = time.monotonic() + 5
        while task.status()["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual((task.status()["status"], task.status()["result"]), ("done", {"ok": True}))
        task.start("check", "Checking", lambda progress: {"fine": 1})  # a finished job makes room for the next

    def test_a_refusal_and_a_crash_both_end_as_a_failure_with_words(self):
        for error, expected in ((BackupError("Plain refusal."), "Plain refusal."),
                                (RuntimeError("disk gone"), "It stopped unexpectedly: RuntimeError: disk gone")):
            task = library_backup.Task()

            def work(progress, error=error):
                raise error

            task.start("backup", "Backing up", work)
            deadline = time.monotonic() + 5
            while task.status()["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual((task.status()["status"], task.status()["error"]), ("failed", expected))


if __name__ == "__main__":
    unittest.main()
