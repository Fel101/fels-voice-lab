import unittest

from chatterbox_support import normalize_chatterbox_sampling


class ChatterboxSamplingContractTests(unittest.TestCase):
    def test_defaults_match_the_installed_original_and_turbo_signatures(self):
        self.assertEqual(normalize_chatterbox_sampling("orig", {}), {
            "exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8,
            "top_p": 1.0, "repetition_penalty": 1.2, "min_p": 0.05,
        })
        self.assertEqual(normalize_chatterbox_sampling("turbo", {}), {
            "temperature": 0.8, "top_k": 1000, "top_p": 0.95, "repetition_penalty": 1.2,
        })

    def test_variant_unsupported_settings_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not support.*top_k"):
            normalize_chatterbox_sampling("orig", {"top_k": 40})
        with self.assertRaisesRegex(ValueError, "does not support.*exaggeration"):
            normalize_chatterbox_sampling("turbo", {"exaggeration": 0.8})

    def test_out_of_range_nonfinite_and_nonintegral_values_are_rejected(self):
        for value in (-0.1, 2.1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_chatterbox_sampling("orig", {"temperature": value})
        with self.assertRaisesRegex(ValueError, "whole number"):
            normalize_chatterbox_sampling("turbo", {"top_k": 20.5})


if __name__ == "__main__":
    unittest.main()
