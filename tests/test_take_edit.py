"""take_edit: what each editing step does to the samples, what it refuses, and how a parent's word and phoneme times move with a
trim. Everything here is synthetic audio; nothing touches a Cast store."""
import copy
import math
import sys
import unittest
import warnings
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import take_edit

RATE = 24_000


def tone(seconds, amplitude=0.5, frequency=220.0):
    axis = np.arange(int(round(seconds * RATE))) / RATE
    return np.rint(amplitude * 32767 * np.sin(2 * np.pi * frequency * axis)).astype(np.int16)


def silence(seconds):
    return np.zeros(int(round(seconds * RATE)), dtype=np.int16)


def clip_of(*parts):
    return np.concatenate(parts)


def edit(duration, **fields):
    return take_edit.normalize_edit(fields, duration)


class NormalizeTests(unittest.TestCase):
    def test_every_field_is_present_and_typed(self):
        result = edit(3.0, start_seconds=1, gain_db=3)
        self.assertEqual(result, {"start_seconds": 1.0, "end_seconds": 3.0, "auto_trim": False, "silence_threshold_db": -50.0,
                                  "keep_silence_ms": 80.0, "fade_in_ms": 0.0, "fade_out_ms": 0.0, "gain_db": 3.0})
        self.assertEqual(set(result), set(take_edit.FIELDS))
        self.assertTrue(all(isinstance(value, (float, bool)) for value in result.values()))

    def test_null_means_not_given(self):
        self.assertEqual(edit(3.0, start_seconds=None, end_seconds=2.0, auto_trim=None, gain_db=None),
                         edit(3.0, end_seconds=2.0))

    def test_numbers_are_rounded_to_microseconds_so_a_repeat_compares_equal(self):
        self.assertEqual(edit(3.0, start_seconds=1.00000049)["start_seconds"], 1.0)
        self.assertEqual(edit(3.0, start_seconds=0.1 + 0.2)["start_seconds"], 0.3)

    def test_the_takes_own_length_is_allowed_to_the_last_digit(self):
        length = 4.2916664
        result = edit(length, start_seconds=0.5, end_seconds=length)
        self.assertEqual(result["end_seconds"], round(length, 6))
        self.assertEqual(edit(length, start_seconds=length - 1)["end_seconds"], round(length, 6))

    def test_what_it_refuses(self):
        cases = [
            ("An edit must be a JSON object", []), ("Unknown edit field 'gain'", {"gain": 3}),
            ("start_seconds must be a number", {"start_seconds": "1"}),
            ("end_seconds must be a number", {"end_seconds": True}),
            ("gain_db must be a number", {"gain_db": float("nan")}),
            ("fade_in_ms must be a number", {"fade_in_ms": float("inf")}),
            ("start_seconds must be from 0 to 3", {"start_seconds": 3.5}),
            ("start_seconds must be from 0 to 3", {"start_seconds": -0.1}),
            ("end_seconds must be from 0 to 3", {"end_seconds": 3.0001}),
            ("end_seconds must be after start_seconds", {"start_seconds": 2.0, "end_seconds": 2.0}),
            ("gain_db must be from -60 to 24", {"gain_db": 24.5}),
            ("fade_out_ms must be from 0 to 5000", {"fade_out_ms": 5001}),
            ("silence_threshold_db must be from -90 to -10", {"silence_threshold_db": -5}),
            ("keep_silence_ms must be from 0 to 2000", {"keep_silence_ms": 2001}),
            ("auto_trim must be true or false", {"auto_trim": 1}),
            ("changes nothing", {}),
            ("changes nothing", {"start_seconds": 0, "end_seconds": 3.0}),
        ]
        for message, request in cases:
            with self.subTest(message, request=request), self.assertRaisesRegex(ValueError, message):
                take_edit.normalize_edit(request, 3.0)

    def test_an_edit_that_does_something_is_not_a_no_op(self):
        for request in ({"start_seconds": 0.1}, {"end_seconds": 2.9}, {"auto_trim": True}, {"fade_in_ms": 5},
                        {"fade_out_ms": 5}, {"gain_db": -0.1}):
            with self.subTest(request):
                take_edit.normalize_edit(request, 3.0)


