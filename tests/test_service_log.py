"""The service log: a line per action or failure, never a request body, and a request ID on every answer."""
import json
import logging
import re
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

import serve
from app_version import VERSION
from cast_store import CastStore
from evaluation_store import EvaluationStore
from request_log import NullRequestLog, RequestLog, new_request_id
from scene_store import SceneStore
from worker_pool import read_catalog


class RequestLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-service-log-")
        self.addCleanup(self.temp.cleanup)

    def make(self, **options):
        log = RequestLog(Path(self.temp.name) / "logs", **options)
        self.addCleanup(log.close)
        return log

    def test_the_file_is_named_for_the_run_and_the_header_names_the_version(self):
        log = self.make()
        self.assertRegex(log.path.name, r"service-\d{8}-\d{6}-\d+\.log")
        log.start("Fixture", "9.9.9", port=8765)
        text = log.path.read_text("utf-8")
        self.assertRegex(text, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} start Fixture 9\.9\.9 pid=\d+ port=8765")

    def test_request_ids_are_kept_only_when_they_are_plain_text(self):
        self.assertEqual(new_request_id("assistant-call_42.a"), "assistant-call_42.a")
        for bad in (None, "", "has space", "line\nbreak", "x" * 65, "naïve", "a/b"):
            with self.subTest(bad=bad):
                self.assertRegex(new_request_id(bad), r"[0-9a-f]{12}\Z")

    def test_only_actions_and_failures_are_worth_a_line(self):
        log = self.make()
        self.assertFalse(log.wants("GET", 200, 0.05))
        self.assertFalse(log.wants("GET", 304, 0.2))
        self.assertTrue(log.wants("GET", 404, 0.01))
        self.assertTrue(log.wants("GET", 200, 1.0))
        self.assertTrue(log.wants("GET", 0, 0.01))
        self.assertTrue(log.wants("POST", 200, 0.01))
        self.assertTrue(log.wants("DELETE", 204, 0.01))

    def test_a_line_carries_the_facts_and_a_failure_message_is_quoted_and_cut(self):
        log = self.make()
        log.request("abc123", "POST", "/api/speak", 400, 1.23456, engine="kokoro", error='bad "input"\nnext line' + "x" * 400,
                    bytes=None, empty="")
        line = log.path.read_text("utf-8").splitlines()
        self.assertEqual(len(line), 1)
        self.assertRegex(line[0], r'^\S+ \S+ abc123 POST /api/speak 400 1\.235s engine=kokoro error="bad \\"input\\"\\nnext line')
        self.assertNotIn("bytes", line[0])
        self.assertNotIn("empty", line[0])
        message = json.loads(re.search(r'error=(".*")$', line[0]).group(1))
        self.assertEqual(len(message), 300)

    def test_a_traceback_is_written_under_its_request(self):
        log = self.make()
        log.request("abc123", "POST", "/x", 500, 0.1, traceback="Traceback (most recent call last):\n  File x\nRuntimeError: no\n")
        lines = log.path.read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(" abc123   | " in item for item in lines[1:]))
        self.assertTrue(lines[3].endswith("RuntimeError: no"))

    def test_the_file_rotates_so_one_run_cannot_fill_the_disk(self):
        log = self.make(max_bytes=2000, backups=1)
        for number in range(200):
            log.request(f"id{number:04d}", "POST", "/api/performance", 200, 0.5, engine="kokoro")
        names = sorted(path.name for path in log.path.parent.iterdir())
        self.assertEqual(names, [log.path.name, log.path.name + ".1"])
        self.assertLess(sum(path.stat().st_size for path in log.path.parent.iterdir()), 6000)

    def test_closing_the_log_releases_its_handler_and_the_file(self):
        log = RequestLog(Path(self.temp.name) / "logs")
        name = log._logger.name
        log.event("hello")
        log.close()
        self.assertEqual(logging.getLogger(name).handlers, [])
        log.path.unlink()  # a file still held open by the handler could not be removed on Windows

    def test_the_null_log_accepts_everything_and_wants_nothing(self):
        log = NullRequestLog()
        log.start("x", "1", port=1)
        log.request("id", "POST", "/", 500, 1.0, error="x")
        log.event("e")
        log.close()
        self.assertFalse(log.wants("POST", 500, 9.0))


