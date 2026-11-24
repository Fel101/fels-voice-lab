"""Install-free audio tools and scene WAV precision tests."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import soundfile as sf

from voice_lab import audio_tools
from voice_lab import export_panel
from tests.test_export_panel import ExportPanelBase, HAS_FFMPEG, tone_wav


class AudioToolsTests(unittest.TestCase):
    def test_master_preserves_source_and_writes_selected_precision_and_peak(self):
        raw = tone_wav(amplitude=0.9)
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            for precision in audio_tools.PRECISIONS:
                with self.subTest(precision=precision):
                    result = audio_tools.master(tmp, raw, {"gain_db": 12, "ceiling_dbtp": -1, "precision": precision})
                    folder = Path(tmp) / "out" / "audio-tools" / result["id"]
                    self.assertEqual(sf.info(folder / "audio.wav").subtype, precision)
                    self.assertEqual(result["source_sha256"], digest)
                    self.assertLessEqual(result["output_measurements"]["true_peak_dbtp"], -1)
                    self.assertEqual(json.loads((folder / "receipt.json").read_text()), result)

    def test_procedural_effect_repeats_for_the_same_variation_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            params = {"description": "light rain", "seconds": 0.5, "seed": 42}
            with self.assertRaisesRegex(ValueError, "Variation number must be a whole number"):
                audio_tools.sound_effect(tmp, {**params, "seed": 1.5})
            first = audio_tools.sound_effect(tmp, params)
            second = audio_tools.sound_effect(tmp, params)
            other = audio_tools.sound_effect(tmp, {**params, "seed": 43})
            self.assertEqual(first["output_sha256"], second["output_sha256"])
            self.assertNotEqual(first["output_sha256"], other["output_sha256"])


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg and ffprobe are needed for scene packages")
class ScenePrecisionTests(ExportPanelBase):
    def test_scene_export_verifies_24_bit_and_float(self):
        scene = self.render_selected_scene()
        for precision in ("PCM_24", "FLOAT"):
            with self.subTest(precision=precision):
                options = {
                    "scene_id": scene,
                    "name": f"test-{precision.lower().replace('_', '-')}",
                    "formats": ["wav"],
                    "wav_precision": precision,
                }
                package = export_panel.run(self.root, options)["export"]
                path = self.root / package["path"] / "scene" / "scene-mix.wav"
                self.assertEqual(sf.info(path).subtype, precision)
                self.assertTrue(export_panel.verify(self.root, package["name"])["ok"])
