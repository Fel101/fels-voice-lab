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


if __name__ == "__main__":
    unittest.main()
