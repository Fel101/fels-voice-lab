"""Setup.exe run over an installed Lab: update.py --after-setup, the releases' file lists tools/build_installer.py stages for
it, and the installer's check that the Lab is closed."""

import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_version import VERSION  # noqa: E402
from tools import build_installer, update  # noqa: E402

SCRIPT = (ROOT / "installer" / "FelsVoiceLab.iss").read_text(encoding="utf-8")
OLD = "0.0.1"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, command, **options):
        self.calls.append(command)
        return subprocess.CompletedProcess(command, 0)


class AfterSetupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # Version 0.0.1 as it shipped, and this version: it changed serve.py and the core's list, added a guide and no longer
        # has two old guides. Setup has copied this version's files; the old guides are still there, and one was changed here.
        self.old = {
            "serve.py": b"old serve",
            "requirements/core.txt": b"numpy==1\n",
            "docs/OLD.md": b"an old guide",
            "docs/EDITED.md": b"as it shipped",
        }
        self.new = {"serve.py": b"new serve", "requirements/core.txt": b"numpy==2\n", "docs/NEW.md": b"a new guide"}
        for path, data in {**self.old, **self.new, "docs/EDITED.md": b"changed here"}.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_bytes(data)
        (self.root / ".venv/Scripts").mkdir(parents=True)
        (self.root / ".venv/Scripts/python.exe").write_bytes(b"")
        self.lists = self.root / "releases.json"
        self.write_lists({OLD: self.old})

    def write_lists(self, releases):
        listing = lambda files: {path: sha(data) for path, data in files.items()}  # noqa: E731
        self.lists.write_text(
            json.dumps(
                {
                    "schema": update.SETUP_FILES_SCHEMA,
                    "this": listing(self.new),
                    "releases": {version: listing(files) for version, files in releases.items()},
                }
            ),
            encoding="utf-8",
        )

    def run_after_setup(self, version=OLD):
        args = SimpleNamespace(
            from_version=version, setup_files=self.lists, wheels=Path("W:/wheels"), summary=self.root / "summary.txt"
        )
        runner, said = Recorder(), []
        with mock.patch.object(update, "advice_items", return_value=[]):
            status = update.after_setup(self.root, args=args, runner=runner, say=said.append)
        return status, runner.calls, " ".join(" ".join(said).split())

    def test_the_files_this_version_dropped_go_unless_changed_here_and_the_update_is_recorded(self):
        status, calls, said = self.run_after_setup()
        self.assertEqual(status, 0)
        backup = self.root / "out/update" / f"{OLD}-to-{VERSION}" / "files"
        self.assertFalse((self.root / "docs/OLD.md").exists())
        self.assertEqual((backup / "docs/OLD.md").read_bytes(), b"an old guide")
        self.assertEqual((self.root / "docs/EDITED.md").read_bytes(), b"changed here", "a file changed here is left")
        self.assertTrue((self.root / "docs/NEW.md").is_file())
        record = update.read_record(self.root)
        self.assertEqual((record["method"], record["state"], record["from"], record["to"]), ("setup", "applied", OLD, VERSION))
        self.assertEqual((record["removed"], record["kept"]), (["docs/OLD.md"], ["docs/EDITED.md"]))
        self.assertEqual(
            record["changed"], ["docs/EDITED.md", "docs/NEW.md", "docs/OLD.md", "requirements/core.txt", "serve.py"]
        )
        self.assertIn("Left in place because you had changed them", said)
        self.assertIn(f"Updated from version {OLD} to {VERSION}.", said)
        self.assertNotIn("--undo", said, "Setup's update cannot be undone")

    def test_the_core_packages_come_from_setups_wheels(self):
        _, calls, _ = self.run_after_setup()
        self.assertEqual(
            calls,
            [
                [
                    str(self.root / ".venv/Scripts/python.exe"),
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "--disable-pip-version-check",
                    "--no-index",
                    "--find-links",
                    str(Path("W:/wheels")),
                    "-r",
                    str(self.root / "requirements/core.txt"),
                ]
            ],
        )

    def test_the_page_and_setups_last_page_say_what_it_did(self):
        advice = [("Kokoro's pinned packages changed. Install it again.", ["Kokoro"], ".venv-kokoro")]
        args = SimpleNamespace(from_version=OLD, setup_files=self.lists, wheels=None, summary=self.root / "summary.txt")
        with mock.patch.object(update, "advice_items", return_value=advice), contextlib.redirect_stdout(io.StringIO()):
            update.after_setup(self.root, args=args, runner=Recorder())
        notice = json.loads(update.notice_path(self.root).read_text(encoding="utf-8"))
        self.assertEqual((notice["state"], notice["from"], notice["to"]), ("updated", OLD, VERSION))
        self.assertEqual(notice["advice"][0]["models"], [{"engine": update.MODEL_ENGINES["Kokoro"], "name": "Kokoro"}])
        summary = (self.root / "summary.txt").read_text(encoding="ascii").strip().split("\n\n")
        self.assertEqual(
            summary[:2],
            [
                f"Updated from version {OLD} to {VERSION}. Your voices, scenes, models and settings are as they were.",
                "Some models need installing again before they speak:\n- Kokoro's pinned packages changed. Install it again.",
            ],
        )
        self.assertIn("docs/EDITED.md", summary[-1])

    def test_a_version_setup_has_no_list_for_removes_nothing_and_says_so(self):
        status, calls, said = self.run_after_setup("0.0.0-mine")
        self.assertEqual(status, 0)
        self.assertTrue((self.root / "docs/OLD.md").is_file())
        record = update.read_record(self.root)
        self.assertEqual((record["removed"], record["changed"]), ([], ["requirements/core.txt", "requirements/desktop.txt"]))
        notice = json.loads(update.notice_path(self.root).read_text(encoding="utf-8"))
        self.assertEqual(len(notice["problems"]), 1)
        self.assertIn("does not have the list of version 0.0.0-mine's files", notice["problems"][0])
        self.assertEqual(len(calls), 1, "the core's list is brought up to date all the same")

    def test_undo_refuses_an_update_setup_made(self):
        self.run_after_setup()
        with self.assertRaisesRegex(update.UpdateStopped, "installed by Setup.exe.*uninstall the Lab and keep your library"):
            update.undo(self.root, args=SimpleNamespace(dry_run=False, yes=True), processes=lambda root: [])

    def test_lists_that_cannot_be_read_stop_it_and_the_options_go_together(self):
        self.lists.write_text('{"schema": 99}', encoding="utf-8")
        with self.assertRaisesRegex(update.UpdateStopped, "not one this version can read"):
            self.run_after_setup()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            update.main(["--after-setup", "--from", OLD])


