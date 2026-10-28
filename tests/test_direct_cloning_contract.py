"""Direct cloning HTTP workflow with synthetic audio and an in-process fake engine.

No model, worker subprocess, GPU, or shared residency lease is used. These checks
exercise the actual Cast upload, provenance, generation, discovery and preview APIs.
"""

import base64
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import quote
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import soundfile as sf

from cast_store import CastStore
import serve
from worker_pool import read_catalog


def synthetic_wav(frequency=220, seconds=1, sample_rate=24_000):
    frames = np.arange(round(seconds * sample_rate), dtype=np.float32)
    samples = (0.12 * np.sin(2 * np.pi * frequency * frames / sample_rate)).astype(np.float32)
    result = io.BytesIO()
    sf.write(result, samples, sample_rate, format="WAV", subtype="PCM_16")
    return result.getvalue()


class FakeCloningEngines:
    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {row["id"]: row for row in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"
        self.loaded = []
        self.calls = []

    def speak(self, request):
        self.calls.append(dict(request))
        frames = np.arange(12_000, dtype=np.float32)
        audio = (0.1 * np.sin(2 * np.pi * (330 + len(self.calls) * 40) * frames / 24_000)).astype(np.float32)
        entry = self.entries[request["engine"]]
        return audio, 24_000, {
            "total_s": 0.01, "backend": "synthetic-contract-engine",
            "checkpoint": entry["checkpoint"], "checkpoint_revision": entry["local_revision"],
            "mode": "clone",
        }


class DirectCloningContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-direct-cloning-")
        self.root = Path(self.temp.name)
        self.lab = self.root / "installation"
        self.lab.mkdir()
        self.cast = CastStore(self.root / "selected-state")
        self.engines = FakeCloningEngines()
        self.lab_patch = patch.object(serve, "LAB", self.lab)
        self.lab_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(
            self.engines, 0, self.cast, evaluation_store=SimpleNamespace()))
        self.server.RequestHandlerClass = serve.make_handler(
            self.engines, self.server.server_address[1], self.cast, evaluation_store=SimpleNamespace())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.lab_patch.stop()
        self.temp.cleanup()

    def request(self, path, *, body=None, headers=None, status=200):
        request = urllib.request.Request(self.base + path, data=body, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, status)
                return response.read(), response.headers
        except urllib.error.HTTPError as error:
            if status < 400:
                self.fail(f"{path}: unexpected HTTP {error.code}: {error.read().decode('utf-8')}")
            self.assertEqual(error.code, status)
            return error.read(), error.headers

    def json(self, path, value=None, *, status=200):
        body = None if value is None else json.dumps(value).encode("utf-8")
        result, _ = self.request(path, body=body, headers={"Content-Type": "application/json"}, status=status)
        return json.loads(result)

    def create_identity(self, name, engine):
        character = self.json("/api/characters", {"name": name, "description": "Owned synthetic test voice"},
                              status=201)["character"]
        identity = self.json(f"/api/characters/{character['id']}/identities", {
            "engine_id": engine, "label": name + " reference", "notes": "Candidate, not approved"},
            status=201)["identity"]
        return character, identity

    def upload(self, identity_id, *, audio=None, filename="my-voice.wav", transcript="My exact recorded words.",
               origin="Owned synthetic recording", allowed_use="Private fixture testing only", status=201):
        metadata = {"filename": filename, "transcript": transcript, "origin": origin,
                    "allowed_use": allowed_use, "edits": {"target_sample_rate": 24_000, "channel": "mono"}}
        encoded = base64.urlsafe_b64encode(json.dumps(metadata, ensure_ascii=False).encode("utf-8")).decode().rstrip("=")
        body, _ = self.request(f"/api/identities/{identity_id}/references",
            body=synthetic_wav() if audio is None else audio,
            headers={"Content-Type": "audio/wav", "X-Reference-Metadata": encoded}, status=status)
        return json.loads(body)

    def perform(self, identity, reference, *, text="These are the new words.", status=200, **extra):
        request = {"identity_id": identity["id"], "reference_version_id": reference["id"],
                   "line_id": "cloning-test", "text": text, "language": "English", "settings": {},
                   "direction": ""}
        request.update(extra)
        return self.request("/api/performance", body=json.dumps(request).encode(),
                            headers={"Content-Type": "application/json"}, status=status)

    def test_two_uploaded_voices_keep_distinct_labels_sources_provenance_and_selected_engines(self):
        examples = (
            ("Ada narrator", "drama-3", 210, "ada.wav", "I am Ada.", "Ada fixture origin", "Local auditions only"),
            ("Bruno guide", "qwen3-tts-1.7b-base", 390, "bruno.wav", "I am Bruno.", "Bruno fixture origin", "Do not redistribute"),
        )
        reference_ids = []
        for name, engine, frequency, filename, transcript, origin, allowed_use in examples:
            character, identity = self.create_identity(name, engine)
            source = synthetic_wav(frequency)
            reference = self.upload(identity["id"], audio=source, filename=filename, transcript=transcript,
                                    origin=origin, allowed_use=allowed_use)["reference"]
            reference_ids.append(reference["id"])
            self.assertEqual(reference["origin"], origin)
            self.assertEqual(reference["allowed_use"], allowed_use)
            self.assertEqual(self.cast.get_media_path("reference_source", reference["id"]).read_bytes(), source)
            body, headers = self.perform(identity, reference, text=f"A new line spoken by {name}.", engine="orig")
            self.assertTrue(body.startswith(b"RIFF"))
            take = self.json("/api/takes/" + headers["X-Take-Id"])["take"]
            call = self.engines.calls[-1]
            self.assertEqual(call["engine"], engine, "the saved identity wins over an unrelated request engine")
            self.assertEqual(call["mode"], "clone")
            self.assertEqual(call["reference_path"], str(self.cast.get_media_path("reference", reference["id"])))
            self.assertEqual(call["reference_text"], transcript)
            self.assertEqual(take["character_id"], character["id"])
            self.assertEqual(take["reference_version_id"], reference["id"])
            self.assertEqual(take["provenance"]["origin"], origin)
            self.assertEqual(take["provenance"]["allowed_use"], allowed_use)
            self.assertEqual(self.cast.get_identity(identity["id"])["status"], "candidate")
            preview, _ = self.request(reference["audio_url"])
            self.assertEqual(preview, self.cast.get_media_path("reference", reference["id"]).read_bytes())
        rows = self.json("/api/references")["references"]
        self.assertEqual(len(rows), 2, "original uploads must not appear as duplicate references")
        self.assertEqual({row["reference_version_id"] for row in rows}, set(reference_ids))
        self.assertEqual({row["character_name"] for row in rows}, {"Ada narrator", "Bruno guide"})
        self.assertEqual({row["identity_label"] for row in rows}, {"Ada narrator reference", "Bruno guide reference"})
        self.assertEqual({row["source_file"] for row in rows}, {"ada.wav", "bruno.wav"})
        self.assertEqual({row["rights_status"] for row in rows}, {"documented"})
        for row in rows:
            preview, headers = self.request(row["audio_url"])
            self.assertEqual(preview, Path(row["path"]).read_bytes())
            self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertNotEqual(rows[0]["path"], rows[1]["path"])
        self.assertNotEqual(Path(rows[0]["path"]).read_bytes(), Path(rows[1]["path"]).read_bytes())

    def test_voxcpm_can_save_audition_and_approve_a_reference_without_transcript(self):
        _, identity = self.create_identity("Transcript-free Vox", "voxcpm2")
        reference = self.upload(identity["id"], transcript="")["reference"]
        self.assertEqual(reference["transcript"], "")
        body, headers = self.perform(identity, reference, direction="Speak with quiet confidence.")
        self.assertTrue(body.startswith(b"RIFF"))
        call = self.engines.calls[-1]
        self.assertEqual(call["engine"], "voxcpm2")
        self.assertEqual(call["direction"], "Speak with quiet confidence.")
        self.assertNotIn("reference_text", call)
        take = self.json("/api/takes/" + headers["X-Take-Id"])["take"]
        self.assertEqual(take["provenance"]["reference_version_id"], reference["id"])
        approved = self.json(f"/api/identities/{identity['id']}/update", {
            "status": "approved", "reference_version_id": reference["id"]})["identity"]
        self.assertEqual(approved["approved_reference_id"], reference["id"])

    def test_chatterbox_cast_requires_a_transcript_before_generation_or_approval(self):
        for engine in ("orig", "turbo"):
            with self.subTest(engine=engine):
                _, identity = self.create_identity("Chatterbox " + engine, engine)
                source = synthetic_wav(seconds=6)
                incomplete = self.upload(identity["id"], audio=source, transcript="")["reference"]
                before = len(self.engines.calls)
                body, _ = self.perform(identity, incomplete, status=400)
                self.assertIn("transcript", json.loads(body)["error"].lower())
                self.json(f"/api/identities/{identity['id']}/update", {
                    "status": "approved", "reference_version_id": incomplete["id"]}, status=400)
                self.assertEqual(len(self.engines.calls), before)
                complete = self.upload(identity["id"], audio=source, transcript="Exact Chatterbox source words.")["reference"]
                self.perform(identity, complete)
                call = self.engines.calls[-1]
                self.assertEqual(call["engine"], engine)
                self.assertIs(call["clone"], True)
                self.assertEqual(call["reference_path"], str(self.cast.get_media_path("reference", complete["id"])))

    def test_reference_from_another_identity_is_rejected_before_any_engine_call(self):
        _, first = self.create_identity("First voice", "drama-3")
        _, second = self.create_identity("Second voice", "drama-3")
        foreign = self.upload(second["id"])["reference"]
        body, _ = self.perform(first, foreign, status=400)
        self.assertIn("identity", json.loads(body)["error"].lower())
        self.assertEqual(self.engines.calls, [])

    def test_unsupported_corrupt_empty_and_missing_rights_uploads_are_rejected_without_media(self):
        _, identity = self.create_identity("Invalid inputs", "qwen3-tts-1.7b-base")
        cases = (
            {"filename": "unknown.wma"},
            {"filename": "payload.pt"},
            {"audio": b"This is not an audio file."},
            {"audio": b""},
            {"origin": ""},
            {"allowed_use": ""},
            {"transcript": ""},
        )
        for fields in cases:
            with self.subTest(fields=list(fields)):
                self.upload(identity["id"], status=400, **fields)
        self.assertEqual(self.cast.get_identity(identity["id"])["references"], [])
        self.assertEqual(self.json("/api/references")["references"], [])
        self.assertEqual(self.engines.calls, [])
        body, _ = self.request("/api/reference-audio?path=" + quote(str(self.lab / "unknown.wav"), safe=""), status=404)
        self.assertIn("unavailable", json.loads(body)["error"])

    def test_malformed_metadata_and_unknown_identity_are_clean_http_errors(self):
        _, identity = self.create_identity("Header validation", "voxcpm2")
        for encoded in ("", "not-base64-json", base64.urlsafe_b64encode(b"[]").decode()):
            with self.subTest(encoded=encoded):
                self.request(f"/api/identities/{identity['id']}/references", body=synthetic_wav(),
                    headers={"Content-Type": "audio/wav", "X-Reference-Metadata": encoded}, status=400)
        self.upload("f" * 32, status=404)
        self.assertEqual(self.cast.get_identity(identity["id"])["references"], [])
        self.assertEqual(self.engines.calls, [])


if __name__ == "__main__":
    unittest.main()
