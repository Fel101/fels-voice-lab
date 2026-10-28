"""VoxCPM2 integration boundaries; the GPU and generation checks need the real model and are not run here."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from cast_store import DIRECTION_ENGINES
from voxcpm_worker import MIN_GPU_GIB, _integer, _number, check_gpu_memory
from worker_pool import EnginePool, read_catalog
from tests.page_source import page_source
from worker_runtime import ENGINE_IDS

ROOT = Path(__file__).resolve().parents[1]


class VoxCPMContractTests(unittest.TestCase):
    def pool(self):
        return EnginePool("cpu", 0, worker_factory=lambda entry: self.fail("invalid request started a worker"),
                          idle_poll_seconds=0.1)

    def test_catalog_and_worker_are_pinned_and_character_ready(self):
        catalog = read_catalog()
        row = next(item for item in catalog["engines"] if item["id"] == "voxcpm2")
        self.assertTrue(row["integration"]["service_api"])
        self.assertEqual(row["local_revision"], "32279effe8c19989596f05d353d1447f51d9e915")
        self.assertIn("voxcpm2", ENGINE_IDS)
        self.assertIn("voxcpm2", DIRECTION_ENGINES)
        self.assertEqual(row["capabilities"]["native_sample_rate"]["hz"], 48_000)
        self.assertEqual(row["capabilities"]["voice_direction"]["status"], "experimental")
        self.assertEqual(json.loads((ROOT / "catalog/engines.json").read_text(encoding="utf-8")), catalog)

    def test_mode_specific_api_rejects_ignored_parameters_before_worker_start(self):
        pool = self.pool()
        try:
            with self.assertRaisesRegex(ValueError, "non-empty voice_description"):
                pool.speak({"engine": "voxcpm2", "mode": "design", "text": "Hello"})
            with self.assertRaisesRegex(ValueError, "requires reference_path"):
                pool.speak({"engine": "voxcpm2", "mode": "clone", "text": "Hello"})
            with self.assertRaisesRegex(ValueError, "requires prompt_path and prompt_text"):
                pool.speak({"engine": "voxcpm2", "mode": "continuation", "text": "Hello",
                            "reference_path": "reference/af_heart.wav"})
            with self.assertRaisesRegex(ValueError, "Unsupported VoxCPM2 design parameter.*temperature"):
                pool.speak({"engine": "voxcpm2", "mode": "design", "text": "Hello",
                            "voice_description": "A clear voice", "temperature": 0.9})
            with self.assertRaisesRegex(ValueError, "Unsupported VoxCPM2 continuation parameter.*direction"):
                pool.speak({"engine": "voxcpm2", "mode": "continuation", "text": "Hello",
                            "reference_path": "reference/af_heart.wav", "prompt_path": "reference/af_heart.wav",
                            "prompt_text": "A transcript", "direction": "Angry"})
            self.assertEqual(pool.loaded, [])
        finally:
            pool.close()

    def test_numeric_controls_reject_invalid_values(self):
        self.assertEqual(_integer({"seed": 7}, "seed", 0, 0, 10), 7)
        self.assertEqual(_integer({"inference_timesteps": "10"}, "inference_timesteps", 10, 1, 100), 10)
        self.assertAlmostEqual(_number({"cfg_value": 2}, "cfg_value", 2, 0.1, 10), 2)
        for callback in (
            lambda: _integer({"seed": True}, "seed", 0, 0, 10),
            lambda: _integer({"inference_timesteps": 1.5}, "inference_timesteps", 10, 1, 100),
            lambda: _number({"cfg_value": float("nan")}, "cfg_value", 2, 0.1, 10),
            lambda: _number({"cfg_value": 0}, "cfg_value", 2, 0.1, 10),
        ):
            with self.assertRaises(ValueError):
                callback()

    def test_studio_exposes_design_clone_continuation_and_actual_controls(self):
        page = page_source()
        for control in ("voxMode", "voxDescription", "voxReference", "voxPromptText",
                        "voxSeed", "voxCfg", "voxSteps", "inference_timesteps",
                        "save_voice_as", "voxDirection"):
            self.assertIn(control, page)


class GpuMemoryTests(unittest.TestCase):
    GIB = 1024 ** 3

    def test_a_gpu_that_is_too_small_is_refused_by_name_and_size(self):
        with self.assertRaisesRegex(RuntimeError, rf"at least {MIN_GPU_GIB} GB.*Small GPU has 2\.0 GB"):
            check_gpu_memory("Small GPU", 2 * self.GIB)

    def test_the_limit_itself_is_allowed_and_a_byte_less_is_not(self):
        check_gpu_memory("Any GPU", MIN_GPU_GIB * self.GIB)
        with self.assertRaises(RuntimeError):
            check_gpu_memory("Any GPU", MIN_GPU_GIB * self.GIB - 1)

    def test_the_name_of_the_card_does_not_matter(self):
        for name in ("AMD Radeon AI PRO R9700", "NVIDIA GeForce RTX 4070", "Something else"):
            with self.subTest(name=name):
                check_gpu_memory(name, 12 * self.GIB)


if __name__ == "__main__":
    unittest.main()
