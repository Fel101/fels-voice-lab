"""Local regression check: the plan, the measures, the comparison rules and the save/check flow, without a real engine."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine_checks  # noqa: E402
import evaluation_regression as er  # noqa: E402
from evaluation_suites import get_suite  # noqa: E402

VOICES = ("af_heart", "bf_emma")
SUITE = {"suite_id": "unit-suite", "version": 1, "repeat_count": 1, "cases": [
    {"case_id": "greeting", "text": "Hello there friend.", "language": "English", "direction": ""},
    {"case_id": "morning", "text": "Good morning everyone.", "language": "English", "direction": ""},
]}


class FakePool:
    """Stands in for worker_pool.EnginePool: speaks a fixed tone, or fails to load when broken."""

    device = "cpu"

    def __init__(self, broken=False):
        self.broken = broken
        self.entries = {"kokoro": {"id": "kokoro", "checkpoint": "fake/kokoro", "local_revision": "r1",
                                   "runtime": {"python": ".venv"}}}

    def load(self, engine):
        if self.broken:
            raise RuntimeError("missing package: fake-tts")
        return {"supported_languages": [], "load_seconds": 0.0}

    def speak(self, request):
        time = np.arange(round(0.5 * 24_000), dtype=np.float32) / 24_000
        return (0.1 * np.sin(2 * np.pi * 220 * time)).astype(np.float32), 24_000, {"total_s": 0.1}

    def unload(self):
        pass

    def close(self):
        pass


def fake_asr_for(texts, *, drop=None):
    """A recognizer stand-in that writes back the suite's text, minus one word for the clip ids in ``drop``."""
    def run(manifest):
        items = json.loads(Path(manifest).read_text(encoding="utf-8"))
        heard = {}
        for item in items:
            case_id = item["id"].split(":")[1]
            words = texts[case_id].split()
            if drop and item["id"] in drop:
                words = words[:-1]
            heard[item["id"]] = " ".join(words)
        return heard
    return run


def suite_texts(suite):
    return {case["case_id"]: case["text"] for case in suite["cases"]}


def clip(status="ok", error_rate=0.0, speaker=0.9, audio_seconds=3.0, reason=None):
    return {"status": status, "reason": reason, "error_rate": error_rate, "speaker": speaker,
            "audio_seconds": audio_seconds}


def result(clips):
    rates = [item["error_rate"] for item in clips.values() if item["status"] == "ok" and item["error_rate"] is not None]
    speakers = [item["speaker"] for item in clips.values() if item["status"] == "ok" and item["speaker"] is not None]
    return {"clips": clips, "summary": {
        "mean_error_rate": round(sum(rates) / len(rates), 4) if rates else None,
        "mean_speaker": round(sum(speakers) / len(speakers), 4) if speakers else None}}


class PlanTests(unittest.TestCase):
    def test_a_language_the_engine_does_not_claim_is_skipped_with_a_reason(self):
        suite = {**SUITE, "cases": SUITE["cases"] + [
            {"case_id": "konnichiwa", "text": "Konnichiwa.", "language": "Japanese", "direction": ""}]}
        plan = er.plan_suite(suite, "kokoro", repeats=2, voices=VOICES)
        self.assertEqual(len(plan), 6)
        english = [item for item in plan if item["case_id"] == "greeting"]
        self.assertEqual([item["id"] for item in english], ["kokoro:greeting:1", "kokoro:greeting:2"])
        self.assertTrue(all(item["skip"] is None and item["voice"] == "af_heart" for item in english))
        japanese = [item for item in plan if item["case_id"] == "konnichiwa"]
        self.assertTrue(all("does not claim Japanese" in item["skip"] for item in japanese))

    def test_the_suite_repeat_count_is_the_default(self):
        suite = get_suite("neutral-clone-three-line")
        plan = er.plan_suite(suite, "kokoro", voices=VOICES)
        self.assertEqual(len(plan), len(suite["cases"]) * suite["repeat_count"])


class MeasureTests(unittest.TestCase):
    def test_a_dropped_word_shows_as_a_word_error_and_speaker_is_left_out_for_kokoro(self):
        texts = suite_texts(SUITE)
        with tempfile.TemporaryDirectory(prefix="voice-lab-regression-") as temp:
            out_dir = Path(temp)
            with mock.patch.object(engine_checks, "installation", return_value={"installed": True, "missing": []}):
                records = er.speak_suite("kokoro", SUITE, pool=FakePool(), out_dir=out_dir, voices=VOICES)
            er.measure(records, out_dir, engine="kokoro",
                       run_asr=fake_asr_for(texts, drop={"kokoro:morning:1"}), similarity=None)
            summary = er.summarize(records)
        self.assertEqual((summary["ok"], summary["failed"], summary["unavailable"]), (2, 0, 0))
        self.assertEqual(len({record["wav"] for record in records}), 2)  # two cases in one language keep two files
        self.assertAlmostEqual(summary["mean_error_rate"], (0 + 1 / 3) / 2, delta=0.001)
        self.assertIsNone(summary["mean_speaker"])
        self.assertTrue(all(record["speaker"] is None for record in records))


