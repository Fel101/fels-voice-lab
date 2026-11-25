"""Export receipts keep each model's own terms apart from the application's MIT licence."""
import unittest
from pathlib import Path

from voice_lab.output_license import (
    DESIGN_ORIGIN, LICENCE_RATINGS, designer_of, license_profile, model_licence, voice_licence, worst_licence,
)
from voice_lab.reference_catalog import design_provenance
from voice_lab.worker_pool import read_catalog

ROOT = Path(__file__).resolve().parents[1]


class OutputLicenseTests(unittest.TestCase):
    def test_every_model_has_a_rating_and_reason(self):
        for entry in read_catalog()["engines"]:
            with self.subTest(engine=entry["id"]):
                self.assertIn(entry["licence"]["rating"], LICENCE_RATINGS)
                self.assertTrue(entry["licence"]["reason"])

    def test_a_designed_recording_is_rated_by_the_model_that_designed_it(self):
        origin = design_provenance("qwen3-tts-1.7b-voice-design", {}, {})["origin"]
        self.assertEqual(designer_of(origin), "qwen3-tts-1.7b-voice-design")
        for other in ("My own recording", "", None, DESIGN_ORIGIN):
            self.assertIsNone(designer_of(other))
        designed = voice_licence("turbo", "reference", None, origin)
        self.assertEqual(designed["rating"], "yes")
        self.assertIn("The recording was designed with Qwen3-TTS 1.7B VoiceDesign: Qwen3-TTS uses Apache-2.0.",
                      designed["reason"])
        self.assertEqual(voice_licence("turbo", "reference", None, f"{DESIGN_ORIGIN}breeze-tts-2; x")["rating"], "no")
        self.assertEqual(voice_licence("turbo", "reference", None, f"{DESIGN_ORIGIN}unknown-model; x")["rating"], "unclear")
        # A rating saved in Cast is the recording's own, and wins over the design model's.
        self.assertEqual(voice_licence("turbo", "reference", "conditions", origin)["rating"], "conditions")
        self.assertEqual(voice_licence("turbo", "reference", None, "My own recording")["rating"], "unclear")

    def test_the_page_labels_a_design_saved_from_recent_clips_as_the_service_does(self):
        # web/voice_workflows.js writes a design's origin in these words and this form (tests/voice_workflows.test.cjs).
        self.assertIn(f"var DESIGN_ORIGIN = '{DESIGN_ORIGIN}';", (ROOT / "web" / "voice_workflows.js").read_text(encoding="utf-8"))
        made = design_provenance("voxcpm2", {"checkpoint": "openbmb/VoxCPM2", "checkpoint_revision": "abc"}, {})["origin"]
        self.assertEqual(made, f"{DESIGN_ORIGIN}voxcpm2; openbmb/VoxCPM2 revision abc.")
        self.assertEqual(design_provenance("voxcpm2", {}, {})["origin"],
                         f"{DESIGN_ORIGIN}voxcpm2; unrecorded checkpoint revision unrecorded revision.")

    def test_worst_recording_or_model_rating_wins(self):
        for engine, ratings in {
            "turbo": ["unclear", "yes", "conditions", "unclear", "no"],
            "drama-3": ["unclear", "conditions", "conditions", "unclear", "no"],
            "breeze-tts-2": ["no", "no", "no", "no", "no"],
        }.items():
            for recording, expected in zip((None, *LICENCE_RATINGS), ratings, strict=True):
                with self.subTest(engine=engine, recording=recording):
                    self.assertEqual(voice_licence(engine, "reference", recording)["rating"], expected)
        self.assertEqual(model_licence("phonon-31m-en")["rating"], "yes")
        self.assertEqual(voice_licence("phonon-31m-en", "preset_voice")["rating"], "unclear")
        self.assertEqual(voice_licence("missing-model", "fixed_voice")["rating"], "unclear")
        self.assertEqual(voice_licence("kokoro", "fixed_voice")["rating"], "yes")
        clearer = model_licence("kokoro")
        restrictive = model_licence("breeze-tts-2")
        self.assertEqual(worst_licence(clearer, restrictive, restrictive), restrictive)

    def test_export_receipts_keep_model_terms_separate_from_application_mit(self):
        drama = license_profile("drama-3", {}, {"allowed_use": "self-recorded permitted use"})
        self.assertEqual(drama["status"], "restricted")
        self.assertIn("Fish Audio Community", drama["weights_license"])
        self.assertIn("Built with Fish Audio", drama["output_use"])
        breeze = license_profile("breeze-tts-2", {}, {"allowed_use": "personal use only"})
        self.assertEqual(breeze["status"], "restricted")
        self.assertIn("Non-Commercial", breeze["weights_license"])

    def test_phonon_receipts_say_the_weights_are_apache_and_the_voices_state_no_licence(self):
        phonon = license_profile("phonon-31m-en", {}, {})
        self.assertEqual(phonon["status"], "unknown")
        self.assertIn("Apache-2.0", phonon["weights_license"])
        self.assertIn("states no licence", phonon["weights_license"])
        self.assertIn("Ask Gradium", phonon["output_use"])
        self.assertIn("ptts", phonon["code_license"])
        self.assertIn("Unknown", phonon["recorded_reference_allowed_use"])


if __name__ == "__main__":
    unittest.main()
