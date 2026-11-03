"""Proxy speaker similarity: the shipped bands, take voice-match routing, and real encoder checks."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import speaker_similarity as ss
from tests import recorder_fixtures as fx
from tests.test_export_panel import ExportPanelBase

ENCODER_READY = ss.encoder_available()


class BandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-band-")
        self.path = Path(self.temp.name) / "bands.json"
        self.path.write_text(json.dumps({"bands": {"likely_same_voice_at_or_above": 0.85,
                                                   "likely_different_voice_below": 0.80}}), encoding="utf-8")

    def tearDown(self):
        ss._bands = None
        self.temp.cleanup()

    def test_scores_map_to_the_three_labels(self):
        with mock.patch.object(ss, "BANDS_FILE", self.path):
            ss._bands = None
            self.assertEqual(ss.band(0.91), "likely_same")
            self.assertEqual(ss.band(0.85), "likely_same")
            self.assertEqual(ss.band(0.82), "uncertain")
            self.assertEqual(ss.band(0.79), "likely_different")

    def test_a_missing_bands_file_fails_loudly_instead_of_passing_everything(self):
        with mock.patch.object(ss, "BANDS_FILE", Path(self.temp.name) / "absent.json"):
            ss._bands = None
            with self.assertRaisesRegex(FileNotFoundError, "bands are missing"):
                ss.band(0.99)


class ShippedBandsTests(unittest.TestCase):
    """catalog/speaker_similarity.json is what a fresh install labels scores with. Nothing here is mocked."""

    def setUp(self):
        ss._bands = None
        self.addCleanup(setattr, ss, "_bands", None)
        self.text = ss.BANDS_FILE.read_text(encoding="utf-8")
        self.data = json.loads(self.text)

    def test_a_fresh_install_labels_scores_with_no_calibration_step(self):
        labels = [ss.band(score) for score in (0.95, 0.847, 0.82, 0.796, 0.5)]
        self.assertEqual(labels, ["likely_same", "likely_same", "uncertain", "uncertain", "likely_different"])

    def test_the_bands_leave_a_gap_for_uncertain(self):
        bands = self.data["bands"]
        self.assertLess(bands["likely_different_voice_below"], bands["likely_same_voice_at_or_above"])

    def test_the_bands_are_the_percentiles_of_the_clip_statistics_beside_them(self):
        groups, bands = self.data["groups"], self.data["bands"]
        self.assertEqual((groups["same_voice"]["n"], groups["other_voice"]["n"]), (41, 87))
        self.assertEqual(bands["likely_same_voice_at_or_above"], groups["same_voice"]["p10"])
        self.assertEqual(bands["likely_different_voice_below"], groups["other_voice"]["p90"])

    def test_the_file_names_no_clip_and_no_folder_of_the_machine_it_was_measured_on(self):
        self.assertNotRegex(self.text, r"\.wav|[A-Za-z]:[\\/]|/Users/|\bout/")

    def test_the_guides_quote_the_numbers_in_the_file(self):
        guide = (ROOT / "docs" / "RECORDER_BRIDGE.md").read_text(encoding="utf-8")
        for number in map(str, self.data["bands"].values()):
            self.assertIn(number, guide)


class VoiceMatchTests(unittest.TestCase):
    class CastWithReference:
        def get_take(self, take_id):
            return {"id": take_id, "reference_version_id": "r" * 32}

        def get_media_path(self, kind, media_id):
            return Path(f"{kind}-{media_id}.wav")

    def setUp(self):
        ss._bands = None
        self.addCleanup(setattr, ss, "_bands", None)

    def test_without_the_encoder_the_reason_names_the_encoder_and_no_other_step(self):
        with mock.patch.object(ss, "encoder_available", return_value=False):
            result = ss.voice_match(self.CastWithReference(), "a" * 32)
        self.assertFalse(result["available"])
        self.assertIn("encoder is not installed", result["reason"])
        self.assertNotIn("calibrat", result["reason"].lower())

    def test_with_the_encoder_a_take_is_labelled_by_the_shipped_bands(self):
        with mock.patch.object(ss, "encoder_available", return_value=True), \
                mock.patch.object(ss, "similarity", return_value=0.9123):
            result = ss.voice_match(self.CastWithReference(), "a" * 32)
        self.assertEqual((result["available"], result["score"], result["band"]), (True, 0.9123, "likely_same"))
        self.assertTrue(result["proxy"])


class VoiceMatchRouteTests(ExportPanelBase):
    def test_unknown_take_is_404_and_preset_take_is_reported_unavailable(self):
        base = self.start_server()
        import urllib.error
        import urllib.request

        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/api/takes/{'0' * 32}/voice-match", timeout=10)
        self.assertEqual(missing.exception.code, 404)

        class PresetCast:
            def get_take(self, take_id):
                return {"id": take_id, "reference_version_id": None}

        self.assertEqual(ss.voice_match(PresetCast(), "a" * 32)["available"], False)


GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "speaker_encoder_golden.json").read_text(encoding="utf-8"))


def tone(seconds, rate, seed):
    """Harmonics with a wobbling pitch, a slow swell and a little hiss: the recipe tests/fixtures/speaker_encoder_golden.json used."""
    t = np.arange(int(seconds * rate)) / rate
    voice = sum(np.sin(2 * np.pi * (110 + 37 * seed) * k * t * (1 + 0.03 * np.sin(2 * np.pi * 0.5 * t))) / k for k in range(1, 9))
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * (2.3 + 0.4 * seed) * t) ** 2
    hiss = np.sin(t * 7919.0 * (1 + seed)) * np.sin(t * 104729.0)
    return (0.08 * voice * envelope + 0.01 * hiss).astype(np.float32)


def with_silence(signal, rate):
    quiet = np.zeros(int(0.6 * rate), dtype=np.float32)
    return np.concatenate([quiet, 0.001 * signal[: rate // 2], signal, quiet])


def synthetic_weights():
    """Full-size LSTM and projection weights from a formula, so PyTorch's answer for them can be kept in the golden file."""
    shapes = {"lstm.weight_ih_l0": (1024, 40), "lstm.weight_hh_l0": (1024, 256), "lstm.bias_ih_l0": (1024,), "lstm.bias_hh_l0": (1024,)}
    for layer in (1, 2):
        shapes.update({f"lstm.weight_ih_l{layer}": (1024, 256), f"lstm.weight_hh_l{layer}": (1024, 256),
                       f"lstm.bias_ih_l{layer}": (1024,), f"lstm.bias_hh_l{layer}": (1024,)})
    shapes.update({"proj.weight": (256, 256), "proj.bias": (256,)})
    return {name: (0.08 * np.sin(np.arange(int(np.prod(shape)), dtype=np.float64) * 0.7311 + number)).astype(np.float32).reshape(shape)
            for number, (name, shape) in enumerate(sorted(shapes.items()))}