class TrimTests(unittest.TestCase):
    def test_a_trim_copies_the_samples_exactly(self):
        samples = tone(2.0)
        result, report = take_edit.apply_edit(samples, RATE, edit(2.0, start_seconds=0.5, end_seconds=1.5))
        self.assertEqual(result.dtype, np.int16)
        np.testing.assert_array_equal(result, samples[12000:36000])
        self.assertEqual((report["start_sample"], report["end_sample"]), (12000, 36000))
        self.assertEqual((report["start_seconds"], report["end_seconds"], report["duration_seconds"]), (0.5, 1.5, 1.0))
        self.assertIsNone(report["auto_trim_removed"])

    def test_the_source_is_not_changed_and_the_result_is_a_copy(self):
        samples = tone(1.0)
        before = samples.copy()
        result, _ = take_edit.apply_edit(samples, RATE, edit(1.0, start_seconds=0.1))
        result[:] = 0
        np.testing.assert_array_equal(samples, before)
        full, _ = take_edit.apply_edit(samples, RATE, edit(1.0, fade_in_ms=10))
        self.assertIsNot(full, samples)

    def test_a_result_shorter_than_fifty_milliseconds_is_refused(self):
        with self.assertRaisesRegex(ValueError, "shorter than 0.05 seconds"):
            take_edit.apply_edit(tone(1.0), RATE, edit(1.0, start_seconds=0.5, end_seconds=0.54))
        take_edit.apply_edit(tone(1.0), RATE, edit(1.0, start_seconds=0.5, end_seconds=0.55))

    def test_a_stereo_take_is_refused(self):
        with self.assertRaisesRegex(ValueError, "mono"):
            take_edit.apply_edit(np.zeros((100, 2), dtype=np.int16), RATE, edit(1.0, start_seconds=0.1))

    def test_a_trim_at_the_very_ends_keeps_every_sample(self):
        samples = tone(1.0)
        result, _ = take_edit.apply_edit(samples, RATE, edit(1.0, end_seconds=1.0, gain_db=0.0, fade_in_ms=0.001))
        self.assertEqual(len(result), len(samples))

    def test_a_position_lands_on_the_nearest_sample_not_the_one_before(self):
        # 0.100021 s is 2400.504 samples and 0.900021 s is 21600.504
        _, report = take_edit.apply_edit(tone(1.0), RATE, edit(1.0, start_seconds=0.100021, end_seconds=0.900021))
        self.assertEqual((report["start_sample"], report["end_sample"]), (2401, 21601))

    def test_the_peak_it_reports_counts_a_negative_full_scale_sample(self):
        samples = np.array([-32768] + [100] * 2399, dtype=np.int16)
        _, report = take_edit.apply_edit(samples, RATE, edit(0.1, end_seconds=0.09))
        self.assertEqual(report["peak_dbfs"], 0.0)


