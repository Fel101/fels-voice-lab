"""Proxy speaker similarity: the shipped bands, take voice-match routing, and real encoder checks."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
