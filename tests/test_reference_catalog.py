"""Reference discovery/preview coverage using owned synthetic audio, no inference."""

import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import quote
import urllib.request
import wave
from http.server import ThreadingHTTPServer

import soundfile as sf

from cast_store import CastStore
import reference_catalog as catalog
import serve
from worker_pool import read_catalog


def synthetic_wav(seconds=1):
    result = io.BytesIO()
    with wave.open(result, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"".join(struct.pack("<h", round(2000 * math.sin(2 * math.pi * 220 * i / 24_000)))
                                   for i in range(round(seconds * 24_000))))
    return result.getvalue()


class ReferenceFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-reference-catalog-")
        self.root = Path(self.temp.name).resolve()
        self.lab = self.root / "lab"
        self.selected = self.root / "selected-state"
        self.lab.mkdir()
        self.selected.mkdir()
        (self.lab / "reference").mkdir()
        (self.lab / "data/voices").mkdir(parents=True)
        self.cast = CastStore(self.selected)
        self.audio = synthetic_wav()
        self.addCleanup(self.temp.cleanup)

    def write_audio(self, relative, body=None):
        path = self.lab / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.audio if body is None else body)
        return path

    def add_cast_reference(self):
        character = self.cast.create_character("Mira", "")
        identity = self.cast.create_identity(character["id"], "qwen3-tts-1.7b-base", "Clear narrator")
        return self.cast.add_reference(identity["id"], "my-microphone.wav", self.audio,
            transcript="This is my exact reference text.", origin="Recorded by the test speaker",
            allowed_use="Local fixture testing only")


