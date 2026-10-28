"""Qwen API boundaries and catalog behavior; real generation is not exercised here."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qwen_worker import QwenWorker, _boolean, _integer, _number
from worker_pool import EnginePool, read_catalog
from tests.audio_fixtures import owned_reference
from tests.page_source import page_source


class QwenContractTests(unittest.TestCase):
    def pool(self):
        return EnginePool("cpu", 0, worker_factory=lambda entry: self.fail("invalid request started a worker"),
                          idle_poll_seconds=0.1)

    def test_catalog_keeps_faster_and_stock_qwen_identities_separate(self):
        entries = {item["id"]: item for item in read_catalog()["engines"]}
        fast = entries["qwen3-tts-1.7b-voice-design"]
        self.assertTrue(fast["integration"]["service_api"])
        self.assertEqual(fast["backend"]["name"], "faster-qwen3-tts")
        self.assertEqual(fast["backend"]["comparison_runtime"]["environment"], ".venv-qwen")
        self.assertFalse(fast["backend"]["comparison_runtime"]["service_api"])
        self.assertEqual(entries["qwen3-tts-1.7b-custom-voice"]["local_revision"],
                         QwenWorker("qwen3-tts-1.7b-custom-voice", "cuda").spec["revision"])

    def test_mode_specific_allowlist_rejects_unsupported_fields_before_startup(self):
        pool = self.pool()
        try:
            with self.assertRaisesRegex(ValueError, "supports mode 'design'"):
                pool.speak({"engine": "qwen3-tts-1.7b-voice-design", "mode": "clone", "text": "Hello"})
            with self.assertRaisesRegex(ValueError, "Unsupported Qwen design parameter.*cfg_scale"):
                pool.speak({"engine": "qwen3-tts-1.7b-voice-design", "text": "Hello",
                            "voice_description": "A clear voice", "cfg_scale": 4.0})
            with self.assertRaisesRegex(ValueError, "requires reference_path"):
                pool.speak({"engine": "qwen3-tts-1.7b-base", "text": "Hello", "mode": "clone",
                            "reference_text": "A transcript"})
            with self.assertRaisesRegex(ValueError, "requires a speaker"):
                pool.speak({"engine": "qwen3-tts-1.7b-custom-voice", "text": "Hello", "mode": "custom_voice"})
            self.assertEqual(pool.loaded, [])
        finally:
            pool.close()

    def test_local_reference_resolver_accepts_project_assets_and_rejects_escape(self):
        reference = owned_reference(self, ROOT)
        self.assertEqual(QwenWorker._reference_path(reference), reference.resolve())
        with self.assertRaisesRegex(ValueError, "under voice-lab/reference/, voice-lab/data/voices/, or voice-lab/data/cast/media/references/"):
            QwenWorker._reference_path(ROOT / "README.md")

    def test_generation_boundaries_reject_nonfinite_and_boolean_numeric_values(self):
        self.assertEqual(_integer({"seed": 42}, "seed", 1, 0, 100), 42)
        self.assertAlmostEqual(_number({"temperature": 0.9}, "temperature", 1, 0.05, 2), 0.9)
        self.assertTrue(_boolean({"xvec_only": True}, "xvec_only"))
        for callback in (
            lambda: _integer({"seed": True}, "seed", 1, 0, 100),
            lambda: _integer({"seed": 3.5}, "seed", 1, 0, 100),
            lambda: _number({"temperature": float("nan")}, "temperature", 1, 0.05, 2),
            lambda: _number({"top_p": 1.1}, "top_p", 1, 0.05, 1),
            lambda: _boolean({"xvec_only": "yes"}, "xvec_only"),
        ):
            with self.assertRaises(ValueError):
                callback()

    def test_page_has_model_specific_qwen_fields(self):
        page = page_source()
        for field in ("qwenPanel", "qwenLanguage", "qwenDescription", "qwenReferenceText",
                      "qwenXvecOnly", "qwenSpeaker", "qwenCloneDirection", "qwenMaxTokens"):
            self.assertIn(field, page)


if __name__ == "__main__":
    unittest.main()
