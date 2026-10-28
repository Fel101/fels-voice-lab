"""The helpers the worker uses to build a Kokoro or Chatterbox engine and to make the default reference clip."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

import engines


class FakeKokoro:
    def speak(self, text, voice=None):
        return np.zeros(2400, dtype=np.float32), 24000, {}


class BuildEngineTests(unittest.TestCase):
    def test_each_worker_key_builds_its_engine_class_for_the_device(self):
        for key, name in (("kokoro", "Kokoro"), ("turbo", "ChatterboxTurbo"), ("orig", "ChatterboxOriginal")):
            with self.subTest(key=key), mock.patch.object(engines, name) as engine_class:
                self.assertIs(engines.build_engine(key, "cpu"), engine_class.return_value)
                engine_class.assert_called_once_with("cpu")

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(KeyError):
            engines.build_engine("nonsense", "cpu")


class EnsureReferenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / "reference"
        self.clip = self.folder / "af_heart.wav"
        patcher = mock.patch.multiple(engines, REFERENCE_DIR=self.folder, REFERENCE_WAV=self.clip)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def ensure():
        with contextlib.redirect_stdout(io.StringIO()):
            engines.ensure_reference()

    def test_an_existing_clip_is_left_alone_and_no_engine_is_built(self):
        self.folder.mkdir()
        self.clip.write_bytes(b"kept")
        with mock.patch.object(engines, "build_engine", side_effect=AssertionError("built an engine")):
            self.ensure()
        self.assertEqual(self.clip.read_bytes(), b"kept")

    def test_a_missing_clip_is_spoken_by_kokoro_on_the_cpu_and_saved(self):
        with mock.patch.object(engines, "build_engine", return_value=FakeKokoro()) as build:
            self.ensure()
        build.assert_called_once_with("kokoro", "cpu")
        info = sf.info(str(self.clip))
        self.assertEqual((info.samplerate, info.frames), (24000, 2400))


if __name__ == "__main__":
    unittest.main()
