import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from kokoro_support import (KOKORO_LANGUAGES, discover_kokoro_voices,
                            normalize_kokoro_voices, resolve_kokoro_language)
from scene_store import SUPPORTED_LANGUAGES


class KokoroM7ContractTests(unittest.TestCase):
    def test_cached_voice_discovery_uses_local_model_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "voices"
            folder.mkdir()
            for name in ("af_heart", "am_fenrir", "ef_dora", "ff_siwis"):
                (folder / (name + ".pt")).write_bytes(b"discovery fixture; not a checkpoint")
            with patch("kokoro_support.kokoro_snapshot_dir", return_value=Path(temp)):
                voices = discover_kokoro_voices()
        self.assertIn("af_heart", voices)
        self.assertIn("am_fenrir", voices)
        self.assertIn("ef_dora", voices)
        self.assertIn("ff_siwis", voices)

    def test_equal_blend_is_canonical_and_rejects_missing_or_duplicate_voices(self):
        self.assertEqual(normalize_kokoro_voices("am_fenrir,af_heart", ["af_heart", "am_fenrir"]),
                         ["af_heart", "am_fenrir"])
        with self.assertRaisesRegex(ValueError, "more than once"):
            normalize_kokoro_voices("af_heart,af_heart", ["af_heart"])
        with self.assertRaisesRegex(ValueError, "not cached locally"):
            normalize_kokoro_voices("af_missing", ["af_heart"])

    def test_language_routing_supports_aliases_and_infers_british_pipeline(self):
        self.assertEqual(resolve_kokoro_language("English", ["bf_emma"]), "b")
        self.assertEqual(resolve_kokoro_language("English", ["af_heart", "am_fenrir"]), "a")
        self.assertEqual(resolve_kokoro_language("es", ["ef_dora"]), "e")
        self.assertEqual(resolve_kokoro_language("French", ["ff_siwis"]), "f")

    def test_scene_schema_lists_the_kokoro_language_labels(self):
        # Presence in the allowlist only; whether a label has a voice pack or a screen is in docs/LANGUAGE_MATRIX.md.
        for language in ("American English", "British English", "Spanish", "French", "Hindi",
                         "Italian", "Brazilian Portuguese"):
            self.assertIn(language, SUPPORTED_LANGUAGES["kokoro"])
        self.assertIn("Japanese", KOKORO_LANGUAGES["j"]["name"])


if __name__ == "__main__":
    unittest.main()
