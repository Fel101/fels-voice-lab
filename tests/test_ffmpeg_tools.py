"""Finding FFmpeg, reading what it can write, and decoding M4A with it."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ffmpeg_tools  # noqa: E402

FFMPEG = ffmpeg_tools.find_ffmpeg()
HAS_AAC = bool(FFMPEG and ffmpeg_tools.capabilities(FFMPEG, ffmpeg_tools.find_ffprobe())["encoders"].get("aac"))
RATE = 24_000
FULL = {"found": True, "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "version": "9.0", "decoders": {"aac": True},
        "encoders": {"mp3": True, "aac": True, "vorbis": True, "flac": True}, "message": None}


def make_m4a(folder, seconds=2.0, name="clip.m4a"):
    """An M4A made by FFmpeg itself from a tone, which is what a phone or an editor would hand over."""
    time = np.arange(round(seconds * RATE)) / RATE
    wav, target = Path(folder) / "tone.wav", Path(folder) / name
    sf.write(wav, (0.1 * np.sin(2 * np.pi * 440 * time)).astype(np.float32), RATE, subtype="PCM_16")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "96k", str(target)],
                   check=True, capture_output=True)
    return target


class FindingFFmpegTests(unittest.TestCase):
    def setUp(self):
        ffmpeg_tools.forget()
        self.addCleanup(ffmpeg_tools.forget)

    def test_nothing_is_found_when_it_is_nowhere(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-no-ffmpeg-") as empty, \
                mock.patch.object(ffmpeg_tools.shutil, "which", return_value=None), \
                mock.patch.object(ffmpeg_tools, "WINGET_FOLDER", Path(empty)):
            self.assertIsNone(ffmpeg_tools.find_ffmpeg())
            self.assertIsNone(ffmpeg_tools.find_ffprobe())
            self.assertIsNone(ffmpeg_tools.resolve(None, "ffmpeg"))
            report = ffmpeg_tools.capabilities(ffmpeg_tools.find_ffmpeg(), ffmpeg_tools.find_ffprobe())
        self.assertFalse(report["found"])
        self.assertIn("winget install Gyan.FFmpeg", report["message"])
        self.assertFalse(any(ffmpeg_tools.writable_formats(report).values()))

    def test_the_winget_folder_is_searched_when_path_has_nothing_and_the_newest_build_wins(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-winget-") as winget:
            for build in ("ffmpeg-7.0-full_build", "ffmpeg-8.1.1-full_build"):
                (Path(winget) / build / "bin").mkdir(parents=True)
                for tool in ("ffmpeg", "ffprobe"):
                    (Path(winget) / build / "bin" / f"{tool}.exe").write_bytes(b"")
            with mock.patch.object(ffmpeg_tools.shutil, "which", return_value=None), \
                    mock.patch.object(ffmpeg_tools, "WINGET_FOLDER", Path(winget)):
                self.assertTrue(ffmpeg_tools.find_ffmpeg().endswith(str(Path("ffmpeg-8.1.1-full_build/bin/ffmpeg.exe"))))
                self.assertTrue(ffmpeg_tools.find_ffprobe().endswith("ffprobe.exe"))

    def test_a_program_a_caller_names_is_used_instead_of_the_search(self):
        with mock.patch.object(ffmpeg_tools.shutil, "which", side_effect=lambda name: f"C:/tools/{name}.exe"):
            self.assertEqual(ffmpeg_tools.resolve("mine", "ffmpeg"), "C:/tools/mine.exe")
            self.assertEqual(ffmpeg_tools.resolve(None, "ffprobe"), "C:/tools/ffprobe.exe")

    def test_what_a_build_can_write_decides_which_formats_are_offered(self):
        report = {**FULL, "encoders": {**FULL["encoders"], "mp3": False}}
        self.assertEqual(ffmpeg_tools.writable_formats(FULL), dict.fromkeys(ffmpeg_tools.FORMAT_ENCODER, True))
        self.assertEqual({name: ok for name, ok in ffmpeg_tools.writable_formats(report).items() if not ok}, {"mp3": False})
        self.assertIsNone(ffmpeg_tools.cannot_write(["wav", "m4a"], report))
        self.assertIn("cannot write MP3", ffmpeg_tools.cannot_write(["wav", "mp3"], report))
        self.assertIn("MP3 or AAC (M4A)", ffmpeg_tools.cannot_write(
            ["mp3", "m4a"], {**report, "encoders": {**report["encoders"], "aac": False}}))
        no_probe = {**FULL, "ffprobe": None, "message": "FFmpeg was found but ffprobe was not."}
        self.assertEqual(ffmpeg_tools.cannot_write(["wav"], no_probe), no_probe["message"])
        self.assertFalse(any(ffmpeg_tools.writable_formats(no_probe).values()))

    def test_the_answer_is_remembered_for_a_minute_unless_asked_again(self):
        calls = []

        def capabilities(ffmpeg, ffprobe):
            calls.append((ffmpeg, ffprobe))
            return {**FULL, "version": f"call-{len(calls)}"}

        with mock.patch.object(ffmpeg_tools, "capabilities", capabilities), \
                mock.patch.object(ffmpeg_tools, "find_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(ffmpeg_tools, "find_ffprobe", return_value="ffprobe"):
            self.assertEqual(ffmpeg_tools.status()["version"], "call-1")
            self.assertEqual(ffmpeg_tools.status()["version"], "call-1")
            self.assertEqual(ffmpeg_tools.status(refresh=True)["version"], "call-2")
            ffmpeg_tools.forget()
            self.assertEqual(ffmpeg_tools.status()["version"], "call-3")
            with mock.patch.object(ffmpeg_tools.time, "monotonic", return_value=10 ** 9):
                self.assertEqual(ffmpeg_tools.status()["version"], "call-4")

    @unittest.skipUnless(FFMPEG, "needs FFmpeg")
    def test_a_real_ffmpeg_reports_its_version_and_encoders(self):
        report = ffmpeg_tools.capabilities(FFMPEG, ffmpeg_tools.find_ffprobe())
        self.assertTrue(report["found"])
        self.assertRegex(report["version"], r"^\d")
        self.assertEqual(sorted(report["encoders"]), ["aac", "flac", "mp3", "vorbis"])
        self.assertTrue(all(isinstance(value, bool) for value in report["encoders"].values()))
        self.assertIn("aac", report["decoders"])


@unittest.skipUnless(HAS_AAC, "needs FFmpeg with its AAC encoder to make an M4A")
class DecodingM4aTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-m4a-")
        self.addCleanup(self.temp.cleanup)

    def test_an_m4a_file_is_decoded_at_its_own_rate_with_its_length(self):
        data = make_m4a(self.temp.name, seconds=2.0).read_bytes()
        audio, rate = ffmpeg_tools.decode_audio(data, ".m4a", 600)
        self.assertEqual((rate, audio.shape[1]), (RATE, 1))
        self.assertAlmostEqual(audio.shape[0] / rate, 2.0, delta=0.15)  # AAC pads its frames
        self.assertTrue(np.isfinite(audio).all())
        self.assertGreater(float(np.abs(audio).max()), 0.05)

    def test_decoding_stops_just_past_the_ceiling_it_is_given(self):
        data = make_m4a(self.temp.name, seconds=4.0).read_bytes()
        audio, rate = ffmpeg_tools.decode_audio(data, ".m4a", 1)
        self.assertLessEqual(audio.shape[0] / rate, 2.01)

    def test_a_file_that_only_has_the_right_name_is_refused_not_guessed_at(self):
        wav = Path(self.temp.name) / "plain.wav"
        sf.write(wav, np.zeros(RATE, dtype=np.float32) + 0.1, RATE, subtype="PCM_16")
        playlist = b"ffconcat version 1.0\nfile 'plain.wav'\n"
        for label, data in (("a WAV named M4A", wav.read_bytes()), ("a playlist named M4A", playlist),
                            ("random bytes", bytes(range(256)) * 20)):
            with self.subTest(label), self.assertRaisesRegex(ValueError, "FFmpeg could not read this M4A file"):
                ffmpeg_tools.decode_audio(data, ".m4a", 600)

    def test_a_missing_ffmpeg_says_how_to_get_it_and_what_to_do_meanwhile(self):
        data = make_m4a(self.temp.name, seconds=1.0).read_bytes()
        with mock.patch.object(ffmpeg_tools, "find_ffmpeg", return_value=None):
            with self.assertRaises(ffmpeg_tools.FFmpegMissing) as raised:
                ffmpeg_tools.decode_audio(data, ".m4a", 600)
        self.assertIsInstance(raised.exception, ValueError)
        self.assertIn("winget install Gyan.FFmpeg", str(raised.exception))
        self.assertIn("convert the file to WAV, FLAC, OGG or MP3", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
