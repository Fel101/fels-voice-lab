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

    def test_a_kokoro_that_is_already_loaded_speaks_it_and_no_second_one_is_built(self):
        with mock.patch.object(engines, "build_engine", side_effect=AssertionError("built a second Kokoro")), \
                contextlib.redirect_stdout(io.StringIO()):
            engines.ensure_reference(FakeKokoro())
        self.assertEqual(sf.info(str(self.clip)).frames, 2400)


class KokoroWorkerReferenceTests(unittest.TestCase):
    """Kokoro has .venv-kokoro to itself, so its own worker makes the default reference the cloning checks clone."""

    def make(self, voices, ensure):
        from worker_runtime import EngineWorkerRuntime
        model = FakeKokoro()
        with mock.patch("kokoro_support.discover_kokoro_voices", return_value=voices), \
                mock.patch.object(engines, "ensure_reference", ensure), contextlib.redirect_stdout(io.StringIO()) as printed:
            EngineWorkerRuntime._make_default_reference(model)
        return model, printed.getvalue()

    def test_the_loaded_model_speaks_the_clip(self):
        ensure = mock.Mock()
        model, _printed = self.make(["af_heart", "bm_george"], ensure)
        ensure.assert_called_once_with(model)

    def test_without_the_af_heart_voice_nothing_is_made(self):
        ensure = mock.Mock()
        self.make(["bm_george"], ensure)
        ensure.assert_not_called()

    def test_a_failure_is_printed_and_the_load_goes_on(self):
        _model, printed = self.make(["af_heart"], mock.Mock(side_effect=OSError("disk full")))
        self.assertIn("The default reference clip was not made: OSError: disk full", printed)


if __name__ == "__main__":
    unittest.main()
