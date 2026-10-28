"""The calls scripts depend on, held to what docs/API.md says: /api/speak is one blocking whole-file call, /api/performance saves a
take and can be repeated safely, and the service describes itself. The real handler runs on a temporary library with fake engines."""
import io
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_service
import api_spec
import serve


def documented(key):
    return next(row for row in api_spec.documented() if api_spec.key(row) == key)


class SpeakContractTests(unittest.TestCase):
    def setUp(self):
        self.service = api_service.Service(self)
        self.engines = self.service.engines

    def test_the_answer_is_one_finished_wav_with_its_length_stated(self):
        status, headers, body = self.service.request("POST", "/api/speak", {"text": "Hello there."})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertIsNone(headers.get("Transfer-Encoding"), "the answer must not be chunked")
        samples, rate = sf.read(io.BytesIO(body))
        self.assertEqual((len(samples), rate), (api_service.RATE // 10, api_service.RATE))

    def test_nothing_at_all_is_sent_until_the_clip_is_finished(self):
        self.engines.gate = threading.Event()
        body = json.dumps({"text": "Hello there."}).encode()
        sock = socket.create_connection(("127.0.0.1", self.service.port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(b"POST /api/speak HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                     + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.assertTrue(self.engines.started.wait(5))
        sock.settimeout(0.4)
        with self.assertRaises(socket.timeout):         # not a byte, not even the headers, while the engine is working
            sock.recv(1)
        self.engines.gate.set()
        sock.settimeout(10)
        raw = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
        head, _, content = raw.partition(b"\r\n\r\n")
        self.assertIn(b" 200 ", head.split(b"\r\n")[0])
        self.assertIn(b"Content-Length: " + str(len(content)).encode(), head)
        self.assertEqual(content[:4], b"RIFF")

    def test_the_headers_the_reference_always_lists_are_sent(self):
        _status, headers, _body = self.service.request("POST", "/api/speak", {"text": "Hello there."})
        self.assertEqual(headers["X-Audio-Seconds"], "0.10")
        self.assertEqual(headers["X-Gen-Seconds"], "0.10")
        self.assertIn("X-First-Chunk", headers)
        self.assertIsNone(headers.get("X-Voice-Backend"), "an engine that reports nothing sends no backend header")

    def test_the_headers_the_engine_reports_are_passed_on(self):
        self.engines.extra_info = {"backend": "fixture-backend", "checkpoint": "fixture-checkpoint",
                                   "checkpoint_revision": "rev-1", "mode": "design"}
        _status, headers, _body = self.service.request("POST", "/api/speak", {"text": "Hello there."})
        self.assertEqual((headers["X-Voice-Backend"], headers["X-Model-Checkpoint"], headers["X-Checkpoint-Revision"],
                          headers["X-Generation-Mode"]), ("fixture-backend", "fixture-checkpoint", "rev-1", "design"))
        listed = {field.name for field in documented("POST /api/speak").headers}
        self.assertTrue({"X-Voice-Backend", "X-Model-Checkpoint", "X-Checkpoint-Revision", "X-Generation-Mode"} <= listed)

    def test_a_refusal_is_a_json_error_with_a_400(self):
        status, headers, raw = self.service.request("POST", "/api/speak", {"text": "bad"})
        self.assertEqual(status, 400)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(raw), {"error": "The text is not allowed."})

    def test_a_busy_service_refuses_and_does_not_queue(self):
        self.engines.busy = True
        status, _headers, raw = self.service.request("POST", "/api/speak", {"text": "Hello there."})
        self.assertEqual(status, 409)
        self.assertIn("busy", json.loads(raw)["error"])

    def test_a_query_string_is_not_a_route(self):
        self.assertEqual(self.service.request("POST", "/api/speak?x=1", {"text": "Hello there."})[0], 404)

    def test_a_body_over_64_kib_is_refused_with_413(self):
        status, _headers, raw = self.service.request("POST", "/api/speak", {"text": "a" * 70_000})
        self.assertEqual(status, 413)
        self.assertIn("error", json.loads(raw))

    def post_and_read(self, declared, pieces):
        """Send the headers of a POST that declares `declared` bytes, then each of `pieces` after a short pause, and read the
        whole answer. Returns the answer and how many seconds it took."""
        head = (b"POST /api/speak HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {declared}\r\n\r\n".encode())
        began = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.service.port), timeout=10) as sock:
            sock.sendall(head)
            for piece in pieces:
                time.sleep(0.2)
                sock.sendall(piece)
            raw = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
        return raw, time.monotonic() - began

    def test_the_413_reaches_a_client_that_sends_its_body_after_the_refusal(self):
        # The body is read through before the refusal goes out. Closing with it unread resets the connection, and on Windows the
        # client then loses the 413 it had already received. Here the body follows the headers by a moment.
        body = json.dumps({"text": "a" * 70_000}).encode()
        for _ in range(3):
            raw, _seconds = self.post_and_read(len(body), [body])
            self.assertIn(b" 413 ", raw.split(b"\r\n")[0])
            self.assertIn("error", json.loads(raw.partition(b"\r\n\r\n")[2]))

    def test_a_refused_body_too_big_to_read_through_is_not_waited_for(self):
        with mock.patch.object(serve, "MAX_DISCARD_BYTES", 100_000):
            raw, seconds = self.post_and_read(200_000, [])
        self.assertIn(b" 413 ", raw.split(b"\r\n")[0])
        self.assertLess(seconds, 3)

    def test_a_refused_body_that_stalls_is_given_up_on(self):
        with mock.patch.object(serve, "DISCARD_SECONDS", 0.5):
            raw, seconds = self.post_and_read(200_000, [b"x" * 10])
        self.assertIn(b" 413 ", raw.split(b"\r\n")[0])
        self.assertLess(seconds, 5)

    def test_every_answer_carries_a_request_id_and_keeps_the_callers(self):
        _s, headers, _b = self.service.request("GET", "/api/health")
        self.assertRegex(headers["X-Request-Id"], r"^[0-9a-f]{12}$")
        _s, headers, _b = self.service.request("GET", "/api/health", headers={"X-Request-Id": "my-script.7"})
        self.assertEqual(headers["X-Request-Id"], "my-script.7")


class PerformContractTests(unittest.TestCase):
    def setUp(self):
        self.service = api_service.Service(self)
        self.engines = self.service.engines
        self.identity = self.service.add_voice("Nell", "Warm")

    def perform(self, **more):
        body = {"identity_id": self.identity["id"], "line_id": "scene1-001", "text": "We made it home.", **more}
        return self.service.request("POST", "/api/performance", body)

    def test_the_answer_is_the_wav_and_names_the_saved_take(self):
        status, headers, body = self.perform()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertRegex(headers["X-Take-Id"], r"^[a-f0-9]{32}$")
        self.assertEqual((headers["X-Cast-Character"], headers["X-Cast-Identity"], headers["X-Cast-Line-Id"]),
                         ("Nell", "Warm", "scene1-001"))
        self.assertEqual((headers["X-Generation-Mode"], headers["X-Audio-Seconds"], headers["X-Gen-Seconds"]),
                         ("fixed_voice", "0.10", "0.1"))

    def test_every_header_the_reference_lists_is_sent(self):
        _status, headers, _body = self.perform()
        for field in documented("POST /api/performance").headers:
            with self.subTest(field.name):
                if field.name == "X-Idempotent-Replay":
                    self.assertIsNone(headers.get(field.name), "only a replay carries this")
                else:
                    self.assertIn(field.name, headers)

    def test_the_take_is_saved_and_can_be_read_and_downloaded_again(self):
        _status, headers, wav = self.perform()
        take_id = headers["X-Take-Id"]
        take = self.service.get_json(f"/api/takes/{take_id}")["take"]
        self.assertEqual((take["line_id"], take["text"], take["identity_id"]),
                         ("scene1-001", "We made it home.", self.identity["id"]))
        for key in ("id", "character_id", "character_name", "identity_id", "identity_label", "engine_id",
                    "reference_version_id", "line_id", "text", "spoken_text", "language", "direction", "settings", "model",
                    "provenance", "sample_rate", "duration_seconds", "status", "rating", "notes", "created_at", "loudness",
                    "waveform_checks", "has_word_timings", "has_phoneme_timings", "audio_url", "retired"):
            with self.subTest(key):
                self.assertIn(key, take)
        self.assertEqual(self.service.request("GET", f"/api/takes/{take_id}/audio")[2], wav)
        listed = self.service.get_json(f"/api/takes?identity_id={self.identity['id']}&line_id=scene1-001")["takes"]
        self.assertEqual([item["id"] for item in listed], [take_id])

    def test_a_repeat_with_the_same_key_returns_the_same_take_without_generating_again(self):
        first = self.perform(idempotency_key="attempt-1")
        made = len(self.engines.requests)
        second = self.perform(idempotency_key="attempt-1")
        self.assertEqual(second[0], 200)
        self.assertEqual(len(self.engines.requests), made, "a replay must not reach the engine")
        self.assertEqual(second[1]["X-Take-Id"], first[1]["X-Take-Id"])
        self.assertEqual(second[1]["X-Idempotent-Replay"], "true")
        self.assertEqual(second[2], first[2])
        self.assertIsNone(second[1].get("X-Gen-Seconds"), "a replay carries X-Take-Id and the three X-Cast-* headers only")
        self.assertEqual((second[1]["X-Cast-Character"], second[1]["X-Cast-Identity"], second[1]["X-Cast-Line-Id"]),
                         ("Nell", "Warm", "scene1-001"))
        self.assertEqual(len(self.service.get_json("/api/takes")["takes"]), 1)

    def test_a_key_used_again_with_other_text_is_refused(self):
        self.perform(idempotency_key="attempt-1")
        status, _headers, raw = self.perform(idempotency_key="attempt-1", text="A different line.")
        self.assertEqual(status, 400)
        self.assertIn("idempotency_key", json.loads(raw)["error"])

    def test_a_key_is_a_repeat_only_when_every_input_is_the_same(self):
        stern = self.service.add_voice(self.service.cast.get_character(self.identity["character_id"]), "Stern")
        self.perform(idempotency_key="attempt-1")
        for name, change in (("line", {"line_id": "scene1-002"}), ("pronunciations", {"use_pronunciations": True}),
                             ("settings", {"settings": {"speed": 1.1}}), ("voice", {"identity_id": stern["id"]})):
            with self.subTest(name):
                status, _headers, raw = self.perform(idempotency_key="attempt-1", **change)
                self.assertEqual(status, 400)
                self.assertIn("idempotency_key", json.loads(raw)["error"])
        self.assertEqual(len(self.service.get_json("/api/takes")["takes"]), 1)

    def test_a_key_is_not_a_repeat_when_only_the_text_differs_and_the_spoken_text_is_the_same(self):
        self.service.request("POST", "/api/pronunciations", {
            "source": "Nell", "replacement": "Nel", "character_id": self.identity["character_id"]})
        self.perform(idempotency_key="attempt-1", text="Nell speaks.", use_pronunciations=True)
        status, _headers, raw = self.perform(idempotency_key="attempt-1", text="Nel speaks.", use_pronunciations=True)
        self.assertEqual(status, 400)
        self.assertIn("idempotency_key", json.loads(raw)["error"])

    def test_the_limits_the_reference_states_are_the_real_ones(self):
        for field, limit in (("text", 1200), ("line_id", 100), ("idempotency_key", 200)):
            with self.subTest(field):
                self.assertEqual(self.perform(**{field: "a" * limit})[0], 200)
                made = len(self.engines.requests)
                self.assertEqual(self.perform(**{field: "a" * (limit + 1)})[0], 400)
                self.assertEqual(len(self.engines.requests), made, "a request over the limit is refused before any generating")
        made = len(self.engines.requests)
        for change in ({"text": "   "}, {"line_id": "  "}, {"idempotency_key": ""}):
            with self.subTest(change):
                self.assertEqual(self.perform(**change)[0], 400)
        self.assertEqual(len(self.engines.requests), made, "a blank field is refused before any generating")
        self.service.request("POST", "/api/pronunciations", {
            "source": "Nell", "replacement": "N", "character_id": self.identity["character_id"]})
        self.assertEqual(self.perform(text="Nell " + "a" * 1195, use_pronunciations=True)[0], 200, "1200 characters as sent")
        made = len(self.engines.requests)
        self.assertEqual(self.perform(text="Nell " + "a" * 1196, use_pronunciations=True)[0], 400,
                         "the limit is on the text as sent, not on the shorter text that is spoken")
        self.assertEqual(len(self.engines.requests), made)
        for field in ("text", "line_id", "idempotency_key"):
            self.assertRegex(" ".join(item.doc for item in documented("POST /api/performance").body if item.name == field),
                             {"text": r"1 to 1200", "line_id": r"1 to 100", "idempotency_key": r"1 to 200"}[field])

    def test_without_a_key_every_call_is_a_new_take(self):
        self.perform()
        self.perform()
        self.assertEqual(len(self.service.get_json("/api/takes")["takes"]), 2)

    def test_an_unknown_voice_is_a_404_and_a_missing_line_id_a_400(self):
        status, _headers, raw = self.service.request("POST", "/api/performance", {
            "identity_id": "0" * 32, "line_id": "x", "text": "Hello."})
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(raw))
        status, _headers, raw = self.service.request("POST", "/api/performance", {
            "identity_id": self.identity["id"], "text": "Hello."})
        self.assertEqual(status, 400)
        self.assertIn("Line ID", json.loads(raw)["error"])

    def test_a_busy_service_refuses_and_leaves_no_take(self):
        self.engines.busy = True
        status, _headers, _raw = self.perform()
        self.assertEqual(status, 409)
        self.assertEqual(self.service.get_json("/api/takes")["takes"], [])

    def test_a_name_that_is_not_ascii_arrives_percent_encoded(self):
        identity = self.service.add_voice("Zoë", "Chaleureuse")
        status, headers, _body = self.service.request("POST", "/api/performance", {
            "identity_id": identity["id"], "line_id": "x", "text": "Bonjour."})
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Cast-Character"], quote("Zoë", safe=""))
        self.assertIn("X-Cast-Character", headers["X-Metadata-Encoding"])
        self.assertEqual(headers["X-Cast-Identity"], "Chaleureuse", "plain ASCII is sent as it is")

    def test_pronunciations_change_what_is_spoken_and_the_take_keeps_both_texts(self):
        self.service.request("POST", "/api/pronunciations", {
            "source": "Nell", "replacement": "Nel", "character_id": self.identity["character_id"]})
        _status, headers, _body = self.perform(text="Nell speaks.", use_pronunciations=True)
        self.assertEqual(self.engines.requests[-1]["text"], "Nel speaks.")
        take = self.service.get_json(f"/api/takes/{headers['X-Take-Id']}")["take"]
        self.assertEqual((take["text"], take["spoken_text"]), ("Nell speaks.", "Nel speaks."))


class ServiceDescribesItselfTests(unittest.TestCase):
    def setUp(self):
        self.service = api_service.Service(self)

    def test_the_service_serves_its_own_openapi_document(self):
        status, headers, raw = self.service.request("GET", "/api/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(raw), api_spec.build_openapi())

    def test_the_file_in_the_program_folder_is_the_same_document(self):
        written = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(self.service.get_json("/api/openapi.json"), written)

    def test_health_has_the_documented_keys(self):
        health = self.service.get_json("/api/health")
        for key in ("status", "busy", "device", "version", "workers", "last_worker_error", "last_unload"):
            self.assertIn(key, health)
        self.assertEqual(health["version"], api_spec.VERSION)

    def test_the_cast_has_the_documented_shape(self):
        identity = self.service.add_voice("Nell", "Warm")
        cast = self.service.get_json("/api/cast")
        self.assertEqual(cast["schema_version"], 1)
        character = cast["characters"][0]
        for key in ("id", "name", "description", "tags", "identities"):
            self.assertIn(key, character)
        listed = character["identities"][0]
        for key in ("id", "engine_id", "label", "status", "kind", "engine_options", "approved_reference_id", "notes",
                    "references"):
            self.assertIn(key, listed)
        self.assertEqual((listed["id"], listed["status"], listed["kind"]), (identity["id"], "candidate", "fixed_voice"))

    def test_every_documented_read_that_needs_no_arguments_answers(self):
        # Reads that walk the program's own out/ folder, which a test has no business measuring.
        skipped = {"/api/system", "/api/system/storage"}
        checked = []
        for row in api_spec.documented():
            if row.method != "GET" or api_spec.path_names(row.path) or any(item.required for item in row.query):
                continue
            if row.path in skipped:
                continue
            with self.subTest(row.path):
                status, headers, raw = self.service.request("GET", row.path)
                self.assertEqual(status, 200, raw[:200])
                if row.reply_kind == "json":
                    self.assertEqual(headers["Content-Type"], "application/json")
                    json.loads(raw)
                checked.append(row.path)
        self.assertGreater(len(checked), 15)


if __name__ == "__main__":
    unittest.main()
