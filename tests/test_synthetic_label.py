"""The AI-generated speech label inside the audio files the Lab makes (synthetic_label.py)."""

import io
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ffmpeg_tools  # noqa: E402
import synthetic_label  # noqa: E402
from app_version import APP_NAME, VERSION  # noqa: E402
from tools import scene_export  # noqa: E402

TONE = (0.1 * np.sin(2 * np.pi * 220 * np.arange(24000) / 24000)).astype(np.float32)


class LabelTextTests(unittest.TestCase):
    def test_the_label_says_it_is_synthetic_names_the_model_and_the_lab_and_carries_the_iptc_term(self):
        text = synthetic_label.comment("kokoro")
        self.assertTrue(text.startswith("AI-generated speech: a synthetic voice made by Kokoro 82M with "))
        self.assertIn(f"{APP_NAME} {VERSION}", text)
        self.assertIn("not a recording of a person speaking", text)
        self.assertIn("http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia", text)
        self.assertTrue(text.isascii())

    def test_without_an_engine_it_names_none_and_an_unknown_engine_is_named_by_its_id(self):
        self.assertNotIn(" by ", synthetic_label.comment().split(" with ")[0])
        self.assertIn("made by mystery-1 with", synthetic_label.comment("mystery-1"))

    def test_a_label_is_recognised_and_other_text_is_not(self):
        self.assertTrue(synthetic_label.is_labelled(synthetic_label.comment("turbo")))
        for other in (None, "", "Recorded in the studio", "ai-generated speech: lower case"):
            self.assertFalse(synthetic_label.is_labelled(other), other)


class WavTests(unittest.TestCase):
    def test_a_wav_carries_the_label_and_the_lab_as_its_software_and_the_samples_are_untouched(self):
        data = synthetic_label.wav_bytes(TONE, 24000, engine_id="phonon-31m-en")
        info = sf.SoundFile(io.BytesIO(data))
        self.assertEqual(info.comment, synthetic_label.comment("phonon-31m-en"))
        self.assertTrue(info.software.startswith(f"{APP_NAME} {VERSION}"))
        samples, rate = sf.read(io.BytesIO(data), dtype="float32")
        plain = io.BytesIO()
        sf.write(plain, TONE, 24000, format="WAV", subtype="PCM_16")
        self.assertEqual((rate, samples.tolist()), (24000, sf.read(io.BytesIO(plain.getvalue()), dtype="float32")[0].tolist()))

    def test_stereo_and_files_on_disk_work_too(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mix.wav"
            synthetic_label.write_wav(path, np.stack([TONE, TONE], axis=1), 24000)
            self.assertEqual((sf.info(str(path)).channels, sf.SoundFile(str(path)).comment), (2, synthetic_label.comment()))

    def test_a_streamed_wav_is_labelled_afterwards_with_its_samples_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "take.wav"
            sf.write(path, TONE, 44100, subtype="PCM_16")
            before = sf.read(str(path), dtype="int16")[0]
            synthetic_label.label_file(path, engine_id="drama-3")
            self.assertTrue(synthetic_label.is_labelled(sf.SoundFile(str(path)).comment))
            self.assertEqual(sf.read(str(path), dtype="int16")[0].tolist(), before.tolist())
            self.assertEqual(sorted(item.name for item in Path(folder).iterdir()), ["take.wav"])


class TakeTests(unittest.TestCase):
    def test_a_saved_take_is_labelled_and_a_reference_recording_is_not(self):
        from tests.library_fixture import build_library

        with tempfile.TemporaryDirectory() as folder:
            cast = build_library(Path(folder) / "lab")
            take = cast.list_takes()[0]
            with sf.SoundFile(str(cast.get_media_path("take", take["id"]))) as take_file:
                self.assertEqual(take_file.comment, synthetic_label.comment("kokoro"))
            character = cast.create_character("Narrator", "")
            identity = cast.create_identity(character["id"], "qwen3-tts-1.7b-base", "Own voice")
            recording = io.BytesIO()
            sf.write(recording, np.tile(TONE, 8), 24000, format="WAV", subtype="PCM_16")
            reference = cast.add_reference(
                identity["id"],
                "voice.wav",
                recording.getvalue(),
                transcript="A clear reference line.",
                origin="local fixture",
                allowed_use="tests only",
            )
            for kind in ("reference", "reference_source"):
                with sf.SoundFile(str(cast.get_media_path(kind, reference["id"]))) as reference_file:
                    self.assertFalse(
                        synthetic_label.is_labelled(reference_file.comment), f"a person's own {kind} is never labelled"
                    )


@unittest.skipUnless(ffmpeg_tools.find_ffmpeg() and ffmpeg_tools.find_ffprobe(), "FFmpeg is not installed here")
class ExportFormatTests(unittest.TestCase):
    def test_every_format_the_exporter_writes_keeps_the_label(self):
        ffmpeg, ffprobe = ffmpeg_tools.find_ffmpeg(), ffmpeg_tools.find_ffprobe()
        report = ffmpeg_tools.capabilities(ffmpeg, ffprobe)
        args = type(
            "Args",
            (),
            {
                "output_rate": None,
                "trim_start_seconds": 0.0,
                "trim_end_seconds": 0.0,
                "fade_in_ms": 0,
                "fade_out_ms": 0,
                "loudness_lufs": None,
                "loudness_true_peak": -1.0,
                "limiter_dbfs": None,
            },
        )()
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.wav"
            sf.write(source, TONE, 24000, subtype="PCM_16")
            probe = scene_export.probe_media(source, ffprobe)
            self.assertIsNone(probe["comment"])
            for fmt, writable in ffmpeg_tools.writable_formats(report).items():
                if not writable:
                    continue
                with self.subTest(format=fmt):
                    destination = Path(folder) / f"out{scene_export.FORMAT_CONFIG[fmt][0]}"
                    scene_export.transcode(ffmpeg, source, destination, fmt, args, probe)
                    self.assertEqual(scene_export.probe_media(destination, ffprobe)["comment"], synthetic_label.comment())

    def test_the_label_options_are_given_to_ffmpeg(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            Path(command[-1]).write_bytes(b"x")
            return subprocess.CompletedProcess(command, 0, "", "")

        args = type(
            "Args",
            (),
            {
                "output_rate": None,
                "trim_start_seconds": 0.0,
                "trim_end_seconds": 0.0,
                "fade_in_ms": 0,
                "fade_out_ms": 0,
                "loudness_lufs": None,
                "loudness_true_peak": -1.0,
                "limiter_dbfs": None,
            },
        )()
        with tempfile.TemporaryDirectory() as folder, unittest.mock.patch.object(scene_export.subprocess, "run", run):
            scene_export.transcode(
                "ffmpeg", Path(folder) / "in.wav", Path(folder) / "out.mp3", "mp3", args, {"duration_seconds": 1.0}
            )
        command = calls[0]
        self.assertEqual(command[command.index("-metadata") + 1], f"comment={synthetic_label.comment()}")
        self.assertIn(f"encoded_by={APP_NAME} {VERSION}", command)


if __name__ == "__main__":
    unittest.main()
