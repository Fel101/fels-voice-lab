"""Reference audio from MP3 and M4A files: read, kept untouched as the source, and refused in plain words when it cannot be."""
import hashlib
import io
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cast_store  # noqa: E402
import ffmpeg_tools  # noqa: E402
import serve  # noqa: E402
from cast_store import CastStore  # noqa: E402
from tests.test_service_log import FakeEngines  # noqa: E402

RATE = 24_000
FFMPEG = ffmpeg_tools.find_ffmpeg()
_ENCODERS = ffmpeg_tools.capabilities(FFMPEG, ffmpeg_tools.find_ffprobe())["encoders"] if FFMPEG else {}
HAS_AAC = bool(_ENCODERS.get("aac"))
HAS_LAME = bool(_ENCODERS.get("mp3"))
TERMS = {"transcript": "A clear reference line.", "origin": "local import fixture", "allowed_use": "tests only",
         "edits": {"target_sample_rate": RATE}}


def tone(seconds=2.0):
    time = np.arange(round(seconds * RATE)) / RATE
    return (0.1 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)


def mp3_bytes(seconds=2.0):
    buffer = io.BytesIO()
    sf.write(buffer, tone(seconds), RATE, format="MP3")
    return buffer.getvalue()


def m4a_bytes(folder, seconds=2.0):
    wav, target = Path(folder) / "tone.wav", Path(folder) / "clip.m4a"
    sf.write(wav, tone(seconds), RATE, subtype="PCM_16")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "96k", str(target)],
                   check=True, capture_output=True)
    return target.read_bytes()


class ImportBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-import-")
        self.addCleanup(self.temp.cleanup)
        self.store = CastStore(Path(self.temp.name) / "state")
        character = self.store.create_character("Imported voice", "")
        self.identity = self.store.create_identity(character["id"], "qwen3-tts-1.7b-base", "Imported")

    def reference_folders(self):
        return sorted((self.store.data_root / "media" / "references").glob("*"))


class Mp3ImportTests(ImportBase):
    def test_an_mp3_is_read_saved_as_the_source_and_turned_into_a_wav_reference(self):
        data = mp3_bytes()
        reference = self.store.add_reference(self.identity["id"], "voice.mp3", data, **TERMS)
        self.assertEqual(reference["source_name"], "voice.mp3")
        source = self.store.get_media_path("reference_source", reference["id"])
        self.assertEqual(source.name, "source.mp3")
        self.assertEqual(source.read_bytes(), data)
        self.assertEqual(reference["source_sha256"], hashlib.sha256(data).hexdigest())
        wav, rate = sf.read(self.store.get_media_path("reference", reference["id"]), dtype="float32")
        self.assertEqual(rate, RATE)
        self.assertAlmostEqual(len(wav) / rate, 2.0, delta=0.2)
        self.assertGreater(float(np.abs(wav).max()), 0.05)

    def test_an_mp3_can_be_previewed_and_given_a_new_version_from_its_stored_source(self):
        data = mp3_bytes(3.0)
        wav_bytes, metrics = self.store.preview_reference(self.identity["id"], "voice.mp3", data)
        self.assertEqual(sf.info(io.BytesIO(wav_bytes)).samplerate, RATE)
        self.assertIn("quality", metrics)
        first = self.store.add_reference(self.identity["id"], "voice.mp3", data, **TERMS)
        second = self.store.add_reference_version(first["id"], {"start_seconds": 0.5, "end_seconds": 2.0})
        self.assertAlmostEqual(second["duration_seconds"], 1.5, delta=0.05)
        self.assertEqual(second["source_sha256"], first["source_sha256"])

    def test_an_mp3_is_measured_on_its_samples_and_never_read_past_the_ceiling(self):
        requests = []
        real = sf.SoundFile

        class Recording(real):
            def read(self, frames=-1, *args, **kwargs):
                requests.append(frames)
                return super().read(frames, *args, **kwargs)

        data = mp3_bytes(3.0)
        with mock.patch.object(cast_store, "MAX_SOURCE_SECONDS", 1.0), mock.patch.object(cast_store.sf, "SoundFile", Recording):
            with self.assertRaisesRegex(ValueError, "Source audio duration must be"):
                CastStore._decode_audio(data, "voice.mp3")
        self.assertEqual(requests, [RATE + 1])  # one second and a frame, not the three seconds in the file
        audio, rate, duration, suffix = CastStore._decode_audio(data, "voice.mp3")
        self.assertEqual((suffix, rate), (".mp3", RATE))
        self.assertAlmostEqual(duration, len(audio) / rate)
        self.assertAlmostEqual(duration, 3.0, delta=0.2)

    def test_an_mp3_that_is_not_audio_is_refused_without_leaving_a_reference_behind(self):
        with self.assertRaisesRegex(ValueError, "Could not read reference audio"):
            self.store.add_reference(self.identity["id"], "voice.mp3", b"not an mp3 at all" * 50, **TERMS)
        self.assertEqual(self.reference_folders(), [])

    def test_the_source_is_served_with_its_own_type(self):
        data = mp3_bytes()
        reference = self.store.add_reference(self.identity["id"], "voice.mp3", data, **TERMS)
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        server.RequestHandlerClass = serve.make_handler(FakeEngines(), server.server_address[1], self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/api/references/{reference['id']}/source", timeout=10) as response:
            self.assertEqual(response.headers["Content-Type"], "audio/mpeg")
            self.assertEqual(response.read(), data)
        m4a = Path(self.temp.name) / "voice.m4a"
        m4a.write_bytes(b"placeholder")
        with mock.patch.object(self.store, "get_media_path", return_value=m4a):
            with urllib.request.urlopen(f"{base}/api/references/{reference['id']}/source", timeout=10) as response:
                self.assertEqual(response.headers["Content-Type"], "audio/mp4")


class OtherFormatTests(ImportBase):
    def test_unsupported_files_are_refused_and_the_message_lists_what_is_accepted(self):
        for name in ("voice.aac", "voice.wma", "voice.mp4", "voice"):
            with self.subTest(name), self.assertRaisesRegex(ValueError, "WAV, FLAC, OGG, MP3 or M4A"):
                self.store.add_reference(self.identity["id"], name, b"\0" * 64, **TERMS)
        self.assertEqual(self.reference_folders(), [])

    def test_the_purge_scan_looks_for_copies_in_the_new_formats_too(self):
        self.assertLessEqual({".wav", ".flac", ".ogg", ".mp3", ".m4a"}, cast_store.AUDIO_SUFFIXES)
        self.assertEqual(cast_store.AUDIO_SUFFIXES, cast_store.SUPPORTED_AUDIO)

    def test_an_m4a_without_ffmpeg_is_refused_in_plain_words_and_leaves_nothing_behind(self):
        with mock.patch.object(ffmpeg_tools, "find_ffmpeg", return_value=None):
            for action in (lambda: self.store.add_reference(self.identity["id"], "voice.m4a", b"x" * 400, **TERMS),
                           lambda: self.store.preview_reference(self.identity["id"], "voice.m4a", b"x" * 400)):
                with self.assertRaisesRegex(ValueError, "M4A files are read with FFmpeg.*winget install Gyan.FFmpeg"):
                    action()
        self.assertEqual(self.reference_folders(), [])
        self.assertEqual(self.store.get_identity(self.identity["id"])["references"], [])


@unittest.skipUnless(HAS_LAME, "needs FFmpeg with its MP3 encoder to make a second kind of MP3")
class FfmpegMp3ImportTests(ImportBase):
    def test_a_variable_bit_rate_mp3_with_a_lame_header_imports_at_its_true_length(self):
        wav, target = Path(self.temp.name) / "tone.wav", Path(self.temp.name) / "vbr.mp3"
        sf.write(wav, tone(2.5), RATE, subtype="PCM_16")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "libmp3lame", "-q:a", "4",
                        str(target)], check=True, capture_output=True)
        reference = self.store.add_reference(self.identity["id"], "vbr.mp3", target.read_bytes(), **TERMS)
        wav_back, rate = sf.read(self.store.get_media_path("reference", reference["id"]), dtype="float32")
        self.assertEqual(rate, RATE)
        self.assertAlmostEqual(len(wav_back) / rate, 2.5, delta=0.1)
        self.assertGreater(float(np.abs(wav_back).max()), 0.05)