class ReferenceCatalogTests(ReferenceFixtures):
    def test_saved_voice_labels_transcript_and_rights_are_explicit(self):
        loose = self.write_audio("reference/unidentified.wav")
        designed = self.write_audio("data/voices/characters/mira.wav")
        designed.with_suffix(".json").write_text(json.dumps({
            "label": "Mira — calm narrator", "description": "Warm mid-range, measured delivery",
            "transcript": "The ferry leaves at dawn.", "origin": "Original synthetic design candidate",
            "allowed_use": "Subject to the recorded checkpoint license", "secret": "not an API field",
        }), encoding="utf-8")
        rows = catalog.list_references(self.lab, self.cast)
        self.assertEqual(len(rows), 2)
        unknown, known = {row["path"]: row for row in rows}[str(loose)], {row["path"]: row for row in rows}[str(designed)]
        self.assertEqual(unknown["rights_status"], "unknown")
        self.assertEqual(unknown["origin"], "")
        self.assertEqual(unknown["allowed_use"], "")
        self.assertEqual(known["rights_status"], "documented")
        self.assertEqual(known["display_name"], "Mira — calm narrator")
        self.assertEqual(known["description"], "Warm mid-range, measured delivery")
        self.assertEqual(known["transcript"], "The ferry leaves at dawn.")
        self.assertEqual(known["duration_seconds"], 1)
        self.assertEqual(known["sample_rate_hz"], 24000)
        self.assertEqual(known["source_label"], "Saved designed voice")
        self.assertEqual(known["audio_url"], "/api/reference-audio?path=" + quote(str(designed), safe=""))
        self.assertNotIn("secret", known)

    def test_cast_uses_selected_state_and_excludes_original_upload_duplicates(self):
        reference = self.add_cast_reference()
        processed = self.cast.get_media_path("reference", reference["id"])
        original = self.cast.get_media_path("reference_source", reference["id"])
        self.write_audio("data/cast/media/references/unselected/reference.wav")
        rows = catalog.list_references(self.lab, self.cast)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["path"], str(processed))
        self.assertNotEqual(row["path"], str(original))
        self.assertEqual(row["display_name"], "Mira / Clear narrator / v1 / my-microphone.wav")
        self.assertEqual(row["reference_version_id"], reference["id"])
        self.assertEqual(row["character_name"], "Mira")
        self.assertEqual(row["identity_label"], "Clear narrator")
        self.assertEqual(row["source_file"], "my-microphone.wav")
        self.assertEqual(row["transcript"], "This is my exact reference text.")
        self.assertEqual(row["rights_status"], "documented")
        self.assertEqual(catalog.resolve_reference_audio(str(processed), self.lab, self.cast), (processed, "audio/wav"))
        with self.assertRaises(ValueError):
            catalog.resolve_reference_audio(str(original), self.lab, self.cast)

    def test_reference_metadata_separates_design_source_from_reuse_and_marks_preferred_versions(self):
        character = self.cast.create_character("Harbor narrator", "An invented calm narrator")
        identity = self.cast.create_identity(character["id"], "qwen3-tts-1.7b-base", "Candidate narrator")
        audio, rate = sf.read(io.BytesIO(self.audio), dtype="float32")
        source = io.BytesIO()
        sf.write(source, audio, rate, format="WAV", subtype="PCM_16")
        reference = self.cast.add_reference(identity["id"], "source.wav", source.getvalue(),
            transcript="Words in the recording.", origin="Original synthetic design", allowed_use="Local fixtures only")
        generation_identity = self.cast.get_generation_identity(identity["id"], reference["id"])
        take = self.cast.create_take(generation_identity, line_id="audition", text="Words in the recording.",
            spoken_text="Words in the recording.", language="English", direction="", settings={},
            model={"source_engine_id": "qwen3-tts-1.7b-voice-design"},
            wav=audio, sample_rate=rate, generation_info={})
        self.assertEqual(take["audio_sha256"], reference["source_sha256"])
        self.cast.update_identity(identity["id"], {"status": "approved", "reference_version_id": reference["id"]})
        newer = self.cast.add_reference_version(reference["id"], {"transcript": "A corrected transcript."})
        rows = catalog.list_references(self.lab, self.cast)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["character_id"], character["id"])
            self.assertEqual(row["identity_id"], identity["id"])
            self.assertEqual(row["identity_status"], "approved")
            self.assertEqual(row["engine_id"], "qwen3-tts-1.7b-base")
            self.assertEqual(row["generation_engine_id"], "qwen3-tts-1.7b-voice-design")
            self.assertIn("VoiceDesign", row["generation_engine_name"])
            self.assertEqual(row["generation_source_take_id"], take["id"])
            self.assertEqual(row["preferred_reference_id"], reference["id"])
        by_id = {row["reference_version_id"]: row for row in rows}
        self.assertTrue(by_id[reference["id"]]["is_preferred_reference"])
        self.assertFalse(by_id[newer["id"]]["is_preferred_reference"])

    def test_source_engine_is_never_guessed_from_names(self):
        character = self.cast.create_character("Narrator Qwen Fish demo", "An ordinary saved character", ["my-voices"])
        identity = self.cast.create_identity(character["id"], "qwen3-tts-1.7b-base", "Neutral")
        reference = self.cast.add_reference(identity["id"], "fish-qwen-design.wav", self.audio,
            transcript="My words.", origin="The recording source is documented here", allowed_use="Fixture use only")
        row = catalog.list_references(self.lab, self.cast)[0]
        self.assertIsNone(row["generation_engine_id"])
        self.assertEqual(row["preferred_reference_id"], reference["id"])
        self.assertTrue(row["is_preferred_reference"])

    def test_broken_audio_invalid_sidecars_and_oversized_files_do_not_break_discovery(self):
        good = self.write_audio("reference/good.wav")
        good.with_suffix(".json").write_text("{invalid", encoding="utf-8")
        self.write_audio("reference/broken.wav", b"not audio")
        self.write_audio("reference/empty.wav", b"")
        self.write_audio("reference/large.wav", synthetic_wav(2))
        self.write_audio("reference/private.json", b'{"not": "audio"}')
        with patch.object(catalog, "MAX_AUDIO_BYTES", 60_000):
            rows = catalog.list_references(self.lab, self.cast)
        self.assertEqual([row["path"] for row in rows], [str(good)])
        self.assertEqual(rows[0]["rights_status"], "unknown")

    def test_catalogue_bounds_count_directories_and_files(self):
        for number in range(6):
            self.write_audio(f"reference/voice-{number}.wav")
        self.assertEqual(len(catalog.list_references(self.lab, self.cast, max_items=2)), 2)
        self.assertEqual(len(catalog.list_references(self.lab, self.cast, max_candidates=1)), 1)
        (self.lab / "reference/aaa-empty").mkdir()
        self.assertEqual(catalog.list_references(self.lab, self.cast, max_candidates=1), [])

    def test_preview_rejects_foreign_missing_relative_and_non_audio_paths(self):
        outside = self.root / "outside.wav"
        outside.write_bytes(self.audio)
        non_audio = self.write_audio("reference/settings.json", b"{}")
        for value in (None, "", "reference/missing.wav", str(outside), str(non_audio), str(self.lab / "reference/missing.wav")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                catalog.resolve_reference_audio(value, self.lab, self.cast)

    def test_linked_files_directories_and_sidecars_are_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.wav").write_bytes(self.audio)
        link = self.lab / "reference/linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink creation is unavailable: {exc}")
        good = self.write_audio("reference/good.wav")
        (self.lab / "reference/file-link.wav").symlink_to(outside / "private.wav")
        (outside / "metadata.json").write_text('{"origin":"untrusted","allowed_use":"untrusted"}')
        good.with_suffix(".json").symlink_to(outside / "metadata.json")
        rows = catalog.list_references(self.lab, self.cast)
        self.assertEqual([row["path"] for row in rows], [str(good)])
        self.assertEqual(rows[0]["rights_status"], "unknown")
        with self.assertRaises(ValueError):
            catalog.resolve_reference_audio(str(link / "private.wav"), self.lab, self.cast)

    @unittest.skipUnless(os.name == "nt", "Windows directory-junction policy")
    def test_windows_junctions_inside_and_outside_roots_are_rejected(self):
        original = self.write_audio("reference/actual/voice.wav")
        link = self.lab / "reference/junction"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(original.parent)],
                       check=True, capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(link.rmdir)
        rows = catalog.list_references(self.lab, self.cast)
        self.assertEqual([row["path"] for row in rows], [str(original)])
        with self.assertRaises(ValueError):
            catalog.resolve_reference_audio(str(link / "voice.wav"), self.lab, self.cast)
        other_lab = self.root / "other-lab"
        other_lab.mkdir()
        linked_root = other_lab / "reference"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(linked_root), str(original.parent)],
                       check=True, capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(linked_root.rmdir)
        self.assertEqual(catalog.list_references(other_lab, self.cast), [])

    def test_new_designs_keep_exact_text_description_and_model_restrictions(self):
        entries = {row["id"]: row for row in read_catalog()["engines"]}
        for engine, required_terms in (("breeze-tts-2", ("Non-Commercial", "self-hosted")),
                                       ("qwen3-tts-1.7b-voice-design", ("Apache-2.0", "Unknown")),
                                       ("voxcpm2", ("Apache-2.0", "Unknown"))):
            path = self.lab / "data/voices" / f"{engine}.wav"
            request = {"engine": engine, "save_voice_as": f"characters/{engine}",
                       "text": "My exact spoken words.", "voice_description": "A calm lower register."}
            metadata = catalog.save_designed_reference(path, self.audio, request, {}, entries[engine])
            for term in required_terms:
                self.assertIn(term, metadata["allowed_use"])
            self.assertEqual(metadata["checkpoint_revision"], entries[engine]["local_revision"])
            with self.assertRaises(FileExistsError):
                catalog.save_designed_reference(path, self.audio, request, {}, entries[engine])
            self.assertEqual(path.read_bytes(), self.audio)
        rows = catalog.list_references(self.lab, self.cast)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["transcript"], "My exact spoken words.")
            self.assertEqual(row["description"], "A calm lower register.")
            self.assertEqual(row["rights_status"], "documented")

    def test_saving_a_design_does_not_import_the_exporter(self):
        entries = {row["id"]: row for row in read_catalog()["engines"]}
        path = self.lab / "data/voices/no-exporter.wav"
        request = {"engine": "breeze-tts-2", "save_voice_as": "characters/no-exporter",
                   "text": "My exact spoken words.", "voice_description": "A calm lower register."}
        # The exporter pulls in the timing and lip-sync modules and their optional packages; a design save must not.
        with patch.dict(sys.modules, {"tools.scene_export": None, "fit": None, "lipsync": None}):
            metadata = catalog.save_designed_reference(path, self.audio, request, {}, entries["breeze-tts-2"])
        self.assertIn("Non-Commercial", metadata["allowed_use"])

    def test_design_metadata_collision_preserves_prior_sidecar_and_removes_only_own_new_wav(self):
        path = self.lab / "data/voices/new-character.wav"
        sidecar = path.with_suffix(".json")
        sidecar.write_text('{"label":"Keep this existing metadata"}', encoding="utf-8")
        request = {"engine": "voxcpm2", "save_voice_as": "new-character", "text": "My text."}
        with self.assertRaises(FileExistsError):
            catalog.save_designed_reference(path, self.audio, request, {}, {})
        self.assertFalse(path.exists())
        self.assertEqual(json.loads(sidecar.read_text())["label"], "Keep this existing metadata")


