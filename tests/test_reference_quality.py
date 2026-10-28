"""Pre-save reference screen: each check on synthetic audio with a known answer, then the store and the preview route."""

import base64
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf
from scipy.signal import fftconvolve

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import reference_quality as rq  # noqa: E402
import serve  # noqa: E402
from cast_store import CastStore  # noqa: E402
from worker_pool import read_catalog  # noqa: E402

RATE = 24_000


def burst_envelope(seconds, on=0.8, off=0.4):
    """1 while a word would be spoken, 0 in the gap; 5 ms ramps stop the edges clicking."""
    t = np.arange(round(seconds * RATE)) / RATE
    envelope = (t % (on + off) < on).astype(np.float64)
    ramp = round(0.005 * RATE)
    return np.convolve(envelope, np.ones(ramp) / ramp, mode="same")


def speech_like(seconds=12.0, level_db=-20.0, pause_db=-70.0, seed=1):
    """Noise bursts over a quiet floor: a stand-in for read speech with clean pauses."""
    rng = np.random.default_rng(seed)
    envelope = burst_envelope(seconds)
    gain = envelope * 10 ** (level_db / 20) + (1 - envelope) * 10 ** (pause_db / 20)
    return rng.standard_normal(gain.size) * gain, envelope


def reverberant(samples, rt60, seed=2):
    """Convolve with a decaying noise impulse response of the given RT60 (seconds)."""
    rng = np.random.default_rng(seed)
    t = np.arange(round(1.2 * RATE)) / RATE
    impulse = rng.standard_normal(t.size) * np.exp(-6.9 * t / rt60)
    impulse /= np.sqrt(np.sum(impulse ** 2))
    tail = fftconvolve(samples, impulse)[: len(samples)]
    return tail / np.max(np.abs(tail)) * np.max(np.abs(samples)) * 0.5


def tone_voice(frequency, seconds=6.0):
    """A pure tone in bursts over a quiet floor: one 'voice' for the speaker check."""
    t = np.arange(round(seconds * RATE)) / RATE
    envelope = burst_envelope(seconds)
    rng = np.random.default_rng(int(frequency))
    return 0.1 * np.sin(2 * np.pi * frequency * t) * envelope + 10 ** (-70 / 20) * rng.standard_normal(t.size) * (1 - envelope)


class TwoToneSpeakers:
    """Stands in for speaker_similarity: each clip's embedding is its energy share at 440 Hz and 1 kHz."""

    def embedding_from_samples(self, samples, rate):
        spectrum = np.abs(np.fft.rfft(samples)) ** 2
        freqs = np.fft.rfftfreq(len(samples), 1 / rate)
        vector = np.array([spectrum[np.abs(freqs - hz) < 20].sum() for hz in (440, 1000)])
        return vector / np.linalg.norm(vector)

    @staticmethod
    def band(score):
        return "likely_same" if score >= 0.847 else "likely_different" if score < 0.796 else "uncertain"


class BrokenSpeakers(TwoToneSpeakers):
    def embedding_from_samples(self, samples, rate):
        raise RuntimeError("encoder weights missing")


class ReferenceScreenTests(unittest.TestCase):
    def test_clean_speech_like_clip_passes_with_the_speaker_check_off(self):
        samples, _ = speech_like()
        result = rq.assess(samples, RATE)
        self.assertEqual(result["warnings"], [])
        self.assertEqual((result["score"], result["verdict"]), (100, "good"))
        self.assertEqual(result["checks"]["speakers"]["status"], "not_checked")
        self.assertIn("not installed", result["checks"]["speakers"]["reason"])

    def test_a_high_noise_floor_is_flagged(self):
        samples, _ = speech_like(pause_db=-45.0)
        result = rq.assess(samples, RATE)
        check = result["checks"]["noise_floor"]
        self.assertEqual(check["status"], "warn")
        self.assertAlmostEqual(check["floor_dbfs"], -45, delta=3)
        self.assertTrue(any(warning.startswith("Noise:") for warning in result["warnings"]))

    def test_reverberant_tails_are_flagged_with_an_estimate_near_the_true_rt60(self):
        dry, _ = speech_like()
        wet = reverberant(speech_like()[0], rt60=0.9)
        self.assertLess(rq.assess(dry, RATE)["checks"]["reverb"]["rt60_seconds"], rq.RT60_WARN_SECONDS)
        check = rq.assess(wet, RATE)["checks"]["reverb"]
        self.assertEqual(check["status"], "warn")
        self.assertAlmostEqual(check["rt60_seconds"], 0.9, delta=0.25)

    def test_a_steady_tone_in_the_pauses_is_flagged_by_frequency(self):
        samples, envelope = speech_like()
        t = np.arange(samples.size) / RATE
        hummed = samples + 10 ** (-45 / 20) * np.sin(2 * np.pi * 1000 * t) * (1 - envelope)
        check = rq.assess(hummed, RATE)["checks"]["background_tones"]
        self.assertEqual(check["status"], "warn")
        self.assertAlmostEqual(check["strongest_hz"], 1000, delta=20)

    def test_clipping_is_counted_and_heavier_clipping_scores_lower(self):
        samples, _ = speech_like()
        touched = samples.copy()
        touched[100] = 1.0
        light = rq.assess(touched, RATE)
        heavy = rq.assess(np.clip(samples * 8, -1, 1), RATE)
        self.assertEqual(light["checks"]["clipping"]["clipped_samples"], 1)
        self.assertFalse(light["checks"]["clipping"]["heavy"])
        self.assertTrue(heavy["checks"]["clipping"]["heavy"])
        self.assertLess(heavy["score"], light["score"])

    def test_a_clip_under_one_second_reports_each_acoustic_check_as_not_checked(self):
        result = rq.assess(speech_like(seconds=0.6)[0], RATE)
        for name in rq.ACOUSTIC_CHECKS:
            self.assertEqual(result["checks"][name]["status"], "not_checked")
        self.assertEqual(result["verdict"], "good")