@unittest.skipUnless(HAS_AAC, "needs FFmpeg with its AAC encoder to make an M4A")
class M4aImportTests(ImportBase):
    def test_an_m4a_is_decoded_with_ffmpeg_and_the_original_is_kept_byte_for_byte(self):
        data = m4a_bytes(self.temp.name, 2.0)
        reference = self.store.add_reference(self.identity["id"], "voice.m4a", data, **TERMS)
        source = self.store.get_media_path("reference_source", reference["id"])
        self.assertEqual((source.name, source.read_bytes()), ("source.m4a", data))
        self.assertEqual(reference["source_sha256"], hashlib.sha256(data).hexdigest())
        wav, rate = sf.read(self.store.get_media_path("reference", reference["id"]), dtype="float32")
        self.assertEqual(rate, RATE)
        self.assertAlmostEqual(len(wav) / rate, 2.0, delta=0.2)
        second = self.store.add_reference_version(reference["id"], {"start_seconds": 0.2, "end_seconds": 1.2})
        self.assertAlmostEqual(second["duration_seconds"], 1.0, delta=0.05)

    def test_an_m4a_longer_than_the_ceiling_is_refused_with_the_usual_message(self):
        data = m4a_bytes(self.temp.name, 3.0)
        with mock.patch.object(cast_store, "MAX_SOURCE_SECONDS", 1.0):
            with self.assertRaisesRegex(ValueError, "Source audio duration must be"):
                self.store.add_reference(self.identity["id"], "voice.m4a", data, **TERMS)
        self.assertEqual(self.reference_folders(), [])

    def test_a_wav_renamed_m4a_is_refused_by_ffmpeg_not_decoded(self):
        wav = io.BytesIO()
        sf.write(wav, tone(2.0), RATE, format="WAV", subtype="PCM_16")
        with self.assertRaisesRegex(ValueError, "FFmpeg could not read this M4A file"):
            self.store.add_reference(self.identity["id"], "voice.m4a", wav.getvalue(), **TERMS)
        self.assertEqual(self.reference_folders(), [])


if __name__ == "__main__":
    unittest.main()
