"""What Setup.exe's uninstaller removes (tools/uninstall_lab.py): the program and everything it installed, and the library only
when the person says so. Run on a stand-in Lab folder."""

import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from library_backup import SECTIONS  # noqa: E402
from tools import uninstall_lab  # noqa: E402

PROGRAM = ["serve.py", "app_version.py", "tools/update.py", "docs/SETUP.md", ".venv/Scripts/python.exe",
           ".venv-desktop/Scripts/pythonw.exe", ".venv-kokoro/Lib/site-packages/torch/x.py", "hf-cache/hub/model.bin",
           "ffmpeg/bin/ffmpeg.exe", "out/logs/service.log", "out/install/setup-exe.log", "out/voxcpm2/source/x.py",
           "out/drama3/runtime/speech.exe", "out/workers/x", "a-file-update-bat-added-later.py"]  # fmt: skip
LIBRARY = ["data/cast/cast.sqlite3", "data/scenes/scenes.sqlite3", "data/voices/v.wav", "backups/library-1.zip",
           "restored/data/cast/cast.sqlite3", "reference/af_heart.wav", "desktop.local.json", "runtime.local.json",
           "out/scenes/s/take.wav", "out/exports/package/manifest.json", "out/comparisons/run/manifest.json",
           "out/drama3/studio/job.json"]  # fmt: skip
UNINSTALLER = ["python/python.exe", "python/Lib/os.py", "unins000.exe", "unins000.dat"]


def files(root):
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


class UninstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-uninstall-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "FelsVoiceLab"
        for name in PROGRAM + LIBRARY + UNINSTALLER:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

    def run_it(self, keep_library):
        with redirect_stdout(io.StringIO()) as said:
            code = uninstall_lab.run(self.root, keep_library=keep_library)
        return code, said.getvalue()

    def test_keeping_the_library_removes_the_program_and_keeps_every_part_of_the_library(self):
        code, said = self.run_it(keep_library=True)
        self.assertEqual(code, 0, said)
        self.assertEqual(files(self.root), sorted(LIBRARY + UNINSTALLER))
        self.assertFalse((self.root / "out" / "logs").exists())
        self.assertIn("Kept the library in", said)

    def test_everything_leaves_only_what_the_uninstaller_removes_itself(self):
        code, said = self.run_it(keep_library=False)
        self.assertEqual(code, 0, said)
        self.assertEqual(files(self.root), sorted(UNINSTALLER))
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["python", "unins000.dat", "unins000.exe"])

    def test_a_folder_opened_for_the_library_goes_when_nothing_in_it_was_kept(self):
        for name in ("out/scenes", "out/exports", "out/comparisons", "out/drama3/studio"):
            shutil.rmtree(self.root / name)
        self.run_it(keep_library=True)
        self.assertFalse((self.root / "out").exists())
        self.assertTrue((self.root / "data" / "cast" / "cast.sqlite3").is_file())

    def test_a_read_only_file_is_removed(self):
        path = self.root / "tools" / "update.py"
        os.chmod(path, stat.S_IREAD)
        self.run_it(keep_library=True)
        self.assertFalse(path.exists())

    @unittest.skipUnless(sys.platform == "win32", "junctions are Windows folders")
    def test_a_junction_is_removed_as_a_link_and_what_it_points_to_stays(self):
        import _winapi

        elsewhere = self.root.parent / "models-elsewhere"
        (elsewhere / "hub").mkdir(parents=True)
        (elsewhere / "hub" / "weights.bin").write_bytes(b"keep me")
        _winapi.CreateJunction(str(elsewhere), str(self.root / "hf-cache-linked"))
        self.run_it(keep_library=False)
        self.assertFalse((self.root / "hf-cache-linked").exists())
        self.assertEqual((elsewhere / "hub" / "weights.bin").read_bytes(), b"keep me")

    def test_the_library_is_what_a_backup_holds_and_more(self):
        kept = {Path(item).as_posix() for item in uninstall_lab.LIBRARY}
        for section in SECTIONS:
            for folder in section.folders:
                with self.subTest(folder):
                    self.assertTrue(any(folder == item or folder.startswith(item + "/") for item in kept), folder)
        self.assertTrue({"backups", "restored", "reference", "desktop.local.json", "runtime.local.json"} <= kept)

    def test_the_command_line_needs_a_choice(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            uninstall_lab.main([])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(uninstall_lab.main(["--keep-library", "--root", str(self.root)]), 0)
        self.assertTrue((self.root / "data" / "cast" / "cast.sqlite3").is_file())
        self.assertFalse((self.root / "serve.py").exists())


if __name__ == "__main__":
    unittest.main()
