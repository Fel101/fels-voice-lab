import importlib.util
import json
import sys
import unittest
from unittest import mock

import kokoro_support
import kokoro_timing as kt
import lipsync
from tests.phoneme_stub import stub_phonemes

MISAKI = importlib.util.find_spec("misaki") is not None


class FakeToken:
    def __init__(self, text, phonemes, whitespace=False):
        self.text = text
        self.phonemes = phonemes
        self.whitespace = whitespace


class KokoroTimingTests(unittest.TestCase):
    def test_durations_map_to_known_symbols_after_bos_and_before_eos(self):
        vocab = {"a": 1, "b": 2, "c": 3, " ": 4}
        frames = kt.phoneme_frames("ab c", [2, 3, 4, 5, 1, 6], vocab)
        self.assertEqual(frames, {0: (2, 5), 1: (5, 9), 2: (9, 14), 3: (14, 15)})

    def test_unknown_characters_are_dropped_before_the_model_so_they_get_no_time(self):
        vocab = {"a": 1, "c": 3}
        frames = kt.phoneme_frames("a?c", [2, 3, 4, 6], vocab)  # '?' is not in the vocabulary
        self.assertEqual(frames, {0: (2, 5), 2: (5, 9)})
        with self.assertRaises(ValueError):
            kt.phoneme_frames("ab c", [2, 3, 4], {"a": 1, "b": 2, "c": 3, " ": 4})

    def test_words_and_phonemes_come_out_in_seconds_with_the_chunk_offset(self):
        vocab = {"a": 1, "b": 2, "c": 3, " ": 4}
        tokens = [FakeToken("ab", "ab", whitespace=True), FakeToken("c", "c")]
        timing = kt.kokoro_timing("ab c", tokens, [2, 3, 4, 5, 1, 6], vocab, offset_seconds=1.0)
        self.assertEqual(timing["words"], [{"word": "ab", "start": 1.05, "end": 1.225},
                                           {"word": "c", "start": 1.35, "end": 1.375}])
        self.assertEqual([p["symbol"] for p in timing["phonemes"]], ["a", "b", " ", "c"])
        self.assertEqual(timing["unplaced_words"], 0)

    def test_a_token_that_is_not_in_the_string_is_counted_not_guessed(self):
        vocab = {"a": 1, "b": 2, " ": 3}
        timing = kt.kokoro_timing("ab", [FakeToken("zz", "zz")], [2, 1, 1, 2], vocab)
        self.assertEqual(timing["words"], [])
        self.assertEqual(timing["unplaced_words"], 1)

    def test_punctuation_adds_phonemes_but_no_word(self):
        vocab = {"a": 1, ".": 2}
        tokens = [FakeToken("A", "a"), FakeToken(".", ".")]
        timing = kt.kokoro_timing("a.", tokens, [1, 2, 3, 1], vocab)
        self.assertEqual([w["word"] for w in timing["words"]], ["A"])
        self.assertEqual(len(timing["phonemes"]), 2)


class PhonemeTableTests(unittest.TestCase):
    def test_every_symbol_in_kokoros_vocabulary_has_a_mouth_shape_or_is_marked_silent(self):
        try:
            config = kokoro_support.kokoro_snapshot_dir() / "config.json"
            vocab = json.loads(config.read_text(encoding="utf-8"))["vocab"]
        except (OSError, KeyError, ValueError):
            self.skipTest("the Kokoro snapshot is not on this machine")
        uncovered = [symbol for symbol in vocab
                     if symbol not in lipsync.SYMBOL_VISEME and symbol not in lipsync.MARKERS]
        self.assertEqual(uncovered, [], "add these symbols to SYMBOL_VISEME or MARKERS")
        self.assertTrue(all(value in lipsync.VISEMES for value in lipsync.SYMBOL_VISEME.values()))

    def test_markers_have_no_mouth_shape_and_no_time(self):
        self.assertIsNone(lipsync.viseme_for(" "))
        self.assertIsNone(lipsync.viseme_for("ˈ"))
        self.assertEqual(lipsync.viseme_for("p"), "closed")
        self.assertEqual(lipsync.viseme_for("I"), "open_wide")


