"""VoxCPM2 integration boundaries; the GPU and generation checks need the real model and are not run here."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from cast_store import DIRECTION_ENGINES
from engine_checks import CHECK_TEXT, CHECK_VOICE
from voxcpm_worker import (MIN_GPU_GIB, RUN_ON_PATCHES_PER_TOKEN, RUN_ON_TRIES, VoxCPMWorker, _integer, _number,
                           check_gpu_memory, generate_until_it_ends, retry_seed, run_on_ratio, token_count)
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


RATE = 48_000
PATCH = 0.16                       # seconds in one VoxCPM2 patch: 4 latent frames of 640 samples at 16 kHz
PATCH_SAMPLES = int(RATE * PATCH)


def words(text):
    """A stand-in tokenizer: one token a word."""
    return text.split()


class FakeVoxCPM:
    """VoxCPM's generate as far as a take's length goes: a seed in `runs_on` never stops, so its take is cut at VoxCPM's
    cap, int(tokens * ratio + 10) patches; any other seed ends after `ends_after` patches."""

    def __init__(self, runs_on=(), ends_after=18, report_seed=None):
        self.runs_on, self.ends_after, self.report_seed = set(runs_on), ends_after, report_seed
        self.calls = []
        audio_vae = SimpleNamespace(chunk_size=640, sample_rate=16_000)
        self.tts_model = SimpleNamespace(text_tokenizer=words, last_successful_seed=None, patch_size=4, audio_vae=audio_vae,
                                         sample_rate=RATE)

    def generate(self, **call):
        self.calls.append(call)
        cap = int(len(words(call["text"])) * call["retry_badcase_ratio_threshold"] + 10)
        patches = cap if call["seed"] in self.runs_on else min(self.ends_after, cap)
        self.tts_model.last_successful_seed = call["seed"] if self.report_seed is None else self.report_seed
        return np.full(patches * PATCH_SAMPLES, 0.1, dtype=np.float32)


def loaded_worker(model):
    worker = VoxCPMWorker("cuda")
    worker.model, worker.sample_rate, worker.runtime_identity = model, RATE, "identity"
    tts = model.tts_model
    worker.patch_seconds = tts.patch_size * tts.audio_vae.chunk_size / tts.audio_vae.sample_rate
    return worker


FAKE_TORCH = SimpleNamespace(cuda=SimpleNamespace(reset_peak_memory_stats=lambda: None, max_memory_allocated=lambda: 1))


class RunOnTests(unittest.TestCase):
    """A take that keeps going past its text is made again from another seed, and is never handed back as finished."""

    def test_the_cap_and_rule_follow_voxcpm_and_count_only_the_spoken_text(self):
        self.assertEqual(token_count(words, "one\ntwo   three "), 3)
        self.assertEqual(token_count(words, " "), 1)
        # The seed-42 take on the R9700: 20 tokens with the description, so the cap was 130 patches, 20.8 s.
        self.assertAlmostEqual((20 * RUN_ON_PATCHES_PER_TOKEN + 10) * PATCH, 20.8)
        # With the ratio the worker gives VoxCPM, the cap is the spoken text's alone.
        for model_tokens, spoken in ((20, 9), (13, 9), (9, 9), (1000, 2)):
            with self.subTest(model_tokens=model_tokens, spoken=spoken):
                cap = model_tokens * run_on_ratio(model_tokens, spoken) + 10
                self.assertAlmostEqual(cap, spoken * RUN_ON_PATCHES_PER_TOKEN + 10)

    def test_a_retry_seed_is_derived_from_the_requested_one_and_is_not_the_next_take_seed(self):
        self.assertEqual(retry_seed(42, 0), 42)
        tries = [retry_seed(42, attempt) for attempt in range(RUN_ON_TRIES)]
        self.assertEqual(tries, [retry_seed(42, attempt) for attempt in range(RUN_ON_TRIES)])
        self.assertEqual(len(set(tries)), RUN_ON_TRIES)
        for value in tries[1:]:
            self.assertTrue(0 <= value <= 2_147_483_647)
            self.assertNotIn(value, range(42, 50))       # the seeds of Perform's other takes
        self.assertNotEqual(retry_seed(42, 1), retry_seed(43, 1))

    def test_a_take_that_ends_is_kept_and_one_that_runs_on_is_made_again(self):
        made = []

        def generate_once(seed):
            made.append(seed)
            return np.zeros(int((30.0 if seed == 7 else 2.0) * RATE), dtype=np.float32)

        waveform, seed, tries = generate_until_it_ends(generate_once, 7, 10.0, RATE)
        self.assertEqual(made, [7, retry_seed(7, 1)])
        self.assertEqual(seed, retry_seed(7, 1))
        self.assertEqual(waveform.size, 2 * RATE)
        self.assertEqual(tries, [{"seed": 7, "seconds": 30.0}, {"seed": retry_seed(7, 1), "seconds": 2.0}])
        made.clear()
        self.assertEqual(generate_until_it_ends(generate_once, 8, 10.0, RATE)[1], 8)
        self.assertEqual(made, [8])

    def test_a_take_that_runs_on_every_time_is_refused_with_the_seeds_it_tried(self):
        seeds = [retry_seed(5, attempt) for attempt in range(RUN_ON_TRIES)]
        with self.assertRaisesRegex(RuntimeError, "past the end of the text in all 3 tries.*" + ", ".join(map(str, seeds))):
            generate_until_it_ends(lambda seed: np.zeros(11 * RATE, dtype=np.float32), 5, 10.0, RATE)

    def test_the_models_page_check_that_ran_on_gets_its_take_from_the_next_try(self):
        model = FakeVoxCPM(runs_on={42})
        worker = loaded_worker(model)
        request = {"text": CHECK_TEXT, "mode": "design", "voice_description": CHECK_VOICE, "seed": 42}
        with patch.dict(sys.modules, {"torch": FAKE_TORCH}):
            waveform, rate, info = worker.generate(request)
        spoken, prompt = len(words(CHECK_TEXT)), len(words(f"({CHECK_VOICE}){CHECK_TEXT}"))
        self.assertEqual([call["seed"] for call in model.calls], [42, retry_seed(42, 1)])
        for call in model.calls:
            self.assertEqual(call["text"], f"({CHECK_VOICE}){CHECK_TEXT}")
            self.assertFalse(call["retry_badcase"])
            self.assertAlmostEqual(call["retry_badcase_ratio_threshold"], RUN_ON_PATCHES_PER_TOKEN * spoken / prompt)
        self.assertEqual((rate, waveform.size), (RATE, 18 * PATCH_SAMPLES))
        self.assertEqual(info["requested_seed"], 42)
        self.assertEqual(info["effective_controls"]["seed"], retry_seed(42, 1))
        self.assertAlmostEqual(info["run_on"]["limit_seconds"], spoken * RUN_ON_PATCHES_PER_TOKEN * PATCH)
        first, second = info["run_on"]["tries"]
        self.assertEqual(first["seed"], 42)
        self.assertGreaterEqual(first["seconds"], info["run_on"]["limit_seconds"])
        self.assertEqual(second, {"seed": retry_seed(42, 1), "seconds": round(18 * PATCH, 3)})

    def test_a_take_that_ends_keeps_the_requested_seed(self):
        model = FakeVoxCPM(ends_after=6)           # about a second for two words; the limit is 1.92 s
        worker = loaded_worker(model)
        with patch.dict(sys.modules, {"torch": FAKE_TORCH}):
            _waveform, _rate, info = worker.generate({"text": "Hello there.", "mode": "design",
                                                      "voice_description": "A warm voice", "seed": 9})
        self.assertEqual([call["seed"] for call in model.calls], [9])
        self.assertEqual((info["requested_seed"], info["effective_controls"]["seed"]), (9, 9))
        self.assertEqual(len(info["run_on"]["tries"]), 1)

    def test_a_model_that_reports_another_seed_is_still_refused(self):
        worker = loaded_worker(FakeVoxCPM(ends_after=6, report_seed=1))
        with patch.dict(sys.modules, {"torch": FAKE_TORCH}):
            with self.assertRaisesRegex(RuntimeError, "used seed 1; requested seed was 9"):
                worker.generate({"text": "Hello there.", "mode": "design", "voice_description": "A warm voice", "seed": 9})


if __name__ == "__main__":
    unittest.main()
