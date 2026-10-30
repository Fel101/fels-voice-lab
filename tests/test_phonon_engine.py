"""engines.Phonon: the wrapper around ptts, run against a stand-in runtime. The real package is not needed."""
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engines  # noqa: E402
import phonon_support  # noqa: E402

CATALOG = json.loads((ROOT / "catalog" / "engines.json").read_text("utf-8"))
ENTRY = next(entry for entry in CATALOG["engines"] if entry["id"] == phonon_support.PHONON_ENGINE_ID)
VOICES = [{"name": "Harper", "language": "en", "locale": "en-us", "gender": "female", "age": "Adult", "description": "Modern.",
           "filename": "voices/harper.safetensors"},
          {"name": "Old Mortlake", "language": "en", "locale": "characters", "gender": None, "age": None, "description": "A gravedigger.",
           "filename": "voices/old-mortlake.safetensors"}]


class FakeTTS:
    """What ptts.TTS is to the wrapper: constructor arguments in, a calls list out, and a clip of a known length back."""
    instances = []
    sample_rate = 24000

    def __init__(self, **options):
        self.options, self.calls = options, []
        FakeTTS.instances.append(self)

    def set_conditions(self, conditions):
        self.calls.append(("set_conditions", dict(conditions)))

    def synth(self, text, **options):
        self.calls.append(("synth", text, dict(options)))
        return np.full((1, 2400), 0.25, dtype=np.float64)


def fake_ptts():
    module = types.ModuleType("ptts")
    module.TTS = FakeTTS
    module.__version__ = "0.5.0"
    return module


class PhononEngineTests(unittest.TestCase):
    def setUp(self):
        FakeTTS.instances.clear()
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-phonon-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "catalog").mkdir()
        shutil.copyfile(ROOT / "catalog" / "engines.json", self.root / "catalog" / "engines.json")
        self.model, self.voices = phonon_support.snapshot_dirs(self.root)
        for name in phonon_support.MODEL_FILES:
            self.write(self.model / name)
        self.write(self.voices / phonon_support.VOICE_CATALOG, "\n".join(json.dumps(item) for item in VOICES).encode() + b"\n")
        for item in VOICES:
            self.write(self.voices / item["filename"])
        for patch in (mock.patch.dict(sys.modules, {"ptts": fake_ptts()}), mock.patch.object(phonon_support, "LAB", self.root)):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def write(path, data=b"x"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_it_loads_the_pinned_snapshots_on_the_cpu_whatever_device_the_service_chose(self):
        engine = engines.build_engine(phonon_support.PHONON_ENGINE_ID, "cuda")
        self.assertIsInstance(engine, engines.Phonon)
        self.assertEqual(engine.device, "cpu")
        self.assertEqual(FakeTTS.instances[0].options,
                         {"model": str(self.model), "voice_catalog": str(self.voices), "lang": "en", "quant": "q8", "device": "cpu"})
        self.assertEqual(engine.sr, 24000)
        self.assertEqual(engine.voices, ["Harper", "Old Mortlake"])

    def test_missing_files_are_named_with_the_launcher_that_puts_them_back(self):
        (self.model / "model.q8.gguf").unlink()
        with self.assertRaisesRegex(RuntimeError, r"incomplete: models--gradium--phonon-31m-en/snapshots/\w+/model\.q8\.gguf is "
                                                  r"missing from hf-cache/hub\. Run setup-phonon\.bat"):
            engines.Phonon("cpu")
        self.assertEqual(FakeTTS.instances, [], "nothing is constructed from an incomplete snapshot")

    def test_a_clip_comes_back_as_flat_float32_with_the_voice_the_pins_and_the_settings(self):
        engine = engines.Phonon("cpu")
        wav, rate, info = engine.speak("Hello there.", voice="old mortlake", seed=7, temperature=0.5, duration_delta=0.0)
        self.assertEqual((wav.dtype, wav.shape, rate), (np.dtype("float32"), (2400,), 24000))
        self.assertEqual(FakeTTS.instances[0].calls[-1], ("synth", "Hello there.", {"voice": "Old Mortlake", "seed": 7, "temperature": 0.5}))
        self.assertEqual(info["checkpoint"], ENTRY["checkpoint"])
        self.assertEqual(info["checkpoint_revision"], ENTRY["local_revision"])
        self.assertEqual((info["voices_checkpoint"], info["voices_revision"]),
                         (ENTRY["backend"]["voices_checkpoint"], ENTRY["backend"]["voices_revision"]))
        self.assertEqual((info["backend"], info["backend_version"], info["device"]), ("ptts", "0.5.0", "cpu"))
        self.assertEqual((info["engine_id"], info["language"], info["speaker"]), (phonon_support.PHONON_ENGINE_ID, "English", "Old Mortlake"))
        self.assertEqual((info["seed"], info["temperature"], info["duration_delta"]), (7, 0.5, 0.0))
        self.assertGreaterEqual(info["total_s"], 0.0)

    def test_no_voice_named_speaks_in_the_default_voice_and_an_unknown_one_is_refused(self):
        engine = engines.Phonon("cpu")
        _wav, _rate, info = engine.speak("Hello.")
        self.assertEqual(info["speaker"], "Harper")
        with self.assertRaisesRegex(ValueError, "no installed voice named 'Nobody'"):
            engine.speak("Hello.", voice="Nobody")

    def test_the_pace_condition_is_set_only_when_it_changes_and_cleared_for_the_models_own_pace(self):
        engine = engines.Phonon("cpu")
        tts = FakeTTS.instances[0]
        engine.speak("One.")
        engine.speak("Two.")
        engine.speak("Three.", duration_delta=-0.3)
        engine.speak("Four.", duration_delta=-0.3)
        engine.speak("Five.", duration_delta=0.0)
        conditions = [call[1] for call in tts.calls if call[0] == "set_conditions"]
        self.assertEqual(conditions, [{}, {"duration_delta": -0.3}, {}])
        order = [call[0] if call[0] == "synth" else "set" for call in tts.calls]
        self.assertEqual(order, ["set", "synth", "synth", "set", "synth", "synth", "set", "synth"],
                         "a pace is set before the clip that uses it")

    def test_the_other_engines_are_still_built_by_their_keys(self):
        with self.assertRaises(KeyError):
            engines.build_engine("not-an-engine", "cpu")


if __name__ == "__main__":
    unittest.main()
