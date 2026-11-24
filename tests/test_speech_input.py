import base64
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from voice_lab import engine_adapters
from voice_lab import live_speech
from voice_lab import speech_input
from tests import test_openai_route as fixture
from tests.test_export_panel import tone_wav
from voice_lab.worker_pool import ModelNotInstalled


class SpeechProfiles(unittest.TestCase):
    def test_openai_alias_prefers_configured_small_for_translation_and_respects_an_explicit_choice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tiny, small = root / "tiny", root / "small"
            for path in (tiny, small):
                path.mkdir()
                for name in (*speech_input.MODEL_FILES, "vocabulary.txt"):
                    (path / name).touch()
            settings = {
                "VOICE_LAB_ASR_PYTHON": sys.executable,
                "VOICE_LAB_ASR_MODEL": str(tiny),
                "VOICE_LAB_ASR_TRANSLATION_MODEL": str(small),
            }
            with mock.patch.dict("os.environ", settings), mock.patch.object(speech_input, "_worker", return_value={}) as worker:
                self.assertEqual(
                    dict(speech_input.available_models()).keys(), {speech_input.MODEL, speech_input.TRANSLATION_MODEL}
                )
                for task, requested, expected_path, expected_id in (
                    ("translate", "whisper-1", small, "faster-whisper-small"),
                    ("transcribe", "whisper-1", tiny, "faster-whisper-tiny"),
                    ("translate", "faster-whisper-tiny", tiny, "faster-whisper-tiny"),
                    ("transcribe", "faster-whisper-small", small, "faster-whisper-small"),
                ):
                    options = speech_input.settings({"model": requested}, translate=task == "translate")
                    result = speech_input.run(root, b"fixture audio", ".wav", options)
                    self.assertEqual(result["model"], expected_id)
                    self.assertEqual(worker.call_args.args[1], expected_path)
                (small / "model.bin").unlink()
                options = speech_input.settings({"model": "whisper-1"}, translate=True)
                with self.assertRaisesRegex(speech_input.InputUnavailable, "Install speech recognition"):
                    speech_input.run(root, b"fixture audio", ".wav", options)


class LiveChunks(unittest.TestCase):
    def test_twenty_millisecond_chunks_can_fill_one_turn(self):
        sessions = live_speech.Sessions(None)
        ident = sessions.create({})["id"]
        chunk = b"\0\0" * 320
        for sequence in range(1500):
            self.assertEqual(sessions.push(ident, sequence * 320, chunk)["next_offset"], (sequence + 1) * 320)
        self.assertTrue(sessions.push(ident, 0, chunk)["replayed"])
        with self.assertRaisesRegex(ValueError, "1,500 chunks"):
            sessions.push(ident, 480000, chunk)