class AutoTrimTests(unittest.TestCase):
    def duration_of(self, samples):
        return len(samples) / RATE

    def test_leading_and_trailing_silence_go_and_the_padding_stays(self):
        samples = clip_of(silence(0.5), tone(1.0), silence(0.7))
        result, report = take_edit.apply_edit(samples, RATE, edit(self.duration_of(samples), auto_trim=True, keep_silence_ms=100))
        self.assertAlmostEqual(self.duration_of(result), 1.2, delta=0.03)
        self.assertAlmostEqual(report["start_seconds"], 0.4, delta=0.02)
        self.assertAlmostEqual(report["end_seconds"], 1.6, delta=0.03)
        self.assertAlmostEqual(report["auto_trim_removed"]["start_seconds"], 0.4, delta=0.02)
        self.assertAlmostEqual(report["auto_trim_removed"]["end_seconds"], 0.6, delta=0.03)

    def test_no_padding_leaves_only_the_speech(self):
        samples = clip_of(silence(0.5), tone(1.0), silence(0.7))
        result, _ = take_edit.apply_edit(samples, RATE, edit(self.duration_of(samples), auto_trim=True, keep_silence_ms=0))
        self.assertAlmostEqual(self.duration_of(result), 1.0, delta=0.03)

    def test_silence_between_sounds_is_kept(self):
        samples = clip_of(silence(0.3), tone(0.5), silence(0.4), tone(0.5), silence(0.3))
        result, _ = take_edit.apply_edit(samples, RATE, edit(self.duration_of(samples), auto_trim=True, keep_silence_ms=0))
        self.assertAlmostEqual(self.duration_of(result), 1.4, delta=0.03)

    def test_padding_cannot_reach_past_the_chosen_range(self):
        samples = clip_of(silence(0.05), tone(1.0), silence(0.05))
        result, report = take_edit.apply_edit(samples, RATE, edit(self.duration_of(samples), auto_trim=True, keep_silence_ms=500))
        np.testing.assert_array_equal(result, samples)
        self.assertEqual(report["auto_trim_removed"], {"start_seconds": 0.0, "end_seconds": 0.0})

    def test_it_works_inside_the_trim_window_only(self):
        samples = clip_of(tone(0.4), silence(0.5), tone(0.4), silence(0.5))
        window = edit(self.duration_of(samples), start_seconds=0.9, auto_trim=True, keep_silence_ms=0)
        result, report = take_edit.apply_edit(samples, RATE, window)
        self.assertAlmostEqual(report["start_seconds"], 0.9, delta=0.02)
        self.assertAlmostEqual(self.duration_of(result), 0.4, delta=0.03)

    def test_the_threshold_decides_what_counts_as_silence(self):
        noise = (tone(1.0, amplitude=0.001) // 1).astype(np.int16)               # about -60 dBFS
        samples = clip_of(noise, tone(0.5), noise)
        length = self.duration_of(samples)
        loose, _ = take_edit.apply_edit(samples, RATE, edit(length, auto_trim=True, silence_threshold_db=-50, keep_silence_ms=0))
        tight, _ = take_edit.apply_edit(samples, RATE, edit(length, auto_trim=True, silence_threshold_db=-75, keep_silence_ms=0))
        self.assertAlmostEqual(self.duration_of(loose), 0.5, delta=0.03)
        self.assertAlmostEqual(self.duration_of(tight), 2.5, delta=0.03)

    def test_a_silent_range_is_refused_with_the_threshold_named(self):
        with self.assertRaisesRegex(ValueError, "louder than -50 dB"):
            take_edit.apply_edit(silence(1.0), RATE, edit(1.0, auto_trim=True))

    def test_speech_bounds_on_short_and_empty_input(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")                      # an empty take is answered before anything is divided
            self.assertIsNone(take_edit.speech_bounds(np.zeros(0, dtype=np.int16), RATE, -50))
        self.assertEqual(take_edit.speech_bounds(tone(0.005), RATE, -50), (0, len(tone(0.005))))
        self.assertIsNone(take_edit.speech_bounds(silence(0.005), RATE, -50))

    def test_the_bounds_are_read_over_twenty_milliseconds_every_ten(self):
        # sound from 0.305 s to 1.305 s: the first 20 ms window to reach it starts at 0.290 s, the last starts at 1.300 s
        bounds = take_edit.speech_bounds(clip_of(silence(0.305), tone(1.0), silence(0.5)), RATE, -50)
        self.assertEqual(bounds, (6960, 31680))
        self.assertEqual((bounds[0] / RATE, bounds[1] / RATE), (0.29, 1.32))

    def test_sound_in_the_last_window_is_read(self):
        self.assertEqual(take_edit.speech_bounds(clip_of(silence(0.28), tone(0.02)), RATE, -50), (6480, 7200))

    def test_the_padding_is_rounded_to_the_nearest_sample(self):
        samples = clip_of(silence(0.5), tone(1.0), silence(0.5))
        # 80.021 ms is 1920.504 samples; the first window to reach the sound starts at sample 11760
        _, report = take_edit.apply_edit(samples, RATE, edit(2.0, auto_trim=True, keep_silence_ms=80.021))
        self.assertEqual(report["start_sample"], 11760 - 1921)


class FadeAndGainTests(unittest.TestCase):
    def test_a_fade_in_starts_at_zero_and_leaves_the_rest_alone(self):
        samples = np.full(RATE, 10_000, dtype=np.int16)
        result, report = take_edit.apply_edit(samples, RATE, edit(1.0, fade_in_ms=100))
        self.assertEqual(report["fade_in_samples"], 2400)
        self.assertEqual(int(result[0]), 0)
        self.assertTrue(np.all(np.diff(result[:2400].astype(int)) >= 0), "a fade in only rises")
        self.assertLess(int(result[2300]), 10_000)
        np.testing.assert_array_equal(result[2400:], samples[2400:])
        self.assertAlmostEqual(float(result[1200]), 5000, delta=2)          # halfway through the curve is half the level

    def test_a_fade_out_ends_at_zero_and_leaves_the_rest_alone(self):
        samples = np.full(RATE, 10_000, dtype=np.int16)
        result, report = take_edit.apply_edit(samples, RATE, edit(1.0, fade_out_ms=250))
        self.assertEqual(report["fade_out_samples"], 6000)
        self.assertEqual(int(result[-1]), 0)
        self.assertTrue(np.all(np.diff(result[-6000:].astype(int)) <= 0), "a fade out only falls")
        np.testing.assert_array_equal(result[:-6000], samples[:-6000])

    def test_both_fades_and_the_limit_on_their_length(self):
        samples = np.full(RATE, 10_000, dtype=np.int16)
        result, _ = take_edit.apply_edit(samples, RATE, edit(1.0, fade_in_ms=500, fade_out_ms=500))
        self.assertEqual((int(result[0]), int(result[-1])), (0, 0))
        with self.assertRaisesRegex(ValueError, "fades are longer"):
            take_edit.apply_edit(samples, RATE, edit(1.0, fade_in_ms=600, fade_out_ms=500))

    def test_gain_scales_by_the_decibels_asked_for(self):
        samples = np.full(2400, 8000, dtype=np.int16)
        louder, report = take_edit.apply_edit(samples, RATE, edit(0.1, gain_db=6.0206))
        quieter, _ = take_edit.apply_edit(samples, RATE, edit(0.1, gain_db=-6.0206))
        self.assertTrue(np.all(np.abs(louder.astype(int) - 16000) <= 1))
        self.assertTrue(np.all(np.abs(quieter.astype(int) - 4000) <= 1))
        self.assertEqual(report["peak_dbfs"], round(20 * math.log10(louder.max() / 32768), 2))

    def test_a_gain_that_would_clip_is_refused_and_says_how_much_is_possible(self):
        samples = np.full(2400, 16384, dtype=np.int16)                           # -6 dBFS
        with self.assertRaisesRegex(ValueError, r"would clip.*at most 6 dB"):
            take_edit.apply_edit(samples, RATE, edit(0.1, gain_db=6.5))
        result, _ = take_edit.apply_edit(samples, RATE, edit(0.1, gain_db=6.0))
        self.assertLessEqual(int(result.max()), 32767)

    def test_a_gain_that_would_round_up_to_full_scale_is_refused_not_wrapped(self):
        samples = np.full(2400, 16384, dtype=np.int16)
        # 6.020599 dB doubles 16384 to 32767.9965, which rounds to 32768 and does not fit in 16 bits
        with self.assertRaisesRegex(ValueError, "would clip"):
            take_edit.apply_edit(samples, RATE, edit(0.1, gain_db=6.020599))

    def test_the_gain_it_offers_is_one_the_take_can_take(self):
        samples = np.full(2400, 22_000, dtype=np.int16)                          # 3.46 dB of room
        with self.assertRaisesRegex(ValueError, r"at most 3\.4 dB"):
            take_edit.apply_edit(samples, RATE, edit(0.1, gain_db=3.5))
        result, _ = take_edit.apply_edit(samples, RATE, edit(0.1, gain_db=3.4))
        self.assertEqual(int(result.max()), round(22_000 * 10 ** (3.4 / 20)))

    def test_the_recomputed_samples_are_rounded_to_the_nearest(self):
        samples = np.array([1004, -1004] * 1200, dtype=np.int16)                 # halved: 501.999995, nearly 502
        result, _ = take_edit.apply_edit(samples, RATE, edit(0.1, gain_db=-6.0206))
        self.assertEqual((int(result[0]), int(result[1])), (502, -502))

    def test_the_fades_follow_a_raised_cosine(self):
        samples = np.full(RATE, 10_000, dtype=np.int16)
        result, _ = take_edit.apply_edit(samples, RATE, edit(1.0, fade_in_ms=100, fade_out_ms=1))
        rise = 0.5 * (1.0 - np.cos(np.pi * np.arange(2400) / 2400))
        fall = 0.5 * (1.0 + np.cos(np.pi * (np.arange(24) + 1) / 24))
        np.testing.assert_allclose(result[:2400], 10_000 * rise, atol=1)
        np.testing.assert_allclose(result[-24:], 10_000 * fall, atol=1)
        self.assertEqual(int(result[-1]), 0)

    def test_the_fades_come_before_the_gain_check(self):
        samples = np.full(RATE, 16384, dtype=np.int16)
        take_edit.apply_edit(samples, RATE, edit(1.0, fade_in_ms=10, gain_db=6.0))

    def test_gain_on_silence_is_allowed_and_changes_nothing(self):
        result, _ = take_edit.apply_edit(silence(0.5), RATE, edit(0.5, gain_db=20))
        self.assertEqual(int(np.abs(result).max()), 0)

    def test_a_negative_full_scale_sample_is_never_wrapped(self):
        samples = np.array([-32768] * 2400, dtype=np.int16)
        result, _ = take_edit.apply_edit(samples, RATE, edit(0.1, fade_in_ms=1))
        self.assertTrue(np.all(result <= 0))
        with self.assertRaises(ValueError):
            take_edit.apply_edit(samples, RATE, edit(0.1, gain_db=1))

    def test_steps_run_in_order_trim_then_auto_trim_then_fades_then_gain(self):
        samples = clip_of(tone(0.3), silence(0.4), tone(0.5), silence(0.4))
        request = edit(len(samples) / RATE, start_seconds=0.5, auto_trim=True, keep_silence_ms=0, fade_in_ms=20, gain_db=-6)
        result, report = take_edit.apply_edit(samples, RATE, request)
        self.assertAlmostEqual(report["start_seconds"], 0.7, delta=0.02)          # the first sound is outside the trim
        self.assertEqual(int(result[0]), 0)
        self.assertLess(int(np.abs(result).max()), int(np.abs(samples).max()))


class WaveformTests(unittest.TestCase):
    def test_peaks_cover_the_take_and_name_where_the_speech_is(self):
        samples = clip_of(silence(0.5), tone(1.0, amplitude=0.5), silence(0.5))
        shown = take_edit.waveform(samples, RATE, 100)
        self.assertEqual((shown["points"], len(shown["peaks"]), shown["duration_seconds"], shown["sample_rate"]), (100, 100, 2.0, RATE))
        self.assertEqual(shown["peaks"][0], [0.0, 0.0])
        self.assertGreater(shown["peaks"][50][1], 0.45)
        self.assertLess(shown["peaks"][50][0], -0.45)
        self.assertAlmostEqual(shown["peak_dbfs"], -6.02, delta=0.05)
        self.assertAlmostEqual(shown["speech"]["start_seconds"], 0.5, delta=0.02)
        self.assertAlmostEqual(shown["speech"]["end_seconds"], 1.5, delta=0.03)
        self.assertEqual(shown["speech"]["threshold_db"], -50.0)

    def test_the_levels_are_read_from_the_magnitude_of_the_samples(self):
        shown = take_edit.waveform(np.full(RATE, -20_000, dtype=np.int16), RATE, 50)
        level = round(20 * math.log10(20_000 / 32768), 2)
        self.assertEqual((shown["peak_dbfs"], shown["rms_dbfs"]), (level, level))
        shown = take_edit.waveform(tone(1.0, amplitude=0.5), RATE, 50)
        self.assertAlmostEqual(shown["rms_dbfs"], -9.03, delta=0.05)             # a sine's rms is its peak less 3 dB

    def test_quiet_speech_is_found_at_the_suggested_threshold(self):
        quiet = clip_of(silence(0.5), tone(1.0, amplitude=0.01), silence(0.5))   # about -43 dBFS, over -50 and under -30
        shown = take_edit.waveform(quiet, RATE, 50)
        self.assertAlmostEqual(shown["speech"]["start_seconds"], 0.5, delta=0.02)
        self.assertAlmostEqual(shown["speech"]["end_seconds"], 1.5, delta=0.03)

    def test_silence_has_no_speech_and_no_levels(self):
        shown = take_edit.waveform(silence(0.5), RATE, 50)
        self.assertEqual((shown["peak_dbfs"], shown["rms_dbfs"], shown["speech"]), (None, None, None))

    def test_there_are_never_more_points_than_samples(self):
        shown = take_edit.waveform(np.array([1, -2, 3], dtype=np.int16), RATE, 600)
        self.assertEqual(shown["points"], 3)
        self.assertEqual(shown["peaks"], [[round(1 / 32768, 4)] * 2, [round(-2 / 32768, 4)] * 2, [round(3 / 32768, 4)] * 2])

    def test_an_empty_take_still_answers(self):
        shown = take_edit.waveform(np.zeros(0, dtype=np.int16), RATE, 100)
        self.assertEqual((shown["points"], shown["duration_seconds"], shown["speech"]), (1, 0.0, None))


class TimingTests(unittest.TestCase):
    WORDS = [{"word": "We", "start": 0.2, "end": 0.4, "probability": 0.9},
             {"word": "made", "start": 0.5, "end": 0.9, "probability": 0.8},
             {"word": "it", "start": 1.0, "end": 1.2, "probability": 0.7},
             {"word": "home.", "start": 1.3, "end": 1.9, "probability": 0.6}]

    def record(self, **more):
        return {"schema": 1, "aligner": {"id": "whisper-tiny"}, "audio_sha256": "parent", "language": "en",
                "text": "We made it home.", "aligned_at": "2026-10-26T00:00:00+00:00", "duration_seconds": 2.0,
                "words": copy.deepcopy(self.WORDS), "checks": {"status": "ok"}, **more}

    def test_words_move_earlier_by_the_start_of_the_trim(self):
        record = self.record()
        derived = take_edit.derive_word_record(record, "p" * 32, "child", 0.45, 2.0, 1.55)
        self.assertEqual([(w["word"], w["start"], w["end"], w["probability"]) for w in derived["words"]],
                         [("made", 0.05, 0.45, 0.8), ("it", 0.55, 0.75, 0.7), ("home.", 0.85, 1.45, 0.6)])
        self.assertEqual(derived["text"], "made it home.")

    def test_the_parent_record_is_not_changed(self):
        record = self.record()
        before = copy.deepcopy(record)
        take_edit.derive_word_record(record, "p" * 32, "child", 0.45, 1.5, 1.05)
        self.assertEqual(record, before)

    def test_a_word_the_trim_cuts_into_is_kept_shortened_when_enough_is_left(self):
        derived = take_edit.derive_word_record(self.record(), "p" * 32, "child", 0.6, 1.6, 1.0)
        words = {w["word"]: w for w in derived["words"]}
        self.assertEqual((words["made"]["start"], words["made"]["end"]), (0.0, 0.3))
        self.assertEqual((words["home."]["start"], words["home."]["end"]), (0.7, 1.0))
        self.assertEqual(derived["derived_from"]["spans_shortened"], 2)

    def test_a_word_with_only_a_sliver_left_is_dropped_and_the_text_follows_the_words_that_are_left(self):
        derived = take_edit.derive_word_record(self.record(), "p" * 32, "child", 0.38, 1.95, 1.57)
        self.assertEqual([w["word"] for w in derived["words"]], ["made", "it", "home."])
        self.assertEqual(derived["text"], "made it home.")
        self.assertEqual(derived["derived_from"]["spans_dropped"], 1)

    def test_the_text_is_kept_as_it_was_when_no_word_is_dropped(self):
        record = self.record(text="We made it, home.")
        derived = take_edit.derive_word_record(record, "p" * 32, "child", 0.0, 1.95, 1.95)
        self.assertEqual(derived["text"], "We made it, home.")

    def test_the_text_is_kept_as_it_was_when_a_word_is_only_shortened(self):
        derived = take_edit.derive_word_record(self.record(text="We made it, home."), "p" * 32, "child", 0.0, 1.6, 1.6)
        self.assertEqual((derived["derived_from"]["spans_shortened"], derived["derived_from"]["spans_dropped"]), (1, 0))
        self.assertEqual(derived["text"], "We made it, home.")

    def test_the_record_names_its_parent_and_the_new_audio(self):
        derived = take_edit.derive_word_record(self.record(), "ab" * 16, "childhash", 0.2, 1.9, 1.7)
        self.assertEqual(derived["audio_sha256"], "childhash")
        self.assertEqual(derived["duration_seconds"], 1.7)
        self.assertEqual(derived["derived_from"], {"take_id": "ab" * 16, "audio_sha256": "parent", "start_seconds": 0.2,
                                                   "end_seconds": 1.9, "spans_dropped": 0, "spans_shortened": 0})
        self.assertEqual((derived["aligner"], derived["checks"], derived["language"]),
                         ({"id": "whisper-tiny"}, {"status": "ok"}, "en"))

    def test_a_parent_without_a_usable_record_gives_none(self):
        self.assertIsNone(take_edit.derive_word_record(None, "p" * 32, "c", 0.0, 1.0, 1.0))
        self.assertIsNone(take_edit.derive_word_record(self.record(error="no speech"), "p" * 32, "c", 0.0, 1.0, 1.0))
        self.assertIsNone(take_edit.derive_word_record(self.record(words=[]), "p" * 32, "c", 0.0, 1.0, 1.0))
        self.assertIsNone(take_edit.derive_word_record({"text": "We made it home."}, "p" * 32, "c", 0.0, 1.0, 1.0), "no words at all")
        self.assertIsNone(take_edit.derive_word_record(self.record(), "p" * 32, "c", 1.95, 2.0, 0.05), "no word is left")

    def test_phoneme_times_move_the_same_way(self):
        record = {"frame_seconds": 0.025, "unplaced_words": 0,
                  "words": [{"word": "hi", "start": 0.1, "end": 0.5}],
                  "phonemes": [{"symbol": "h", "start": 0.1, "end": 0.2}, {"symbol": "a", "start": 0.2, "end": 0.4},
                               {"symbol": "ɪ", "start": 0.4, "end": 0.5}]}
        before = copy.deepcopy(record)
        derived = take_edit.derive_phoneme_record(record, "p" * 32, "child", 0.15, 0.45, 0.3)
        self.assertEqual(record, before)
        self.assertEqual([(p["symbol"], p["start"], p["end"]) for p in derived["phonemes"]],
                         [("h", 0.0, 0.05), ("a", 0.05, 0.25), ("ɪ", 0.25, 0.3)])
        self.assertEqual((derived["words"][0]["start"], derived["words"][0]["end"]), (0.0, 0.3))
        self.assertEqual((derived["frame_seconds"], derived["audio_sha256"], derived["duration_seconds"]), (0.025, "child", 0.3))
        self.assertEqual(derived["derived_from"]["take_id"], "p" * 32)

    def test_phonemes_wholly_outside_the_trim_leave_and_no_sound_left_gives_none(self):
        record = {"frame_seconds": 0.025, "words": [], "phonemes": [{"symbol": "a", "start": 0.0, "end": 0.1},
                                                                     {"symbol": "b", "start": 0.1, "end": 0.2}]}
        derived = take_edit.derive_phoneme_record(record, "p" * 32, "child", 0.1, 0.2, 0.1)
        self.assertEqual([p["symbol"] for p in derived["phonemes"]], ["b"])
        self.assertIsNone(take_edit.derive_phoneme_record(record, "p" * 32, "child", 0.5, 0.6, 0.1))
        self.assertIsNone(take_edit.derive_phoneme_record(None, "p" * 32, "child", 0.0, 0.1, 0.1))
        self.assertIsNone(take_edit.derive_phoneme_record({"phonemes": []}, "p" * 32, "child", 0.0, 0.1, 0.1))
        self.assertIsNone(take_edit.derive_phoneme_record({"words": [{"word": "hi", "start": 0.0, "end": 0.1}]}, "p" * 32, "child", 0.0, 0.1, 0.1))

    def test_the_phoneme_record_counts_the_words_it_cut_as_well_as_the_sounds(self):
        record = {"phonemes": [{"symbol": "a", "start": 0.0, "end": 0.1}, {"symbol": "b", "start": 0.1, "end": 0.2},
                               {"symbol": "c", "start": 0.2, "end": 0.5}],
                  "words": [{"word": "x", "start": 0.0, "end": 0.12}, {"word": "y", "start": 0.12, "end": 0.5}]}
        derived = take_edit.derive_phoneme_record(record, "p" * 32, "child", 0.1, 0.45, 0.35)
        self.assertEqual([p["symbol"] for p in derived["phonemes"]], ["b", "c"])
        self.assertEqual([w["word"] for w in derived["words"]], ["y"])
        counted = derived["derived_from"]
        self.assertEqual((counted["spans_dropped"], counted["spans_shortened"]), (2, 2))    # a and x went; c and y were cut into

    def test_a_span_is_dropped_only_when_the_cut_leaves_under_forty_milliseconds_and_under_half_of_it(self):
        # (length of the span, how much of it the cut leaves, whether it goes)
        for whole, left, goes in ((0.5, 0.035, True), (0.5, 0.045, False), (0.05, 0.03, False), (1.0, 0.3, False),
                                  (0.08, 0.03, True), (0.08, 0.0, True)):
            with self.subTest(whole=whole, left=left):
                kept, dropped, _ = take_edit._move([{"start": 1.0 + left - whole, "end": 1.0 + left}], 1.0, 5.0)
                self.assertEqual((dropped, len(kept)), (1, 0) if goes else (0, 1))

    def test_a_zero_length_span_inside_the_trim_stays_and_outside_it_goes(self):
        spans = [{"start": 0.1, "end": 0.1}, {"start": 0.9, "end": 0.9}]
        kept, dropped, shortened = take_edit._move(spans, 0.0, 0.5)
        self.assertEqual((kept, dropped, shortened), ([{"start": 0.1, "end": 0.1}], 1, 0))


if __name__ == "__main__":
    unittest.main()