class ReleaseFileTests(unittest.TestCase):
    def test_the_release_tags_up_to_the_version_oldest_first(self):
        tags = b"v0.1.0\nv0.10.0\nv0.2.0\nv0.3.0\nv0.4.0-rc1\nnot-a-release\n"
        with mock.patch.object(build_installer, "git", return_value=tags):
            self.assertEqual(build_installer.release_tags("0.3.0"), ["v0.1.0", "v0.2.0", "v0.3.0"])
            self.assertEqual(build_installer.release_tags("0.10.0"), ["v0.1.0", "v0.2.0", "v0.3.0", "v0.10.0"])

    def test_the_lists_are_what_update_reads_and_a_clone_without_tags_stops_the_build(self):
        target = Path(self.enterContext(tempfile.TemporaryDirectory())) / "releases.json"
        hashes = lambda ref: {"serve.py": ref}  # noqa: E731
        with (
            mock.patch.object(build_installer, "release_tags", return_value=["v0.2.0", "v0.3.0"]),
            mock.patch.object(build_installer, "file_hashes", side_effect=hashes),
        ):
            self.assertEqual(build_installer.stage_release_files("HEAD", "0.3.0", target), ["v0.2.0", "v0.3.0"])
        lists = update.read_setup_files(target)
        self.assertEqual(lists["this"], {"serve.py": "HEAD"})
        self.assertEqual(
            lists["releases"], {"0.2.0": {"serve.py": "refs/tags/v0.2.0"}, "0.3.0": {"serve.py": "refs/tags/v0.3.0"}}
        )
        with (
            mock.patch.object(build_installer, "release_tags", return_value=[]),
            self.assertRaisesRegex(build_installer.BuildError, "fetch-depth: 0"),
        ):
            build_installer.stage_release_files("HEAD", "0.3.0", target)

    @unittest.skipUnless(shutil.which("git"), "git is needed")
    def test_a_commits_hashes_are_those_of_the_files_setup_installs(self):
        target = Path(self.enterContext(tempfile.TemporaryDirectory()))
        build_installer.stage_app("HEAD", target)
        staged = {path.relative_to(target).as_posix(): sha(path.read_bytes()) for path in target.rglob("*") if path.is_file()}
        self.assertEqual(build_installer.file_hashes("HEAD"), staged)


class InstallerScriptTests(unittest.TestCase):
    def test_setup_carries_the_lists_and_finishes_an_update_from_an_older_version(self):
        self.assertIn('Source: "{#Staging}\\releases.json"; DestDir: "{tmp}"', SCRIPT)
        self.assertRegex(SCRIPT, r"CompareVersions\(InstalledVersion, '\{#AppVersion\}'\) < 0\) then\s+if not SetUpStep")
        self.assertRegex(SCRIPT, r"tools\\update\.py'\) \+ '\" --after-setup --from ' \+ InstalledVersion \+ ' --setup-files")
        for option in ("--wheels", "--summary"):
            self.assertIn(f"'\" {option} \"'", SCRIPT)
        self.assertIn("function PrepareToInstall", SCRIPT)
        self.assertIn("Setup does not put an older version over a newer one", SCRIPT)

    def lab_check(self):
        """The Python the installer runs, with the Lab's own Python, to learn whether the Lab is open (LabIsRunning)."""
        joined = re.sub(r"' \+\s+'", "", SCRIPT)
        return re.search(r"'-c \"(.+?)\" \"' \+ Folder", joined).group(1)

    @unittest.skipUnless(sys.platform == "win32", "the Lab's programs are found the Windows way")
    def test_the_installers_check_says_whether_a_program_runs_from_the_labs_environments(self):
        lab = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("tools/update.py", "updates.py", "app_version.py"):
            (lab / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, lab / name)
        python = getattr(sys, "_base_executable", sys.executable)
        check = lambda: subprocess.run([python, "-c", self.lab_check(), str(lab)], cwd=lab).returncode  # noqa: E731
        self.assertEqual(check(), 0)
        # A program that runs from one of the Lab's environments: a copy of ping that waits, in .venv-test.
        (lab / ".venv-test").mkdir()
        ping = shutil.copy2(Path(shutil.which("ping")), lab / ".venv-test" / "ping.exe")
        waiting = subprocess.Popen([ping, "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL)
        self.addCleanup(waiting.wait)
        self.addCleanup(waiting.kill)
        self.assertEqual(check(), 3)


if __name__ == "__main__":
    unittest.main()