class IntegrationRoutes(fixture.RouteCase):
    def raw(self, path, data, mime="application/octet-stream"):
        request = urllib.request.Request(self.service.base + path, data=data, headers={"Content-Type": mime})
        try:
            with urllib.request.urlopen(request, timeout=15) as answer:
                return answer.status, answer.headers, answer.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_multipart_live_replay_and_buffered_speech(self):
        audio = tone_wav(1, 16000)
        fields = b'--v\r\nContent-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n'
        form = (
            fields
            + b'--v\r\nContent-Disposition: form-data; name="file"; filename="clip.wav"\r\n\r\n'
            + audio
            + b"\r\n--v--\r\n"
        )

        def recognize(_root, raw, suffix, settings, **_options):
            self.assertTrue(raw.startswith(b"RIFF"))
            self.assertEqual(suffix, ".wav")
            return {"text": "Hello.", "segments": [], "language": "en", "task": settings["task"]}

        with mock.patch.object(speech_input, "run", side_effect=recognize) as worker:
            code, _, body = self.raw("/v1/audio/transcriptions", form, "multipart/form-data; boundary=v")
            self.assertEqual((code, json.loads(body)), (200, {"text": "Hello."}))
            self.assertEqual(self.raw("/v1/audio/translations", form, "multipart/form-data; boundary=v")[0], 200)
            self.assertEqual(worker.call_args.args[3]["task"], "translate")
            code, _, result = self.service.request("POST", "/api/live", {"language": "en"})
            self.assertEqual(code, 201)
            session = "/api/live/" + json.loads(result)["id"]
            data = b"\x01\x00" * 16000
            self.assertEqual(self.raw(session + "/audio?offset=0", data)[0], 200)
            self.assertTrue(json.loads(self.raw(session + "/audio?offset=0", data)[2])["replayed"])
            self.assertEqual(self.raw(session + "/audio?offset=1", data)[0], 400)
            self.assertEqual(self.service.request("POST", session + "/partial")[0], 200)
            self.assertEqual(self.service.request("POST", session + "/final")[0], 200)
            replay = self.service.get_json(session + "?after=1")
            self.assertEqual([row["type"] for row in replay["events"]], ["transcript.partial", "transcript.final"])
            self.assertEqual(worker.call_count, 4)
            self.assertEqual(self.service.request("POST", session + "/final")[0], 200)
            self.assertEqual(worker.call_count, 4)
            status, headers, content = self.service.request(
                "POST", session + "/speak", {"text": "A reply.", "voice": "af_heart"}
            )
            self.assertEqual((status, headers["X-Voice-Lab-Stream"]), (200, "buffered"))
            rows = [json.loads(line) for line in content.splitlines()]
            self.assertEqual(rows[0]["type"], "start")
            self.assertEqual(rows[-1]["sample_count"], 2400)
            self.assertEqual(self.service.request("POST", session + "/cancel")[0], 200)
            self.assertEqual(
                self.service.request("POST", session + "/speak", {"text": "A reply.", "voice": "af_heart"})[0], 400
            )

    def test_native_session_preserves_original_pcm_offsets_and_completion(self):
        (adapter,) = [key for key in engine_adapters.engines_where("streams") if engine_adapters.get(key).saves_designs]
        sent = []
        chunk = b"\x01\x00" * 1000

        def streaming(req, start, write):
            sent.append(req)
            start({"sample_rate": 24000})
            for sequence in range(2):
                self.assertTrue(
                    write(
                        {
                            "sequence": sequence,
                            "sample_start": sequence * 1000,
                            "sample_count": 1000,
                            "sample_rate": 24000,
                            "data": base64.b64encode(chunk).decode("ascii"),
                        }
                    )
                )
            return {"sample_count": 2000, "sample_rate": 24000, "chunk_count": 2, "engine": adapter}

        self.engines.stream_qwen = streaming
        status, _, raw = self.service.request("POST", "/api/live", {})
        self.assertEqual(status, 201)
        path = "/api/live/" + json.loads(raw)["id"] + "/speak"
        status, headers, raw = self.service.request(
            "POST", path, {"text": "A reply.", "engine": adapter, "voice_description": "A clear voice."}
        )
        self.assertEqual((status, headers["X-Voice-Lab-Stream"]), (200, "native-generator"))
        events = [json.loads(row) for row in raw.splitlines()]
        self.assertEqual([row["type"] for row in events], ["start", "audio", "audio", "complete"])
        self.assertEqual([row["sample_start"] for row in events[1:-1]], [0, 1000])
        self.assertEqual(events[-1]["sample_count"], 2000)
        self.assertEqual(sent[0]["voice_description"], "A clear voice.")

    def test_native_session_preserves_unavailable_worker_status(self):
        (adapter,) = [key for key in engine_adapters.engines_where("streams") if engine_adapters.get(key).saves_designs]
        self.engines.stream_qwen = mock.Mock(side_effect=ModelNotInstalled("fixture worker did not start"))
        _, _, created = self.service.request("POST", "/api/live", {})
        path = "/api/live/" + json.loads(created)["id"] + "/speak"
        status, _, raw = self.service.request("POST", path, {"text": "Hello", "engine": adapter})
        self.assertEqual(status, 503, raw)
        self.assertIn("fixture worker did not start", json.loads(raw)["error"])
