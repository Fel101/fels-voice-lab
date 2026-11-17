"""Tests for the local Voice Slate handoff route and archive validation."""

import hashlib
import io
import json
import urllib.error
import urllib.request
import zipfile

from recorder_clip import ClipRefused
from slate_bundle import read_bundle
from tests.recorder_fixtures import sidecar_template
from tests.test_export_panel import ExportPanelBase, tone_wav


def sample_bundle(**changes):
    wav = tone_wav(seconds=8.0)
    sidecar = sidecar_template(seconds=8.0, master_seconds=8.0, sha256=hashlib.sha256(wav).hexdigest())
    sidecar.update(changes)
    return as_bundle(wav, json.dumps(sidecar, ensure_ascii=False).encode("utf-8"))


def as_bundle(wav, sidecar):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr("clip.wav", wav)
        archive.writestr("clip.json", sidecar)
    return result.getvalue()


class SlateHandoffTests(ExportPanelBase):
    def request_bundle(self, path, raw):
        request = urllib.request.Request(
            self.base + "/api/cast/slate-clip/" + path,
            data=raw,
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as response:
            return response.code, json.loads(response.read())

    def test_archive_checks_names_and_bytes(self):
        raw = sample_bundle()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            sidecar, wav = archive.read("clip.json"), archive.read("clip.wav")
        self.assertEqual(read_bundle(raw).sidecar_bytes, sidecar)
        with self.assertRaisesRegex(ClipRefused, "SHA-256"):
            read_bundle(as_bundle(wav + b"changed", sidecar))
        with self.assertRaisesRegex(ClipRefused, "consent_confirmed"):
            read_bundle(sample_bundle(consent_confirmed=False))
        evil = json.loads(sidecar)
        for invalid in ("../other.wav", "Z:\\outside.wav", "NUL.wav"):
            evil["export"]["file"] = invalid
            with self.subTest(name=invalid), self.assertRaisesRegex(ClipRefused, "portable"):
                read_bundle(as_bundle(wav, json.dumps(evil).encode("utf-8")))

    def test_import_preview_draft_confirmation_and_repeat(self):
        self.start_server()
        raw = sample_bundle(transcript_status="asr_draft")
        status, body = self.request_bundle("preview", raw)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["clip"]["transcript_status"], "asr_draft")
        self.assertEqual(len(self.cast.list_cast()), 1)
        path = "import?character=Handoff&engine_id=qwen3-tts-1.7b-base&create_character=1"
        status, body = self.request_bundle(path, raw)
        self.assertEqual(status, 400, body)
        self.assertEqual(len(self.cast.list_cast()), 1)
        status, changed = self.request_bundle(path + "&accept_draft=1&sidecar_sha256=" + "a" * 64, raw)
        self.assertEqual(status, 400, changed)
        self.assertIn("changed after preview", changed["error"])
        self.assertEqual(len(self.cast.list_cast()), 1)
        reviewed = read_bundle(raw, accept_asr_draft=True)
        status, imported = self.request_bundle(path + "&accept_draft=1&sidecar_sha256=" + reviewed.sidecar_sha256, raw)
        self.assertEqual(status, 201, imported)
        identity, reference = self.cast.get_identity(imported["identity_id"]), imported["reference_id"]
        self.assertEqual(identity["status"], "candidate")
        self.assertIsNone(identity["approved_reference_id"])
        source = self.cast.get_media_path("reference_source", reference)
        self.assertEqual(source.read_bytes(), reviewed.wav_bytes)
        self.assertEqual(source.with_name("recorder-clip.json").read_bytes(), reviewed.sidecar_bytes)
        status, again = self.request_bundle(path + "&accept_draft=1", raw)
        self.assertEqual(status, 201, again)
        self.assertEqual((again["status"], again["reference_id"]), ("already_registered", reference))