class TrackTests(unittest.TestCase):
    def test_a_word_span_is_shared_out_in_proportion_to_phoneme_weight_and_fills_the_span(self):
        spans = lipsync.distribute(1.0, 2.0, ["ɛ", "k", "s", "t"])
        self.assertAlmostEqual(spans[0][1], 1.0)
        self.assertAlmostEqual(spans[-1][2], 2.0)
        lengths = [end - start for _, start, end in spans]
        self.assertAlmostEqual(sum(lengths), 1.0)
        self.assertGreater(lengths[0], lengths[1], "a vowel takes more time than a stop")

    def test_markers_take_no_time_when_a_word_is_shared_out(self):
        spans = lipsync.distribute(0.0, 1.0, ["ˈ", "p", "ɪ"])
        self.assertEqual([symbol for symbol, _, _ in spans], ["p", "ɪ"])

    def test_a_track_is_contiguous_from_zero_to_its_duration_with_rest_between_sounds(self):
        track = lipsync.build_track([("p", 0.2, 0.3), ("ɑ", 0.3, 0.6), ("m", 0.9, 1.0)], 1.2,
                                    timing_source="word-distribution")
        keys = track["keys"]
        self.assertEqual(keys[0]["start"], 0.0)
        self.assertEqual(keys[-1]["end"], 1.2)
        for before, after in zip(keys, keys[1:]):
            self.assertAlmostEqual(before["end"], after["start"], places=3)
        self.assertEqual([k["viseme"] for k in keys], ["rest", "closed", "open_wide", "rest", "closed", "rest"])
        self.assertEqual(track["format"], lipsync.FORMAT)

    def test_a_clip_with_no_sounds_is_one_rest_key(self):
        track = lipsync.build_track([], 0.8, timing_source="kokoro-durations")
        self.assertEqual(track["keys"], [{"start": 0.0, "end": 0.8, "viseme": "rest"}])

    @mock.patch.object(lipsync, "english_word_phonemes", stub_phonemes)
    def test_words_are_matched_one_to_one_or_the_track_is_refused(self):
        take = {"probe": {"duration_seconds": 1.0},
                "word_timings": {"language": "en", "text": "one word here",
                                 "words": [{"word": "one", "start": 0.0, "end": 0.5},
                                           {"word": "word", "start": 0.5, "end": 1.0}]}}
        track, reason = lipsync.track_for_takes([take])  # three words, two aligned
        self.assertIsNone(track)
        self.assertIn("2 aligned words but 3 phonetic words", reason)

    @unittest.skipUnless(MISAKI, "misaki is not installed; it comes with Kokoro")
    def test_english_phonemes_come_from_misaki_for_each_word(self):
        pairs = lipsync.english_word_phonemes("The next train, Dr. Vale.")
        self.assertEqual([word for word, _ in pairs], ["The", "next", "train", "Dr.", "Vale"])
        self.assertTrue(all(symbols for _, symbols in pairs))
        words = [{"word": word, "start": i * 0.4, "end": i * 0.4 + 0.35}
                 for i, word in enumerate(["The", "next", "train", "Dr.", "Vale"])]
        take = {"probe": {"duration_seconds": 2.5},
                "word_timings": {"language": "en", "text": "The next train, Dr. Vale.", "words": words}}
        track, reason = lipsync.track_for_takes([take])
        self.assertIsNone(reason)
        self.assertEqual(track["timing"]["source"], "word-distribution")
        self.assertEqual(track["checks"]["unmapped_symbols"], 0)


