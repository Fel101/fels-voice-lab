"""The command-line tool: what it sends, what it writes, what it prints and the exit status it ends with. It talks to the real
handler on a temporary library with fake engines."""
import ast
import contextlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_service
from tools import lab_cli

CLI = ROOT / "tools" / "lab_cli.py"


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


DOCUMENTED_DEFAULT_URL = lab_cli.DEFAULT_URL


class CliCase(unittest.TestCase):
    def setUp(self):
        # A real Lab may be listening on the default address of the computer these tests run on, and the user may have set
        # VOICE_LAB_URL. A test that forgot to name its service must fail, not reach somebody's real one.
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("VOICE_LAB_URL", None)
        default = mock.patch.object(lab_cli, "DEFAULT_URL", f"http://127.0.0.1:{unused_port()}")
        default.start()
        self.addCleanup(default.stop)
        self.service = api_service.Service(self)
        self.engines = self.service.engines
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-cli-", ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def run_cli(self, *argv, url=True, stdin=None):
        """(exit status, what it printed, what it printed to standard error)."""
        args = (["--url", self.service.base] if url else []) + list(argv)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is None:
                code = lab_cli.main(args)
            else:
                with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                    code = lab_cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def wav_frames(self, path):
        data, rate = sf.read(path)
        return len(data), rate


class HealthTests(CliCase):
    def test_it_says_the_version_and_whether_the_service_is_ready(self):
        code, out, err = self.run_cli("health")
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"^Voice Lab \d+\.\d+\.\d+ is ready at http://127\.0\.0\.1:\d+ \(device: cpu\)\n$")

    def test_json_prints_what_the_service_said(self):
        code, out, _err = self.run_cli("health", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.service.get_json("/api/health"))

    def test_nothing_answering_is_exit_status_3(self):
        url = f"http://127.0.0.1:{unused_port()}"
        code, out, err = self.run_cli("--url", url, "health", url=False)
        self.assertEqual((code, out), (3, ""))
        self.assertTrue(err.startswith(f"error: no answer from {url}"), err)
        self.assertIn("Is the service running?", err)

    def test_wait_keeps_trying_until_the_service_has_started(self):
        port = unused_port()
        started = []
        thread = threading.Thread(target=lambda: (time.sleep(1.5), started.append(api_service.Service(self, port=port))))
        thread.start()
        self.addCleanup(thread.join)
        code, out, _err = self.run_cli("--url", f"http://127.0.0.1:{port}", "health", "--wait", "30", url=False)
        self.assertEqual(code, 0)
        self.assertIn("is ready", out)

    def run_health_on_a_clock(self, *argv, answer_on=None):
        """Run `health` with the tool's own clock replaced by one that only moves when it sleeps, and with the service
        down until try number answer_on. Real time would not tell these apart: on Windows one refused connection takes about
        two seconds by itself. Returns (exit status, what it printed, the clock's reading at each try)."""
        clock, tries = [0.0], []

        def get_json(base, path, timeout):
            tries.append(clock[0])
            if answer_on is None or len(tries) < answer_on:
                raise lab_cli.NoAnswer("down")
            return {"status": "ready", "version": "1.2.3", "device": "cpu"}

        def sleep(seconds):
            clock[0] += seconds

        fake_time = types.SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
        with mock.patch.object(lab_cli, "get_json", get_json), mock.patch.object(lab_cli, "time", fake_time):
            code, out, _err = self.run_cli(*argv)
        return code, out, tries

    def test_wait_tries_once_a_second_until_the_time_is_spent_and_then_gives_up(self):
        code, _out, tries = self.run_health_on_a_clock("health", "--wait", "2.5")
        self.assertEqual((code, tries), (3, [0.0, 1.0, 2.0, 3.0]))
        code, _out, tries = self.run_health_on_a_clock("health", "--wait", "1")
        self.assertEqual((code, tries), (3, [0.0, 1.0]))

    def test_without_wait_it_tries_once(self):
        code, _out, tries = self.run_health_on_a_clock("health")
        self.assertEqual((code, tries), (3, [0.0]))

    def test_wait_stops_as_soon_as_the_service_answers(self):
        code, out, tries = self.run_health_on_a_clock("health", "--wait", "30", answer_on=3)
        self.assertEqual((code, tries), (0, [0.0, 1.0, 2.0]))
        self.assertIn("Voice Lab 1.2.3 is ready", out)

    def test_the_service_being_behind_a_proxy_setting_does_not_matter(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}):
            self.assertEqual(self.run_cli("health")[0], 0)


