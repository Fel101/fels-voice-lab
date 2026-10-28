"""tools/backup_library.py: back up, check, restore and switch libraries from a terminal."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_backup  # noqa: E402
from tests.library_fixture import build_library  # noqa: E402
from tools import backup_library  # noqa: E402

SCRIPT = ROOT / "tools" / "backup_library.py"


class CliBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-backup-cli-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.lab, self.app = self.base / "lab", self.base / "program"
        self.app.mkdir()
        build_library(self.lab)
        patch = mock.patch.object(backup_library, "ROOT", self.app)  # desktop.local.json is read and written beside the program
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *argv):
        """(exit status, what it printed, what it printed as an error)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = backup_library.main([str(item) for item in argv])
        return status, out.getvalue(), err.getvalue()

    def backup(self):
        status, out, _err = self.run_cli("create", "--state-root", self.lab)
        self.assertEqual(status, 0, out)
        return next((self.lab / library_backup.BACKUP_FOLDER).glob("*.zip"))

    def settings(self):
        return json.loads((self.app / "desktop.local.json").read_text("utf-8"))


class CreateTests(CliBase):
    def test_create_writes_a_checked_backup_beside_the_library_and_says_where(self):
        status, out, err = self.run_cli("create", "--state-root", self.lab)
        self.assertEqual((status, err), (0, ""))
        written = list((self.lab / "backups").glob("voice-lab-library-*.zip"))
        self.assertEqual(len(written), 1)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"Backed up {self.lab.resolve()}")
        self.assertEqual(Path(lines[1].strip()), written[0].resolve())
        self.assertRegex(lines[2], r"^  \d+ files, [\d.]+ (bytes|KB|MB); read back and checked\.$")
        self.assertTrue(library_backup.verify(written[0])["ok"])

    def test_the_destination_and_the_left_out_sections_are_the_callers_choice(self):
        (self.lab / "out" / "exports" / "p1").mkdir(parents=True)
        (self.lab / "out" / "exports" / "p1" / "manifest.json").write_text("{}", "utf-8")
        (self.lab / "data" / "voices" / "a").mkdir(parents=True)
        (self.lab / "data" / "voices" / "a" / "voice.wav").write_bytes(b"RIFF")
        destination = self.base / "elsewhere"
        status, out, _err = self.run_cli("create", "--state-root", self.lab, "--to", destination, "--without", "exports", "drama")
        self.assertEqual(status, 0, out)
        self.assertFalse((self.lab / "backups").exists())
        with zipfile.ZipFile(next(destination.glob("*.zip"))) as archive:
            names = set(archive.namelist())
        self.assertIn("library/data/voices/a/voice.wav", names)
        self.assertNotIn("library/out/exports/p1/manifest.json", names)

    def test_with_no_library_named_it_backs_up_the_one_the_desktop_app_opens(self):
        (self.app / "desktop.local.json").write_text(json.dumps({"state_root": str(self.lab)}), "utf-8")
        status, out, _err = self.run_cli("create")
        self.assertEqual(status, 0, out)
        self.assertEqual(out.splitlines()[0], f"Backed up {self.lab.resolve()}")
        self.assertEqual(len(list((self.lab / "backups").glob("*.zip"))), 1)

    def test_a_note_is_printed_when_something_was_left_out(self):
        with mock.patch.object(library_backup, "create", return_value={
                "path": "p.zip", "name": "p.zip", "bytes": 10, "files": 1, "warnings": ["2 items were left out: x (it is a link)."]}):
            status, out, _err = self.run_cli("create", "--state-root", self.lab)
        self.assertEqual(status, 0)
        self.assertIn("  note: 2 items were left out: x (it is a link).", out.splitlines())

    def test_an_unknown_section_is_refused_by_the_argument_parser(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            backup_library.main(["create", "--without", "nonsense"])
        self.assertEqual(stopped.exception.code, 2)

    def test_a_library_with_nothing_in_it_is_an_error_message_and_status_one(self):
        status, out, err = self.run_cli("create", "--state-root", self.base / "empty-lab")
        self.assertEqual((status, out), (1, ""))
        self.assertRegex(err, r"^error: There is nothing to back up yet")
        self.assertNotIn("Traceback", err)


class VerifyTests(CliBase):
    def test_a_good_backup_passes(self):
        status, out, _err = self.run_cli("verify", self.backup())
        self.assertEqual(status, 0)
        self.assertRegex(out, r"^voice-lab-library-\d{8}-\d{6}\.zip: every file matches its checksum and each database passes SQLite's "
                              r"integrity check \(\d+ files, [\d.]+ (bytes|KB|MB)\)\.\n$")

    def test_a_damaged_backup_fails_and_names_what_is_wrong(self):
        source = self.backup()
        damaged = self.base / "voice-lab-library-20261025-130000.zip"
        with zipfile.ZipFile(source) as archive, zipfile.ZipFile(damaged, "w") as out:
            for info in archive.infolist():
                data = archive.read(info)
                out.writestr(info.filename, b"damaged" + data[7:] if info.filename.startswith("library/data/cast/media/takes/") else data)
        status, out, _err = self.run_cli("verify", damaged)
        self.assertEqual(status, 1)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"{damaged.name} did not pass:")
        self.assertTrue(any("does not match its checksum" in line for line in lines[1:]), out)

    def test_a_file_that_is_not_a_backup_and_one_that_is_not_there_fail_plainly(self):
        stranger = self.base / "notes.zip"
        stranger.write_bytes(b"not a zip")
        for target in (stranger, self.base / "missing.zip"):
            with self.subTest(target=target.name):
                status, out, err = self.run_cli("verify", target)
                self.assertEqual((status, err), (1, ""))
                self.assertIn("did not pass", out)
                self.assertIn("cannot be read as a backup", out)