def synthetic_partials():
    return (2.0 * np.abs(np.sin(np.arange(3 * 160 * 40, dtype=np.float64) * 0.013))).astype(np.float32).reshape(3, 160, 40)


def write_safetensors(path, tensors):
    """The smallest safetensors writer: an 8-byte header length, a JSON header, then the float32 data."""
    header, offset, blobs = {}, 0, []
    for name, value in tensors.items():
        data = np.ascontiguousarray(value, dtype="<f4").tobytes()
        header[name] = {"dtype": "F32", "shape": list(value.shape), "data_offsets": [offset, offset + len(data)]}
        offset += len(data)
        blobs.append(data)
    encoded = json.dumps(header).encode("utf-8")
    Path(path).write_bytes(len(encoded).to_bytes(8, "little") + encoded + b"".join(blobs))


class NumpyEncoderTests(unittest.TestCase):
    """The encoder runs with NumPy alone. These hold each step to what librosa 0.11.0 and Chatterbox 0.1.7 (with PyTorch) gave
    for the same input, from tests/fixtures/speaker_encoder_golden.json, so they need neither those packages nor Turbo's weights."""

    def test_the_mel_bank_is_librosas(self):
        bank = ss.mel_bank()
        self.assertEqual((bank.shape, bank.dtype), ((40, 201), np.float32))
        np.testing.assert_allclose(bank.sum(axis=1), GOLDEN["mel_bank"]["row_sums"], rtol=1e-6)
        np.testing.assert_allclose(bank.max(axis=1), GOLDEN["mel_bank"]["row_max"], rtol=1e-6)
        self.assertEqual(bank.argmax(axis=1).tolist(), GOLDEN["mel_bank"]["row_peaks"])

    def test_the_trim_cuts_where_librosa_cuts(self):
        for seed in (1, 2):
            signal = with_silence(tone(1.7, 16000, seed), 16000)
            start, end = GOLDEN["trim"][str(seed)]
            np.testing.assert_array_equal(ss.trim(signal), signal[start:end])
        self.assertEqual(ss.trim(np.zeros(0, dtype=np.float32)).size, 0)

    def test_the_mel_frames_are_chatterboxs(self):
        mels = ss.mel_frames(tone(0.73, 16000, 3))
        self.assertEqual(list(mels.shape), GOLDEN["mel_frames"]["shape"])
        np.testing.assert_allclose(mels.sum(axis=1), GOLDEN["mel_frames"]["frame_sums"], rtol=1e-5)
        np.testing.assert_allclose(mels.sum(axis=0), GOLDEN["mel_frames"]["band_sums"], rtol=1e-5)

    def test_the_partials_are_the_windows_chatterbox_cuts(self):
        for frames, (count, needed) in GOLDEN["partials"].items():
            mels = np.arange(int(frames) * 40, dtype=np.float32).reshape(int(frames), 40)
            parts = ss.partials(mels)
            self.assertEqual(parts.shape, (count, 160, 40), frames)
            padded = np.concatenate([mels, np.zeros((max(0, needed - len(mels)), 40), dtype=np.float32)])
            np.testing.assert_array_equal(parts[-1], padded[(count - 1) * 77:(count - 1) * 77 + 160])

    def test_the_lstm_gives_what_pytorch_gave_for_the_same_weights(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ve.safetensors"
            write_safetensors(path, synthetic_weights())
            weights = ss.read_safetensors(path)
        embeddings = ss.encode(synthetic_partials(), weights)
        np.testing.assert_allclose(embeddings, GOLDEN["synthetic_lstm"], atol=2e-6)
        np.testing.assert_allclose(np.linalg.norm(embeddings, axis=1), 1.0, atol=1e-6)

    def test_weights_in_another_number_format_are_refused_by_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ve.safetensors"
            header = json.dumps({"proj.bias": {"dtype": "F16", "shape": [2], "data_offsets": [0, 4]}}).encode()
            path.write_bytes(len(header).to_bytes(8, "little") + header + b"\0" * 4)
            with self.assertRaisesRegex(ValueError, "proj.bias is F16"):
                ss.read_safetensors(path)

    def test_the_encoder_needs_only_its_weights(self):
        with mock.patch.object(ss, "ENCODER_WEIGHTS", ROOT / "no-such-folder" / "ve.safetensors"):
            self.assertFalse(ss.encoder_available())
        imports = [line.split()[1] for line in (ROOT / "speaker_similarity.py").read_text(encoding="utf-8").splitlines()
                   if line.startswith(("import ", "from "))]
        self.assertFalse({name.split(".")[0] for name in imports} & {"torch", "chatterbox", "librosa", "safetensors"}, imports)

    @unittest.skipUnless(ENCODER_READY, "the Chatterbox speaker encoder weights are not present")
    def test_with_turbos_weights_the_embeddings_are_the_ones_chatterbox_made(self):
        import hashlib

        if hashlib.sha256(ss.ENCODER_WEIGHTS.read_bytes()).hexdigest() != GOLDEN["real_weights_sha256"]:
            self.skipTest("these are not the pinned weights the golden embeddings were made with")
        for name, rate, seed in (("tone_24k", 24000, 4), ("tone_16k", 16000, 5)):
            vector = ss.embedding_from_samples(with_silence(tone(6.2, rate, seed), rate), rate)
            np.testing.assert_allclose(vector, GOLDEN["real"][name], atol=2e-6, err_msg=name)


@unittest.skipUnless(ENCODER_READY, "the Chatterbox speaker encoder weights are not present")
class EncoderTests(unittest.TestCase):
    """The real encoder on generated speech-like audio, so no recording or earlier output is needed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-encoder-")
        self.addCleanup(self.temp.cleanup)

    def clip(self, name, **voice):
        path = Path(self.temp.name) / name
        sf.write(str(path), fx.synth_voice(seconds=8.0, **voice), fx.KEPT_RATE, subtype="PCM_16")
        return path

    def test_a_clip_scores_one_against_itself(self):
        clip = self.clip("a.wav")
        self.assertAlmostEqual(ss.similarity(clip, clip), 1.0, places=3)

    def test_the_same_voice_scores_above_a_different_voice(self):
        target = self.clip("target.wav", f0=120.0, seed=0)
        same = self.clip("same.wav", f0=120.0, seed=7)
        other = self.clip("other.wav", f0=220.0, seed=0, formant_shift=1.3)
        self.assertGreater(ss.similarity(target, same), ss.similarity(target, other))


if __name__ == "__main__":
    unittest.main()