class SpeakTests(CliCase):
    def test_it_writes_the_wav_and_says_what_it_wrote(self):
        out_file = self.folder / "hello.wav"
        code, out, err = self.run_cli("speak", "--text", "Hello there.", "--out", str(out_file))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.wav_frames(out_file), (api_service.RATE // 10, api_service.RATE))
        self.assertEqual(out, f"Wrote {out_file}: 0.10 s of audio, made in 0.10 s\n")
        self.assertEqual(self.engines.requests, [{"text": "Hello there."}], "nothing is sent that was not asked for")
        self.assertEqual(list(self.folder.iterdir()), [out_file], "no temporary file is left behind")

    def test_the_flags_and_set_pairs_become_fields_of_the_request(self):
        code, _out, _err = self.run_cli("speak", "--text", "Hello there.", "--out", str(self.folder / "a.wav"),
                                        "--engine", "kokoro", "--voice", "af_heart,bm_george", "--speed", "1.2",
                                        "--language", "English", "--set", "seed=7", "--set", "xvec_only=true",
                                        "--set", "mode=design", "--set", 'extra={"a": 1}')
        self.assertEqual(code, 0)
        self.assertEqual(self.engines.requests[-1], {
            "text": "Hello there.", "engine": "kokoro", "voice": "af_heart,bm_george", "speed": 1.2, "language": "English",
            "seed": 7, "xvec_only": True, "mode": "design", "extra": {"a": 1}})

    def test_the_text_can_come_from_a_file_or_from_standard_input(self):
        text_file = self.folder / "line.txt"
        text_file.write_text("﻿A line from a file.\n", encoding="utf-8")
        self.assertEqual(self.run_cli("speak", "--text-file", str(text_file), "--out", str(self.folder / "a.wav"))[0], 0)
        self.assertEqual(self.engines.requests[-1]["text"], "A line from a file.")
        self.assertEqual(self.run_cli("speak", "--text-file", "-", "--out", str(self.folder / "b.wav"),
                                      stdin="From the pipe.\n")[0], 0)
        self.assertEqual(self.engines.requests[-1]["text"], "From the pipe.")

    def test_a_request_file_is_the_base_and_the_flags_win(self):
        request = self.folder / "request.json"
        request.write_text(json.dumps({"text": "From the file.", "engine": "kokoro", "seed": 3}), encoding="utf-8")
        self.assertEqual(self.run_cli("speak", "--request-file", str(request), "--text", "From the flag.",
                                      "--out", str(self.folder / "a.wav"))[0], 0)
        self.assertEqual(self.engines.requests[-1], {"text": "From the flag.", "engine": "kokoro", "seed": 3})

    def test_json_prints_one_object(self):
        out_file = self.folder / "a.wav"
        code, out, _err = self.run_cli("speak", "--text", "Hello there.", "--out", str(out_file), "--json")
        result = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual((result["file"], result["bytes"], result["audio_seconds"], result["gen_seconds"]),
                         (str(out_file), out_file.stat().st_size, 0.1, 0.1))

    def test_a_folder_that_does_not_exist_is_made_and_a_file_that_does_is_replaced(self):
        out_file = self.folder / "new" / "deeper" / "a.wav"
        self.assertEqual(self.run_cli("speak", "--text", "One.", "--out", str(out_file))[0], 0)
        out_file.write_bytes(b"old")
        self.assertEqual(self.run_cli("speak", "--text", "Two.", "--out", str(out_file))[0], 0)
        self.assertEqual(out_file.read_bytes()[:4], b"RIFF")

    def test_no_text_is_a_mistake_in_the_command_line(self):
        code, out, err = self.run_cli("speak", "--out", str(self.folder / "a.wav"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("give --text", err)
        self.assertEqual(self.engines.requests, [])

    def test_a_refusal_is_exit_status_1_with_the_services_own_message_and_no_file(self):
        out_file = self.folder / "a.wav"
        code, out, err = self.run_cli("speak", "--text", "bad", "--out", str(out_file))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: the service answered 400: The text is not allowed.\n")
        self.assertFalse(out_file.exists())
        self.engines.busy = True
        code, _out, err = self.run_cli("speak", "--text", "Hello there.", "--out", str(out_file))
        self.assertEqual(code, 1)
        self.assertIn("answered 409", err)

    def test_an_answer_that_is_not_a_wav_file_writes_nothing(self):
        out_file = self.folder / "a.wav"
        # Neither marker alone is enough: a WAV file starts RIFF, four bytes of length, then WAVE.
        for answer in (b"<html>not audio</html>", b"RIFF\x00\x00\x00\x00AVI ", b"JUNK\x00\x00\x00\x00WAVE", b"RIFF", b""):
            with self.subTest(answer), mock.patch.object(lab_cli, "call", return_value=({}, answer)):
                code, _out, err = self.run_cli("speak", "--text", "Hello there.", "--out", str(out_file))
                self.assertEqual(code, 1)
                self.assertIn("not a WAV file", err)
                self.assertEqual(list(self.folder.iterdir()), [])
        with mock.patch.object(lab_cli, "call", return_value=({}, b"RIFF\x00\x00\x00\x00WAVE")):
            self.assertEqual(self.run_cli("speak", "--text", "Hello there.", "--out", str(out_file))[0], 0)

    def test_a_write_that_fails_keeps_the_old_file_and_leaves_no_partial_one(self):
        out_file = self.folder / "a.wav"
        out_file.write_bytes(b"old")
        with mock.patch.object(lab_cli.os, "replace", side_effect=OSError(28, "No space left on device")):
            code, _out, err = self.run_cli("speak", "--text", "Hello there.", "--out", str(out_file))
        self.assertEqual(code, 1)
        self.assertIn("cannot write", err)
        self.assertIn("No space left on device", err)
        self.assertEqual(out_file.read_bytes(), b"old")
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["a.wav"])

    def test_a_service_that_does_not_answer_in_time_is_exit_status_3(self):
        self.engines.gate = threading.Event()
        self.addCleanup(self.engines.gate.set)
        code, _out, err = self.run_cli("--timeout", "0.5", "speak", "--text", "Hello there.", "--out", str(self.folder / "a.wav"))
        self.assertEqual(code, 3)
        self.assertIn("within 0.5 seconds", err)

    def test_nothing_answering_is_exit_status_3_and_writes_nothing(self):
        out_file = self.folder / "a.wav"
        code, _out, err = self.run_cli("--url", f"http://127.0.0.1:{unused_port()}", "speak", "--text", "Hello.",
                                       "--out", str(out_file), url=False)
        self.assertEqual(code, 3)
        self.assertIn("no answer from", err)
        self.assertFalse(out_file.exists())

    def test_a_pair_that_is_not_key_value_is_a_mistake(self):
        code, _out, err = self.run_cli("speak", "--text", "Hello.", "--out", str(self.folder / "a.wav"), "--set", "seed")
        self.assertEqual(code, 2)
        self.assertIn("not KEY=VALUE", err)

    def test_an_answer_that_stops_part_way_is_reported_as_cut_short(self):
        class CutShort:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self):
                raise http.client.IncompleteRead(b"RIFF", 996)

        out_file = self.folder / "a.wav"
        with mock.patch.object(urllib.request.OpenerDirector, "open", return_value=CutShort()):
            code, _out, err = self.run_cli("speak", "--text", "Hello.", "--out", str(out_file))
        self.assertEqual(code, 3)
        self.assertIn("ended before the answer was complete", err)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_connection_that_ends_mid_answer_is_exit_status_3_and_writes_nothing(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)

            def answer_part_of_it():
                connection, _address = server.accept()
                with connection:
                    connection.recv(65536)
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nContent-Length: 1000\r\n\r\nRIFF")

            thread = threading.Thread(target=answer_part_of_it)
            thread.start()
            self.addCleanup(thread.join)
            out_file = self.folder / "a.wav"
            code, _out, err = self.run_cli("--url", f"http://127.0.0.1:{server.getsockname()[1]}", "speak", "--text", "Hello.",
                                           "--out", str(out_file), url=False)
        self.assertEqual(code, 3)
        self.assertTrue(err.startswith("error: "), err)
        self.assertEqual(list(self.folder.iterdir()), [], "no file, and no partial one")

    def test_a_request_file_that_cannot_be_used_is_a_mistake(self):
        for name, content in (("list.json", "[1, 2]"), ("broken.json", "{not json"), ("text.json", '"x"')):
            path = self.folder / name
            path.write_text(content, encoding="utf-8")
            with self.subTest(name):
                code, _out, err = self.run_cli("speak", "--request-file", str(path), "--out", str(self.folder / "a.wav"))
                self.assertEqual(code, 2)
                self.assertIn("request file", err)
        code, _out, err = self.run_cli("speak", "--request-file", str(self.folder / "missing.json"),
                                       "--out", str(self.folder / "a.wav"))
        self.assertEqual(code, 2)
        self.assertIn("cannot read the request file", err)
        self.assertEqual(self.engines.requests, [])

    def test_a_request_file_alone_is_enough_and_may_start_with_a_byte_order_mark(self):
        request = self.folder / "request.json"
        request.write_text("﻿" + json.dumps({"text": "From the file.", "seed": 3}), encoding="utf-8")   # Windows PowerShell 5.1 writes one
        self.assertEqual(self.run_cli("speak", "--request-file", str(request), "--out", str(self.folder / "a.wav"))[0], 0)
        self.assertEqual(self.engines.requests[-1], {"text": "From the file.", "seed": 3})

    def test_no_output_file_is_a_mistake_in_the_command_line(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            lab_cli.main(["--url", self.service.base, "speak", "--text", "Hello."])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.engines.requests, [])


class PerformTests(CliCase):
    def setUp(self):
        super().setUp()
        self.identity = self.service.add_voice("Nell", "Warm")

    def perform(self, *more, **kwargs):
        return self.run_cli("perform", "--text", "We made it home.", "--out", str(self.folder / "nell.wav"), *more, **kwargs)

    def test_a_character_by_name_is_spoken_and_the_take_is_named(self):
        code, out, err = self.perform("--character", "nell", "--line-id", "scene1-001")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.wav_frames(self.folder / "nell.wav"), (api_service.RATE // 10, api_service.RATE))
        take = self.service.get_json("/api/takes")["takes"][0]
        self.assertEqual(out, f"Wrote {self.folder / 'nell.wav'}: 0.10 s of audio, as Nell (Warm), take {take['id']}, "
                              "made in 0.10 s\n")
        self.assertEqual((take["line_id"], take["identity_id"]), ("scene1-001", self.identity["id"]))

    def test_the_line_label_defaults_to_cli(self):
        self.assertEqual(self.perform("--character", "Nell")[0], 0)
        self.assertEqual(self.service.get_json("/api/takes")["takes"][0]["line_id"], "cli")

    def test_a_voice_by_id_needs_no_lookup(self):
        code, _out, _err = self.perform("--identity-id", self.identity["id"], "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.engines.requests[-1]["text"], "We made it home.")

    def test_json_names_the_take_and_the_voice(self):
        code, out, _err = self.perform("--character", "Nell", "--json")
        result = json.loads(out)
        take = self.service.get_json("/api/takes")["takes"][0]
        self.assertEqual(code, 0)
        self.assertEqual((result["take_id"], result["character"], result["voice"], result["mode"], result["line_id"]),
                         (take["id"], "Nell", "Warm", "fixed_voice", "cli"))
        self.assertNotIn("replay", result)

    def test_a_character_with_several_voices_needs_one_chosen(self):
        character = self.service.cast.get_character(self.identity["character_id"])
        self.service.add_voice(character, "Stern")
        code, out, err = self.perform("--character", "Nell")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Stern (kokoro, candidate); Warm (kokoro, candidate)", err)
        self.assertIn("--voice LABEL", err)
        self.assertEqual(self.engines.requests, [])
        self.assertEqual(self.perform("--character", "Nell", "--voice", "stern")[0], 0)
        self.assertEqual(self.service.get_json("/api/takes")["takes"][0]["identity_label"], "Stern")

    def test_the_one_approved_voice_is_the_default(self):
        character = self.service.cast.get_character(self.identity["character_id"])
        approved = self.service.add_voice(character, "Stern", approved=True)
        self.assertEqual(self.perform("--character", "Nell")[0], 0)
        self.assertEqual(self.service.get_json("/api/takes")["takes"][0]["identity_id"], approved["id"])

    def test_an_unknown_voice_label_lists_the_ones_there_are(self):
        code, _out, err = self.perform("--character", "Nell", "--voice", "Missing")
        self.assertEqual(code, 2)
        self.assertIn("Warm", err)

    def test_an_unknown_character_lists_the_known_ones(self):
        self.service.add_voice("Brann", "Gruff")
        code, _out, err = self.perform("--character", "Nobody")
        self.assertEqual(code, 2)
        self.assertIn("no character called 'Nobody'", err)
        self.assertIn("Brann, Nell", err)

    def test_a_retired_character_is_not_found(self):
        self.service.cast.retire("character", self.identity["character_id"])
        code, _out, err = self.perform("--character", "Nell")
        self.assertEqual(code, 2)
        self.assertIn("no character called 'Nell'", err)

    def test_the_voice_must_be_named_one_way_or_the_other(self):
        self.assertEqual(self.perform()[0], 2)
        self.assertEqual(self.perform("--character", "Nell", "--identity-id", self.identity["id"])[0], 2)
        self.assertEqual(self.engines.requests, [])

    def test_a_name_with_accents_is_shown_as_it_is_written(self):
        self.service.add_voice("Zoë", "Chaleureuse")
        code, out, _err = self.perform("--character", "Zoë")
        self.assertEqual(code, 0)
        self.assertIn("as Zoë (Chaleureuse)", out)
        _code, out, _err = self.perform("--character", "Zoë", "--json")
        self.assertEqual(json.loads(out)["character"], "Zoë")

    def test_a_character_and_a_voice_that_both_have_accents_are_shown_as_written(self):
        self.service.add_voice("Zoë", "Rêveuse")
        code, out, _err = self.perform("--character", "Zoë")
        self.assertEqual(code, 0)
        self.assertIn("as Zoë (Rêveuse)", out)
        _code, out, _err = self.perform("--character", "Zoë", "--json")
        result = json.loads(out)
        self.assertEqual((result["character"], result["voice"]), ("Zoë", "Rêveuse"))

    def test_the_same_key_gives_the_same_take_and_says_so(self):
        self.assertEqual(self.perform("--character", "Nell", "--idempotency-key", "attempt-1")[0], 0)
        made = len(self.engines.requests)
        code, out, _err = self.perform("--character", "Nell", "--idempotency-key", "attempt-1")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.engines.requests), made)
        self.assertIn("an earlier take replayed", out)
        self.assertNotIn("made in", out)
        _code, out, _err = self.perform("--character", "Nell", "--idempotency-key", "attempt-1", "--json")
        self.assertIs(json.loads(out)["replay"], True)

    def test_settings_pronunciations_and_language_are_sent(self):
        self.service.request("POST", "/api/pronunciations", {
            "source": "Nell", "replacement": "Nel", "character_id": self.identity["character_id"]})
        code, _out, _err = self.run_cli("perform", "--text", "Nell speaks.", "--out", str(self.folder / "a.wav"),
                                        "--character", "Nell", "--setting", "speed=1.1", "--pronunciations",
                                        "--language", "English")
        self.assertEqual(code, 0)
        request = self.engines.requests[-1]
        self.assertEqual((request["text"], request["speed"], request["language"]), ("Nel speaks.", 1.1, "English"))

    def test_a_direction_for_an_engine_with_none_is_the_services_refusal(self):
        code, _out, err = self.perform("--character", "Nell", "--direction", "angrily")
        self.assertEqual(code, 1)
        self.assertIn("answered 400", err)
        self.assertIn("kokoro has no verified text-direction control", err)
        self.assertFalse((self.folder / "nell.wav").exists())

    def test_a_service_that_is_busy_is_exit_status_1(self):
        self.engines.busy = True
        code, _out, err = self.perform("--character", "Nell")
        self.assertEqual(code, 1)
        self.assertIn("answered 409", err)

    def test_no_text_is_a_mistake_and_nothing_is_sent(self):
        for argv in (["--character", "Nell"], ["--character", "Nell", "--text", "   "]):
            with self.subTest(argv):
                code, _out, err = self.run_cli("perform", "--out", str(self.folder / "a.wav"), *argv)
                self.assertEqual(code, 2)
                self.assertIn("give --text", err)
        self.assertEqual(self.engines.requests, [])

    def test_every_flag_reaches_the_request(self):
        sent = []

        def call(base, method, path, *, body=None, timeout):
            sent.append((method, path, body, timeout))
            return {}, b"RIFF\x00\x00\x00\x00WAVE"

        with mock.patch.object(lab_cli, "call", side_effect=call):
            code, out, _err = self.run_cli(
                "--timeout", "30", "perform", "--text", "Hello.", "--out", str(self.folder / "a.wav"), "--identity-id", "id1",
                "--line-id", "l7", "--reference-version-id", "ref1", "--direction", "softly", "--language", "French",
                "--setting", "speed=1.1", "--setting", "seed=4", "--pronunciations", "--idempotency-key", "k1")
        self.assertEqual(code, 0)
        self.assertEqual(sent, [("POST", "/api/performance", {
            "identity_id": "id1", "line_id": "l7", "text": "Hello.", "reference_version_id": "ref1", "direction": "softly",
            "language": "French", "idempotency_key": "k1", "settings": {"speed": 1.1, "seed": 4},
            "use_pronunciations": True}, 30.0)])
        self.assertEqual(out, f"Wrote {self.folder / 'a.wav'} (12 bytes)\n", "an answer with no headers still says what was written")

    def test_looking_a_name_up_never_waits_longer_than_ten_seconds(self):
        waited = []
        cast = {"characters": [{"name": "Nell", "identities": [
            {"id": "i1", "label": "Warm", "status": "approved", "engine_id": "kokoro"}]}]}

        def get_json(base, path, timeout):
            waited.append(timeout)
            return cast

        with mock.patch.object(lab_cli, "get_json", side_effect=get_json), \
                mock.patch.object(lab_cli, "call", return_value=({}, b"RIFF\x00\x00\x00\x00WAVE")):
            self.assertEqual(self.perform("--character", "Nell")[0], 0)
            self.assertEqual(self.perform("--character", "Nell", "--timeout", "3")[0], 0)
        self.assertEqual(waited, [10, 3.0])


class CastTests(CliCase):
    def test_an_empty_cast_says_so(self):
        self.assertEqual(self.run_cli("cast"), (0, "The Cast is empty.\n", ""))

    def test_characters_and_voices_are_listed_with_their_ids(self):
        warm = self.service.add_voice("Nell", "Warm")
        stern = self.service.add_voice(self.service.cast.get_character(warm["character_id"]), "Stern", approved=True)
        code, out, _err = self.run_cli("cast")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "Nell",
            f"  Stern  (kokoro, approved)  identity {stern['id']}",
            f"  Warm  (kokoro, candidate)  identity {warm['id']}"])

    def test_a_retired_character_is_marked(self):
        warm = self.service.add_voice("Nell", "Warm")
        self.service.cast.retire("character", warm["character_id"])
        _code, out, _err = self.run_cli("cast")
        self.assertEqual(out.splitlines()[0], "Nell  (retired)")

    def test_retirement_shows_and_json_is_the_raw_answer(self):
        warm = self.service.add_voice("Nell", "Warm")
        self.service.cast.retire("identity", warm["id"])
        _code, out, _err = self.run_cli("cast")
        self.assertIn("(kokoro, candidate, retired)", out)
        _code, out, _err = self.run_cli("cast", "--json")
        self.assertEqual(json.loads(out), self.service.get_json("/api/cast"))