class FakeEngines:
    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"
        self.loaded = []

    def health(self):
        return {"status": "ready", "busy": False, "device": "cpu", "workers": [],
                "last_worker_error": None, "last_unload": []}

    def speak(self, req):
        if req.get("text") == "bad":
            raise ValueError("The text is not allowed.")
        if req.get("text") == "explode":
            raise RuntimeError("the worker fell over. Log: out/logs/workers/kokoro/worker-1.log")
        if req.get("text") == "slow":
            time.sleep(0.5)
        axis = np.arange(2400, dtype=np.float32) / 24_000
        return (0.1 * np.sin(2 * np.pi * 260 * axis)).astype(np.float32), 24_000, {"total_s": 0.1, "first_chunk_s": None}


class ServiceLogHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-service-http-")
        root = Path(self.temp.name)
        engines = FakeEngines()
        cast = CastStore(root)
        scenes = SceneStore(cast, engines.entries, root=root)
        evaluations = EvaluationStore(root / "data" / "evaluations" / "evaluations.sqlite3", lab_root=root)
        self.log = RequestLog(root / "logs")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(engines, 0, cast, scenes, None, evaluations))
        self.server.RequestHandlerClass = serve.make_handler(
            engines, self.server.server_address[1], cast, scenes, None, evaluations, self.log)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.log.close()
        self.temp.cleanup()

    def call(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def log_text(self, needle, timeout=3.0):
        """The line is written once the answer has gone out, so give the server thread a moment."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.log.path.read_text("utf-8")
            if needle in text:
                return text
            time.sleep(0.02)
        self.fail(f"{needle!r} never reached the log:\n{self.log.path.read_text('utf-8')}")

    def test_health_names_the_version_and_every_answer_has_a_request_id(self):
        status, headers, raw = self.call("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["version"], VERSION)
        self.assertRegex(headers["X-Request-Id"], r"[0-9a-f]{12}\Z")
        _, kept, _ = self.call("/api/health", headers={"X-Request-Id": "assistant-call-7"})
        self.assertEqual(kept["X-Request-Id"], "assistant-call-7")
        _, replaced, _ = self.call("/api/health", headers={"X-Request-Id": "not valid"})
        self.assertRegex(replaced["X-Request-Id"], r"[0-9a-f]{12}\Z")
        status, headers, _ = self.call("/missing")
        self.assertEqual(status, 404)
        self.assertRegex(headers["X-Request-Id"], r"[0-9a-f]{12}\Z")

    def test_a_good_read_leaves_no_line_but_an_action_and_a_miss_do(self):
        self.call("/api/health")
        _, headers, _ = self.call("/missing?token=do-not-log-this")
        status, speak_headers, _ = self.call("/api/speak", {"engine": "kokoro", "text": "a private line of dialogue"})
        self.assertEqual(status, 200)
        text = self.log_text(f"{speak_headers['X-Request-Id']} POST /api/speak 200")
        self.assertIn(f"{headers['X-Request-Id']} GET /missing 404", text)
        self.assertIn('error="not found"', text)
        self.assertIn("engine=kokoro", text)
        self.assertNotIn("/api/health", text)
        self.assertNotIn("do-not-log-this", text)
        self.assertNotIn("private line", text)

    def test_a_refused_request_logs_why_and_a_crash_logs_its_traceback(self):
        status, bad_headers, _ = self.call("/api/speak", {"engine": "kokoro", "text": "bad"})
        self.assertEqual(status, 400)
        status, crash_headers, _ = self.call("/api/speak", {"engine": "kokoro", "text": "explode"})
        self.assertEqual(status, 500)
        text = self.log_text(f"{crash_headers['X-Request-Id']}   | RuntimeError")
        self.assertIn(f'{bad_headers["X-Request-Id"]} POST /api/speak 400', text)
        self.assertIn('error="The text is not allowed."', text)
        self.assertIn(f'{crash_headers["X-Request-Id"]} POST /api/speak 500', text)
        self.assertIn("Log: out/logs/workers/kokoro/worker-1.log", text)
        self.assertIn(f"{crash_headers['X-Request-Id']}   | Traceback (most recent call last):", text)

    def test_a_client_that_stops_waiting_leaves_a_line_not_a_traceback(self):
        body = json.dumps({"engine": "kokoro", "text": "slow"}).encode()
        sock = socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=5)
        sock.sendall(b"POST /api/speak HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                     + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        time.sleep(0.1)  # the request has arrived and the engine is working on it
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close with a reset, as a browser tab does
        sock.close()
        text = self.log_text("the client closed the connection before the answer was sent")
        self.assertRegex(text, r"POST /api/speak \d+ [\d.]+s engine=kokoro error=\"the client closed")
        status, _, _ = self.call("/api/health")  # the service carries on
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