class RestoreAndUseTests(CliBase):
    def test_restore_builds_a_checked_copy_and_says_how_to_use_it(self):
        target = self.base / "restored-copy"
        status, out, err = self.run_cli("restore", self.backup(), target)
        self.assertEqual((status, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], f"Restored to {target.resolve()}")
        self.assertRegex(lines[1], r"^  \d+ files, 1 characters, 2 takes; every file matched its checksum and the databases opened\. "
                                   r"The library in use was not touched\.$")
        self.assertIn(f'serve.py --state-root "{target.resolve()}"', out)
        self.assertIn(f'backup_library.py use "{target.resolve()}"', out)
        self.assertEqual(library_backup.library_counts(target)["takes"], 2)
        self.assertFalse((self.app / "desktop.local.json").exists(), "nothing is switched unless asked")

    def test_restore_with_use_points_the_desktop_app_at_the_copy_and_names_the_old_choice(self):
        target = self.base / "restored-copy"
        status, out, _err = self.run_cli("restore", self.backup(), target, "--use")
        self.assertEqual(status, 0, out)
        self.assertEqual(Path(self.settings()["state_root"]), target.resolve())
        self.assertIn(f"The desktop app opens {target.resolve()} from its next start", out)
        self.assertIn("Before this it opened this project;", out)
        self.assertNotIn("serve.py --state-root", out)

    def test_use_switches_the_library_and_switching_back_names_where_it_was(self):
        first = self.base / "first"
        library_backup.restore(self.backup(), first)
        status, out, _err = self.run_cli("use", first)
        self.assertEqual(status, 0, out)
        status, out, _err = self.run_cli("use", self.lab)
        self.assertEqual(status, 0, out)
        self.assertEqual(Path(self.settings()["state_root"]), self.lab.resolve())
        self.assertIn(f"Before this it opened {first.resolve()};", out)

    def test_use_refuses_a_folder_that_is_not_a_library_and_writes_nothing(self):
        (self.base / "plain").mkdir()
        status, out, err = self.run_cli("use", self.base / "plain")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("is not a library", err)
        self.assertFalse((self.app / "desktop.local.json").exists())

    def test_restore_never_writes_into_a_folder_that_has_something_in_it(self):
        taken = self.base / "taken"
        taken.mkdir()
        (taken / "mine.txt").write_text("keep me", "utf-8")
        status, out, err = self.run_cli("restore", self.backup(), taken)
        self.assertEqual((status, out), (1, ""))
        self.assertIn("already exists and is not empty", err)
        self.assertEqual([path.name for path in taken.iterdir()], ["mine.txt"])

    def test_a_disk_error_is_a_message_and_status_one(self):
        with mock.patch.object(library_backup, "restore", side_effect=PermissionError("access is denied")):
            status, out, err = self.run_cli("restore", self.backup(), self.base / "x")
        self.assertEqual((status, out, err), (1, "", "error: access is denied\n"))


class ScriptTests(unittest.TestCase):
    def test_it_runs_as_a_script_from_any_folder_and_finds_the_library_module_not_itself(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-cli-script-") as folder:
            done = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=folder, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("create", done.stdout)
        self.assertIn("restore", done.stdout)
        self.assertIn("use", done.stdout)

    def test_a_missing_backup_is_a_failure_status_when_run_as_a_script(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-cli-script-") as folder:
            done = subprocess.run([sys.executable, str(SCRIPT), "verify", str(Path(folder) / "missing.zip")], capture_output=True,
                                  text=True, timeout=120)
        self.assertEqual(done.returncode, 1)
        self.assertIn("did not pass", done.stdout)


if __name__ == "__main__":
    unittest.main()
