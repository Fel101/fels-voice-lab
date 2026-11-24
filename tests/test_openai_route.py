"""The OpenAI-style routes (openai_speech.py, route_groups/engines.py): POST /v1/audio/speech in each format, its voices and
models, its refusals in OpenAI's error shape, and waiting in line by default; against the stand-in engines."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_service  # noqa: E402
from voice_lab import ffmpeg_tools  # noqa: E402
from voice_lab import kokoro_support  # noqa: E402
from voice_lab import openai_speech  # noqa: E402
from voice_lab import phonon_support  # noqa: E402
from voice_lab import synthetic_label  # noqa: E402


class LineEngines(api_service.FakeEngines):
    """The stand-in engines, with a line for the model worker that keeps how long each request would wait, and a rate."""

    def __init__(self, rate=api_service.RATE):
        super().__init__()
        self.waits = []
        self.rate = rate

    def queued(self, request_id=None, what=None, wait_seconds=0, priority="normal", alive=None):
        self.waits.append((what, wait_seconds))
        return contextlib.nullcontext()

    def speak(self, request):
        samples, _rate, info = super().speak(request)
        if self.rate != api_service.RATE:
            samples = np.zeros(self.rate // 10, dtype=np.float32)
        return samples, self.rate, info


class RouteCase(unittest.TestCase):
    rate = api_service.RATE

    def setUp(self):
        self.engines = LineEngines(self.rate)
        self.service = api_service.Service(self, self.engines)
        self.installed = {"kokoro", "phonon-31m-en"}
        self.engines.install_probe = lambda entry: {"installed": entry["id"] in self.installed}
        self.enterContext(mock.patch.object(kokoro_support, "discover_kokoro_voices", lambda: ["af_heart", "ef_dora"]))
        phonon = [{"name": "Harper", "description": "Harper, warm"}]
        self.enterContext(mock.patch.object(phonon_support, "discover_phonon_voices", lambda: phonon))

    def speech(self, body, headers=None):
        return self.service.request("POST", "/v1/audio/speech", body, headers)

    def refused(self, body):
        status, _headers, raw = self.speech(body)
        return status, json.loads(raw)["error"]


class FormatTests(RouteCase):
    def test_wav_is_the_labelled_clip_and_keeps_nothing(self):
        status, headers, raw = self.speech({"model": "tts-1", "input": "Hello.", "voice": "af_heart", "response_format": "wav"},
                                           {"Authorization": "Bearer sk-anything"})  # fmt: skip
        self.assertEqual((status, headers["Content-Type"]), (200, "audio/wav"))
        with sf.SoundFile(io.BytesIO(raw)) as clip:
            self.assertTrue(synthetic_label.is_labelled(clip.comment))
            self.assertEqual(clip.frames, api_service.RATE // 10)
        self.assertEqual(headers["X-Audio-Seconds"], "0.10")
        request = self.engines.requests[-1]
        self.assertEqual((request["engine"], request["voice"], request["text"]), ("kokoro", "af_heart", "Hello."))
        self.assertEqual(self.service.cast.list_cast(), [])

    def test_flac_carries_the_label_and_pcm_is_raw_24_khz_16_bit(self):
        status, headers, raw = self.speech({"input": "Hello.", "voice": "af_heart", "response_format": "flac"})
        self.assertEqual((status, headers["Content-Type"], raw[:4]), (200, "audio/flac", b"fLaC"))
        with sf.SoundFile(io.BytesIO(raw)) as clip:
            self.assertTrue(synthetic_label.is_labelled(clip.comment))
        status, headers, raw = self.speech({"input": "Hello.", "voice": {"id": "af_heart"}, "response_format": "pcm"})
        self.assertEqual((status, headers["Content-Type"], len(raw)), (200, "audio/pcm", 2 * 2400))

    @unittest.skipUnless(ffmpeg_tools.status()["found"], "FFmpeg is not on this computer")
    def test_mp3_is_the_default_and_opus_and_aac_are_made_by_ffmpeg(self):
        status, headers, raw = self.speech({"input": "Hello.", "voice": "af_heart"})
        self.assertEqual((status, headers["Content-Type"]), (200, "audio/mpeg"))
        self.assertTrue(raw[:3] == b"ID3" or raw[:2] in (b"\xff\xfb", b"\xff\xf3"), raw[:4])
        self.assertIn(synthetic_label.PREFIX.encode(), raw[:4096], "the ID3 comment carries the label")
        status, headers, raw = self.speech({"input": "Hello.", "voice": "af_heart", "response_format": "opus"})
        self.assertEqual((status, headers["Content-Type"], raw[:4]), (200, "audio/ogg", b"OggS"))
        status, headers, raw = self.speech({"input": "Hello.", "voice": "af_heart", "response_format": "aac"})
        self.assertEqual((status, headers["Content-Type"], raw[0], raw[1] & 0xF0), (200, "audio/aac", 0xFF, 0xF0))

    def test_without_ffmpeg_a_format_it_makes_is_refused_before_anything_is_spoken(self):
        missing = {"found": False, "ffmpeg": None, "encoders": {}}
        with mock.patch.object(ffmpeg_tools, "status", lambda refresh=False: missing):
            status, error = self.refused({"input": "Hello.", "voice": "af_heart"})
        self.assertEqual((status, error["code"], error["type"]), (503, "ffmpeg_not_installed", "server_error"))
        self.assertEqual(
            error["message"],
            "MP3 is made with FFmpeg, which was not found on this computer. Press Install FFmpeg on System & backups, or ask "
            "for response_format wav, flac or pcm.",
        )
        no_opus = {"found": True, "ffmpeg": "ffmpeg", "encoders": {"mp3": True, "aac": True, "opus": False}}
        with mock.patch.object(ffmpeg_tools, "status", lambda refresh=False: no_opus):
            status, error = self.refused({"input": "Hello.", "voice": "af_heart", "response_format": "opus"})
        self.assertEqual((status, error["code"]), (503, "ffmpeg_not_installed"))
        self.assertIn("cannot make Opus: its encoder (libopus) is not included", error["message"])
        self.assertEqual(self.engines.requests, [])


class ResampledTests(RouteCase):
    rate = 48_000

    def test_pcm_is_24_khz_whatever_rate_the_model_speaks_at(self):
        status, _headers, raw = self.speech({"input": "Hello.", "voice": "af_heart", "response_format": "pcm"})
        self.assertEqual((status, len(raw)), (200, 2 * 2400))


class VoiceTests(RouteCase):
    def test_a_cast_voice_speaks_by_name_without_keeping_a_take(self):
        self.service.add_voice("Mira", "Warm", approved=True)
        status, _headers, raw = self.speech({"model": "tts-1", "input": "We made it home.", "voice": "Mira",
                                             "response_format": "wav"})  # fmt: skip
        self.assertEqual(status, 200)
        self.assertEqual(self.engines.requests[-1]["voice"], "af_heart")
        self.assertEqual(self.service.get_json("/api/takes")["takes"], [])

    def test_the_lists_name_the_ready_voices_and_their_models(self):
        voices = self.service.get_json("/v1/audio/voices")["voices"]
        self.assertEqual(
            voices[:2],
            [{"id": "af_heart", "name": "Heart · American English · Kokoro 82M"},
             {"id": "ef_dora", "name": "Dora · Spanish · Kokoro 82M"}],
        )  # fmt: skip
        self.assertIn({"id": "Harper", "name": "Harper, warm · Phonon 31M English"}, voices)
        models = self.service.get_json("/v1/models")
        self.assertEqual(models["object"], "list")
        self.assertEqual([row["id"] for row in models["data"]], ["kokoro", "phonon-31m-en"])
        self.assertEqual(models["data"][0], {"id": "kokoro", "object": "model", "created": 0, "owned_by": "fels-voice-lab",
                                             "name": "Kokoro 82M"})  # fmt: skip
        self.assertEqual(self.service.get_json("/v1/audio/models")["models"][0], {"id": "kokoro", "name": "Kokoro 82M"})

    def test_speed_goes_to_a_model_that_has_one_in_its_range_and_directions_only_to_voices_that_take_one(self):
        self.assertEqual(self.speech({"input": "Hi.", "voice": "af_heart", "speed": 1.5, "response_format": "wav",
                                      "instructions": "softly"})[0], 200)  # fmt: skip
        self.assertEqual(self.engines.requests[-1]["speed"], 1.5)
        self.assertNotIn("direction", self.engines.requests[-1])
        status, error = self.refused({"input": "Hi.", "voice": "af_heart", "speed": 3, "response_format": "wav"})
        self.assertEqual((status, error["message"]),
                         (400, "Kokoro 82M speaks at 0.5 to 2 times its pace. Send a speed in that range."))  # fmt: skip
        status, error = self.refused({"input": "Hi.", "voice": "Harper", "speed": 1.2, "response_format": "wav"})
        self.assertEqual(
            (status, error["message"]),
            (400, "Phonon 31M English has no speed control, so Harper speaks at its own pace. Send speed 1."),
        )


class RefusalTests(RouteCase):
    def test_refusals_say_what_to_do_in_openais_shape(self):
        status, error = self.refused({"input": "Hello.", "voice": "alloy"})
        self.assertEqual(
            (status, error["type"], error["code"], error["param"]), (404, "invalid_request_error", "not_found", None)
        )
        self.assertEqual(
            error["message"],
            "alloy is one of OpenAI's own voices, which the Lab does not have. Choose one of the Lab's: GET /v1/audio/voices "
            "lists them.",
        )
        status, error = self.refused({"input": "Hello.", "voice": "Nobody"})
        self.assertEqual(
            error["message"], "There is no voice called 'Nobody'. GET /v1/audio/voices lists the voices that can speak."
        )
        status, error = self.refused({"model": "orig", "input": "Hello.", "voice": "af_heart"})
        self.assertEqual(
            (status, error["message"]),
            (400, "af_heart is a voice of Kokoro 82M, not of Chatterbox 500M. Send model kokoro, or a name such as tts-1, which "
                  "means the voice's own model."),
        )  # fmt: skip
        for body, message in (
            ({"input": " ", "voice": "af_heart"}, "input must be the text to speak."),
            ({"input": "a" * 10_001, "voice": "af_heart"}, "input has 10,001 characters; the Lab takes up to 10,000. Send it in pieces."),
            ({"input": "Hi.", "voice": "af_heart", "response_format": "ogg"}, "response_format must be one of mp3, opus, aac, flac, wav, pcm."),
            ({"input": "Hi.", "voice": "af_heart", "stream_format": "sse"},
             "stream_format must be audio: the Lab answers with the whole file, not with events."),
            ({"input": "Hi.", "voice": "af_heart", "speed": 5}, "speed must be a number from 0.25 to 4.0."),
        ):  # fmt: skip
            with self.subTest(message=message):
                self.assertEqual(self.refused(body), (400, {"message": message, "type": "invalid_request_error", "param": None,
                                                           "code": "invalid_request"}))  # fmt: skip
        self.assertEqual(self.engines.requests, [])

    def test_a_voice_whose_model_is_not_installed_answers_503_with_its_code(self):
        self.installed = {"phonon-31m-en"}
        status, error = self.refused({"input": "Hello.", "voice": "af_heart", "response_format": "wav"})
        self.assertEqual((status, error["code"]), (503, "model_not_installed"))
        self.assertEqual(error["message"], "Kokoro 82M is not installed. Open Models & features and press Install on its card.")

    def test_another_websites_page_is_refused_here_too(self):
        status, _headers, raw = self.speech({"input": "Hello.", "voice": "af_heart"}, {"Origin": "https://example.com"})
        self.assertEqual((status, json.loads(raw)["error"]["code"]), (403, "not_local"))

    def test_an_unknown_path_under_v1_answers_in_openais_shape(self):
        status, _headers, raw = self.service.request("GET", "/v1/chat/completions")
        self.assertEqual((status, json.loads(raw)["error"]["code"]), (404, "not_found"))


class LineTests(RouteCase):
    def test_a_request_waits_in_line_by_default_and_may_say_how_long(self):
        self.speech({"input": "One.", "voice": "af_heart", "response_format": "wav"})
        self.speech({"input": "Two.", "voice": "af_heart", "response_format": "wav", "wait_seconds": 30})
        self.service.request("POST", "/api/speak", {"text": "Three."})
        self.assertEqual(self.engines.waits, [("Clip", 600), ("Clip", 30), ("Clip", 0)])


class ParseTests(unittest.TestCase):
    def test_the_request_is_read_with_openais_defaults(self):
        self.assertEqual(
            openai_speech.parse({"model": "tts-1", "input": " Hi. ", "voice": " Mira "}),
            {"text": " Hi. ", "voice": "Mira", "model": "tts-1", "format": "mp3", "speed": 1.0, "instructions": ""},
        )


if __name__ == "__main__":
    unittest.main()
