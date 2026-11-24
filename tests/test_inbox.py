"""The inbox (slate_inbox.py, suite contract 6): clips Voice Slate hands over wait in inbox/voice-lab/ until the person
imports or dismisses them on the Cast page.

The clip is made here by hand, from the contract's own rules for a .voiceslate file (contract 10), with the standard
library only, so that the test checks the contract and not the Lab's own way of writing one. It is put in the inbox with
fels_suite.inbox_write, as Voice Slate will. When Voice Slate's Send to Voice Lab exists, a second file that Voice Slate
itself wrote joins it."""

import hashlib
import io
import json
import math
import struct
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
import wave
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_lab import fels_suite  # noqa: E402
from voice_lab import slate_inbox  # noqa: E402
from voice_lab.slate_inbox import NotWaiting, SlateInbox  # noqa: E402
from tests.test_export_panel import ExportPanelBase  # noqa: E402

ENGINE = "qwen3-tts-1.7b-base"


def contract_clip(*, seconds=8.0, transcript_status="typed", wav_change=b"", **changes):
    """A .voiceslate file as contract 10 describes it: a ZIP of exactly clip.wav and clip.json, both stored."""
    rate = 24_000
    samples = (int(3000 * math.sin(2 * math.pi * 220 * n / rate)) for n in range(round(seconds * rate)))
    audio = io.BytesIO()
    with wave.open(audio, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"".join(struct.pack("<h", value) for value in samples))
    wav = audio.getvalue()
    sidecar = {
        "schema": "voice-recorder-clip-v2",
        "id": "c0ffee01",
        "speaker": "maya",
        "created": "2026-12-01T10:02:11Z",
        "source": "mic",
        "transcript": "The lantern swung once and the harbour went quiet.",
        "transcript_status": transcript_status,
        "origin": "Recorded by the tester at a desk",
        "allowed_use": "Tests of Voice Lab only",
        "consent_confirmed": True,
        "export": {
            "file": "maya-take-3.wav",
            "sample_rate": rate,
            "channels": 1,
            "bit_depth": 16,
            "duration_seconds": seconds,
            "sha256": hashlib.sha256(wav).hexdigest(),
        },
        # Not in contract 10, which lists what the receiver checks: the Lab also needs the clip's id and where Slate keeps
        # its master. Voice Slate writes both.
        "master": {"file": "masters/maya-take-3.raw.wav"},
    }
    sidecar.update(changes)
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("clip.wav", wav + wav_change)
        archive.writestr("clip.json", json.dumps(sidecar).encode("utf-8"))
    return result.getvalue()


class InboxCase(unittest.TestCase):
    def setUp(self):
        self.suite = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="voice-lab-inbox-")))
        self.environ = {"FELS_SUITE_DIR": str(self.suite)}
        self.folder = fels_suite.inbox_dir("voice-lab", self.environ, make=True)
        self.cleared = []
        self.inbox = SlateInbox(self.environ, discard=self.cleared.append)

    def send(self, data, name="Maya, take 3.voiceslate"):
        return fels_suite.inbox_write("voice-lab", "voice-slate", name, data, self.environ)