class TakeTrackTests(unittest.TestCase):
    def take(self, seconds, words, text, language="en"):
        return {"probe": {"duration_seconds": seconds},
                "word_timings": {"language": language, "text": text, "words": words}}

    @mock.patch.object(lipsync, "english_word_phonemes", stub_phonemes)
    def test_takes_share_one_timeline_and_the_start_trim_moves_it_earlier(self):
        first = self.take(1.0, [{"word": "Hi", "start": 0.1, "end": 0.5}], "Hi")
        second = self.take(1.0, [{"word": "there", "start": 0.2, "end": 0.7}], "there")
        track, reason = lipsync.track_for_takes([first, second], trim_start=0.2)
        self.assertIsNone(reason)
        self.assertEqual(track["duration_seconds"], 1.8)
        lipsync.check_track(track)
        first_teeth = next(key for key in track["keys"] if key["viseme"] == "tongue_th")
        self.assertAlmostEqual(first_teeth["start"], 1.0)  # "there" starts at 1.2 s on the joined audio, less 0.2 s

    @mock.patch.object(lipsync, "english_word_phonemes", stub_phonemes)
    def test_a_line_without_usable_word_times_gets_no_track_and_the_reason_says_why(self):
        timed = self.take(1.0, [{"word": "Hi", "start": 0.0, "end": 0.4}], "Hi")
        untimed = {"probe": {"duration_seconds": 1.0}, "word_timings": None}
        failed = {"probe": {"duration_seconds": 1.0},
                  "word_timings": {"language": "en", "text": "Hi", "error": "no result"}}
        french = self.take(1.0, [{"word": "Bonjour", "start": 0.0, "end": 0.6}], "Bonjour", language="fr")
        for takes, phrase in [([timed, untimed], "no word times yet"),
                              ([timed, failed], "could not be aligned: no result"),
                              ([french], "no phonemes")]:
            track, reason = lipsync.track_for_takes(takes)
            self.assertIsNone(track)
            self.assertIn(phrase, reason)
        track, reason = lipsync.track_for_takes([timed], trim_start=1.0)
        self.assertIsNone(track)
        self.assertIn("trim", reason)

    def test_without_misaki_an_aligned_take_gets_no_track_and_the_reason_says_so(self):
        aligned = self.take(1.0, [{"word": "Hi", "start": 0.0, "end": 0.4}], "Hi")
        with mock.patch.dict(sys.modules, {"misaki": None}):  # an environment where misaki was never installed
            track, reason = lipsync.track_for_takes([aligned])
        self.assertIsNone(track)
        self.assertIn("misaki", reason)
        self.assertIn("not installed", reason)

    def test_spans_outside_the_clip_are_cut_to_it(self):
        track = lipsync.build_track([("p", 0.9, 1.5)], 1.0, timing_source="word-distribution")
        self.assertEqual(track["keys"][-1], {"start": 0.9, "end": 1.0, "viseme": "closed"})
        lipsync.check_track(track)

    def test_a_track_with_a_gap_or_an_unknown_mouth_shape_fails_the_check(self):
        track = lipsync.build_track([("p", 0.1, 0.2)], 0.5, timing_source="word-distribution")
        lipsync.check_track(track)
        gap = json.loads(json.dumps(track))
        gap["keys"][1]["start"] += 0.1
        with self.assertRaises(ValueError):
            lipsync.check_track(gap)
        with self.assertRaises(ValueError):
            lipsync.check_track(dict(track, keys=[{"start": 0.0, "end": 0.5, "viseme": "wink"}]))


class TimingForChunksTests(unittest.TestCase):
    vocab = {"a": 1, "b": 2, "c": 3, " ": 4}

    def test_chunks_are_placed_on_one_clip_timeline_by_their_offsets(self):
        first = {"phonemes": "ab", "tokens": [FakeToken("ab", "ab")], "durations": [1, 2, 3, 1], "offset": 0.0}
        second = {"phonemes": "c", "tokens": [FakeToken("c", "c")], "durations": [1, 2, 1], "offset": 0.175}
        timing, reason = kt.timing_for_chunks([first, second], self.vocab)
        self.assertIsNone(reason)
        self.assertEqual(timing["source"], "kokoro-durations")
        self.assertEqual(timing["frame_seconds"], 0.025)
        self.assertEqual([(p["symbol"], p["start"], p["end"]) for p in timing["phonemes"]],
                         [("a", 0.025, 0.075), ("b", 0.075, 0.15), ("c", 0.2, 0.25)])
        self.assertEqual([w["word"] for w in timing["words"]], ["ab", "c"])

    def test_a_chunk_without_tokens_keeps_its_phoneme_times_and_says_it_has_no_words(self):
        chunk = {"phonemes": "ab", "tokens": None, "durations": [1, 2, 3, 1], "offset": 0.0}
        timing, reason = kt.timing_for_chunks([chunk], self.vocab)
        self.assertIsNone(reason)
        self.assertFalse(timing["words_available"])
        self.assertEqual(timing["words"], [])
        self.assertEqual([(p["symbol"], p["start"], p["end"]) for p in timing["phonemes"]],
                         [("a", 0.025, 0.075), ("b", 0.075, 0.15)])

    def test_a_clip_is_refused_with_the_reason_when_a_chunk_cannot_be_placed(self):
        good = {"phonemes": "ab", "tokens": [], "durations": [1, 2, 3, 1], "offset": 0.0}
        cases = [
            ([], "no audio"),
            ([dict(good, durations=None)], "no durations"),
            ([dict(good, durations=[1, 2, 1])], "expected 4 durations"),
        ]
        for chunks, phrase in cases:
            with self.subTest(phrase=phrase):
                timing, reason = kt.timing_for_chunks(chunks, self.vocab)
                self.assertIsNone(timing)
                self.assertIn(phrase, reason)