class ReferencePreviewApiTests(ReferenceFixtures):
    def test_http_preview_returns_real_audio_and_preserves_local_only_guard(self):
        source = self.write_audio("reference/listen.wav")
        flac = self.lab / "reference/listen.flac"
        samples, rate = sf.read(source)
        sf.write(flac, samples, rate)
        entries = {entry["id"]: entry for entry in read_catalog()["engines"]}
        engines = SimpleNamespace(entries=entries, device="cpu", loaded=[])
        with patch.object(serve, "LAB", self.lab):
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(
                engines, 0, self.cast, evaluation_store=SimpleNamespace()))
            server.RequestHandlerClass = serve.make_handler(engines, server.server_address[1], self.cast,
                                                           evaluation_store=SimpleNamespace())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/api/references", timeout=5) as response:
                    rows = json.load(response)["references"]
                self.assertEqual(len(rows), 2)
                with patch.object(serve, "discover_kokoro_voices", side_effect=[["af_heart"], ["af_heart", "am_adam"]]):
                    for expected in (["af_heart"], ["af_heart", "am_adam"]):
                        with urllib.request.urlopen(base + "/api/info", timeout=5) as response:
                            self.assertEqual(json.load(response)["voices"], expected)
                for row in rows:
                    with urllib.request.urlopen(base + row["audio_url"], timeout=5) as response:
                        self.assertEqual(response.read(), Path(row["path"]).read_bytes())
                        self.assertEqual(response.headers["Content-Type"], catalog.AUDIO_TYPES[Path(row["path"]).suffix])
                for headers in ({"Host": "foreign.invalid"}, {"Origin": "http://foreign.invalid"}):
                    request = urllib.request.Request(base + rows[0]["audio_url"], headers=headers)
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(failure.exception.code, 403)
                for query in ("", "?path=" + quote(str(self.root / "outside.wav"), safe="")):
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        urllib.request.urlopen(base + "/api/reference-audio" + query, timeout=5)
                    self.assertEqual(failure.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
