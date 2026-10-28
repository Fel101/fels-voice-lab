"""Saved-voice discovery using owned synthetic fixtures, without model inference."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import numpy as np
import soundfile as sf

from cast_store import CastStore
import reference_catalog
import serve
from worker_pool import read_catalog


TEXT = "Synthetic contract fixture only."


class VoiceLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-voice-library-")
        self.root = Path(self.temp.name)
        self.lab = self.root / "lab"
        self.selected = self.root / "selected-state"
        self.lab.mkdir()
        self.selected.mkdir()
        self.cast = CastStore(self.selected)
        self.addCleanup(self.temp.cleanup)
        samples = .12 * np.sin(np.arange(28800) * .03)
        buffer = io.BytesIO()
        sf.write(buffer, samples, 24000, format="WAV", subtype="PCM_16")
        self.audio = buffer.getvalue()
        self.samples, self.rate = sf.read(io.BytesIO(self.audio), dtype="float32")
        self.fixtures = []
        for number in range(1, 11):
            source = ("drama-3" if number <= 5 else
                "qwen3-tts-1.7b-voice-design" if number <= 7 else
                "voxcpm2" if number == 8 else "kokoro" if number == 9 else "orig")
            reuse = "qwen3-tts-1.7b-base" if source.endswith("voice-design") else source
            case_id = f"{number:02d}-synthetic-character"
            character = self.cast.create_character(f"Fixture {number:02d}",
                "Synthetic fixture, no listening or rights approval")
            options = {"voice": "bf_alice,bf_emma"} if reuse == "kokoro" else {}
            identity = self.cast.create_identity(character["id"], reuse, "Audition candidate", options)
            reference = None
            if reuse != "kokoro":
                reference = self.cast.add_reference(identity["id"], case_id + ".wav", self.audio,
                    transcript=TEXT, origin="Synthetic tone fixture", allowed_use="Tests only")
            generation = self.cast.get_generation_identity(identity["id"], reference["id"] if reference else None)
            take = self.cast.create_take(generation, line_id="audition", text=TEXT, spoken_text=TEXT,
                language="English", direction="", settings={},
                model={"engine_id":source, "source_engine_id":source,
                       "checkpoint":"synthetic-fixture", "checkpoint_revision":"fixture"},
                wav=self.samples, sample_rate=self.rate, generation_info={})
            self.fixtures.append({"character":character, "identity":identity, "reference":reference,
                                  "take":take, "source":source, "reuse":reuse, "case_id":case_id})

    def library(self):
        return reference_catalog.list_voice_library(self.lab, self.cast)

    def by_identity(self, result=None):
        return {row["identity_id"]:row for row in (result or self.library())["voices"]}

    def test_ten_mixed_candidates_include_fixed_blend_and_actual_design_source(self):
        result = self.library()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["counts"], {"voices":10, "references":9, "auditions":10})
        self.assertEqual(len({row["id"] for row in result["voices"]}), 10)
        self.assertEqual([row["character_name"] for row in result["voices"]],
                         [f"Fixture {number:02d}" for number in range(1, 11)])
        rows = self.by_identity(result)
        for fixture in self.fixtures:
            row = rows[fixture["identity"]["id"]]
            self.assertEqual(row["id"], fixture["identity"]["id"])
            self.assertEqual(row["engine_id"], fixture["reuse"])
            self.assertEqual(row["generation_engine_id"], fixture["source"])
            self.assertEqual(row["identity_status"], "candidate")
            self.assertEqual(row["audition"]["take_id"], fixture["take"]["id"])
            self.assertEqual(row["audition"]["generation_engine_id"], fixture["source"])
            self.assertEqual(row["audition"]["audio_url"], fixture["take"]["audio_url"])
            self.assertEqual(row["audition_count"], 1)
            self.assertEqual(row["character_name"], fixture["character"]["name"])
        fixed = rows[self.fixtures[8]["identity"]["id"]]
        self.assertEqual(fixed["identity_kind"], "fixed_voice")
        self.assertEqual(fixed["engine_options"]["voice"], "bf_alice,bf_emma")
        self.assertEqual(fixed["reference_count"], 0)
        self.assertEqual(fixed["reference_version_ids"], [])
        self.assertIsNone(fixed["preferred_reference_id"])
        self.assertIsNotNone(fixed["audition"])
        qwen = rows[self.fixtures[5]["identity"]["id"]]
        self.assertIn("Design", qwen["generation_engine_name"])
        self.assertIn("Base", qwen["engine_name"])

    def test_reference_preference_retains_approved_version_then_uses_newest_candidate(self):
        fixture = self.fixtures[0]
        first = fixture["reference"]
        second = self.cast.add_reference_version(first["id"], {"transcript":"New candidate text."})
        self.cast.update_identity(fixture["identity"]["id"],
            {"status":"approved", "reference_version_id":first["id"]})
        row = self.by_identity()[fixture["identity"]["id"]]
        self.assertEqual(row["reference_count"], 2)
        self.assertEqual(set(row["reference_version_ids"]), {first["id"], second["id"]})
        self.assertEqual(row["preferred_reference_id"], first["id"])
        self.cast.update_identity(fixture["identity"]["id"], {"status":"candidate"})
        row = self.by_identity()[fixture["identity"]["id"]]
        self.assertEqual(row["preferred_reference_id"], second["id"])

    def test_audition_is_the_selected_take_and_skips_a_rejected_one(self):
        fixture = self.fixtures[5]
        identity_id = fixture["identity"]["id"]
        generation = self.cast.get_generation_identity(identity_id, fixture["reference"]["id"])
        later = self.cast.create_take(generation, line_id="different-line", text="Other synthetic fixture.",
            spoken_text="Other synthetic fixture.", language="English", direction="", settings={},
            model={"engine_id":fixture["reuse"], "source_engine_id":fixture["reuse"]},
            wav=self.samples, sample_rate=self.rate, generation_info={})
        self.assertEqual(self.by_identity()[identity_id]["audition_count"], 2)
        self.cast.set_take_state(fixture["take"]["id"], status="selected")
        self.assertEqual(self.by_identity()[identity_id]["audition"]["take_id"], fixture["take"]["id"])
        self.cast.set_take_state(fixture["take"]["id"], status="rejected")
        self.assertEqual(self.by_identity()[identity_id]["audition"]["take_id"], later["id"])

    def test_missing_media_is_reported_without_hiding_identity_or_fabricating_audition(self):
        fixture = self.fixtures[0]
        self.cast.get_media_path("take", fixture["take"]["id"]).unlink()
        self.cast.get_media_path("reference", fixture["reference"]["id"]).unlink()
        row = self.by_identity()[fixture["identity"]["id"]]
        self.assertEqual(row["reference_count"], 1)
        self.assertEqual(row["available_reference_count"], 0)
        self.assertEqual(row["preferred_reference_id"], fixture["reference"]["id"])
        self.assertFalse(row["preferred_reference_available"])
        self.assertIsNone(row["audition"])
        self.assertEqual(row["audition_count"], 0)

    def test_discovery_preserves_database_media_and_unrelated_voice_files(self):
        loose = self.lab / "reference/unregistered.wav"
        loose.parent.mkdir()
        loose.write_bytes(self.audio)
        def snapshot():
            return {str(path.relative_to(self.root)):hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self.root.rglob("*") if path.is_file()}
        before = snapshot()
        first = self.library()
        second = self.library()
        self.assertEqual(first, second)
        self.assertEqual(first["counts"]["voices"], 10)
        self.assertEqual(first["counts"]["auditions"], 10)
        self.assertEqual(snapshot(), before)

    def test_http_selected_state_has_all_ten_playable_auditions_and_local_only_guard(self):
        other = CastStore(self.lab)
        unrelated = other.create_character("Another selected-state fixture")
        other.create_identity(unrelated["id"], "kokoro", "Other voice", {"voice":"af_heart"})
        entries = {entry["id"]:entry for entry in read_catalog()["engines"]}
        engines = SimpleNamespace(entries=entries, device="cpu", loaded=[])
        server = serve.LocalThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        with patch.object(serve, "LAB", self.lab):
            server.RequestHandlerClass = serve.make_handler(engines, server.server_address[1], self.cast,
                scene_store=SimpleNamespace(root=self.selected), batch_runner=SimpleNamespace(),
                evaluation_store=SimpleNamespace())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/api/voice-library", timeout=5) as response:
                    result = json.load(response)
                self.assertEqual(result["counts"]["voices"], 10)
                self.assertNotIn("Another selected-state fixture", [row["character_name"] for row in result["voices"]])
                for row in result["voices"]:
                    self.assertEqual(row["id"], row["identity_id"])
                    with urllib.request.urlopen(base + row["audition"]["audio_url"], timeout=5) as response:
                        self.assertEqual(response.headers.get_content_type(), "audio/wav")
                        self.assertEqual(response.read(), self.audio)
                for headers in ({"Host":"example.invalid"}, {"Origin":"https://example.invalid"}, {"Origin":"null"}):
                    request = urllib.request.Request(base + "/api/voice-library", headers=headers)
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(caught.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)


if __name__ == "__main__":
    unittest.main()
