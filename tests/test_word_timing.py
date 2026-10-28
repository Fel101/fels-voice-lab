import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import align_worker as aw
import fit
import word_timing as wt
from cast_store import CAST_SCHEMA_VERSION, CastStore
from tools import scene_export

ROOT = Path(__file__).resolve().parents[1]
SR = 16000
LIBRARY = importlib.util.find_spec("librosa") is not None


def speech_clip(seconds=2.0, start=0.2, end=1.8, level=0.3):
    audio = np.zeros(int(seconds * SR), dtype=np.float32)
    t = np.arange(int((end - start) * SR)) / SR
    audio[int(start * SR):int(end * SR)] = level * np.sin(2 * np.pi * 220 * t)
    return audio


class FakeTokenizer:
    def __init__(self, tokenizer, multilingual, task=None, language=None):
        self.language = language

    def encode(self, text):
        return [ord(ch) for ch in text if not ch.isspace()][:6]


class FakeModel:
    hf_tokenizer = None

    class _Inner:
        is_multilingual = True

    model = _Inner()

    def __init__(self, timings):
        self.timings = timings

    def feature_extractor(self, audio):
        return np.zeros((80, max(1, audio.size // 160)), dtype=np.float32)

    def encode(self, features):
        return features

    def find_alignment(self, tokenizer, text_tokens, encoder_output, num_frames):
        return [self.timings]


def timing(word, start, end, probability=0.9):
    return {"word": word, "start": start, "end": end, "probability": probability, "tokens": [1]}


class AlignWorkerTests(unittest.TestCase):
    def test_speech_bounds_find_the_loud_part_of_the_clip(self):
        start, end = aw.speech_bounds(speech_clip())
        self.assertAlmostEqual(start, 0.2, delta=0.03)
        self.assertAlmostEqual(end, 1.8, delta=0.03)
        self.assertEqual(aw.speech_bounds(np.zeros(SR, dtype=np.float32)), (None, None))

    def test_punctuation_joins_the_word_before_it_and_keeps_no_time_of_its_own(self):
        merged = aw.merge_punctuation([{"word": "station", "start": 1.0, "end": 1.5, "probability": 0.9},
                                       {"word": ".", "start": 1.5, "end": 1.9, "probability": 0.2}])
        self.assertEqual(merged, [{"word": "station.", "start": 1.0, "end": 1.5, "probability": 0.9}])

    def test_snap_moves_leading_and_trailing_silence_onto_the_speech(self):
        words = [{"word": "a", "start": 0.0, "end": 0.8}, {"word": "b", "start": 0.8, "end": 2.0}]
        snapped, edges = aw.snap_to_speech(words, speech_clip())
        self.assertEqual(edges, ["first word start", "last word end"])
        self.assertAlmostEqual(snapped[0]["start"], 0.2, delta=0.03)
        self.assertAlmostEqual(snapped[-1]["end"], 1.8, delta=0.03)

    def test_checks_pass_words_on_speech_and_review_words_in_silence(self):
        good = [{"word": "a", "start": 0.2, "end": 1.0}, {"word": "b", "start": 1.0, "end": 1.8}]
        self.assertEqual(aw.quality_checks(good, speech_clip())["status"], "ok")
        silent = [{"word": "a", "start": 0.0, "end": 0.1}, {"word": "b", "start": 0.1, "end": 0.15}]
        checks = aw.quality_checks(silent, speech_clip())
        self.assertEqual(checks["status"], "review")
        self.assertGreater(checks["silent_word_fraction"], aw.SILENT_WORD_MAX)

    def test_aligner_returns_words_checks_and_edges_for_one_clip(self):
        model = FakeModel([timing("hello", 0.0, 0.9), timing("world", 0.9, 1.6), timing(".", 1.6, 1.7)])
        aligner = aw.Aligner(model, lambda path, sampling_rate: speech_clip(), lambda x: x, FakeTokenizer)
        result = aligner.align_item({"id": "a", "audio": "x.wav", "text": "hello world.", "language": "en"})
        self.assertEqual([word["word"] for word in result["words"]], ["hello", "world."])
        self.assertAlmostEqual(result["words"][0]["start"], 0.2, delta=0.03)
        self.assertEqual(result["checks"]["snapped_edges"], ["first word start"])
        self.assertAlmostEqual(result["duration_seconds"], 2.0, places=2)

    def test_a_bad_clip_becomes_an_error_entry_and_the_batch_continues(self):
        aligner = aw.Aligner(FakeModel([timing("x", 0.0, 0.5)]), lambda path, sampling_rate: speech_clip(seconds=31.0),
                             lambda x: x, FakeTokenizer)
        result = aligner.align_item({"id": "long", "audio": "long.wav", "text": "x", "language": "en"})
        self.assertEqual(result["id"], "long")
        self.assertIn("alignment takes clips up to 30 s", result["error"])
        self.assertNotIn("words", result)


class WordTimingTests(unittest.TestCase):
    def test_only_the_markup_an_engine_acts_on_is_removed(self):
        line = "[laugh] Hello (sighs) there <|speaker:1|> friend"
        self.assertEqual(wt.speech_text(line, "turbo"), "Hello (sighs) there friend")
        self.assertEqual(wt.speech_text(line, "drama-3"), "Hello (sighs) there friend")
        self.assertEqual(wt.speech_text(line, "breeze-tts-2"), "[laugh] Hello there friend")
        for engine in ("kokoro", "orig", "voxcpm2", "qwen3-tts-1.7b-base", "qwen3-tts-1.7b-voice-design", None):
            with self.subTest(engine=engine):
                self.assertEqual(wt.speech_text(line, engine), "[laugh] Hello (sighs) there friend")

    def test_text_for_another_engine_loses_every_markup_syntax(self):
        self.assertEqual(wt.text_without_markup("[laugh] Hello (sighs) there <|speaker:1|> friend"), "Hello there friend")

    def test_words_in_parentheses_that_an_engine_speaks_reach_the_aligner(self):
        takes = [{"id": "k", "engine_id": "kokoro", "language": "English", "spoken_text": "Well (she paused) fine.",
                  "audio_sha256": "aa", "media_path": "k.wav"}]
        sent = []

        def fake_worker(items, scratch_dir):
            sent.extend(items)
            return {"aligner": {"id": "fake"}, "items": [{"id": "k", "duration_seconds": 2.0, "words": [], "checks": {}}]}

        records = wt.align_takes(takes, run_worker=fake_worker)
        self.assertEqual(sent[0]["text"], "Well (she paused) fine.")
        self.assertEqual(records["k"]["text"], "Well (she paused) fine.")

    def test_language_labels_map_to_whisper_codes(self):
        self.assertEqual(wt.whisper_code("American English"), "en")
        self.assertEqual(wt.whisper_code("Chinese"), "zh")
        self.assertIsNone(wt.whisper_code("Atlantean"))
        self.assertEqual(wt.joiner_for("zh"), "")
        self.assertEqual(wt.joiner_for("en"), " ")

    def test_batch_alignment_records_words_errors_and_the_aligner(self):
        takes = [
            {"id": "t1", "language": "English", "spoken_text": "Hello world.", "audio_sha256": "aa", "media_path": "a.wav"},
            {"id": "t2", "language": "Atlantean", "spoken_text": "Hi.", "audio_sha256": "bb", "media_path": "b.wav"},
            {"id": "t3", "engine_id": "turbo", "language": "English", "spoken_text": "[laugh]", "audio_sha256": "cc",
             "media_path": "c.wav"},
        ]
        calls = []

        def fake_worker(items, scratch_dir):
            calls.append([item["id"] for item in items])
            return {"aligner": {"id": "fake"}, "items": [
                {"id": "t1", "duration_seconds": 1.5, "words": [{"word": "Hello", "start": 0.1, "end": 0.6, "probability": 0.9}],
                 "checks": {"status": "ok"}}]}

        records = wt.align_takes(takes, run_worker=fake_worker)
        self.assertEqual(calls, [["t1"]], "one aligner call for the whole batch, and only for takes that can be aligned")
        self.assertEqual(records["t1"]["aligner"], {"id": "fake"})
        self.assertEqual(records["t1"]["words"][0]["word"], "Hello")
        self.assertIn("no Whisper code", records["t2"]["error"])
        self.assertIn("no speech text", records["t3"]["error"])

    def test_phrases_break_at_sentence_ends_and_at_the_length_limit(self):
        words = [{"word": "Hello", "start": 0.0, "end": 0.4}, {"word": "there.", "start": 0.4, "end": 0.9},
                 {"word": "Next", "start": 1.0, "end": 1.3}]
        phrases = wt.word_phrases(words, " ")
        self.assertEqual([phrase["text"] for phrase in phrases], ["Hello there.", "Next"])
        long_words = [{"word": "w" * 20, "start": index, "end": index + 0.5} for index in range(3)]
        self.assertEqual(len(wt.word_phrases(long_words, " ")), 2)

    def test_shifted_words_follow_the_take_order_in_the_scene_mix(self):
        shifted = wt.shifted_words([(1.2, [{"word": "a", "start": 0.1, "end": 0.5}]),
                                    (1.0, [{"word": "b", "start": 0.0, "end": 0.3}])])
        self.assertEqual([(item["start"], item["end"]) for item in shifted], [(0.1, 0.5), (1.2, 1.5)])


class StoreTimingTests(unittest.TestCase):
    def test_a_new_cast_database_has_the_timing_column_and_no_unaligned_takes_to_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CastStore(Path(tmp))
            self.assertEqual(store._row("PRAGMA user_version")[0], CAST_SCHEMA_VERSION)
            self.assertEqual(store.take_ids_without_word_timings(), [])

    def test_an_empty_stored_value_means_not_aligned_not_aligned_without_words(self):
        self.assertIsNone(wt.stored_timings(""))
        self.assertEqual(wt.stored_timings('{"words": []}'), {"words": []})


class ExporterCaptionTests(unittest.TestCase):
    def timed(self):
        return [{"line_id": "L1", "character_name": "Mira", "text": "Hello there. Bye now.",
                 "start_seconds": 2.0, "end_seconds": 5.0}]

    def test_a_line_with_word_times_becomes_phrases_on_the_scene_clock(self):
        take1 = {"language": "English", "probe": {"duration_seconds": 1.2},
                 "word_timings": {"words": [{"word": "Hello", "start": 0.1, "end": 0.5},
                                            {"word": "there.", "start": 0.5, "end": 1.0}]}}
        take2 = {"language": "English", "probe": {"duration_seconds": 1.0},
                 "word_timings": {"words": [{"word": "Bye", "start": 0.0, "end": 0.3},
                                            {"word": "now.", "start": 0.3, "end": 0.8}]}}
        cues = scene_export.caption_cues_for_scene(self.timed(), [{"line": {"line_id": "L1"}, "takes": [take1, take2]}])
        self.assertEqual([cue["text"] for cue in cues], ["Hello there.", "Bye now."])
        self.assertAlmostEqual(cues[0]["start_seconds"], 2.1, places=3)
        self.assertAlmostEqual(cues[1]["start_seconds"], 3.2, places=3)
        self.assertEqual([cue["show_speaker"] for cue in cues], [True, False])
        srt, _ = scene_export.subtitle_text(cues)
        self.assertIn("Mira: Hello there.", srt)
        self.assertNotIn("Mira: Bye now.", srt)

    def test_a_line_reports_its_slot_fit_and_the_scene_mix_keeps_the_take_as_rendered(self):
        self.assertIsNone(scene_export.line_fit(1.0, None))
        self.assertEqual(scene_export.line_fit(1.0, 1000)["status"], "within_slot")
        self.assertEqual(scene_export.line_fit(1.0, 1400)["status"], "needs_rerender")

    def test_a_line_without_word_times_keeps_its_line_caption(self):
        take = {"language": "English", "probe": {"duration_seconds": 1.0}, "word_timings": None}
        cues = scene_export.caption_cues_for_scene(self.timed(), [{"line": {"line_id": "L1"}, "takes": [take]}])
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "Hello there. Bye now.")
        self.assertTrue(cues[0]["show_speaker"])


class FitTests(unittest.TestCase):
    def test_the_status_follows_the_bound(self):
        self.assertEqual(fit.fit_status(1.0, 1.005)["status"], fit.STATUS_WITHIN_SLOT)
        self.assertEqual(fit.fit_status(1.0, 1.10)["status"], fit.STATUS_STRETCHED)
        self.assertEqual(fit.fit_status(1.0, 1.40)["status"], fit.STATUS_NEEDS_RERENDER)
        with self.assertRaises(ValueError):
            fit.fit_status(1.0, 1.1, max_ratio=0.9)

    @unittest.skipUnless(LIBRARY, "librosa is optional and is not installed; only the stretch needs it")
    def test_a_stretch_within_the_bound_reaches_the_slot_and_says_so(self):
        result = fit.fit_to_slot(speech_clip(seconds=1.0, start=0.0, end=1.0), SR, 1.05)
        self.assertEqual(result["status"], fit.STATUS_STRETCHED)
        self.assertAlmostEqual(result["actual_seconds"], 1.05, delta=0.03)
        self.assertIn("listen", result["note"])

    def test_a_take_outside_the_bound_is_not_changed(self):
        audio = speech_clip(seconds=1.0, start=0.0, end=1.0)
        result = fit.fit_to_slot(audio, SR, 2.0)
        self.assertEqual(result["status"], fit.STATUS_NEEDS_RERENDER)
        self.assertEqual(result["audio"].size, audio.size)

    def test_a_stretch_without_librosa_says_what_is_missing_and_other_fits_still_work(self):
        audio = speech_clip(seconds=1.0, start=0.0, end=1.0)
        with mock.patch.dict(sys.modules, {"librosa": None}):  # an install without the optional package
            with self.assertRaisesRegex(RuntimeError, "librosa"):
                fit.fit_to_slot(audio, SR, 1.05)
            self.assertEqual(fit.fit_to_slot(audio, SR, 2.0)["status"], fit.STATUS_NEEDS_RERENDER)
            self.assertEqual(fit.fit_to_slot(audio, SR, 1.0)["status"], fit.STATUS_WITHIN_SLOT)

    def test_the_status_check_and_the_exporter_import_without_librosa(self):
        # A fresh interpreter, because an earlier import in this process would hide a module-level import.
        code = ("import sys; sys.modules['librosa'] = None; sys.path.insert(0, sys.argv[1]); "
                "import fit; from tools import scene_export; print(fit.fit_status(1.0, 1.1)['status'])")
        result = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        self.assertEqual(result.stdout.strip(), fit.STATUS_STRETCHED)


if __name__ == "__main__":
    unittest.main()