class CompareTests(unittest.TestCase):
    def baseline(self, **clips):
        return result({"a": clip(), "b": clip(error_rate=0.1, audio_seconds=4.0), **clips})

    def test_identical_runs_have_no_problems(self):
        self.assertEqual(er.compare(self.baseline(), self.baseline()), [])

    def test_a_small_rise_in_word_error_is_within_the_margin(self):
        current = result({"a": clip(error_rate=0.05), "b": clip(error_rate=0.1, audio_seconds=4.0)})
        self.assertEqual(er.compare(current, self.baseline()), [])

    def test_a_large_rise_in_word_error_is_a_regression(self):
        current = result({"a": clip(error_rate=0.3), "b": clip(error_rate=0.4, audio_seconds=4.0)})
        problems = er.compare(current, self.baseline())
        self.assertTrue(any(problem.startswith("mean word error rate rose") for problem in problems))

    def test_a_fall_in_the_speaker_proxy_beyond_the_margin_is_a_regression(self):
        current = result({"a": clip(speaker=0.7), "b": clip(error_rate=0.1, speaker=0.7, audio_seconds=4.0)})
        problems = er.compare(current, self.baseline())
        self.assertTrue(any(problem.startswith("mean speaker proxy fell") for problem in problems))

    def test_a_clip_that_stopped_speaking_is_a_regression_with_its_reason(self):
        current = result({"a": clip(status="failed", reason="missing package: fake-tts"),
                          "b": clip(error_rate=0.1, audio_seconds=4.0)})
        problems = er.compare(current, self.baseline())
        self.assertIn("a: spoke in the baseline, now failed: missing package: fake-tts", problems)

    def test_a_length_change_beyond_the_tolerance_is_a_regression(self):
        longer = result({"a": clip(audio_seconds=4.5), "b": clip(error_rate=0.1, audio_seconds=4.0)})
        self.assertTrue(any("in the baseline" in problem for problem in er.compare(longer, self.baseline())))
        close = result({"a": clip(audio_seconds=3.6), "b": clip(error_rate=0.1, audio_seconds=4.0)})
        self.assertEqual(er.compare(close, self.baseline()), [])

    def test_a_clip_that_was_not_spoken_in_the_baseline_is_not_compared(self):
        baseline = result({"a": clip(status="unavailable", reason="not installed"), "b": clip(error_rate=0.1, audio_seconds=4.0)})
        current = result({"a": clip(error_rate=0.9), "b": clip(error_rate=0.1, audio_seconds=4.0)})
        self.assertEqual(er.compare(current, baseline), [])

    def test_a_changed_clip_set_asks_for_a_new_baseline(self):
        current = result({"a": clip(), "c": clip()})
        self.assertEqual(er.compare(current, self.baseline()),
                         ["the suite's clips differ from the baseline's; save a new baseline before comparing"])


class CommandTests(unittest.TestCase):
    def test_check_without_a_baseline_stops_before_speaking_anything(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-regression-") as temp, \
                mock.patch.object(er, "BASELINES", Path(temp)), \
                mock.patch.object(er, "_execute") as execute, contextlib.redirect_stderr(io.StringIO()):
            code = er.main(["check", "--suite", "neutral-clone-three-line", "--engine", "kokoro"])
        self.assertEqual(code, 2)
        execute.assert_not_called()

    def test_an_unknown_suite_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(er.main(["check", "--suite", "no-such-suite", "--engine", "kokoro"]), 2)

    def test_save_then_check_passes_and_a_broken_engine_is_a_regression(self):
        texts = suite_texts(SUITE)
        with tempfile.TemporaryDirectory(prefix="voice-lab-regression-") as temp, \
                mock.patch.object(er, "BASELINES", Path(temp) / "baselines"), \
                mock.patch.object(engine_checks, "installation", return_value={"installed": True, "missing": []}), \
                mock.patch("kokoro_support.discover_kokoro_voices", return_value=list(VOICES)), \
                mock.patch.object(er.lm, "_run_asr", fake_asr_for(texts)), \
                mock.patch.object(er, "get_suite", return_value=SUITE), contextlib.redirect_stdout(io.StringIO()):
            out = Path(temp) / "run"
            args = ["--suite", SUITE["suite_id"], "--engine", "kokoro", "--out-dir", str(out)]
            self.assertEqual(er.main(["save-baseline", *args], pool_factory=FakePool), 0)
            self.assertEqual(er.main(["check", *args], pool_factory=FakePool), 0)
            self.assertEqual(er.main(["check", *args], pool_factory=lambda: FakePool(broken=True)), 3)


if __name__ == "__main__":
    unittest.main()
