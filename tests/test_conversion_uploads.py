"""Voice conversion's files from this computer: POST /api/conversion/uploads, the lists that offer them, and the manifest of a
conversion made from them. The engine is a stand-in; the service, the decoder and the files it writes are real."""

import base64
import hashlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

import ffmpeg_tools
import serve
from cast_store import MAX_UPLOAD_BYTES, CastStore
from worker_pool import read_catalog

RATE = 24_000
FFMPEG = ffmpeg_tools.find_ffmpeg()
TERMS = {"permission": True, "origin": "My own voice, recorded for this test", "allowed_use": "Private tests only"}


def tone(seconds, *, frequency=220, format="WAV"):
    """A recording of a steady tone, as a file's bytes: WAV (16-bit) or MP3."""
    time = np.arange(round(seconds * RATE)) / RATE
    buffer = io.BytesIO()
    samples = (0.2 * np.sin(2 * np.pi * frequency * time)).astype(np.float32)
    sf.write(buffer, samples, RATE, format=format, **({"subtype": "PCM_16"} if format == "WAV" else {}))
    return buffer.getvalue()


class FakeConversionEngines:
    """The engine pool, with a voice conversion that answers at once and remembers what it was given."""

    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {row["id"]: row for row in self.catalog["engines"]}
        self.api_entries = {
            key: row for key, row in self.entries.items() if row.get("integration", {}).get("service_api") is True
        }
        self.device = "cpu"
        self.loaded = []
        self.conversions = []

    def convert(self, source, target):
        self.conversions.append((source, target))
        frames = np.arange(RATE, dtype=np.float32)
        audio = (0.1 * np.sin(2 * np.pi * 330 * frames / RATE)).astype(np.float32)
        return audio, RATE, {"engine": "chatterbox-vc", "checkpoint": "ResembleAI/chatterbox"}


class ConversionUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-conversion-uploads-")
        self.lab = Path(self.temp.name) / "lab"
        self.lab.mkdir()
        self.uploads = (self.lab / "out" / "conversions" / "uploads").resolve()
        self.engines = FakeConversionEngines()
        for name, value in (("LAB", self.lab), ("OUT", self.lab / "out")):
            patcher = patch.object(serve, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        cast = CastStore(Path(self.temp.name) / "state")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), serve.make_handler(self.engines, 0, cast, evaluation_store=SimpleNamespace())
        )
        self.server.RequestHandlerClass = serve.make_handler(
            self.engines, self.server.server_address[1], cast, evaluation_store=SimpleNamespace()
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, path, *, body=None, headers=None, status=200):
        request = urllib.request.Request(self.base + path, data=body, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                self.assertEqual(response.status, status)
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8")
            if status < 400:
                self.fail(f"{path}: unexpected HTTP {error.code}: {text}")
            self.assertEqual(error.code, status, text)
            return json.loads(text)

    def upload(self, data, filename, role, *, status=201, **terms):
        metadata = {"filename": filename, "role": role, **terms}
        encoded = base64.urlsafe_b64encode(json.dumps(metadata).encode("utf-8")).decode("ascii").rstrip("=")
        headers = {"Content-Type": "application/octet-stream", "X-Reference-Metadata": encoded}
        return self.request("/api/conversion/uploads", body=data, headers=headers, status=status)

    def convert(self, source, target, *, status=201):
        body = json.dumps({"source_path": source, "target_reference_path": target}).encode("utf-8")
        return self.request("/api/convert", body=body, headers={"Content-Type": "application/json"}, status=status)

    def listed(self, role):
        key = "targets" if role == "target" else "sources"
        return self.request(f"/api/conversion/{key}")[key]

    def kept(self):
        return sorted(path.relative_to(self.uploads).as_posix() for path in self.uploads.rglob("*") if path.is_file())

    def test_a_wav_source_is_kept_by_its_sha256_and_offered_under_its_name(self):
        data = tone(2.0)
        digest = hashlib.sha256(data).hexdigest()
        upload = self.upload(data, "My take.wav", "source")["upload"]
        path = Path(upload["path"])
        self.assertEqual(path, self.uploads / "source" / f"{digest}.wav")
        info = sf.info(path)
        self.assertEqual((info.channels, info.samplerate, round(info.duration, 2)), (1, RATE, 2.0))
        record = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(
            {key: record[key] for key in ("role", "original_name", "original_sha256", "duration_seconds", "sample_rate_hz")},
            {
                "role": "source",
                "original_name": "My take.wav",
                "original_sha256": digest,
                "duration_seconds": 2.0,
                "sample_rate_hz": RATE,
            },
        )
        self.assertTrue(record["uploaded_at"])
        self.assertNotIn("origin", record)  # a source's voice is replaced: it needs no terms
        self.assertEqual(
            {key: upload[key] for key in ("role", "original_name")}, {"role": "source", "original_name": "My take.wav"}
        )

        # The same file sent again lands in the same place, and the list offers it once, first, under its own name.
        self.assertEqual(self.upload(data, "My take.wav", "source")["upload"]["path"], str(path))
        sources = self.listed("source")
        self.assertEqual([row["path"] for row in sources].count(str(path)), 1)
        self.assertEqual(
            (sources[0]["path"], sources[0]["name"], sources[0]["original_name"]), (str(path), "My take.wav", "My take.wav")
        )
        self.assertEqual(sources[0]["duration_seconds"], 2.0)
        self.assertNotIn(str(path), [row["path"] for row in self.listed("target")])  # given as a source only

        # A name that climbs out of a folder or names a drive is a label only: the file still lands among the uploads.
        for number, name in enumerate(
            ("..\\..\\..\\outside.wav", "C:\\Windows\\System32\\outside.wav", "../../../outside.wav")
        ):
            other = tone(1.0, frequency=300 + 40 * number)
            upload = self.upload(other, name, "source")["upload"]
            self.assertEqual(Path(upload["path"]).parent, self.uploads / "source")
            self.assertEqual(upload["original_name"], "outside.wav")
        self.assertFalse(any(self.lab.parent.rglob("outside.wav")))
        self.assertEqual(len(self.kept()), 8)  # four recordings, each beside its record

        # Nothing but the recordings can be picked: not the records beside them.
        self.assertFalse([row for row in sources + self.listed("source") if not row["path"].endswith(".wav")])
        error = self.convert(str(path.with_suffix(".json")), str(path), status=400)["error"]
        self.assertEqual(error, "Source performance audio must be a local WAV, FLAC, or OGG file.")

    def test_an_mp3_target_needs_the_permission_and_keeps_its_terms(self):
        mp3 = tone(3.0, format="MP3")
        for terms in (
            {},
            {**TERMS, "permission": False},
            {**TERMS, "origin": " "},
            {**TERMS, "allowed_use": ""},
            {"origin": TERMS["origin"], "allowed_use": TERMS["allowed_use"]},
        ):
            self.assertEqual(self.upload(mp3, "voice.mp3", "target", status=400, **terms)["error"], serve.CONVERSION_PERMISSION)
        self.assertEqual(self.kept(), [])  # nothing is kept from a refused target
        upload = self.upload(mp3, "voice.mp3", "target", **TERMS)["upload"]
        path = Path(upload["path"])
        self.assertEqual(path, self.uploads / "target" / f"{hashlib.sha256(mp3).hexdigest()}.wav")
        self.assertAlmostEqual(sf.info(path).duration, 3.0, delta=0.1)
        record = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(
            {key: record[key] for key in ("role", "original_name", *TERMS)},
            {"role": "target", "original_name": "voice.mp3", **TERMS},
        )
        targets = self.listed("target")
        self.assertEqual(
            {key: targets[0][key] for key in ("path", "name", "origin", "allowed_use")},
            {"path": str(path), "name": "voice.mp3", "origin": TERMS["origin"], "allowed_use": TERMS["allowed_use"]},
        )
        self.assertNotIn(str(path), [row["path"] for row in self.listed("source")])

    @unittest.skipUnless(FFMPEG, "FFmpeg is not installed: M4A is read through it")
    def test_an_m4a_is_read_through_ffmpeg(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-m4a-") as folder:
            wav, m4a = Path(folder) / "tone.wav", Path(folder) / "phone memo.m4a"
            wav.write_bytes(tone(2.0))
            subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "96k", str(m4a)],
                check=True,
                capture_output=True,
            )
            upload = self.upload(m4a.read_bytes(), m4a.name, "source")["upload"]
        self.assertEqual(upload["original_name"], "phone memo.m4a")
        self.assertAlmostEqual(sf.info(upload["path"]).duration, 2.0, delta=0.1)

    def test_each_role_has_its_length_limit_and_the_upload_its_size_limit(self):
        trim = "Trim it, or choose a shorter one."
        self.assertEqual(
            self.upload(tone(61.0), "long.wav", "source", status=400)["error"],
            f"A source performance must be 0.5 to 60 seconds long. {trim}",
        )
        self.assertEqual(
            self.upload(tone(0.2), "short.wav", "source", status=400)["error"],
            f"A source performance must be 0.5 to 60 seconds long. {trim}",
        )
        self.assertEqual(
            self.upload(tone(11.0), "long.wav", "target", status=400, **TERMS)["error"],
            f"A target voice reference must be 0.5 to 10 seconds long. {trim}",
        )
        self.assertEqual(
            self.upload(tone(11.0, format="MP3"), "long.mp3", "target", status=400, **TERMS)["error"],
            f"A target voice reference must be 0.5 to 10 seconds long. {trim}",
        )
        self.upload(b"\0" * (MAX_UPLOAD_BYTES + 1), "huge.wav", "source", status=413)
        self.assertEqual(
            self.upload(tone(1.0), "notes.txt", "source", status=400)["error"],
            "Source performance files must be WAV, FLAC, OGG, MP3 or M4A audio.",
        )
        self.assertIn('"role" is "source" or "target"', self.upload(tone(1.0), "take.wav", "voice", status=400)["error"])
        self.assertIn("Send the file's name", self.upload(tone(1.0), "", "source", status=400)["error"])
        no_header = self.request("/api/conversion/uploads", body=tone(1.0), headers={"Content-Type": "audio/wav"}, status=400)
        self.assertEqual(
            no_header["error"], "Send the file's name and role as base64url JSON in the X-Reference-Metadata header."
        )
        self.assertEqual(self.kept(), [])

    def test_a_conversion_from_uploaded_files_records_their_names_and_the_targets_terms(self):
        source = self.upload(tone(4.0), "performance.wav", "source")["upload"]["path"]
        target = self.upload(tone(3.0, frequency=330, format="MP3"), "voice.mp3", "target", **TERMS)["upload"]["path"]
        conversion = self.convert(source, target)["conversion"]
        self.assertEqual(self.engines.conversions, [(source, target)])
        manifest = conversion["manifest"]
        self.assertEqual(json.loads(Path(conversion["manifest_path"]).read_text(encoding="utf-8")), manifest)
        reference = manifest["target_voice_reference"]
        self.assertEqual(
            {key: reference[key] for key in ("path", "original_name", "origin", "allowed_use")},
            {"path": target, "original_name": "voice.mp3", "origin": TERMS["origin"], "allowed_use": TERMS["allowed_use"]},
        )
        self.assertEqual(reference["sha256"], hashlib.sha256(Path(target).read_bytes()).hexdigest())
        self.assertEqual(
            (manifest["source_performance"]["path"], manifest["source_performance"]["original_name"]),
            (source, "performance.wav"),
        )

    def test_a_target_without_its_terms_is_neither_offered_nor_converted_to(self):
        source = self.upload(tone(4.0), "performance.wav", "source")["upload"]["path"]
        other = self.upload(tone(3.0, frequency=410), "another take.wav", "source")["upload"]["path"]
        # A file sent as a source performance never had the permission asked for.
        error = self.convert(source, other, status=400)["error"]
        self.assertTrue(error.startswith(serve.CONVERSION_PERMISSION), error)
        # Nor has a target whose record lost its terms.
        target = Path(self.upload(tone(3.0, frequency=330), "voice.wav", "target", **TERMS)["upload"]["path"])
        record = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
        target.with_suffix(".json").write_text(json.dumps({**record, "allowed_use": ""}), encoding="utf-8")
        self.assertNotIn(str(target), [row["path"] for row in self.listed("target")])
        self.assertTrue(self.convert(source, str(target), status=400)["error"].startswith(serve.CONVERSION_PERMISSION))
        target.with_suffix(".json").unlink()
        self.assertNotIn(str(target), [row["path"] for row in self.listed("target")])
        self.assertTrue(self.convert(source, str(target), status=400)["error"].startswith(serve.CONVERSION_PERMISSION))
        self.assertEqual(self.engines.conversions, [])
        # Both inputs must still be inside the Lab's folders.
        outside = Path(self.temp.name) / "outside.wav"
        outside.write_bytes(tone(2.0))
        self.assertIn("must be under voice-lab", self.convert(str(outside), str(target), status=400)["error"])


if __name__ == "__main__":
    unittest.main()
