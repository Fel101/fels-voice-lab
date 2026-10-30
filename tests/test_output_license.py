"""Export receipts keep each model's own terms apart from the application's MIT licence."""
import unittest

from output_license import license_profile


class OutputLicenseTests(unittest.TestCase):
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
