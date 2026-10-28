"""Reference denoiser: input rules, no silent overwrite, the too-short refusal, and the identity gate."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import denoise_ref as dr
import speaker_similarity as ss

try:
    FFMPEG_READY = dr.find_ffmpeg() is not None
except dr.DenoiseError:
    FFMPEG_READY = False


def speechlike_wav(path, seconds):
    """A noisy harmonic tone; it is not speech, so only the file rules and gate wiring are tested."""
    rate = 24_000
    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    tone = 0.2 * np.sin(2 * np.pi * 180 * t) + 0.05 * np.sin(2 * np.pi * 360 * t)
    noise = np.random.default_rng(3).normal(0, 0.01, t.shape).astype(np.float32)
    sf.write(str(path), (tone + noise).astype(np.float32), rate, subtype="PCM_16")
    return path


class DenoiseRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-denoise-")
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_noise_floor_outside_afftdn_range_is_refused_before_any_work(self):
        clip = speechlike_wav(self.dir / "clip.wav", 1.0)
        for nf in (-10, -85):
            with self.subTest(nf=nf):
                with self.assertRaisesRegex(dr.DenoiseError, "between -80 and -20"):
                    dr.denoise(clip, self.dir / "out.wav", nf=nf)

    def test_missing_input_is_reported(self):
        with self.assertRaisesRegex(dr.DenoiseError, "not found"):
            dr.denoise(self.dir / "missing.wav", self.dir / "out.wav")

    @unittest.skipUnless(FFMPEG_READY, "ffmpeg is not installed")
    def test_existing_output_is_kept_unless_overwrite_is_given(self):
        clip = speechlike_wav(self.dir / "clip.wav", 8.0)
        target = self.dir / "kept.wav"
        target.write_bytes(b"do not replace")
        with self.assertRaisesRegex(dr.DenoiseError, "Refusing to replace"):
            dr.denoise(clip, target)
        self.assertEqual(target.read_bytes(), b"do not replace")

    @unittest.skipUnless(FFMPEG_READY and ss.encoder_available(), "ffmpeg or the encoder is missing")
    def test_window_at_or_under_five_seconds_fails_and_strict_writes_nothing(self):
        clip = speechlike_wav(self.dir / "clip.wav", 8.0)
        report = dr.denoise(clip, self.dir / "short.wav", start=0.0, dur=4.0)
        self.assertEqual(report["verdict"], "fail")
        self.assertTrue(any("needs more than 5 s" in reason for reason in report["reasons"]))
        strict_target = self.dir / "strict.wav"
        with self.assertRaises(dr.DenoiseError):
            dr.denoise(clip, strict_target, start=0.0, dur=4.0, strict=True)
        self.assertFalse(strict_target.exists(), "strict mode must not leave a failing clip behind")


@unittest.skipUnless(FFMPEG_READY and ss.encoder_available(), "ffmpeg or the encoder is missing")
class IdentityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-gate-")
        self.dir = Path(self.temp.name)
        self.clip = speechlike_wav(self.dir / "clip.wav", 8.0)

    def tearDown(self):
        self.temp.cleanup()

    def test_without_a_target_the_identity_is_not_checked_and_the_verdict_is_at_least_warn(self):
        report = dr.denoise(self.clip, self.dir / "no-target.wav")
        self.assertEqual(report["verdict"], "warn")
        self.assertIsNone(report["identity"]["target"]["path"])
        self.assertTrue(any("identity not checked" in reason for reason in report["reasons"]))
        self.assertTrue((self.dir / "no-target.json").is_file())

    def test_a_target_is_scored_before_and_after_cleaning_and_the_report_says_so(self):
        target = speechlike_wav(self.dir / "target.wav", 8.0)
        report = dr.denoise(self.clip, self.dir / "against.wav", compare_to=target)
        measured = report["identity"]["target"]
        self.assertEqual(measured["path"], str(target))
        self.assertIsNotNone(measured["uncleaned"])
        self.assertIsNotNone(measured["cleaned"])
        self.assertIn(report["identity"]["target_band"], {"likely_same", "uncertain", "likely_different"})
        self.assertTrue((self.dir / "against.wav").is_file(), "a non-strict run keeps the clip for review")


if __name__ == "__main__":
    unittest.main()
