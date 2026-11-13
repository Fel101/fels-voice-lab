"""tools/setup_core.py, which setup.bat runs. It finishes a core install that was interrupted, never changes a .venv that is
already there, and warns before it installs into a folder OneDrive syncs. venv and pip are stand-ins, so nothing is installed."""
import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("setup_core", ROOT / "tools" / "setup_core.py")
setup_core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_core)


def temporary_folder(case):
    temp = tempfile.TemporaryDirectory(prefix="voice-lab-setup-", ignore_cleanup_errors=True)
    case.addCleanup(temp.cleanup)
    return Path(temp.name)


@unittest.skipUnless(sys.version_info[:2] == (3, 12), "setup_core.py runs only on Python 3.12")
class SetupCoreTests(unittest.TestCase):
    def setUp(self):
        self.root = temporary_folder(self)
        self.environment = self.root / ".venv"
        self.commands, self.built, self.failing = [], [], None
        for patch in (mock.patch.object(setup_core, "ROOT", self.root),
                      mock.patch.object(setup_core.subprocess, "run", self.run_command),
                      mock.patch.object(setup_core.venv, "EnvBuilder", self.builder),
                      mock.patch.object(setup_core, "onedrive_folder", lambda root: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_command(self, command, cwd=None):
        """Stands in for subprocess.run. A command that contains `self.failing` exits with 1."""
        text = " ".join(str(part) for part in command[1:])
        self.commands.append(text)
        return SimpleNamespace(returncode=1 if self.failing and self.failing in text else 0)

    def builder(self, **options):
        built = self.built

        class Builder:
            def create(self, folder):
                built.append(Path(folder))
                (Path(folder) / "Scripts").mkdir(parents=True, exist_ok=True)
                (Path(folder) / "Scripts" / "python.exe").write_bytes(b"")
        return Builder()

    def setup(self):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            setup_core.main()
        return printed.getvalue()

    def pip_commands(self):
        return [text for text in self.commands if text.startswith("-m pip install")]

    def test_a_new_install_builds_the_environment_and_leaves_no_unfinished_mark(self):
        self.assertIn("Core ready.", self.setup())
        self.assertEqual(self.built, [self.environment])
        self.assertEqual(len(self.pip_commands()), 2)
        self.assertTrue(self.pip_commands()[1].endswith("core.txt"))
        self.assertFalse((self.environment / setup_core.UNFINISHED).exists())

    def test_setup_exe_installs_the_core_from_the_wheels_it_carries_with_no_index(self):
        wheels = self.root / "wheels"
        with contextlib.redirect_stdout(io.StringIO()):
            setup_core.main(["--wheels", str(wheels)])
        self.assertEqual(len(self.pip_commands()), 2)
        for command in self.pip_commands():
            self.assertIn(f"--no-index --find-links {wheels}", command)
        self.assertTrue(self.pip_commands()[0].endswith(setup_core.PIP))

    def test_an_interrupted_install_is_finished_by_the_next_run(self):
        self.failing = "core.txt"
        with self.assertRaisesRegex(SystemExit, "run setup.bat again: it continues where it stopped"):
            self.setup()
        self.assertTrue((self.environment / setup_core.UNFINISHED).is_file())
        self.failing, self.commands = None, []
        self.setup()
        self.assertEqual(len(self.built), 2)
        self.assertEqual(len(self.pip_commands()), 2)
        self.assertFalse(any("import numpy" in text for text in self.commands), "an unfinished install is not checked, finished")
        self.assertFalse((self.environment / setup_core.UNFINISHED).exists())

    def test_a_working_venv_is_checked_and_left_alone(self):
        (self.environment / "Scripts").mkdir(parents=True)
        (self.environment / "Scripts" / "python.exe").write_bytes(b"")
        self.assertIn("Existing .venv verified and preserved.", self.setup())
        self.assertEqual(self.built, [])
        self.assertEqual(self.pip_commands(), [])

    def test_a_venv_that_cannot_load_the_core_is_not_changed_and_the_message_says_what_to_do(self):
        (self.environment / "Scripts").mkdir(parents=True)
        (self.environment / "Scripts" / "python.exe").write_bytes(b"")
        self.failing = "import numpy"
        with self.assertRaisesRegex(SystemExit, "delete the .venv folder and run setup.bat again"):
            self.setup()
        self.assertEqual((self.built, self.pip_commands()), ([], []))

    def test_a_folder_inside_onedrive_gets_a_warning_before_anything_is_installed(self):
        with mock.patch.object(setup_core, "onedrive_folder", lambda root: "D:\\Sync\\OneDrive"), \
                mock.patch.object(setup_core.sys, "stdin", SimpleNamespace(isatty=lambda: False)):
            printed = self.setup()
        self.assertTrue(printed.startswith("This folder is inside OneDrive (D:\\Sync\\OneDrive)."), printed)
        self.assertIn("Core ready.", printed)


class OneDriveTests(unittest.TestCase):
    def test_the_onedrive_folder_is_named_only_when_it_holds_the_lab(self):
        root = temporary_folder(self)
        lab = root / "OneDrive" / "Desktop" / "voice-lab"
        lab.mkdir(parents=True)
        for environ, expected in (({"OneDrive": str(root / "OneDrive")}, str(root / "OneDrive")),
                                  ({"OneDriveCommercial": str(root / "OneDrive")}, str(root / "OneDrive")),
                                  ({"OneDrive": str(root / "Elsewhere")}, None), ({}, None)):
            with self.subTest(environ=environ):
                self.assertEqual(setup_core.onedrive_folder(lab, environ), expected)


class WindowsPowerShellTests(unittest.TestCase):
    def test_windows_powershell_starts_without_an_inherited_module_path(self):
        # PowerShell 7's module path names modules Windows PowerShell cannot load (tests/test_get_python.py says more).
        environ = {"PATH": "C:\\Windows", "PSModulePath": "C:\\Program Files\\PowerShell\\7\\Modules", "TEMP": "C:\\Temp"}
        self.assertEqual(setup_core.windows_powershell_env(environ), {"PATH": "C:\\Windows", "TEMP": "C:\\Temp"})
        self.assertNotIn("psmodulepath", [name.lower() for name in setup_core.windows_powershell_env({"PSMODULEPATH": "x"})])


class SetupBatchTests(unittest.TestCase):
    def test_setup_bat_says_which_python_to_install_when_it_cannot_get_one(self):
        # The order it tries Pythons in is tests/test_get_python.py's.
        data = (ROOT / "setup.bat").read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "batch files keep CRLF line ends")
        text = data.decode("ascii")
        self.assertIn("%PYTHON% -c \"import sys; print('Python', sys.version.split()[0], 'at', sys.executable)\" || goto no_python",
                      text)
        self.assertIn(":no_python", text)
        self.assertIn("https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe", text)


if __name__ == "__main__":
    unittest.main()
