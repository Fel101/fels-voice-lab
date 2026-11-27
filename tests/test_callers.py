"""Speaking for callers: long text in one call, voices by name (GET /api/voices), a Cast voice without a kept take, error
answers with a code, lab_cli's --play and voices, and uninstalling a model from its card (engine_uninstall.py)."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_service  # noqa: E402
from voice_lab import engine_install  # noqa: E402
from voice_lab import engine_uninstall  # noqa: E402
from voice_lab import kokoro_support  # noqa: E402
from voice_lab.lab_errors import NotInstalled  # noqa: E402
from voice_lab.output_license import DESIGN_ORIGIN  # noqa: E402
from voice_lab import recorder_clip  # noqa: E402
from voice_lab import phonon_support  # noqa: E402
from voice_lab import scene_store  # noqa: E402
from voice_lab import synthetic_label  # noqa: E402
from voice_lab import voice_names  # noqa: E402
from tools import lab_cli  # noqa: E402
from test_cast import tone_wav  # noqa: E402
from voice_lab.worker_pool import EnginePool, WorkerBusy, read_catalog  # noqa: E402
from voice_lab.worker_process import WorkerCancelled  # noqa: E402

CATALOG = {entry["id"]: entry for entry in read_catalog()["engines"]}
RATE = 24_000


def sentence(number):
    return f"This is sentence number {number}, which goes on for a little while so that the text grows long."


class Worker:
    """Answers the worker protocol in the test's process: each part is a tenth of a second whose level is the part's number."""

    def __init__(self, after_generate=None):
        self.texts = []
        self.alive = False
        self.pid = 4242
        self.after_generate = after_generate
        self.log_path = Path(tempfile.gettempdir()) / "voice-lab-callers-tests.log"

    def start(self):
        self.alive = True

    def close(self, force=False):
        self.alive = False

    def call(self, method, **params):
        if method == "load":
            return {"pid": 4242, "sample_rate": RATE}
        if method == "unload":
            return {}
        assert method == "generate", method
        self.texts.append(params["text"])
        samples = np.full(RATE // 10, len(self.texts) / 100, dtype=np.float32)
        output = Path(params["output_file"])
        output.parent.mkdir(parents=True, exist_ok=True)
        np.save(output, samples, allow_pickle=False)
        if self.after_generate:
            self.after_generate(len(self.texts))
        return {"audio_file": str(output), "sample_rate": RATE, "info": {"total_s": 0.5, "first_chunk_s": 0.1, "timing": []}}


class WorkerMessageTests(unittest.TestCase):
    def test_a_1200_character_kokoro_answer_with_its_phoneme_times_fits_in_one_message(self):
        from voice_lab import worker_protocol

        # 0.3.0 failed such a part: Kokoro's phoneme and word times for 1,180 characters ran past 64 KiB.
        timing = {"phonemes": [{"symbol": "a", "start": index * 0.025, "end": index * 0.025 + 0.025} for index in range(1400)],
                  "words": [{"word": "word", "start": index * 0.3, "end": index * 0.3 + 0.25} for index in range(240)]}  # fmt: skip
        message = worker_protocol.response("1", result={"audio_file": "x.npy", "sample_rate": RATE, "info": {"timing": timing}})
        line = worker_protocol.encode_message(message)
        self.assertGreater(len(line.encode("utf-8")), 64 * 1024)
        self.assertEqual(worker_protocol.decode_message(line)["result"]["info"]["timing"], timing)


class LongTextTests(unittest.TestCase):
    def pool(self, worker):
        pool = EnginePool("cpu", 0, worker_factory=lambda entry: worker, idle_poll_seconds=0.1)
        self.addCleanup(pool.close)
        return pool

    def test_a_5000_character_text_is_one_clip_with_its_parts_in_order(self):
        text = " ".join(sentence(number) for number in range(1, 60))[:5000].rsplit(" ", 1)[0] + "."
        self.assertGreater(len(text), 4900)
        worker = Worker()
        wav, rate, info = self.pool(worker).speak({"engine": "kokoro", "text": text})
        self.assertEqual(worker.texts, scene_store.segment_text(text))
        self.assertEqual(" ".join(worker.texts), " ".join(text.split()))
        parts = len(worker.texts)
        self.assertGreaterEqual(parts, 5)
        pause = round(RATE * 0.25)
        self.assertEqual((rate, len(wav)), (RATE, parts * RATE // 10 + (parts - 1) * pause))
        levels = [float(wav[index * (RATE // 10 + pause) + 10]) for index in range(parts)]
        self.assertEqual(levels, sorted(levels), "the parts come back in the order they were spoken")
        self.assertTrue(np.all(wav[RATE // 10 : RATE // 10 + pause] == 0), "a quiet pause between parts")
        self.assertEqual((info["parts"], info["total_s"], info["first_chunk_s"]), (parts, 0.5 * parts, 0.1))
        self.assertNotIn("timing", info, "phoneme times of separate parts would not line up")

    def test_a_short_text_is_one_call_as_before(self):
        worker = Worker()
        _wav, _rate, info = self.pool(worker).speak({"engine": "kokoro", "text": "Hello there."})
        self.assertEqual(worker.texts, ["Hello there."])
        self.assertNotIn("parts", info)
        self.assertIn("timing", info)

    def test_a_caller_that_goes_away_stops_it_between_parts(self):
        there = {"alive": True}

        def gone(count):
            there["alive"] = count < 2

        worker = Worker(after_generate=gone)
        pool = self.pool(worker)
        with pool.queued("long-1", alive=lambda: there["alive"]), self.assertRaisesRegex(WorkerCancelled, "after part 2 of"):
            pool.speak({"engine": "kokoro", "text": " ".join(sentence(number) for number in range(1, 40))})
        self.assertEqual(len(worker.texts), 2)
        self.assertEqual(pool.loaded, ["kokoro"], "a stop between parts leaves the model loaded")
        self.assertEqual(pool.queue()["running"], None)

    def test_a_cancel_between_parts_stops_it(self):
        pool = None

        def cancel(count):
            if count == 1:
                pool._queue._running.cancelled = True  # as cancel_request marks it, without ending the stand-in's process

        worker = Worker(after_generate=cancel)
        pool = self.pool(worker)
        with pool.queued("long-2"), self.assertRaises(WorkerCancelled):
            pool.speak({"engine": "kokoro", "text": " ".join(sentence(number) for number in range(1, 40))})
        self.assertEqual(len(worker.texts), 1)

    def test_too_long_and_designed_voices_are_refused_before_any_worker_starts(self):
        worker = Worker()
        pool = self.pool(worker)
        with self.assertRaisesRegex(ValueError, "Keep it to 10,000 characters"):
            pool.speak({"engine": "kokoro", "text": "x " * 5001})
        long_text = " ".join(sentence(number) for number in range(1, 20))
        for engine, request in (("voxcpm2", {}), ("qwen3-tts-1.7b-voice-design", {}), ("breeze-tts-2", {"mode": "design"})):
            with self.subTest(engine=engine), self.assertRaisesRegex(ValueError, "would change from one part to the next"):
                pool.speak({"engine": engine, "text": long_text, "voice_description": "A calm narrator.", **request})
        self.assertEqual(worker.texts, [])

    def test_which_requests_design_a_voice(self):
        from voice_lab import engine_adapters

        designs = {key for key in CATALOG if voice_names_designs(engine_adapters.get(key), {})}
        self.assertEqual(designs, {"voxcpm2", "qwen3-tts-1.7b-voice-design", "breeze-tts-2", "drama-3"})
        from voice_lab.worker_pool import designs_voice

        self.assertFalse(designs_voice(engine_adapters.get("voxcpm2"), {"mode": "clone"}))
        self.assertFalse(designs_voice(engine_adapters.get("breeze-tts-2"), {"mode": "direction"}))


def voice_names_designs(adapter, req):
    from voice_lab.worker_pool import designs_voice

    return designs_voice(adapter, req)


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.service = api_service.Service(self)
        self.engines = self.service.engines
        self.installed = {"kokoro"}
        self.engines.install_probe = lambda entry: {"installed": entry["id"] in self.installed}
        self.enterContext(mock.patch.object(kokoro_support, "discover_kokoro_voices", lambda: ["af_heart", "ef_dora"]))
        self.enterContext(mock.patch.object(phonon_support, "discover_phonon_voices", lambda: []))

    def post(self, path, body):
        status, headers, raw = self.service.request("POST", path, body)
        return status, headers, raw

    def error(self, path, body):
        status, _headers, raw = self.post(path, body)
        return status, json.loads(raw)

    def voices(self):
        return {voice["name"]: voice for voice in self.service.get_json("/api/voices")["voices"]}


class ErrorCodeTests(ServiceCase):
    def test_each_kind_of_failure_has_its_code(self):
        self.assertEqual(self.error("/api/speak", {"text": "bad"}), (400, {"error": "The text is not allowed.",
                                                                          "code": "invalid_request"}))  # fmt: skip
        self.engines.busy = True
        status, answer = self.error("/api/speak", {"text": "Hello."})
        self.assertEqual((status, answer["code"]), (409, "busy"))
        self.engines.busy = False
        with mock.patch.object(self.engines, "speak", side_effect=NotInstalled("Kokoro 82M is not installed. Open ...")):
            status, answer = self.error("/api/speak", {"text": "Hello."})
        self.assertEqual(
            (status, answer), (503, {"error": "Kokoro 82M is not installed. Open ...", "code": "model_not_installed"})
        )

    def test_a_thing_that_is_not_there_is_said_without_quotes_around_the_sentence(self):
        # A KeyError's str() is its message's repr; 0.3.0 answered every such 404 in quotes.
        status, answer = self.error("/api/speak", {"text": "Hello.", "voice": "Nobody"})
        self.assertEqual((status, answer["code"]), (404, "not_found"))
        self.assertEqual(answer["error"], "There is no voice called 'Nobody'. GET /api/voices lists the voices that can speak.")
        status, answer = self.error("/api/queue/nothing-here/cancel", {})
        self.assertEqual((status, answer["error"]), (404, "No request with that id is waiting or running."))
        status, _headers, raw = self.service.request("GET", "/api/nothing-here")
        self.assertEqual((status, json.loads(raw)["code"]), (404, "not_found"))
        status, _headers, raw = self.service.request("GET", "/api/health", headers={"Origin": "http://elsewhere.example"})
        self.assertEqual((status, json.loads(raw)), (403, {"error": "local requests only", "code": "not_local"}))

    def test_a_cancelled_request_says_so_by_its_code(self):
        with mock.patch.object(self.engines, "speak", side_effect=WorkerCancelled("Stopped after part 2 of 5.")):
            self.assertEqual(self.error("/api/speak", {"text": "Hello."}), (499, {"error": "Stopped after part 2 of 5.",
                                                                                 "code": "cancelled"}))  # fmt: skip


class VoiceListTests(ServiceCase):
    def test_cast_voices_have_their_characters_names_and_preset_voices_their_own(self):
        self.service.add_voice("Nell", "Warm")
        mira = self.service.add_voice("Mira", "Bright", approved=True)
        self.service.cast.create_identity(mira["character_id"], "kokoro", "Low", {"voice": "bf_emma"})
        ann = self.service.add_voice("Ann", "One")
        self.service.cast.create_identity(ann["character_id"], "kokoro", "Two", {"voice": "af_heart"})
        rejected = self.service.add_voice("Rex", "Gone")
        self.service.cast.update_identity(rejected["id"], {"status": "rejected"})
        voices = self.voices()
        self.assertEqual(
            list(voices),
            ["Ann (One)", "Ann (Two)", "Mira", "Mira (Low)", "Nell", "af_heart", "ef_dora"],
        )
        self.assertEqual(voices["Mira"]["aliases"], ["Mira (Bright)"])
        self.assertEqual(voices["Mira"]["licence"]["rating"], "yes")
        self.assertTrue(voices["af_heart"]["licence"]["reason"])
        nell = voices["Nell"]
        self.assertEqual(
            {key: nell[key] for key in ("kind", "character", "label", "model", "languages", "ready", "why_not", "status")},
            {"kind": "cast", "character": "Nell", "label": "Warm", "model": "kokoro", "languages": ["American English"],
             "ready": True, "why_not": None, "status": "candidate"},
        )  # fmt: skip
        self.assertEqual(voices["Mira (Low)"]["languages"], ["British English"])
        dora = voices["ef_dora"]
        self.assertEqual(
            {key: dora[key] for key in ("kind", "label", "voice_id", "model", "languages", "aliases")},
            {"kind": "preset", "label": "Dora · Spanish", "voice_id": "ef_dora", "model": "kokoro", "languages": ["Spanish"],
             "aliases": ["kokoro:ef_dora"]},
        )  # fmt: skip
        self.assertFalse(any(key.startswith("_") for voice in voices.values() for key in voice))

    def test_a_voice_whose_model_is_not_installed_says_what_to_do(self):
        self.service.add_voice("Nell", "Warm")
        self.installed.clear()
        voices = self.voices()
        self.assertFalse(voices["Nell"]["ready"])
        self.assertEqual(
            voices["Nell"]["why_not"], "Kokoro 82M is not installed. Open Models & features and press Install on its card."
        )
        status, answer = self.error("/api/speak", {"voice": "af_heart", "text": "Hello."})
        self.assertEqual((status, answer["code"]), (503, "model_not_installed"))

    def test_a_cast_name_wins_and_the_preset_voice_keeps_its_model_name(self):
        self.service.add_voice("af_heart", "Odd")
        self.assertEqual(list(self.voices()), ["af_heart", "kokoro:af_heart", "ef_dora"])
        with mock.patch.object(phonon_support, "discover_phonon_voices", lambda: [{"name": "Harper", "description": "Calm."}]):
            self.installed.add("phonon-31m-en")
            self.service.add_voice("Harper", "Mine")
            voices = self.voices()
            names = list(voices)
            self.assertEqual(voices["phonon-31m-en:Harper"]["licence"]["rating"], "unclear")
        self.assertIn("phonon-31m-en:Harper", names)
        self.assertEqual(names.count("Harper"), 1)

    def test_cloned_voice_reports_licence_after_the_recording_is_rated(self):
        cast = self.service.cast
        character = cast.create_character("Mira")
        identity = cast.create_identity(character["id"], "qwen3-tts-1.7b-base", "Recorded")
        ref = cast.add_reference(
            identity["id"],
            "mira.wav",
            tone_wav(),
            transcript="A test line.",
            origin="Synthetic test fixture",
            allowed_use="Permitted for test use",
        )
        path = f"/api/identities/{identity['id']}/update"
        before = self.voices()["Mira"]["licence"]
        self.assertEqual(before["rating"], "unclear")
        self.assertIn("has no rating", before["reason"])
        status, _ = self.error(path, {"licence_reference_id": ref["id"], "licence_rating": "invalid"})
        self.assertEqual(status, 400)
        status, reply = self.error(path, {"licence_reference_id": ref["id"], "licence_rating": "no"})
        self.assertEqual(status, 200)
        self.assertEqual(reply["identity"]["licence"], self.voices()["Mira"]["licence"])
        self.assertEqual(reply["identity"]["licence"]["rating"], "no")

    def test_each_voice_says_whether_a_recording_words_or_a_model_made_it(self):
        # A program that must not speak with a person's voice needs to know which voices were cloned from a recording.
        cast = self.service.cast
        recorded = cast.create_identity(cast.create_character("Rosa")["id"], "qwen3-tts-1.7b-base", "Recorded")
        cast.add_reference(recorded["id"], "rosa.wav", tone_wav(), transcript="A test line.",
                           origin="Recorded with Voice Slate", allowed_use="Permitted for test use")  # fmt: skip
        designed = cast.create_identity(cast.create_character("Ash")["id"], "qwen3-tts-1.7b-base", "Designed")
        design = cast.add_reference(designed["id"], "ash.wav", tone_wav(), transcript="A test line.",
                                    origin=f"{DESIGN_ORIGIN}voxcpm2; openbmb/VoxCPM2 revision abc.",
                                    allowed_use="Designed from words")  # fmt: skip
        # A take of the designed voice saved as another voice's recording is still made from words; one saved with no
        # recording it came from is treated as a person's.
        kept = cast.create_identity(cast.create_character("Kit")["id"], "qwen3-tts-1.7b-base", "From a take")
        cast.add_reference(kept["id"], "kit.wav", tone_wav(), transcript="A test line.",
                           origin="Derived from local take 1 (voxcpm2, abc).", allowed_use="Designed from words",
                           parent_reference_id=design["id"])  # fmt: skip
        lost = cast.create_identity(cast.create_character("Lee")["id"], "qwen3-tts-1.7b-base", "Unknown")
        cast.add_reference(lost["id"], "lee.wav", tone_wav(), transcript="A test line.",
                           origin="Derived from local take 2 (voxcpm2, abc).", allowed_use="Unknown")  # fmt: skip
        self.service.add_voice("Nell", "Warm")
        voices = self.voices()
        self.assertEqual(
            {name: voices[name]["made_from"] for name in ("Rosa", "Ash", "Kit", "Lee", "Nell", "af_heart")},
            {"Rosa": "recording", "Ash": "words", "Kit": "words", "Lee": "recording", "Nell": "model", "af_heart": "model"},
        )

    def test_a_voice_whose_speaker_permission_expired_is_not_ready(self):
        # The list says which voices can speak now, so a voice whose Slate release has run out is listed with the reason.
        self.installed.add("qwen3-tts-1.7b-base")
        cast = self.service.cast
        voice = cast.create_identity(cast.create_character("Rosa")["id"], "qwen3-tts-1.7b-base", "Recorded")
        recording = cast.add_reference(voice["id"], "rosa.wav", tone_wav(), transcript="A test line.",
                                       origin="Recorded with Voice Slate", allowed_use="Permitted for test use",
                                       release_expires="2027-02-22")  # fmt: skip
        with mock.patch.object(recorder_clip, "date", wraps=date) as clock:
            clock.today.return_value = date(2027, 2, 22)
            cast.update_identity(voice["id"], {"status": "approved", "reference_version_id": recording["id"]})
            self.assertEqual((self.voices()["Rosa"]["ready"], self.voices()["Rosa"]["why_not"]), (True, None))
            clock.today.return_value = date(2027, 2, 23)
            rosa = self.voices()["Rosa"]
        self.assertFalse(rosa["ready"])
        self.assertIn("permission expired on 2027-02-22", rosa["why_not"])

    def test_names_are_found_without_regard_to_case_and_unknown_ones_say_where_to_look(self):
        self.service.add_voice("Nell", "Warm")
        voices = voice_names.list_voices(self.service.cast, self.engines)
        self.assertEqual(voice_names.find(" nell ", voices)["character"], "Nell")
        self.assertEqual(voice_names.find("NELL (warm)", voices)["character"], "Nell")
        with self.assertRaisesRegex(KeyError, "GET /api/voices lists"):
            voice_names.find("Nobody", voices)


class SpeakByNameTests(ServiceCase):
    def test_speaking_as_a_cast_voice_by_name_keeps_no_take(self):
        self.service.add_voice("Nell", "Warm")
        status, headers, body = self.post("/api/speak", {"voice": "Nell", "text": "We made it home.", "direction": ""})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.engines.requests[-1]["engine"], "kokoro")
        self.assertEqual(self.engines.requests[-1]["voice"], "af_heart")
        self.assertEqual((headers["X-Cast-Character"], headers["X-Take-Kept"]), ("Nell", "false"))
        self.assertIsNone(headers.get("X-Take-Id"))
        self.assertEqual(self.service.cast.list_takes(), [])
        with sf.SoundFile(io.BytesIO(body)) as clip:
            self.assertEqual(clip.comment, synthetic_label.comment("kokoro"), "the clip is labelled as AI-generated speech")

    def test_a_preset_voice_by_name_speaks_with_its_model_in_its_own_language(self):
        status, headers, _body = self.post("/api/speak", {"voice": "ef_dora", "text": "Hola."})
        self.assertEqual(status, 200)
        self.assertEqual(
            {key: self.engines.requests[-1].get(key) for key in ("engine", "voice", "language")},
            {"engine": "kokoro", "voice": "ef_dora", "language": "Spanish"},
        )
        self.post("/api/speak", {"voice": "ef_dora", "text": "Hello.", "language": "English"})
        self.assertEqual(self.engines.requests[-1]["language"], "English")

    def test_kokoro_ids_and_named_engines_go_through_as_before(self):
        for body in ({"voice": "af_heart,am_adam", "text": "Hi."}, {"engine": "kokoro", "voice": "Nell", "text": "Hi."}):
            with self.subTest(body=body):
                status, _headers, _raw = self.post("/api/speak", body)
                self.assertEqual(status, 200)
                self.assertEqual(self.engines.requests[-1], body)

    def test_a_name_that_is_nobodys_is_a_404(self):
        status, answer = self.error("/api/speak", {"voice": "Nobody", "text": "Hi."})
        self.assertEqual((status, answer["code"]), (404, "not_found"))
        self.assertIn("GET /api/voices", answer["error"])


class PerformWithoutKeepingTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.identity = self.service.add_voice("Nell", "Warm")

    def test_a_cast_voice_by_name_with_no_take_kept(self):
        long_text = " ".join(sentence(number) for number in range(1, 60))[:5000]
        status, headers, body = self.post("/api/performance", {"voice": "Nell", "text": long_text, "keep_take": False})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.engines.requests[-1]["text"], long_text)
        self.assertEqual(headers["X-Take-Kept"], "false")
        self.assertEqual(self.service.cast.list_takes(), [])

    def test_a_kept_take_keeps_its_limits_and_its_old_shape(self):
        long_text = "word " * 400
        status, answer = self.error("/api/performance", {"voice": "Nell", "text": long_text, "line_id": "a"})
        self.assertEqual(status, 400)
        self.assertIn("keep_take false", answer["error"])
        status, answer = self.error("/api/performance", {"voice": "Nell", "text": "Hi.", "keep_take": False,
                                                         "idempotency_key": "k1"})  # fmt: skip
        self.assertEqual(status, 400)
        self.assertIn("needs keep_take true", answer["error"])
        status, headers, _body = self.post("/api/performance", {"identity_id": self.identity["id"], "text": "Hi.",
                                                                "line_id": "scene-1"})  # fmt: skip
        self.assertEqual(status, 200)
        self.assertRegex(headers["X-Take-Id"], r"^[a-f0-9]{32}$")
        self.assertEqual(len(self.service.cast.list_takes()), 1)

    def test_a_preset_voice_is_not_a_cast_voice(self):
        status, answer = self.error("/api/performance", {"voice": "af_heart", "text": "Hi.", "keep_take": False})
        self.assertEqual(status, 400)
        self.assertIn("not a Cast voice", answer["error"])

    def test_a_spanish_kokoro_voice_reads_spanish_unless_told_otherwise(self):
        dora = self.service.cast.create_character("Dora", "")
        self.service.cast.create_identity(dora["id"], "kokoro", "Calm", {"voice": "ef_dora"})
        self.post("/api/performance", {"voice": "Dora", "text": "Hola.", "keep_take": False})
        self.assertEqual(self.engines.requests[-1]["language"], "Spanish")
        self.post("/api/performance", {"voice": "Nell", "text": "Hello.", "keep_take": False})
        self.assertEqual(self.engines.requests[-1]["language"], "English")


class CliTests(ServiceCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lab_cli.main(["--url", self.service.base, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_voices_lists_the_names_to_send(self):
        self.service.add_voice("Nell", "Warm")
        self.installed.clear()
        code, out, _err = self.run_cli("voices")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("Nell  (Cast: Nell, Warm; Kokoro 82M, American English)  not ready: Kokoro 82M"))
        self.assertTrue(lines[1].startswith("af_heart  (Heart · American English; Kokoro 82M, American English)"))

    def test_play_plays_the_file_it_wrote_and_no_keep_keeps_no_take(self):
        self.service.add_voice("Nell", "Warm")
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        played = []
        with mock.patch.object(lab_cli, "play", played.append):
            code, _out, err = self.run_cli("perform", "--character", "Nell", "--text", "Hi.", "--no-keep", "--play",
                                           "--out", str(folder / "nell.wav"))  # fmt: skip
        self.assertEqual(code, 0, err)
        self.assertEqual(played, [str(folder / "nell.wav")])
        self.assertEqual(self.service.cast.list_takes(), [])
        self.assertIs(self.engines.requests[-1].get("keep_take"), None, "keep_take is the route's, not the model's")

    def test_play_uses_the_windows_sound_player_or_a_known_one_elsewhere(self):
        path = Path("clip.wav")
        fake = mock.MagicMock()
        with mock.patch.object(lab_cli.sys, "platform", "win32"), mock.patch.dict(sys.modules, {"winsound": fake}):
            lab_cli.play(path)
        fake.PlaySound.assert_called_once_with("clip.wav", fake.SND_FILENAME)
        with (
            mock.patch.object(lab_cli.sys, "platform", "linux"),
            mock.patch.object(lab_cli.shutil, "which", lambda name: "/usr/bin/aplay" if name == "aplay" else None),
            mock.patch.object(lab_cli.subprocess, "run") as run,
        ):
            lab_cli.play(path)
        run.assert_called_once_with(["aplay", "-q", "clip.wav"], check=False)
        with (
            mock.patch.object(lab_cli.sys, "platform", "linux"),
            mock.patch.object(lab_cli.shutil, "which", lambda name: None),
            self.assertRaisesRegex(lab_cli.Failed, "was written, but this computer has no player"),
        ):
            lab_cli.play(path)


def make(root, *folders):
    for folder in folders:
        path = root / folder
        path.mkdir(parents=True, exist_ok=True)
        (path / "file.bin").write_bytes(b"x" * 1000)


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.installed = set()
        (self.root / ".venv").mkdir()
        (self.root / "data" / "cast").mkdir(parents=True)
        (self.root / "data" / "cast" / "cast.sqlite3").write_bytes(b"library")

    def probe(self, entry):
        return {"installed": entry["id"] in self.installed}

    def plan(self, key):
        return engine_uninstall.plan(CATALOG, key, self.root, self.probe)

    def run_it(self, key):
        return engine_uninstall.run(CATALOG, key, self.root, self.probe)

    def test_kokoro_alone_goes_with_its_environment_and_the_library_stays(self):
        make(self.root, "hf-cache/hub/models--hexgrad--Kokoro-82M/snapshots/x", ".venv-kokoro/Scripts")
        self.installed.add("kokoro")
        plan = self.plan("kokoro")
        self.assertEqual(
            [item["path"] for item in plan["remove"]], ["hf-cache/hub/models--hexgrad--Kokoro-82M", ".venv-kokoro"]
        )
        self.assertEqual(plan["bytes"], 2000)
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["paragraphs"][0].startswith("Uninstalling Kokoro 82M deletes 2.0 KB from the Lab's folder"))
        done = self.run_it("kokoro")
        self.assertEqual((done["removed"], done["failed"], done["freed"]), (["hf-cache/hub/models--hexgrad--Kokoro-82M",
                                                                            ".venv-kokoro"], [], 2000))  # fmt: skip
        self.assertFalse((self.root / ".venv-kokoro").exists())
        self.assertEqual((self.root / "data" / "cast" / "cast.sqlite3").read_bytes(), b"library")
        self.assertTrue((self.root / ".venv").is_dir())

    def test_what_another_installed_model_uses_stays(self):
        make(self.root, "hf-cache/hub/models--ResembleAI--chatterbox-turbo", "hf-cache/hub/models--ResembleAI--chatterbox",
             ".venv-chatterbox")  # fmt: skip
        self.installed.update({"turbo", "orig", "chatterbox-vc"})
        turbo = self.plan("turbo")
        self.assertEqual([item["path"] for item in turbo["remove"]], ["hf-cache/hub/models--ResembleAI--chatterbox-turbo"])
        users = f"{CATALOG['orig']['name']}, {CATALOG['chatterbox-vc']['name']} uses it too"
        self.assertEqual(turbo["keep"], [{"path": ".venv-chatterbox", "why": users}])
        original = self.plan("orig")
        self.assertFalse(original["ok"])
        self.assertIn("another installed model uses too", original["problems"][0])
        with self.assertRaisesRegex(ValueError, "no files of its own"):
            self.run_it("orig")

    def test_drama_never_takes_the_services_own_environment(self):
        make(self.root, "out/drama3/runtime")
        self.installed.add("drama-3")
        self.assertEqual([item["path"] for item in self.plan("drama-3")["remove"]], ["out/drama3"])

    def test_voxcpm_takes_its_own_files_and_leaves_the_environment_it_borrows(self):
        make(self.root, "hf-cache/models--openbmb--VoxCPM2", "out/voxcpm2/source/src", ".venv-voxcpm", ".venv-fqwen",
             "hf-cache/hub/models--Qwen--Qwen3-TTS-12Hz-0.6B-Base")  # fmt: skip
        self.installed.update({"voxcpm2", "qwen3-tts-0.6b-base"})
        self.assertEqual([item["path"] for item in self.plan("voxcpm2")["remove"]],
                         ["hf-cache/models--openbmb--VoxCPM2", "out/voxcpm2", ".venv-voxcpm"])  # fmt: skip
        qwen = self.plan("qwen3-tts-0.6b-base")
        self.assertEqual([item["path"] for item in qwen["remove"]], ["hf-cache/hub/models--Qwen--Qwen3-TTS-12Hz-0.6B-Base"])
        self.assertEqual(qwen["keep"], [{"path": ".venv-fqwen", "why": f"{CATALOG['voxcpm2']['name']} uses it too"}])

    @unittest.skipUnless(os.name == "nt", "junctions are Windows'")
    def test_a_junction_is_removed_as_a_link_and_what_it_points_at_stays(self):
        import _winapi

        elsewhere = Path(self.enterContext(tempfile.TemporaryDirectory()))
        make(elsewhere, "venv/Scripts", "hub/models--hexgrad--Kokoro-82M")
        _winapi.CreateJunction(str(elsewhere / "venv"), str(self.root / ".venv-kokoro"))
        plan = self.plan("kokoro")
        self.assertEqual(plan["remove"], [{"path": ".venv-kokoro", "bytes": 0, "link": True}])
        self.assertIn("removed as a link", " ".join(plan["paragraphs"]))
        self.run_it("kokoro")
        self.assertFalse(os.path.lexists(self.root / ".venv-kokoro"))
        self.assertTrue((elsewhere / "venv" / "Scripts" / "file.bin").is_file())
        (self.root / "hf-cache").mkdir()
        _winapi.CreateJunction(str(elsewhere / "hub"), str(self.root / "hf-cache" / "hub"))
        plan = self.plan("kokoro")
        self.assertEqual(plan["remove"], [])
        self.assertEqual(plan["keep"][0]["path"], "hf-cache/hub/models--hexgrad--Kokoro-82M")
        self.assertIn("outside the Lab's folder", plan["keep"][0]["why"])
        self.assertTrue((elsewhere / "hub" / "models--hexgrad--Kokoro-82M" / "file.bin").is_file())

    def test_a_file_that_cannot_be_deleted_is_reported(self):
        make(self.root, ".venv-kokoro")
        self.installed.add("kokoro")
        real = engine_uninstall.remove

        def refuse(path):
            if path.name == ".venv-kokoro":
                raise PermissionError(13, "The process cannot access the file")
            return real(path)

        with mock.patch.object(engine_uninstall, "remove", refuse):
            done = self.run_it("kokoro")
        self.assertEqual(done["failed"], [{"path": ".venv-kokoro", "reason": "The process cannot access the file"}])
        self.assertEqual(done["removed"], [])


class Pool:
    def __init__(self, installed):
        self.entries = CATALOG
        self.calls = []
        self.installed = installed

    def install_probe(self, entry):
        return {"installed": entry["id"] in self.installed}

    def hold(self, reason, doing=None):
        self.calls.append(("hold", reason, doing))

    def unload_held(self):
        self.calls.append(("unload",))

    def release_hold(self):
        self.calls.append(("release",))


class UninstallingFromThePageTests(unittest.TestCase):
    def test_the_worker_is_held_and_every_model_unloaded_while_the_files_go(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        make(root, ".venv-kokoro")
        pool = Pool({"kokoro"})
        installer = engine_install.EngineInstaller(pool, root=root)
        self.assertEqual(installer.uninstall_plan("kokoro")["remove"][0]["path"], ".venv-kokoro")
        done = installer.uninstall("kokoro")
        self.assertEqual(done["removed"], [".venv-kokoro"])
        self.assertEqual(
            pool.calls, [("hold", "Kokoro 82M is being uninstalled", "uninstalling Kokoro 82M"), ("unload",), ("release",)]
        )

    def test_not_while_an_install_runs(self):
        installer = engine_install.EngineInstaller(Pool(set()), root=tempfile.gettempdir())
        installer._jobs["engine"] = {"state": "running", "name": "Phonon 31M"}
        with self.assertRaisesRegex(WorkerBusy, "Phonon 31M is being installed"):
            installer.uninstall("kokoro")

    def test_the_page_offers_it_on_an_installed_card_and_asks_first(self):
        page = (ROOT / "web/voice_lab_studio.js").read_text(encoding="utf-8")
        self.assertIn("uninstall=cardNode('button','',LabText.t('studio.uninstall'))", page)
        catalog = json.loads((ROOT / "web/locales/en.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["messages"]["studio.uninstall"], "Uninstall…")
        self.assertIn("view.uninstallButton.hidden=missing||running||row.state==='not_integrated';", page)
        self.assertIn("{dry_run:true}", page[page.index("function planUninstall") :])
        html = (ROOT / "web/serve.html").read_text(encoding="utf-8")
        self.assertIn('<dialog id="uninstallDialog"', html)


if __name__ == "__main__":
    unittest.main()