class KokoroTakeTests(unittest.TestCase):
    @staticmethod
    def kokoro_take(seconds, phonemes, **extra):
        timing = {"source": "kokoro-durations", "frame_seconds": 0.025, "words": [], "unplaced_words": 0,
                  "phonemes": [{"symbol": symbol, "start": start, "end": end} for symbol, start, end in phonemes]}
        return {"probe": {"duration_seconds": seconds}, "phoneme_timings": timing, **extra}

    def test_a_kokoro_take_uses_its_stored_phoneme_times_whatever_the_language_label(self):
        take = self.kokoro_take(0.6, [("b", 0.1, 0.2), (" ", 0.2, 0.3), ("i", 0.3, 0.5)],
                                word_timings={"language": "fr", "text": "Bi", "words": []})
        track, reason = lipsync.track_for_takes([take])
        self.assertIsNone(reason)
        self.assertEqual(track["timing"], {"source": "kokoro-durations", "frame_seconds": 0.025,
                                           "word_source": "kokoro-durations"})
        self.assertEqual([k["viseme"] for k in track["keys"]], ["rest", "closed", "rest", "smile", "rest"])

    def test_a_kokoro_take_needs_no_misaki(self):
        take = self.kokoro_take(0.6, [("b", 0.1, 0.2), ("i", 0.3, 0.5)])
        with mock.patch.dict(sys.modules, {"misaki": None}):
            track, reason = lipsync.track_for_takes([take])
        self.assertIsNone(reason)
        self.assertEqual(track["timing"]["source"], "kokoro-durations")

    @mock.patch.object(lipsync, "english_word_phonemes", stub_phonemes)
    def test_a_line_mixing_kokoro_and_aligned_takes_places_each_on_one_timeline(self):
        kokoro = self.kokoro_take(0.5, [("p", 0.1, 0.2)])
        aligned = {"probe": {"duration_seconds": 1.0},
                   "word_timings": {"language": "en", "text": "Hi",
                                    "words": [{"word": "Hi", "start": 0.0, "end": 0.5}]}}
        track, reason = lipsync.track_for_takes([kokoro, aligned])
        self.assertIsNone(reason)
        self.assertEqual(track["duration_seconds"], 1.5)
        self.assertEqual(track["timing"]["source"], "mixed")
        self.assertEqual(track["timing"]["word_source"], "mixed")
        self.assertEqual(track["timing"]["frame_seconds"], 0.025)
        self.assertEqual(track["keys"][1], {"start": 0.1, "end": 0.2, "viseme": "closed"})
        self.assertEqual(track["keys"][2], {"start": 0.2, "end": 0.5, "viseme": "rest"})
        aligned_keys = [key for key in track["keys"] if key["start"] >= 0.5]  # the aligned take starts at 0.5 s
        self.assertEqual(aligned_keys[0]["start"], 0.5)
        self.assertEqual(aligned_keys[-1]["end"], 1.5)
        lipsync.check_track(track)


if __name__ == "__main__":
    unittest.main()
