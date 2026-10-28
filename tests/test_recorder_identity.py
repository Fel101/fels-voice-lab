"""Identity check: a kept clip against its untouched master, with levels matched and the master cut like the clip."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recorder_identity as ri
import speaker_similarity as ss
from recorder_clip import load_clip
from tests import recorder_fixtures as fx

try:
    import denoise_ref
    FFMPEG = denoise_ref.find_ffmpeg()
except Exception:                     # ffmpeg is optional: only the tests that make cleaned clips need it
    FFMPEG = None
ENCODER_READY = ss.encoder_available()
REAL_VOICE = ROOT / "reference" / "af_heart.wav"


def unit(degrees):
    return np.array([np.cos(np.radians(degrees)), np.sin(np.radians(degrees))])


class FakeEncoder:
    """Hands back the vectors it was given, in call order (kept clip, master, then ``against``), and keeps the audio."""

    def __init__(self, *vectors, error=None):
        self.vectors, self.error, self.calls = list(vectors), error, []

    def __call__(self, samples, rate):
        self.calls.append((np.asarray(samples), rate))
        if self.error:
            raise self.error
        return np.asarray(self.vectors[len(self.calls) - 1], dtype=np.float64)


def tree_hashes(folder):
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(folder).rglob("*")) if path.is_file()}


class IdentityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-identity-")
        self.addCleanup(self.temp.cleanup)
        self.clips = Path(self.temp.name) / "voice-clips"
        self._count = 0

    def clip(self, **kwargs):
        self._count += 1
        kwargs.setdefault("stem", f"20261020-16120{self._count}_mic_clip{self._count:04d}")
        kwargs.setdefault("clip_id", f"clip{self._count:04d}")
        return load_clip(fx.write_clip(self.clips, **kwargs))


class LevelAndAlignmentTests(unittest.TestCase):
    def test_speech_level_is_matched_whatever_the_gain(self):
        voice = fx.synth_voice(6.0)
        reference = ri.match_level(voice, 24_000)
        self.assertAlmostEqual(ri.voiced_rms_db(reference, 24_000), ri.MATCH_LEVEL_DB, places=1)
        for gain in (0.01, 0.1, 4.0):
            with self.subTest(gain=gain):
                np.testing.assert_allclose(ri.match_level(voice * gain, 24_000), reference, rtol=1e-3, atol=1e-4)

    def test_silence_cannot_be_matched(self):
        with self.assertRaises(ri.Unavailable):
            ri.match_level(np.zeros(24_000, np.float32), 24_000)

    def test_without_edits_the_whole_master_is_used(self):
        master = np.arange(48_000 * 3, dtype=np.float32)
        samples, mode, note = ri.aligned_master(master, 48_000, {"edits": []}, 3.0)
        self.assertEqual((mode, note), ("whole_master", ""))
        np.testing.assert_array_equal(samples, master)

    def test_cuts_and_silences_are_applied_to_the_master_and_fades_and_gains_are_not(self):
        rate = 48_000
        master = np.random.default_rng(0).normal(size=14 * rate).astype(np.float32)
        edits = [{"op": "cut", "region": "start", "start_seconds": 0.0, "end_seconds": 1.5},
                 {"op": "cut", "region": "middle", "start_seconds": 4.0, "end_seconds": 5.0},
                 {"op": "silence", "start_seconds": 8.0, "end_seconds": 9.0},
                 {"op": "fade", "direction": "out", "start_seconds": 12.0, "end_seconds": 13.0},
                 {"op": "gain", "gain_db": 6.0, "start_seconds": 2.0, "end_seconds": 3.0}]
        kept_seconds = (14 - 1.5 - 1.0) - 0.010            # one join, crossfaded by 10 ms
        samples, mode, note = ri.aligned_master(master, rate, {"edits": edits, "join_fade_ms": 10.0}, kept_seconds)
        expected = master.copy()
        expected[8 * rate:9 * rate] = 0.0
        expected = np.concatenate([expected[round(1.5 * rate):4 * rate], expected[5 * rate:]])
        self.assertEqual(mode, "edit_aligned")
        np.testing.assert_array_equal(samples, expected)
        self.assertIn("11.50 s of the 14.00 s master", note)

    def test_overlapping_cuts_merge_and_a_cut_shorter_than_twenty_milliseconds_is_ignored(self):
        rate = 48_000
        master = np.zeros(14 * rate, np.float32)
        edits = [{"op": "cut", "start_seconds": 1.0, "end_seconds": 3.0},
                 {"op": "cut", "start_seconds": 2.0, "end_seconds": 4.0},
                 {"op": "cut", "start_seconds": 6.0, "end_seconds": 6.010}]
        samples, mode, _ = ri.aligned_master(master, rate, {"edits": edits}, 11.0)
        self.assertEqual((mode, len(samples)), ("edit_aligned", 11 * rate))

    def test_edits_that_do_not_reproduce_the_clips_length_fall_back_to_the_whole_master(self):
        rate = 48_000
        master = np.zeros(14 * rate, np.float32)
        edits = [{"op": "cut", "start_seconds": 0.0, "end_seconds": 1.5}]
        samples, mode, note = ri.aligned_master(master, rate, {"edits": edits}, 9.0)
        self.assertEqual(mode, "whole_master")
        self.assertIn("so the whole master was compared", note)
        self.assertIs(samples, master)
        _, mode, note = ri.aligned_master(master, rate, {"edits": []}, 9.0)
        self.assertEqual(mode, "whole_master", "no edits cannot explain a shorter clip either")
        self.assertIn("14.00 s but the clip is 9.00 s", note)

    def test_an_edit_list_that_cannot_be_read_never_raises(self):
        rate = 48_000
        master = np.zeros(14 * rate, np.float32)
        broken = ({"edits": [{"op": "cut"}]}, {"edits": "nonsense"}, {"edits": 7},
                  {"edits": [{"op": "cut", "start_seconds": "x", "end_seconds": 1}]},
                  {"edits": [{"op": "cut", "start_seconds": 0.0, "end_seconds": float("inf")}]},
                  {"edits": [{"op": "cut", "start_seconds": float("nan"), "end_seconds": 1.0}]},
                  {"edits": [None]}, {"edits": [], "join_fade_ms": "slow"},
                  {"edits": [{"op": "cut", "start_seconds": 0.0, "end_seconds": 99_999.0}]})
        for sidecar in broken:
            with self.subTest(sidecar=sidecar):
                samples, mode, note = ri.aligned_master(master, rate, sidecar, 14.0)
                self.assertEqual(mode, "whole_master")
                self.assertTrue(note)
                self.assertIs(samples, master)


class VerdictTests(IdentityCase):
    def check(self, kept, master, *, error=None, against=None, against_file=None, clip=None, **kwargs):
        """Check a generated clip with a fake encoder that answers with these vectors (kept, master, against)."""
        encoder = FakeEncoder(*([kept, master] + ([against] if against is not None else [])), error=error)
        result = ri.check_identity(self.clip(**(clip or {})), embed=encoder, against=against_file, **kwargs)
        return result, encoder

    def test_a_score_at_or_above_the_threshold_passes_and_below_it_is_flagged(self):
        passed, _ = self.check(unit(0), unit(20))                  # cos 20 deg = 0.940
        flagged, _ = self.check(unit(0), unit(40))                 # cos 40 deg = 0.766
        self.assertEqual((passed["status"], passed["verdict"], passed["score"]), ("checked", "pass", 0.9397))
        self.assertEqual((flagged["verdict"], flagged["score"]), ("flag", 0.766))
        self.assertEqual(passed["threshold"], ri.DEFAULT_MIN_SIMILARITY)
        strict, _ = self.check(unit(0), unit(20), min_similarity=0.95)
        self.assertEqual((strict["verdict"], strict["threshold"]), ("flag", 0.95))
        edge, _ = self.check(unit(0), unit(0), min_similarity=1.0)
        self.assertEqual(edge["verdict"], "pass", "a score equal to the threshold passes")

    def test_the_threshold_must_be_a_sensible_number(self):
        clip = self.clip()
        for bad in (0, -0.1, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ri.check_identity(clip, min_similarity=bad, embed=FakeEncoder(unit(0), unit(0)))

    def test_a_flag_names_noise_reduction_when_it_was_on_and_says_so_when_it_was_not(self):
        denoised, _ = self.check(unit(0), unit(40), clip={"nf": -25.0})
        text = " ".join(denoised["reasons"])
        self.assertIn("Noise reduction was on (afftdn nf=-25)", text)
        self.assertIn("Listen to the master and the clip", text)
        self.assertEqual(denoised["noise_reduction"]["enabled"], True)
        plain, _ = self.check(unit(0), unit(40))
        self.assertIn("No noise reduction was applied", " ".join(plain["reasons"]))
        quiet, _ = self.check(unit(0), unit(10), clip={"nf": -25.0})
        self.assertEqual(quiet["verdict"], "pass")
        self.assertIn("stayed within the threshold", " ".join(quiet["reasons"]))

    def test_a_high_pass_that_cuts_into_the_voice_is_called_out_when_the_clip_is_flagged(self):
        def high(data):
            data["chain"]["highpass"].update(hz=300.0)
        flagged, _ = self.check(unit(0), unit(40), clip={"change": high})
        self.assertIn("high-pass at 300 Hz", " ".join(flagged["reasons"]))
        fine, _ = self.check(unit(0), unit(40))
        self.assertNotIn("high-pass at", " ".join(fine["reasons"]), "an 80 Hz high-pass is not blamed")

    def test_without_a_master_the_check_is_unavailable_and_the_encoder_is_never_asked(self):
        clip = self.clip()
        (clip.sidecar_path.parent / clip.data["master"]["file"]).unlink()
        encoder = FakeEncoder(unit(0), unit(0))
        result = ri.check_identity(clip, embed=encoder)
        self.assertEqual((result["status"], result["verdict"], result["score"]), ("unavailable", None, None))
        self.assertIn("is not in the sidecar's folder", " ".join(result["reasons"]))
        self.assertEqual(encoder.calls, [])

    def test_missing_encoder_weights_make_the_check_unavailable_instead_of_crashing(self):
        result, _ = self.check(None, None, error=FileNotFoundError("Speaker encoder weights are missing: ve.safetensors"))
        self.assertEqual((result["status"], result["verdict"]), ("unavailable", None))
        self.assertIn("speaker encoder is not available", " ".join(result["reasons"]))
        result, _ = self.check(None, None, error=ModuleNotFoundError("No module named 'chatterbox'"))
        self.assertEqual(result["status"], "unavailable")

    def test_a_silent_master_is_unavailable_not_a_crash(self):
        clip = self.clip(master=np.zeros(48_000 * 8, np.float32))
        result = ri.check_identity(clip, embed=FakeEncoder(unit(0), unit(0)))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("silent", " ".join(result["reasons"]))

    def test_a_master_that_does_not_match_its_sidecar_is_flagged_even_with_a_good_score(self):
        for label, change in (("rate", lambda s: s["master"].update(sample_rate=44_100)),
                              ("channels", lambda s: s["master"].update(channels=2)),
                              ("length", lambda s: s.update(master_seconds=30.0))):
            with self.subTest(label):
                result, _ = self.check(unit(0), unit(5), clip={"change": change})
                self.assertEqual(result["verdict"], "flag")
                self.assertFalse(result["master"]["matches_sidecar"])
                self.assertIn("does not match its sidecar", " ".join(result["reasons"]))
        good, _ = self.check(unit(0), unit(5))
        self.assertTrue(good["master"]["matches_sidecar"])

    def test_against_says_whether_the_clip_moved_toward_or_away_from_a_clean_recording(self):
        target = self.clips / "clean-voice.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(target), fx.synth_voice(8.0), 24_000, subtype="PCM_16")
        # master is 30 degrees from the target; the clip is 5, 30 or 70 degrees from it
        cases = {"toward": unit(25), "unchanged": unit(30), "away": unit(70)}
        for expected, kept in cases.items():
            with self.subTest(expected):
                result, _ = self.check(kept, unit(30), against=unit(0), against_file=target)
                self.assertEqual(result["target"]["direction"], expected)
                self.assertEqual(result["target"]["file"], "clean-voice.wav")
                self.assertIn("Against clean-voice.wav", result["reasons"][-1])
        unreadable, _ = self.check(None, None, against=unit(0), against_file=self.clips / "nothing.wav")
        self.assertEqual(unreadable["status"], "unavailable")

    def test_both_sides_reach_the_encoder_at_the_same_speech_level_and_in_mono(self):
        loud = fx.synth_voice(8.0)
        quiet_stereo = fx.resample(loud, 24_000, 48_000) * 0.03
        clip = self.clip(kept=loud, master=np.stack([quiet_stereo, quiet_stereo * 0.5], axis=1),
                         change=lambda s: s["master"].update(channels=2))
        encoder = FakeEncoder(unit(0), unit(0))
        result = ri.check_identity(clip, embed=encoder)
        self.assertEqual(result["status"], "checked")
        self.assertEqual(result["master"]["channels"], 2)
        (kept_audio, kept_rate), (master_audio, master_rate) = encoder.calls
        self.assertEqual((kept_audio.ndim, master_audio.ndim, kept_rate, master_rate), (1, 1, 24_000, 48_000))
        for audio, rate in ((kept_audio, kept_rate), (master_audio, master_rate)):
            self.assertAlmostEqual(ri.voiced_rms_db(audio, rate), ri.MATCH_LEVEL_DB, places=1)

    def test_the_report_records_how_it_was_made_and_survives_json(self):
        result, _ = self.check(unit(0), unit(20), clip={"nf": -25.0})
        again = json.loads(json.dumps(result))
        self.assertEqual(again, result)
        master_file = self.clips / "my-voice" / "masters"
        self.assertEqual(result["schema"], ri.RESULT_SCHEMA)
        self.assertEqual(result["method"]["encoder"], ri.ENCODER_LABEL)
        self.assertEqual(result["method"]["master_reference"], "whole_master")
        self.assertEqual(result["master"]["sha256"],
                         hashlib.sha256(next(master_file.glob("*.raw.wav")).read_bytes()).hexdigest())
        self.assertEqual(result["processing"]["denoise"]["nf"], -25.0)
        self.assertEqual(set(result["master"]["noise"]), {"speech_dbfs", "noise_floor_dbfs", "speech_over_noise_db"})
        self.assertIn("change check", result["reading"].lower())

    def test_the_check_neither_reads_nor_writes_anything_it_should_not(self):
        clip = self.clip()
        before = tree_hashes(self.clips)
        ri.check_identity(clip, embed=FakeEncoder(unit(0), unit(10)))
        self.assertEqual(tree_hashes(self.clips), before)


class StoredResultTests(IdentityCase):
    def test_results_are_kept_in_the_reference_folder_and_never_overwritten(self):
        import recorder_import
        from cast_store import CastStore
        store = CastStore(Path(self.temp.name) / "lab")
        clip = self.clip()
        report = recorder_import.import_clip(clip, character="Narrator", create_character=True, store=store,
                                             identity_check=False)
        result = ri.check_identity(clip, embed=FakeEncoder(unit(0), unit(10)))
        first = ri.store_result(store, report["reference_id"], result)
        second = ri.store_result(store, report["reference_id"], dict(result, threshold=0.5))
        self.assertNotEqual(first, second, "two checks in the same second still get two files")
        self.assertEqual(first.parent, Path(report["folder"]))
        self.assertEqual([item["threshold"] for item in ri.load_results(store, report["reference_id"])],
                         [result["threshold"], 0.5])
        self.assertEqual(json.loads(first.read_text(encoding="utf-8")), result)


class EncoderBackedTests(IdentityCase):
    """The real speaker encoder and FFmpeg's afftdn on generated speech-like audio (skipped where they are missing)."""

    @classmethod
    def setUpClass(cls):
        if not ENCODER_READY:
            raise unittest.SkipTest("the Chatterbox speaker encoder weights are not present")

    def needs_ffmpeg(self):
        if FFMPEG is None:
            self.skipTest("ffmpeg is not installed")

    def pair(self, voice, **kwargs):
        master = fx.noisy_master(voice, snr_db=25)
        return master, fx.keep(master, **kwargs)

    def test_a_clean_clip_scores_above_an_over_denoised_one_made_from_the_same_master(self):
        self.needs_ffmpeg()
        master, clean = self.pair(fx.synth_voice(12.0, seed=0))
        over = fx.keep(master, filter_text="afftdn=nf=-20:nr=60", ffmpeg=FFMPEG)
        default = fx.keep(master, filter_text="afftdn=nf=-25", ffmpeg=FFMPEG)
        good = ri.check_identity(self.clip(kept=clean, master=master))
        bad = ri.check_identity(self.clip(kept=over, master=master, nf=-20.0))
        recorder_default = ri.check_identity(self.clip(kept=default, master=master, nf=-25.0))
        self.assertEqual((good["verdict"], bad["verdict"]), ("pass", "flag"))
        self.assertGreaterEqual(good["score"], ri.DEFAULT_MIN_SIMILARITY)
        self.assertLess(bad["score"], ri.DEFAULT_MIN_SIMILARITY)
        self.assertGreater(good["score"] - bad["score"], 0.2, (good["score"], bad["score"]))
        self.assertLess(recorder_default["score"], good["score"], "Voice Slate's own default also moves the voice")
        self.assertEqual(recorder_default["verdict"], "flag")
        self.assertIn("Noise reduction was on", " ".join(bad["reasons"]))

    def test_a_plain_gain_change_does_not_flag_a_clip(self):
        master = fx.noisy_master(fx.synth_voice(12.0, seed=0), snr_db=25)
        quiet, loud = fx.keep(master, gain_db=-6.0), fx.keep(master, gain_db=12.0)
        raw = float(np.dot(ss.embedding_from_samples(quiet, 24_000), ss.embedding_from_samples(loud, 24_000)))
        self.assertLess(raw, 0.97, "the encoder alone is level-sensitive, which is why levels are matched first")
        low = ri.check_identity(self.clip(kept=quiet, master=master))
        high = ri.check_identity(self.clip(kept=loud, master=master))
        self.assertEqual((low["verdict"], high["verdict"]), ("pass", "pass"))
        self.assertLess(abs(low["score"] - high["score"]), 0.01)

    def test_a_clip_with_cuts_is_compared_with_the_cut_master(self):
        voice = fx.synth_voice(14.0, seed=0)
        master = fx.noisy_master(voice, snr_db=25)
        cuts = [(0.0, 1.5), (4.0, 5.0)]
        edits = [{"op": "cut", "region": "start", "start_seconds": 0.0, "end_seconds": 1.5},
                 {"op": "cut", "region": "middle", "start_seconds": 4.0, "end_seconds": 5.0}]
        result = ri.check_identity(self.clip(kept=fx.keep(master, cuts=cuts), master=master, edits=edits))
        self.assertEqual(result["method"]["master_reference"], "edit_aligned")
        self.assertEqual(result["verdict"], "pass", result["score"])

    def test_a_master_of_a_different_voice_is_flagged(self):
        kept = fx.keep(fx.noisy_master(fx.synth_voice(12.0, seed=0, f0=120.0), snr_db=25))
        other = fx.noisy_master(fx.synth_voice(12.0, seed=1, f0=210.0, formant_shift=1.12), snr_db=25, seed=2)
        result = ri.check_identity(self.clip(kept=kept, master=other))
        self.assertTrue(result["master"]["matches_sidecar"])
        self.assertEqual(result["verdict"], "flag", result["score"])

    def test_a_clean_recording_of_the_voice_shows_whether_cleaning_helped_or_hurt(self):
        self.needs_ffmpeg()
        voice = fx.synth_voice(12.0, seed=0)
        clean_file = self.clips / "clean-voice.wav"
        clean_file.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(clean_file), voice, 24_000, subtype="PCM_16")
        master, plain = self.pair(voice)
        over = fx.keep(master, filter_text="afftdn=nf=-20:nr=60", ffmpeg=FFMPEG)
        untouched = ri.check_identity(self.clip(kept=plain, master=master), against=clean_file)
        damaged = ri.check_identity(self.clip(kept=over, master=master, nf=-20.0), against=clean_file)
        self.assertNotEqual(untouched["target"]["direction"], "away")
        self.assertEqual(damaged["target"]["direction"], "away")
        self.assertLess(damaged["target"]["clip_to_target"], damaged["target"]["master_to_target"])

    def test_embedding_from_samples_matches_embedding_from_a_file(self):
        path = Path(self.temp.name) / "voice.wav"
        stereo = np.stack([fx.synth_voice(6.0), fx.synth_voice(6.0) * 0.5], axis=1)
        sf.write(str(path), stereo, 24_000, subtype="PCM_16")
        samples, rate = sf.read(str(path), dtype="float32", always_2d=True)
        np.testing.assert_allclose(ss.embedding(path), ss.embedding_from_samples(samples, rate), atol=1e-6)
        self.assertAlmostEqual(float(np.linalg.norm(ss.embedding(path))), 1.0, places=5)

    @unittest.skipUnless(REAL_VOICE.is_file(), "the local af_heart reference clip is not present")
    def test_the_same_separation_holds_on_real_speech(self):
        self.needs_ffmpeg()
        voice, rate = sf.read(str(REAL_VOICE), dtype="float32")
        master = fx.noisy_master(voice, snr_db=25)
        scores = {}
        for label, filter_text in (("plain", None), ("default", "afftdn=nf=-25"), ("over", "afftdn=nf=-20:nr=60")):
            kept = fx.keep(master, filter_text=filter_text, ffmpeg=FFMPEG)
            scores[label] = ri.check_identity(self.clip(kept=kept, master=master,
                                                        nf=None if filter_text is None else -25.0))
        self.assertEqual(scores["plain"]["verdict"], "pass", scores["plain"]["score"])
        self.assertEqual(scores["default"]["verdict"], "flag", scores["default"]["score"])
        self.assertLess(scores["over"]["score"], scores["default"]["score"])
        self.assertLess(scores["over"]["score"], 0.75)


if __name__ == "__main__":
    unittest.main()
