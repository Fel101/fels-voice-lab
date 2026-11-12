"""Setup.exe: what tools/build_installer.py stages and passes to Inno Setup, and what installer/FelsVoiceLab.iss does with it.
Inno Setup itself is not run here; the CI installer job builds, installs and uninstalls it."""

import io
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app_version  # noqa: E402
from tools import build_installer, setup_core, uninstall_lab  # noqa: E402

SCRIPT = (ROOT / "installer" / "FelsVoiceLab.iss").read_text(encoding="utf-8")


def git_available():
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True).returncode == 0
    except OSError:
        return False


class WheelTests(unittest.TestCase):
    def test_the_wheels_are_every_pin_of_the_core_and_the_window_and_setups_pip(self):
        wanted = build_installer.pinned(build_installer.REQUIREMENTS)
        for name in build_installer.REQUIREMENTS:
            for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    self.assertIn(line, wanted)
        self.assertIn(setup_core.PIP, wanted)
        self.assertEqual(len(wanted), len(set(wanted)), "a pin both lists share is fetched once")
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", item) for item in wanted), wanted)

    def test_a_wheel_is_found_by_its_normalised_name_and_version(self):
        self.assertEqual(build_installer.wheel_name("typing_extensions==4.16.0"), "typing_extensions-4.16.0-")
        self.assertEqual(build_installer.wheel_name("Proxy-Tools==0.1.0"), "proxy_tools-0.1.0-")
        self.assertEqual(build_installer.wheel_name("zope.interface==7.0"), "zope_interface-7.0-")

    def test_pip_makes_the_wheels_and_a_missing_one_stops_the_build(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-wheels-") as folder:
            target = Path(folder)
            made = []

            def runner(command):
                made.append(command)
                for item in command[command.index("--wheel-dir") + 2 :]:
                    if not item.startswith("scipy"):
                        name, version = item.split("==")
                        (target / f"{name}-{version}-py3-none-any.whl").write_bytes(b"")
                return SimpleNamespace(returncode=0)

            with self.assertRaisesRegex(build_installer.BuildError, r"No wheel was made for scipy==\S+\."):
                build_installer.stage_wheels(target, runner=runner)
            self.assertEqual(made[0][1:5], ["-m", "pip", "wheel", "--no-deps"])
            (target / "scipy-1.17.1-cp312-cp312-win_amd64.whl").write_bytes(b"")
            self.assertIn("scipy==1.17.1", build_installer.stage_wheels(target, runner=runner))


@unittest.skipUnless(git_available(), "needs git and this repository's history")
class StagingTests(unittest.TestCase):
    def test_the_app_is_the_commits_files_as_a_release_zip_holds_them(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-stage-") as folder:
            target = Path(folder) / "app"
            count = build_installer.stage_app("HEAD", target)
            tracked = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True,
                                     text=True, check=True).stdout.split()  # fmt: skip
            self.assertEqual(count, len(tracked))
            self.assertTrue((target / "installer" / "FelsVoiceLab.iss").is_file())
            self.assertIn(b"\r\n", (target / "setup.bat").read_bytes(), "setup.bat keeps its CRLF, as in the ZIP")
            self.assertNotIn(b"\r\n", (target / "serve.py").read_bytes())
            self.assertFalse((target / "out").exists() or (target / ".venv").exists())

    def test_the_version_comes_from_the_commit(self):
        self.assertRegex(build_installer.version_at("HEAD"), r"^\d+\.\d+\.\d+")


class InnoTests(unittest.TestCase):
    def test_every_define_the_script_uses_is_passed_by_the_build(self):
        command = build_installer.iscc_command(Path("ISCC.exe"), version="9.9.9", staging=Path("S"), output=Path("O"))
        passed = {item[2:].split("=", 1)[0] for item in command if item.startswith("/D")}
        used = set(re.findall(r"\{#(\w+)\}", SCRIPT)) - set(re.findall(r"^#define (\w+)", SCRIPT, re.MULTILINE))
        self.assertEqual(used, passed)
        self.assertIn(f"/DRepository={app_version.REPOSITORY}", command)
        self.assertEqual(command[-1], str(build_installer.SCRIPT))

    def test_it_installs_for_this_user_into_a_short_folder_outside_onedrive(self):
        self.assertIn("PrivilegesRequired=lowest\n", SCRIPT)
        self.assertIn("DefaultDirName={localappdata}\\FelsVoiceLab\n", SCRIPT)
        for name in setup_core.ONEDRIVE_VARIABLES:
            self.assertIn(f"'{name}'", SCRIPT, "the folder page warns with setup_core's rule")
        self.assertEqual(len(re.findall(r"Names\[\d\] := '", SCRIPT)), len(setup_core.ONEDRIVE_VARIABLES))

    def test_it_sets_up_the_core_and_the_window_from_the_wheels_it_carries(self):
        self.assertIn('DestDir: "{tmp}\\wheels"', SCRIPT)
        self.assertRegex(SCRIPT, r"tools\\setup_core\.py'\) \+ '\" --wheels")
        self.assertRegex(SCRIPT, r"tools\\setup_desktop\.py'\) \+ '\" --wheels \"' .* '\" --no-shortcut'")
        self.assertIn("function GetCustomSetupExitCode", SCRIPT)

    def test_uninstalling_asks_and_keeps_the_library_unless_told_otherwise(self):
        self.assertIn("MB_YESNO, IDYES) = IDYES then\n      Choice := '--keep-library'\n    else\n      Choice := '--everything';",
                      SCRIPT)  # fmt: skip
        self.assertIn("tools\\uninstall_lab.py", SCRIPT)
        for option in ("--keep-library", "--everything"):
            with self.subTest(option), tempfile.TemporaryDirectory(prefix="voice-lab-uninstall-") as folder:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(uninstall_lab.main([option, "--root", folder]), 0)

    def test_the_shortcuts_open_the_desktop_window_as_setup_desktop_makes_it(self):
        shortcut = (ROOT / "tools" / "create-shortcut.ps1").read_text(encoding="utf-8")
        self.assertIn(".venv-desktop\\Scripts\\pythonw.exe", shortcut)
        self.assertIn('#define WindowPython "{app}\\.venv-desktop\\Scripts\\pythonw.exe"', SCRIPT)
        self.assertEqual(SCRIPT.count('Parameters: """{app}\\desktop.py"""'), 3, "Start menu, desktop and the last page")

    def test_the_checksums_are_in_sha256sum_form(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-sums-") as folder:
            path = Path(folder) / "FelsVoiceLab-Setup-9.9.9.exe"
            path.write_bytes(b"abc")
            self.assertEqual(build_installer.checksums([path]),
                             "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  FelsVoiceLab-Setup-9.9.9.exe\n")  # fmt: skip

    def test_a_missing_inno_setup_says_how_to_get_it(self):
        with mock.patch.object(build_installer, "ISCC_PLACES", ()), mock.patch.object(build_installer.shutil, "which",
                                                                                      return_value=None), \
                mock.patch.dict(build_installer.os.environ, {"ISCC": ""}):  # fmt: skip
            with self.assertRaisesRegex(build_installer.BuildError, "winget install JRSoftware.InnoSetup"):
                build_installer.find_iscc()


if __name__ == "__main__":
    unittest.main()