class CommandLineTests(CliCase):
    def reaches_the_test_service(self, *argv):
        """True when the command succeeded and it was this test's service that answered."""
        before = self.engines.health_calls
        code = self.run_cli(*argv, url=False)[0]
        return code == 0 and self.engines.health_calls == before + 1

    def test_the_address_comes_from_the_flag_wherever_it_is_given(self):
        for argv in (["--url", self.service.base, "health"], ["health", "--url", self.service.base],
                     ["--url", self.service.base + "/", "health"], ["--url", self.service.base + "///", "health"]):
            with self.subTest(argv):
                self.assertTrue(self.reaches_the_test_service(*argv))

    def test_without_a_flag_the_environment_decides_and_without_either_the_default_address_does(self):
        with mock.patch.dict(os.environ, {"VOICE_LAB_URL": self.service.base}):
            self.assertTrue(self.reaches_the_test_service("health"))
        with mock.patch.dict(os.environ, {"VOICE_LAB_URL": f"http://127.0.0.1:{unused_port()}"}):
            self.assertTrue(self.reaches_the_test_service("--url", self.service.base, "health"), "the flag wins")
            self.assertFalse(self.reaches_the_test_service("health"), "the environment's address is the one tried")
        with mock.patch.object(lab_cli, "DEFAULT_URL", self.service.base):
            self.assertTrue(self.reaches_the_test_service("health"))
        self.assertFalse(self.reaches_the_test_service("health"), "setUp points the default at a port nothing listens on")
        self.assertEqual(DOCUMENTED_DEFAULT_URL, "http://127.0.0.1:8765")

    def test_an_address_that_is_not_a_web_address_is_a_mistake(self):
        code, _out, err = self.run_cli("--url", "127.0.0.1:8765", "health", url=False)
        self.assertEqual(code, 2)
        self.assertIn("not a web address", err)

    def test_timeouts_default_to_ten_seconds_for_reads_and_fifteen_minutes_for_speaking(self):
        parser = lab_cli.build_parser()
        for argv, expected in ((["health"], 10), (["cast"], 10), (["speak", "--text", "x", "--out", "a"], 900),
                               (["perform", "--text", "x", "--out", "a"], 900),
                               (["--timeout", "5", "speak", "--text", "x", "--out", "a"], 5),
                               (["speak", "--text", "x", "--out", "a", "--timeout", "7"], 7), (["health", "--timeout", "3"], 3)):
            with self.subTest(argv):
                self.assertEqual(lab_cli.timeout_for(parser.parse_args(argv)), expected)

    def test_help_lists_the_four_commands(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            lab_cli.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        for command in ("health", "speak", "perform", "cast"):
            self.assertIn(command, out.getvalue())

    def test_an_unknown_command_is_exit_status_2(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            lab_cli.main(["frobnicate"])
        self.assertEqual(caught.exception.code, 2)

    def test_it_runs_on_its_own_with_nothing_but_python(self):
        result = subprocess.run([sys.executable, "-I", str(CLI), "--help"], cwd=self.folder, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("perform", result.stdout)
        result = subprocess.run([sys.executable, "-I", str(CLI), "--url", self.service.base, "health"], cwd=self.folder,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is ready", result.stdout)

    def test_it_imports_nothing_but_the_standard_library(self):
        tree = ast.parse(CLI.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {item.name.split(".")[0] for item in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names.add(node.module.split(".")[0])
        self.assertEqual(sorted(names - set(sys.stdlib_module_names)), [])

    def test_it_uses_only_syntax_that_python_3_8_reads(self):
        ast.parse(CLI.read_text(encoding="utf-8"), feature_version=(3, 8))

    def test_a_name_the_terminal_cannot_show_is_replaced_and_is_not_a_crash(self):
        self.service.add_voice("Zoë", "Chaleureuse")
        result = subprocess.run([sys.executable, str(CLI), "--url", self.service.base, "cast"], cwd=self.folder,
                                env={**os.environ, "PYTHONIOENCODING": "ascii"}, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "Zo?")


class HelperTests(unittest.TestCase):
    def test_an_error_body_is_the_services_message_else_plain_text(self):
        self.assertEqual(lab_cli._message(b'{"error": "No."}'), "No.")
        self.assertEqual(lab_cli._message(b"<html>Bad gateway</html>"), "<html>Bad gateway</html>")
        self.assertEqual(lab_cli._message(b'{"detail": 1}'), '{"detail": 1}')
        self.assertEqual(lab_cli._message(b""), "no message")
        self.assertEqual(lab_cli._message(b"x" * 500), "x" * 300)
        self.assertEqual(lab_cli._message(b"\xff\xfe"), "��")

    def test_a_pair_is_split_at_its_first_equals_sign_and_needs_a_key(self):
        self.assertEqual(lab_cli.parse_pairs(["note=a=b", "n=7", "t=true", "s=text", 'o={"a": 1}', "e="]),
                         {"note": "a=b", "n": 7, "t": True, "s": "text", "o": {"a": 1}, "e": ""})
        self.assertEqual(lab_cli.parse_pairs(None), {})
        for bad in ("=7", "seed", ""):
            with self.subTest(bad), self.assertRaises(lab_cli.Usage):
                lab_cli.parse_pairs([bad])

    def test_an_address_is_checked_and_loses_its_trailing_slashes(self):
        self.assertEqual(lab_cli.normalize_url("http://127.0.0.1:8765/"), "http://127.0.0.1:8765")
        self.assertEqual(lab_cli.normalize_url("HTTP://Localhost:9//"), "HTTP://Localhost:9")
        self.assertEqual(lab_cli.normalize_url("https://example.test"), "https://example.test")
        for bad in ("127.0.0.1:8765", "ftp://example.test", "localhost"):
            with self.subTest(bad), self.assertRaises(lab_cli.Usage):
                lab_cli.normalize_url(bad)

    def test_what_the_service_said_becomes_facts_and_unreadable_numbers_are_left_out(self):
        facts = lab_cli.summarize("a.wav", b"RIFF", {"x-audio-seconds": "oops", "x-gen-seconds": "1.5", "x-take-id": "t1",
                                                      "x-idempotent-replay": "true", "x-cast-line-id": "l1"})
        self.assertEqual(facts, {"file": "a.wav", "bytes": 4, "gen_seconds": 1.5, "take_id": "t1", "replay": True,
                                 "line_id": "l1"})
        self.assertEqual(lab_cli.summarize("a.wav", b"", {}), {"file": "a.wav", "bytes": 0})

    def test_a_line_names_only_what_it_knows(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            lab_cli.say_wrote({"file": "a.wav", "bytes": 44})
            lab_cli.say_wrote({"file": "b.wav", "bytes": 44, "audio_seconds": 1.5, "character": "Nell"})
            lab_cli.say_wrote({"file": "c.wav", "bytes": 44, "audio_seconds": 1.0, "take_id": "t1", "replay": True,
                               "gen_seconds": 1.25})
            lab_cli.say_wrote({"file": "d.wav", "bytes": 44, "take_id": "t2", "gen_seconds": 1.25})
        self.assertEqual(printed.getvalue(), "Wrote a.wav (44 bytes)\nWrote b.wav: 1.50 s of audio, as Nell\n"
                         "Wrote c.wav: 1.00 s of audio, take t1, an earlier take replayed\n"
                         "Wrote d.wav: take t2, made in 1.25 s\n")


class ResolveVoiceTests(unittest.TestCase):
    """Which voice a name stands for, on a cast the tests write out by hand."""

    @staticmethod
    def voice(label, status="candidate", retired=False):
        return {"id": label.lower(), "label": label, "status": status, "engine_id": "kokoro", "retired": retired}

    @staticmethod
    def character(name, *voices, retired=False):
        return {"name": name, "retired": retired, "identities": list(voices)}

    def resolve(self, characters, name="Nell", voice=None):
        with mock.patch.object(lab_cli, "get_json", return_value={"characters": characters}):
            return lab_cli.resolve_voice("http://example.test", name, voice, 5)

    def refusal(self, characters, name="Nell", voice=None):
        with self.assertRaises(lab_cli.Usage) as caught:
            self.resolve(characters, name, voice)
        return str(caught.exception)

    def test_the_only_usable_voice_is_the_one_and_rejected_and_retired_ones_are_not_usable(self):
        nell = self.character("Nell", self.voice("Bad", "rejected"), self.voice("Old", retired=True), self.voice("Warm"))
        self.assertEqual(self.resolve([nell]), "warm")
        for label in ("Bad", "Old"):
            with self.subTest(label):
                message = self.refusal([nell], voice=label)
                self.assertIn("Warm (kokoro, candidate)", message)
                self.assertNotIn("Bad", message)
                self.assertNotIn("Old", message)

    def test_a_character_with_nothing_usable_says_so(self):
        self.assertIn("has no voice that can be used", self.refusal([self.character("Nell", self.voice("Bad", "rejected"))]))
        self.assertIn("has no voice that can be used", self.refusal([self.character("Nell")]))

    def test_several_voices_need_a_choice_unless_exactly_one_is_approved(self):
        two = self.character("Nell", self.voice("Warm"), self.voice("Stern"))
        self.assertIn("Warm (kokoro, candidate); Stern (kokoro, candidate)", self.refusal([two]))
        self.assertEqual(self.resolve([two], voice="stern"), "stern")
        one_approved = self.character("Nell", self.voice("Warm"), self.voice("Stern", "approved"))
        self.assertEqual(self.resolve([one_approved]), "stern")
        two_approved = self.character("Nell", self.voice("Warm", "approved"), self.voice("Stern", "approved"))
        self.assertIn("--voice LABEL", self.refusal([two_approved]))

    def test_capitals_do_not_matter_in_a_name_or_a_label(self):
        self.assertEqual(self.resolve([self.character("Nell", self.voice("Warm"))], name="nELL", voice="WARM"), "warm")

    def test_a_retired_character_is_neither_found_nor_listed(self):
        message = self.refusal([self.character("Nell", self.voice("Old"), retired=True), self.character("Brann", self.voice("Gruff"))])
        self.assertIn("no character called 'Nell'", message)
        self.assertTrue(message.endswith("Characters: Brann"), message)
        live_and_retired = [self.character("Nell", self.voice("Old"), retired=True), self.character("Nell", self.voice("Warm"))]
        self.assertEqual(self.resolve(live_and_retired), "warm")

    def test_an_empty_cast_says_none_yet_and_two_characters_of_one_name_need_an_id(self):
        self.assertTrue(self.refusal([]).endswith("Characters: none yet"))
        twins = [self.character("Nell", self.voice("Warm")), self.character("NELL", self.voice("Stern"))]
        self.assertIn("more than one character is called", self.refusal(twins))
        self.assertIn("--identity-id", self.refusal(twins))


if __name__ == "__main__":
    unittest.main()
