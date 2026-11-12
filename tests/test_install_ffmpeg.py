"""Install FFmpeg (tools/install_ffmpeg.py): the pinned build is downloaded, checked and unpacked into ffmpeg\\ whole or not at
all. The download, the build and FFmpeg itself are stand-ins."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import install_ffmpeg  # noqa: E402

FOLDER = "ffmpeg-1.0-essentials_build"
FILES = {
    "bin/ffmpeg.exe": b"stand-in ffmpeg",
    "bin/ffprobe.exe": b"stand-in ffprobe",
    "LICENSE": b"GPL v3",
    "README.txt": b"read",
}
ENCODERS = " A....D aac  AAC\n A....D flac  FLAC\n A....D libmp3lame  MP3\n A....D libvorbis  Vorbis\n"


def build_zip(extra=None, leave_out=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in {**FILES, "bin/ffplay.exe": b"not wanted", **(extra or {})}.items():
            if path not in leave_out:
                archive.writestr(f"{FOLDER}/{path}", data)
    return buffer.getvalue()


class InstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-ffmpeg-")
        self.addCleanup(temp.cleanup)
        self.lab = Path(temp.name)
        (self.lab / "catalog").mkdir()
        self.zip = build_zip()
        self.build = {
            "version": "1.0",
            "builder": "a stand-in builder",
            "license": "GPL-3.0",
            "url": "https://example.invalid/releases/ffmpeg-1.0-essentials_build.zip",
            "bytes": len(self.zip),
            "sha256": "0" * 64,
            "folder": FOLDER,
            "files": [{"path": path, "size": len(data)} for path, data in FILES.items()],
        }
        (self.lab / install_ffmpeg.CATALOG).write_text(json.dumps(self.build), encoding="utf-8")
        self.fetched, self.checked, self.said = [], [], []
        self.problem = None

    def fetch(self, url, destination, size, sha256, *, say):
        self.fetched.append(url)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(self.zip)
        return True

    def check(self, folder):
        self.checked.append(Path(folder))
        return self.problem

    def install(self, dry_run=False, fetch=None):
        return install_ffmpeg.run(Namespace(yes=True, dry_run=dry_run), root=self.lab, say=self.said.append,
                                  fetch=fetch or self.fetch, check=self.check)  # fmt: skip

    def left(self):
        return sorted(path.name for path in self.lab.iterdir() if path.name not in ("catalog", "out"))

    def test_the_build_is_unpacked_into_ffmpeg_with_only_the_files_it_names(self):
        self.assertEqual(self.install(), 0)
        for path, data in FILES.items():
            self.assertEqual((self.lab / "ffmpeg" / path).read_bytes(), data)
        self.assertFalse((self.lab / "ffmpeg" / "bin" / "ffplay.exe").exists())
        self.assertEqual(self.left(), ["ffmpeg"])
        self.assertEqual(list((self.lab / install_ffmpeg.DOWNLOADS).iterdir()), [], "the ZIP is deleted once unpacked")
        self.assertEqual(self.checked, [self.lab / "ffmpeg.partial" / "bin"], "FFmpeg is tried before it takes its place")
        self.assertIn("FFmpeg 1.0 is installed in the Lab's ffmpeg folder.", self.said[-1])

    def test_the_plan_names_the_build_the_sizes_and_the_folder_and_a_dry_run_changes_nothing(self):
        self.assertEqual(self.install(dry_run=True), 0)
        plan = " ".join(" ".join(self.said).split())
        for words in ("FFmpeg 1.0, a stand-in builder (licence GPL-3.0), from example.invalid", "to download and",
                      "on disk in the Lab's ffmpeg folder", "checked against its SHA-256"):  # fmt: skip
            self.assertIn(words, plan)
        self.assertTrue(self.said[-1].startswith("Checks passed."))
        self.assertEqual((self.fetched, self.left()), ([], []))

    def test_an_ffmpeg_that_does_not_work_here_is_not_put_in_place(self):
        self.problem = "This FFmpeg cannot write with libmp3lame."
        with self.assertRaisesRegex(
            install_ffmpeg.InstallError, "does not work here: This FFmpeg cannot write with libmp3lame"
        ):
            self.install()
        self.assertEqual(self.left(), [])

    def test_a_build_missing_a_file_it_should_hold_is_refused(self):
        self.zip = build_zip(leave_out=("bin/ffprobe.exe",))
        with self.assertRaisesRegex(install_ffmpeg.InstallError, "holds no bin/ffprobe.exe"):
            self.install()
        self.assertEqual(self.left(), [])

    def test_a_download_that_stops_says_what_to_do_and_keeps_what_came(self):
        def offline(*args, **kwargs):
            raise install_ffmpeg.InstallError(
                "The download of x.zip stopped (URLError: no route). Run the installer again to resume it."
            )

        with self.assertRaises(install_ffmpeg.InstallError) as raised:
            self.install(fetch=offline)
        self.assertEqual(str(raised.exception), "The download of x.zip stopped (URLError: no route). If this computer is offline, "
                         "connect it first. What was downloaded is kept, and the next try continues from there.")  # fmt: skip
        self.assertEqual(self.left(), [])

    def test_a_working_ffmpeg_already_there_is_left_alone(self):
        self.assertEqual(self.install(), 0)
        self.fetched, self.said = [], []
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.fetched, [])
        self.assertIn("already in the Lab's ffmpeg folder", self.said[0])
        self.problem = "FFmpeg could not be started (blocked)."
        with self.assertRaisesRegex(install_ffmpeg.InstallError, "does not work: FFmpeg could not be started"):
            self.install()

    def test_an_ffmpeg_folder_the_installer_did_not_make_is_left_alone(self):
        (self.lab / "ffmpeg").mkdir()
        (self.lab / "ffmpeg" / "notes.txt").write_text("mine", encoding="utf-8")
        with self.assertRaisesRegex(install_ffmpeg.InstallError, "without ffmpeg.exe in it"):
            self.install()
        self.assertEqual(sorted(path.name for path in (self.lab / "ffmpeg").iterdir()), ["notes.txt"])
        self.assertEqual(self.fetched, [])

    def test_the_check_needs_every_encoder_the_export_formats_use(self):
        def runner(listing, code=0):
            def run(command, **kwargs):
                return subprocess.CompletedProcess(command, code, listing if "-encoders" in command else "ffprobe 1.0", "")

            return run

        self.assertIsNone(install_ffmpeg.check_build(self.lab, runner=runner(ENCODERS)))
        self.assertEqual(install_ffmpeg.check_build(self.lab, runner=runner(ENCODERS.replace("libmp3lame", "mp3_mf"))),
                         "This FFmpeg cannot write with libmp3lame.")  # fmt: skip
        self.assertIn("stopped with an error", install_ffmpeg.check_build(self.lab, runner=runner(ENCODERS, code=1)))

        def missing(command, **kwargs):
            raise FileNotFoundError(command[0])

        self.assertIn("could not be started", install_ffmpeg.check_build(self.lab, runner=missing))


class CatalogTests(unittest.TestCase):
    def test_the_catalog_pins_a_versioned_build_by_its_sha256(self):
        build = install_ffmpeg.load_build()
        self.assertTrue(build["url"].startswith("https://github.com/GyanD/codexffmpeg/releases/download/"), build["url"])
        self.assertIn(f"/{build['version']}/", build["url"])
        self.assertTrue(build["url"].endswith(f"/{build['folder']}.zip"))
        self.assertRegex(build["sha256"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual([item["path"] for item in build["files"]][:2], ["bin/ffmpeg.exe", "bin/ffprobe.exe"])
        self.assertEqual(build["license"], "GPL-3.0")

    def test_a_dry_run_from_the_command_line_ends_as_the_install_runner_expects(self):
        if (ROOT / "ffmpeg").exists():
            self.skipTest("this copy of the Lab has an ffmpeg folder of its own")
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "install_ffmpeg.py"), "--dry-run"], cwd=ROOT,
                              capture_output=True, text=True, timeout=120)  # fmt: skip
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.rstrip().splitlines()[-1].startswith("Checks passed."))


if __name__ == "__main__":
    unittest.main()
