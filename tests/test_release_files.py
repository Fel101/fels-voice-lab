"""The staged release: the workflow makes a draft, checks the files people will download from it, and only then publishes.

tools/check_release_files.py checks the draft's files (names, SHA256SUMS, attestations), tools/check_setup_update.ps1 installs
the downloaded Setup.exe over the release before it, and .github/workflows/release.yml runs both between the draft and the
publish. What only GitHub can show (the draft, the attestation, the runner) is checked when a release is made.
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import check_release_files  # noqa: E402
from tools.check_release_files import ReleaseFilesError  # noqa: E402

RELEASE = ROOT / ".github" / "workflows" / "release.yml"
VERIFY = ROOT / ".github" / "workflows" / "verify.yml"
UPDATE_CHECK = ROOT / "tools" / "check_setup_update.ps1"


def jobs(workflow):
    """Each job's name and its lines, from a workflow's `jobs:` section (two spaces in, then four)."""
    text = workflow.read_text(encoding="utf-8")
    body = text[text.index("\njobs:\n") + len("\njobs:\n") :]
    found = {}
    for block in re.split(r"\n(?=  [A-Za-z][\w-]*:\n)", body):
        name = block.split(":", 1)[0].strip()
        found[name] = block
    return found


class ReleaseFolder:
    def __init__(self, tag="v1.2.3"):
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name)
        self.installer = self.path / f"FelsVoiceLab-Setup-{tag[1:]}.exe"
        self.installer.write_bytes(b"MZ setup" * 1000)
        self.sums = self.path / "SHA256SUMS"
        self.sums.write_text(f"{check_release_files.sha256(self.installer)}  {self.installer.name}\n", encoding="utf-8")

    def close(self):
        self._temp.cleanup()


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.folder = ReleaseFolder()
        self.addCleanup(self.folder.close)

    def test_the_files_as_built_pass(self):
        self.assertEqual(check_release_files.check_files(self.folder.path, "v1.2.3"), [self.folder.installer, self.folder.sums])

    def test_a_setup_changed_after_its_sums_were_written_is_refused(self):
        with self.folder.installer.open("ab") as handle:
            handle.write(b"!")
        with self.assertRaisesRegex(ReleaseFilesError, "changed after SHA256SUMS was written.*Delete the draft"):
            check_release_files.check_files(self.folder.path, "v1.2.3")

    def test_a_missing_or_extra_file_is_named(self):
        (self.folder.path / "notes.md").write_text("x", encoding="utf-8")
        self.folder.sums.unlink()
        with self.assertRaisesRegex(ReleaseFilesError, "missing SHA256SUMS; not expected notes.md"):
            check_release_files.check_files(self.folder.path, "v1.2.3")

    def test_a_setup_for_another_version_is_refused(self):
        with self.assertRaisesRegex(ReleaseFilesError, "missing FelsVoiceLab-Setup-1.2.4.exe"):
            check_release_files.check_files(self.folder.path, "v1.2.4")

    def test_sums_must_name_the_setup_once(self):
        for text in ("", "nonsense\n", f"{'0' * 64}  other.exe\n", self.folder.sums.read_text(encoding="utf-8") * 2):
            with self.subTest(text=text[:20]):
                self.folder.sums.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ReleaseFilesError, "should hold one line"):
                    check_release_files.check_files(self.folder.path, "v1.2.3")

    def test_a_tag_that_is_not_a_release_tag_is_refused(self):
        for tag in ("1.2.3", "v1.2", "v1.2.3-beta", ""):
            with self.subTest(tag=tag), self.assertRaisesRegex(ReleaseFilesError, "is not a release tag"):
                check_release_files.check_files(self.folder.path, tag)


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self.folder = ReleaseFolder()
        self.addCleanup(self.folder.close)
        self.paths = [self.folder.installer, self.folder.sums]

    def test_each_file_must_be_attested_by_this_repositorys_release_workflow(self):
        commands = []

        def run(command):
            commands.append(command)
            return SimpleNamespace(returncode=0)

        check_release_files.check_attestations(self.paths, "owner/name", run=run, which=lambda name: "gh")
        self.assertEqual(
            commands,
            [
                ["gh", "attestation", "verify", str(path), "--repo", "owner/name"]
                + ["--signer-workflow", "owner/name/.github/workflows/release.yml"]
                for path in self.paths
            ],
        )

    def test_a_file_without_an_attestation_stops_the_release(self):
        with self.assertRaisesRegex(ReleaseFilesError, "SHA256SUMS has no attestation.*Do not publish"):
            check_release_files.check_attestations(
                self.paths,
                "owner/name",
                run=lambda command: SimpleNamespace(returncode=int(command[3].endswith("SHA256SUMS"))),
                which=lambda name: "gh",
            )

    def test_the_command_line_says_what_happened(self):
        tool = [sys.executable, str(ROOT / "tools" / "check_release_files.py"), "--dir", str(self.folder.path)]
        ok = subprocess.run(tool + ["--tag", "v1.2.3", "--no-attestation"], capture_output=True, text=True)
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "The release files for v1.2.3 are as built."))
        wrong = subprocess.run(tool + ["--tag", "v9.9.9", "--no-attestation"], capture_output=True, text=True)
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("missing FelsVoiceLab-Setup-9.9.9.exe", wrong.stderr)

    def test_without_gh_it_says_what_to_install(self):
        with self.assertRaisesRegex(FileNotFoundError, "install it or pass --no-attestation"):
            check_release_files.check_attestations(self.paths, "owner/name", which=lambda name: None)


class WorkflowTests(unittest.TestCase):
    def test_the_release_is_published_only_after_its_draft_is_checked(self):
        found = jobs(RELEASE)
        self.assertEqual(list(found), ["verify", "draft", "check", "publish"])
        for job, before in (("draft", "verify"), ("check", "draft"), ("publish", "check")):
            with self.subTest(job=job):
                self.assertIn(f"    needs: {before}\n", found[job])
        create = [line for line in found["draft"].splitlines() if "gh release create" in line]
        self.assertEqual(len(create), 1)
        self.assertIn("--draft", create[0])
        self.assertNotIn("gh release create", found["check"] + found["publish"])
        self.assertIn("gh release download", found["check"])
        self.assertIn("tools/check_release_files.py", found["check"])
        self.assertIn("./tools/check_setup_update.ps1", found["check"])
        self.assertNotIn("--draft=false", found["draft"] + found["check"])
        self.assertIn('gh release edit "$TAG" --repo "$GITHUB_REPOSITORY" --draft=false', found["publish"])

    def test_both_workflows_run_the_same_update_check(self):
        self.assertIn("./tools/check_setup_update.ps1", jobs(VERIFY)["installer"])
        self.assertIn("./tools/check_setup_update.ps1", jobs(RELEASE)["check"])

    def test_every_node_test_file_runs_on_github(self):
        self.assertIn('node --test "tests/*.test.cjs"', VERIFY.read_text(encoding="utf-8"))
        self.assertTrue(list((ROOT / "tests").glob("*.test.cjs")))

    @unittest.skipUnless(shutil.which("powershell") or shutil.which("pwsh"), "needs PowerShell")
    def test_the_update_check_is_a_powershell_script_without_errors(self):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        parse = (
            "$errors = $null; "
            f"$null = [System.Management.Automation.Language.Parser]::ParseFile('{UPDATE_CHECK}', [ref]$null, [ref]$errors); "
            "$errors | ForEach-Object { $_.Message }; exit $errors.Count"
        )
        result = subprocess.run([shell, "-NoProfile", "-Command", parse], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
