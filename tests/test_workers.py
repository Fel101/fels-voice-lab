"""Worker contract and supervision checks; all scheduling checks use a tiny fake worker."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve
from library_fixture import temp_cast
from worker_pool import EnginePool, process_is_running, read_catalog
from worker_process import WorkerProcess, WorkerTimeout, WorkerUnavailable
from worker_protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, ProtocolError, decode_message, encode_message, request

FAKE_WORKER = ROOT / "tests" / "fixtures" / "fake_worker.py"


class WorkerTests(unittest.TestCase):
    def new_fake_worker(self, engine="kokoro", *, delay=0, marker=None, timeouts=None):
        log_path = Path(tempfile.gettempdir()) / "voice-lab-worker-tests" / f"{engine}-{os.getpid()}-{time.time_ns()}.log"
        command = [sys.executable, "-u", str(FAKE_WORKER), "--engine", engine, "--delay", str(delay)]
        if marker:
            command.extend(["--started-marker", str(marker)])
        return WorkerProcess(command, log_path, cwd=ROOT, timeouts=timeouts)

    def pool(self, factory):
        return EnginePool("cpu", 0, worker_factory=factory, idle_poll_seconds=0.1)

    def test_catalog_and_protocol_are_versioned_and_bounded(self):
        catalog = read_catalog()
        self.assertEqual(catalog["worker_protocol_version"], PROTOCOL_VERSION)
        entries = {entry["id"]: entry for entry in catalog["engines"]}
        self.assertTrue(all(entry.get("local_revision") and entry.get("backend") and entry.get("precision")
                            for entry in catalog["engines"]))
        self.assertTrue(all(entries[key]["integration"]["service_api"] for key in
                            ("kokoro", "turbo", "orig", "breeze-tts-2")))
        self.assertTrue(all(entries[key]["integration"]["service_api"] for key in
                            ("qwen3-tts-1.7b-voice-design", "qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base",
                             "qwen3-tts-1.7b-custom-voice")))
        self.assertTrue(all(entries[key]["backend"]["comparison_runtime"]["service_api"] is False for key in
                            ("qwen3-tts-1.7b-voice-design", "qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base",
                             "qwen3-tts-1.7b-custom-voice")))
        message = request("test-1", "health")
        self.assertEqual(decode_message(encode_message(message))["id"], "test-1")
        with self.assertRaises(ProtocolError):
            encode_message({"text": "x" * MAX_MESSAGE_BYTES})

    def test_worker_lifecycle_audio_handoff_and_owned_process_shutdown(self):
        worker = self.new_fake_worker()
        worker.start()
        pid = worker.pid
        try:
            self.assertFalse(worker.call("health")["model_loaded"])
            self.assertTrue(worker.call("load")["model_loaded"])
            runtime_pid = worker.call("health")["pid"]
            self.assertTrue(process_is_running(runtime_pid))
            prepared = worker.call("prepare_reference", path="reference/fixture.wav")
            self.assertTrue(prepared["prepared"])
            output = ROOT / "out" / "workers" / f"kokoro-{pid}" / "test-lifecycle.npy"
            result = worker.call("generate", text="hello", output_file=str(output))
            samples = np.load(result["audio_file"], allow_pickle=False)
            self.assertEqual(samples.shape, (4800,))
            self.assertEqual(result["sample_rate"], 24000)
            released = worker.call("unload")
            self.assertGreater(released["allocated_bytes_released"], 0)
            output.unlink(missing_ok=True)
            try:
                output.parent.rmdir()  # the folder is this test's own, so it does not pile up in out/workers
            except OSError:
                pass
        finally:
            worker.close(force=False)
        self.assertIsNotNone(worker.returncode)
        self.assertFalse(worker.alive)
        self.assertFalse(process_is_running(runtime_pid))

    def test_busy_http_request_is_rejected_without_queueing(self):
        marker = Path(tempfile.gettempdir()) / f"voice-lab-busy-{os.getpid()}-{time.time_ns()}.started"
        workers = []

        def factory(entry):
            worker = self.new_fake_worker(entry["id"], delay=1.0, marker=marker)
            workers.append(worker)
            return worker

        pool = self.pool(factory)
        server = serve.LocalThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        server.RequestHandlerClass = serve.make_handler(pool, server.server_address[1], temp_cast(self))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/api/speak"
        first_result = {}

        def first_request():
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps({"engine": "kokoro", "text": "slow"}).encode(),
                                                                   headers={"Content-Type": "application/json"}), timeout=20) as response:
                    first_result["status"] = response.status
                    first_result["body"] = response.read()
            except Exception as exc:
                first_result["error"] = exc

        request_thread = threading.Thread(target=first_request, daemon=True)
        request_thread.start()
        deadline = time.time() + 15
        while time.time() < deadline and not marker.exists():
            time.sleep(0.02)
        self.assertTrue(marker.exists(), "fake worker never entered generate")
        try:
            request = urllib.request.Request(url, data=json.dumps({"engine": "kokoro", "text": "second"}).encode(),
                                             headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(rejected.exception.code, 409)
            self.assertIn("busy", rejected.exception.read().decode("utf-8").lower())
            oversized = urllib.request.Request(url, data=b'{"text":"' + b"x" * 65536 + b'"}',
                                                headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as body_limit:
                urllib.request.urlopen(oversized, timeout=3)
            self.assertEqual(body_limit.exception.code, 413)
        finally:
            request_thread.join(timeout=20)
            server.shutdown()
            server.server_close()
            pool.close()
            marker.unlink(missing_ok=True)
        self.assertNotIn("error", first_result, repr(first_result.get("error")))
        self.assertEqual(first_result.get("status"), 200)
        self.assertTrue(first_result["body"].startswith(b"RIFF"))
        self.assertTrue(workers)
        self.assertIsNotNone(workers[0].returncode)

    def test_worker_crash_restarts_on_next_request_and_broker_survives(self):
        workers = []

        def factory(entry):
            worker = self.new_fake_worker(entry["id"])
            workers.append(worker)
            return worker

        pool = self.pool(factory)
        try:
            first = pool.load("kokoro")
            with self.assertRaises(WorkerUnavailable):
                pool.speak({"engine": "kokoro", "text": "__crash__"})
            self.assertEqual(pool.loaded, [])
            self.assertFalse(process_is_running(first["worker_pid"]))
            self.assertEqual(pool.health()["status"], "ready")
            wav, sample_rate, info = pool.speak({"engine": "kokoro", "text": "recovered"})
            self.assertEqual(sample_rate, 24000)
            self.assertTrue(np.isfinite(wav).all())
            self.assertGreater(info["total_s"], 0)
            self.assertEqual(len(workers), 2)
            self.assertIsNotNone(workers[0].returncode)
            self.assertTrue(workers[1].alive)
        finally:
            pool.close()
        self.assertIsNotNone(workers[1].returncode)

    def test_a_clip_past_full_scale_comes_back_with_headroom(self):
        pool = self.pool(lambda entry: self.new_fake_worker(entry["id"]))
        try:
            wav, _rate, _info = pool.speak({"engine": "kokoro", "text": "__past_full_scale__"})
            self.assertAlmostEqual(float(np.max(np.abs(wav))), 10 ** (-1 / 20), places=5)
            wav, _rate, _info = pool.speak({"engine": "kokoro", "text": "an ordinary clip"})
            self.assertAlmostEqual(float(np.max(np.abs(wav))), 0.25, places=6)
        finally:
            pool.close()

    def test_timeout_stops_only_the_worker_that_missed_its_deadline(self):
        workers = []

        def factory(entry):
            key = entry["id"]
            worker = self.new_fake_worker(key, delay=3 if key == "kokoro" else 0,
                                          timeouts={"generate": 0.25})
            workers.append(worker)
            return worker

        pool = self.pool(factory)
        unrelated = self.new_fake_worker("turbo")
        unrelated.start()
        unrelated_load = unrelated.call("load")
        try:
            timed_out_load = pool.load("kokoro")
            with self.assertRaises(WorkerTimeout) as timed_out:
                pool.speak({"engine": "kokoro", "text": "slow timeout"})
            self.assertIn("timed out", str(timed_out.exception).lower())
            self.assertFalse(workers[-1].alive)
            self.assertTrue(unrelated.alive)
            self.assertFalse(process_is_running(timed_out_load["worker_pid"]))
            self.assertTrue(process_is_running(unrelated_load["pid"]))
            self.assertTrue(pool.load("turbo")["loaded"])
        finally:
            pool.close()
            unrelated.close()
        self.assertTrue(all(worker.returncode is not None for worker in workers))

    def test_unload_and_parent_close_stop_only_owned_workers(self):
        workers = []

        def factory(entry):
            worker = self.new_fake_worker(entry["id"])
            workers.append(worker)
            return worker

        pool = self.pool(factory)
        kokoro = pool.load("kokoro")
        turbo = pool.load("turbo")
        self.assertFalse(workers[0].alive, "engine switching evicts the previous resident worker")
        pids = {worker.pid for worker in workers}
        self.assertEqual(len(pids), 2)
        pool.unload()
        self.assertEqual(pool.loaded, [])
        self.assertTrue(all(worker.returncode is not None for worker in workers))
        self.assertFalse(process_is_running(kokoro["worker_pid"]))
        self.assertFalse(process_is_running(turbo["worker_pid"]))
        releases = pool.health()["last_unload"]
        self.assertEqual(len(releases), 1)
        self.assertTrue(all((item["memory_release"] or {}).get("allocated_bytes_released", 0) > 0
                            for item in releases))

        second_workers = []

        def second_factory(entry):
            worker = self.new_fake_worker(entry["id"])
            second_workers.append(worker)
            return worker

        parent_pool = self.pool(second_factory)
        parent_worker = parent_pool.load("kokoro")
        owned = second_workers[0]
        parent_pool.close()
        self.assertIsNotNone(owned.returncode)
        self.assertFalse(process_is_running(parent_worker["worker_pid"]))
        # An unrelated process is never part of the pool's process handles.
        self.assertEqual(len(pids), 2)

    def test_parent_close_interrupts_an_inflight_owned_worker(self):
        marker = Path(tempfile.gettempdir()) / f"voice-lab-shutdown-{os.getpid()}-{time.time_ns()}.started"
        workers = []

        def factory(entry):
            worker = self.new_fake_worker(entry["id"], delay=60, marker=marker,
                                          timeouts={"generate": 90})
            workers.append(worker)
            return worker

        pool = self.pool(factory)
        result = {}

        def request_generation():
            try:
                pool.speak({"engine": "kokoro", "text": "hold until parent exits"})
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=request_generation, daemon=True)
        thread.start()
        runtime_pid = None
        try:
            deadline = time.time() + 15
            while time.time() < deadline and not marker.exists():
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "fake worker never entered generate")
            runtime_pid = workers[0].runtime_pid
        finally:
            pool.close()
            thread.join(timeout=10)
            marker.unlink(missing_ok=True)
        self.assertFalse(thread.is_alive(), "request thread did not return after owned worker shutdown")
        self.assertIn("error", result)
        self.assertIsNotNone(workers[0].returncode)
        self.assertFalse(process_is_running(runtime_pid))


if __name__ == "__main__":
    unittest.main()