class InboxListTests(InboxCase):
    def test_a_sent_clip_is_listed_with_its_speaker_and_transcript(self):
        sent = self.send(contract_clip())
        (listed,) = self.inbox.waiting()
        self.assertEqual(listed["name"], sent.name)
        self.assertEqual(
            (listed["sender"], listed["sender_name"], listed["size"]), ("voice-slate", "Voice Slate", sent.stat().st_size)
        )
        self.assertEqual(listed["state"], "ready")
        self.assertEqual(listed["clip"]["speaker"], "maya")
        self.assertEqual(listed["clip"]["transcript"], "The lantern swung once and the harbour went quiet.")
        self.assertEqual(
            (listed["clip"]["origin"], listed["clip"]["allowed_use"]),
            ("Recorded by the tester at a desk", "Tests of Voice Lab only"),
        )
        self.assertEqual(self.inbox.count(), 1)

    def test_an_unchecked_transcript_is_listed_for_the_person_to_accept(self):
        self.send(contract_clip(transcript_status="asr_draft"))
        (listed,) = self.inbox.waiting()
        self.assertEqual((listed["state"], listed["clip"]["transcript_status"]), ("ready", "asr_draft"))

    def test_files_that_fail_a_check_are_listed_as_refused_with_the_reason(self):
        self.send(contract_clip(wav_change=b"\x00\x00"), "changed.voiceslate")
        self.send(contract_clip(consent_confirmed=False), "no-consent.voiceslate")
        (self.folder / "song.mp3").write_bytes(b"ID3")
        (self.folder / ".half-written.voiceslate").write_bytes(b"PK")
        listed = {item["name"].split("-voice-slate-")[-1]: item for item in self.inbox.waiting()}
        self.assertEqual(sorted(listed), ["changed.voiceslate", "no-consent.voiceslate", "song.mp3"])
        self.assertTrue(all(item["state"] == "refused" for item in listed.values()))
        self.assertEqual(listed["song.mp3"]["code"], "kind")
        self.assertEqual(listed["song.mp3"]["problem"], "Voice Lab does not take .mp3 files.")
        with mock.patch.object(slate_inbox, "MAX_BUNDLE_BYTES", 1_048_576):
            (self.folder / "big.voiceslate").write_bytes(b"x" * 2_097_152)
            big = next(item for item in self.inbox.waiting() if item["name"] == "big.voiceslate")
        self.assertEqual((big["state"], big["code"]), ("refused", "size"))
        self.assertEqual(big["problem"], "It is 2.0 MB; Voice Lab takes files up to 1 MB.")
        self.assertEqual(listed["changed.voiceslate"]["code"], "clip")
        self.assertIn("the file changed after Voice Slate kept it.", listed["changed.voiceslate"]["problem"])
        self.assertTrue(listed["no-consent.voiceslate"]["problem"].startswith("Consent_confirmed is not true"))

    def test_each_file_is_checked_once_until_it_changes(self):
        sent = self.send(contract_clip())
        with mock.patch.object(slate_inbox, "read_bundle", wraps=slate_inbox.read_bundle) as check:
            self.inbox.waiting()
            self.inbox.waiting()
            self.assertEqual(check.call_count, 1)
            sent.write_bytes(contract_clip(transcript="A line put right in Voice Slate."))
            (listed,) = self.inbox.waiting()
            self.assertEqual(check.call_count, 2)
        self.assertEqual(listed["clip"]["transcript"], "A line put right in Voice Slate.")

    def test_a_full_inbox_is_checked_over_several_listings(self):
        for number in range(3):
            self.send(contract_clip(), f"take {number}.voiceslate")
        moments = iter([0.0, 0.1, 9.0, 9.0] + [20.0] * 10)
        self.inbox._clock = lambda: next(moments)
        states = [item["state"] for item in self.inbox.waiting()]
        self.assertEqual(states, ["ready", "checking", "checking"])
        self.inbox._clock = lambda: 0.0
        self.assertEqual([item["state"] for item in self.inbox.waiting()], ["ready"] * 3)

    def test_a_dismissed_file_goes_to_the_recycle_bin_and_is_forgotten(self):
        sent = self.send(contract_clip())
        self.inbox.waiting()
        self.inbox.dismiss(sent.name)
        self.assertEqual(self.cleared, [str(sent)])
        self.assertEqual(self.inbox._checked, {})
        with self.assertRaises(NotWaiting):
            self.inbox.dismiss("20261201T100211Z-voice-slate-elsewhere.voiceslate")