class SpeakerScreenTests(unittest.TestCase):
    def test_a_second_voice_in_the_clip_is_flagged(self):
        samples = np.concatenate([tone_voice(440), tone_voice(1000)])
        result = rq.assess(samples, RATE, speakers=TwoToneSpeakers())
        check = result["checks"]["speakers"]
        self.assertEqual(check["status"], "warn")
        self.assertTrue(any(warning.startswith("Voice:") for warning in result["warnings"]))
        self.assertEqual(result["score"], 70)

    def test_one_voice_passes(self):
        result = rq.assess(tone_voice(440, seconds=12.0), RATE, speakers=TwoToneSpeakers())
        check = result["checks"]["speakers"]
        self.assertEqual(check["status"], "pass")
        self.assertGreaterEqual(min(part["score"] for part in check["parts"]), 0.99)

    def test_an_encoder_failure_is_reported_and_does_not_raise(self):
        result = rq.assess(tone_voice(440, seconds=12.0), RATE, speakers=BrokenSpeakers())
        check = result["checks"]["speakers"]
        self.assertEqual(check["status"], "not_checked")
        self.assertIn("encoder weights missing", check["reason"])


def wav_bytes(samples):
    result = io.BytesIO()
    sf.write(result, samples, RATE, format="WAV", subtype="PCM_16")
    return result.getvalue()


class NoEngines:
    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {row["id"]: row for row in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"
        self.loaded = []
        self.calls = []


class ReferenceScreenStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-reference-screen-")
        self.root = Path(self.temp.name)
        self.store = CastStore(self.root)
        self.store.speakers = None  # the speaker check is covered above; these tests stay off the encoder
        character = self.store.create_character("Ada", "Synthetic test voice")
        self.identity = self.store.create_identity(character["id"], "qwen3-tts-1.7b-base", "Neutral")

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_and_saved_reference_carry_the_screen_but_the_stored_row_does_not(self):
        source = wav_bytes(speech_like()[0])
        _audio, metrics = self.store.preview_reference(self.identity["id"], "ada.wav", source,
                                                       edits={"target_sample_rate": RATE})
        self.assertEqual(metrics["quality"]["verdict"], "good")
        saved = self.store.add_reference(self.identity["id"], "ada.wav", source, transcript="A clear line.",
                                         origin="synthetic fixture", allowed_use="tests only",
                                         edits={"target_sample_rate": RATE})
        self.assertEqual(saved["quality"]["score"], 100)
        self.assertNotIn("quality", self.store.get_reference(saved["id"]))


class ReferenceScreenRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-reference-route-")
        self.cast = CastStore(Path(self.temp.name))
        self.cast.speakers = None
        character = self.cast.create_character("Ada", "Synthetic test voice")
        self.identity = self.cast.create_identity(character["id"], "qwen3-tts-1.7b-base", "Neutral")
        engines = NoEngines()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(
            engines, 0, self.cast, evaluation_store=SimpleNamespace()))
        self.server.RequestHandlerClass = serve.make_handler(
            engines, self.server.server_address[1], self.cast, evaluation_store=SimpleNamespace())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def test_preview_response_carries_the_screen_in_a_header(self):
        metadata = {"filename": "ada.wav", "edits": {"target_sample_rate": RATE, "channel": "mono"}}
        encoded = base64.urlsafe_b64encode(json.dumps(metadata).encode("utf-8")).decode("ascii").rstrip("=")
        request = urllib.request.Request(
            f"{self.base}/api/identities/{self.identity['id']}/references/preview",
            data=wav_bytes(speech_like()[0]), method="POST",
            headers={"Content-Type": "audio/wav", "X-Reference-Metadata": encoded})
        with urllib.request.urlopen(request, timeout=10) as response:
            header = response.headers["X-Reference-Quality"]
        quality = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
        self.assertEqual((quality["score"], quality["verdict"]), (100, "good"))


if __name__ == "__main__":
    unittest.main()