class InboxRouteTests(ExportPanelBase):
    def setUp(self):
        super().setUp()
        self.environ = {"FELS_SUITE_DIR": str(self.root / "suite")}
        fels_suite.inbox_dir("voice-lab", self.environ, make=True)
        self.cleared = []
        self.start_server()
        self.lab = self.server.RequestHandlerClass.lab
        self.lab.inbox = SlateInbox(self.environ, discard=self.cleared.append)

    def send(self, data, name="Maya, take 3.voiceslate"):
        return fels_suite.inbox_write("voice-lab", "voice-slate", name, data, self.environ)

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as response:
            return response.code, json.loads(response.read())

    def route(self, name, action):
        return f"/api/inbox/{urllib.parse.quote(name, safe='')}/{action}"

    def test_a_waiting_clip_imports_as_a_candidate_with_its_consent_record_and_origin(self):
        raw = contract_clip()
        sent = self.send(raw)
        status, listing = self.call("GET", "/api/inbox")
        self.assertEqual(status, 200, listing)
        self.assertIn(ENGINE, listing["engines"])
        self.assertEqual(Path(listing["folder"]), sent.parent)
        (listed,) = listing["files"]
        request = {
            "character": "Maya",
            "engine_id": ENGINE,
            "create_character": True,
            "sidecar_sha256": listed["clip"]["sidecar_sha256"],
        }
        status, imported = self.call("POST", self.route(listed["name"], "import"), request)
        self.assertEqual(status, 201, imported)
        self.assertEqual((imported["status"], imported["cleared"]), ("registered", True))
        self.assertEqual(self.cleared, [str(sent)])
        identity = self.cast.get_identity(imported["identity_id"])
        self.assertEqual(identity["status"], "candidate")
        self.assertIsNone(identity["approved_reference_id"])
        reference = next(item for item in identity["references"] if item["id"] == imported["reference_id"])
        self.assertTrue(reference["origin"].startswith("Recorded by the tester at a desk"))
        self.assertIn("Voice Slate clip c0ffee01", reference["origin"])
        self.assertEqual(reference["allowed_use"], "Tests of Voice Lab only")
        source = self.cast.get_media_path("reference_source", imported["reference_id"])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(source.with_name("recorder-clip.json").read_bytes(), archive.read("clip.json"))
            self.assertEqual(source.read_bytes(), archive.read("clip.wav"))

    def test_an_unchecked_transcript_needs_the_persons_yes(self):
        name = self.send(contract_clip(transcript_status="asr_draft")).name
        request = {"character": "Maya", "engine_id": ENGINE, "create_character": True}
        status, refused = self.call("POST", self.route(name, "import"), request)
        self.assertEqual(status, 400, refused)
        self.assertIn("accept the draft explicitly", refused["error"])
        self.assertEqual(self.cleared, [])
        status, imported = self.call("POST", self.route(name, "import"), {**request, "accept_draft": True})
        self.assertEqual(status, 201, imported)

    def test_a_clip_that_changed_after_its_preview_is_refused(self):
        sent = self.send(contract_clip())
        (listed,) = self.call("GET", "/api/inbox")[1]["files"]
        sent.write_bytes(contract_clip(transcript="Another line altogether."))
        request = {
            "character": "Maya",
            "engine_id": ENGINE,
            "create_character": True,
            "sidecar_sha256": listed["clip"]["sidecar_sha256"],
        }
        status, refused = self.call("POST", self.route(sent.name, "import"), request)
        self.assertEqual(status, 400, refused)
        self.assertEqual(refused["error"], "The clip changed after preview. Preview it again before you import it.")
        self.assertEqual((self.cleared, len(self.cast.list_cast())), ([], 1))

    def test_dismiss_clears_any_listed_file_and_a_gone_file_is_404(self):
        (self.lab.inbox.folder() / "song.mp3").write_bytes(b"ID3")
        (listed,) = self.call("GET", "/api/inbox")[1]["files"]
        self.assertEqual((listed["state"], listed["code"]), ("refused", "kind"))
        status, refused = self.call("POST", self.route("song.mp3", "import"), {"character": "Maya", "engine_id": ENGINE})
        self.assertEqual(status, 400, refused)
        self.assertEqual(refused["error"], "Voice Lab does not take .mp3 files. Dismiss it from the list.")
        self.assertEqual(self.call("POST", self.route("song.mp3", "dismiss")), (200, {"ok": True}))
        self.assertEqual(self.cleared, [str(self.lab.inbox.folder() / "song.mp3")])
        for name in ("missing.voiceslate", "..", ".hidden.voiceslate", "..\\song.mp3"):
            with self.subTest(name=name):
                status, answer = self.call("POST", self.route(name, "dismiss"))
                self.assertEqual(status, 404, answer)

    def test_a_name_with_letters_beyond_ascii_reaches_its_file(self):
        name = self.send(contract_clip(), "Maéva, prise 2.voiceslate").name
        self.assertIn("Maéva", name)
        self.assertEqual(self.call("POST", self.route(name, "dismiss")), (200, {"ok": True}))

    def test_a_file_the_recycle_bin_refuses_stays_listed_and_the_import_says_so(self):
        name = self.send(contract_clip()).name

        def refuse(path):
            raise OSError("Windows could not move it to the Recycle Bin (code 32).")

        self.lab.inbox.discard = refuse
        request = {"character": "Maya", "engine_id": ENGINE, "create_character": True}
        status, imported = self.call("POST", self.route(name, "import"), request)
        self.assertEqual(status, 201, imported)
        self.assertFalse(imported["cleared"])
        self.assertIn("its file stays in the inbox", imported["warnings"][-1])
        status, failed = self.call("POST", self.route(name, "dismiss"))
        self.assertEqual(status, 500, failed)
        self.assertIn("Close any program that has it open", failed["error"])
        self.lab.inbox.discard = self.cleared.append
        status, again = self.call("POST", self.route(name, "import"), request)
        self.assertEqual((status, again["status"], again["cleared"]), (201, "already_registered", True))


if __name__ == "__main__":
    unittest.main()
