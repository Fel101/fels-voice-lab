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



class QueueTests(unittest.TestCase):
    """The line for the model worker over HTTP, with stand-in workers that take `delay` seconds to make a clip: waiting in
    order, the 409 for a caller that does not wait, cancelling while waiting, and stopping a running clip, after which the next
    request loads the model again. Then a scene batch with a person's take in the middle of it, and a cancel that stops the
    line the batch is rendering."""

    def setUp(self):
        self.delays = []        # the delay of each new worker, in order; 0 once the list is used up
        self.workers = []
        self.marker = Path(tempfile.gettempdir()) / f"voice-lab-queue-{os.getpid()}-{time.time_ns()}.started"
        self.addCleanup(self.marker.unlink, missing_ok=True)

        def factory(entry):
            delay = self.delays.pop(0) if self.delays else 0
            log_path = Path(tempfile.gettempdir()) / "voice-lab-worker-tests" / f"queue-{os.getpid()}-{time.time_ns()}.log"
            worker = WorkerProcess([sys.executable, "-u", str(FAKE_WORKER), "--engine", entry["id"], "--delay", str(delay),
                                    "--started-marker", str(self.marker)], log_path, cwd=ROOT, timeouts={"generate": 120})
            self.workers.append(worker)
            return worker

        self.pool = EnginePool("cpu", 0, worker_factory=factory, idle_poll_seconds=0.1)
        self.addCleanup(self.pool.close)

    def serve(self, cast=None, scenes=None):
        """The service's address, and its scene batch runner when there are scenes."""
        from scene_store import SceneBatchRunner

        server = serve.LocalThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        runner = SceneBatchRunner(scenes, base) if scenes is not None else None
        server.RequestHandlerClass = serve.make_handler(self.pool, port, cast or temp_cast(self), scenes, runner)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            if runner is not None:
                runner.close(timeout=20)
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)

        self.addCleanup(stop)
        return (base, runner) if scenes is not None else base

    @staticmethod
    def call(base, path, body=None, request_id=None, timeout=60):
        headers = {"Content-Type": "application/json"}
        if request_id:
            headers["X-Request-Id"] = request_id
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, (raw if raw.startswith(b"RIFF") else json.loads(raw))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def in_thread(self, base, path, body, request_id):
        result = {}

        def run():
            started = time.monotonic()
            result["status"], result["body"] = self.call(base, path, body, request_id)
            result["seconds"] = time.monotonic() - started

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result

    def wait_for(self, condition, what, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}")

    def queue(self, base):
        return self.call(base, "/api/queue")[1]

    def test_a_request_that_asks_to_wait_runs_after_the_one_running_and_one_that_does_not_is_refused(self):
        self.delays = [1.5]
        base = self.serve()
        first, first_result = self.in_thread(base, "/api/speak", {"engine": "kokoro", "text": "slow"}, "slow-a")
        self.wait_for(self.marker.exists, "the first clip to start")
        second, second_result = self.in_thread(base, "/api/speak", {"engine": "kokoro", "text": "next", "wait_seconds": 30},
                                               "wait-b")
        self.wait_for(lambda: [item["id"] for item in self.queue(base)["waiting"]] == ["wait-b"], "the second to wait")
        running = self.queue(base)["running"]
        self.assertEqual({key: running[key] for key in ("id", "what", "engine", "priority", "can_stop")},
                         {"id": "slow-a", "what": "Clip", "engine": "kokoro", "priority": "normal", "can_stop": True})
        status, body = self.call(base, "/api/speak", {"engine": "kokoro", "text": "now"}, "no-wait")
        self.assertEqual(status, 409)
        self.assertIn("busy", body["error"])
        first.join(30)
        second.join(30)
        self.assertEqual((first_result["status"], second_result["status"]), (200, 200))
        self.assertTrue(second_result["body"].startswith(b"RIFF"))
        self.assertEqual(self.queue(base), {"running": None, "waiting": [], "max_waiting": 20})
        self.assertEqual(self.call(base, "/api/speak", {"engine": "kokoro", "text": "x", "wait_seconds": -1})[0], 400)

    def test_a_waiting_request_can_be_cancelled_and_answers_at_once(self):
        self.delays = [3]
        base = self.serve()
        first, first_result = self.in_thread(base, "/api/speak", {"engine": "kokoro", "text": "slow"}, "slow-a")
        self.wait_for(self.marker.exists, "the first clip to start")
        second, second_result = self.in_thread(base, "/api/speak", {"engine": "kokoro", "text": "next", "wait_seconds": 30},
                                               "wait-b")
        self.wait_for(lambda: self.queue(base)["waiting"], "the second to wait")
        self.assertEqual(self.call(base, "/api/queue/wait-b/cancel", {}), (200, {"cancelled": "waiting"}))
        second.join(5)
        self.assertEqual(second_result["status"], 499)
        self.assertIn("did not run", second_result["body"]["error"])
        self.assertLess(second_result["seconds"], 3)
        first.join(30)
        self.assertEqual(first_result["status"], 200)
        self.assertEqual(self.call(base, "/api/queue/wait-b/cancel", {})[0], 404)

    def test_stopping_a_running_clip_ends_it_in_moments_and_the_next_request_loads_the_model_again(self):
        self.delays = [60]
        base = self.serve()
        first, first_result = self.in_thread(base, "/api/speak", {"engine": "kokoro", "text": "long"}, "long-a")
        self.wait_for(self.marker.exists, "the long clip to start")
        stopped_at = time.monotonic()
        self.assertEqual(self.call(base, "/api/queue/long-a/cancel", {}), (200, {"cancelled": "running"}))
        first.join(10)
        self.assertLess(time.monotonic() - stopped_at, 5)
        self.assertEqual(first_result["status"], 499)
        self.assertEqual(first_result["body"]["error"], "Stopped. The model will load again for the next request.")
        self.assertFalse(self.workers[0].alive)
        status, body = self.call(base, "/api/speak", {"engine": "kokoro", "text": "after"})
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"RIFF"))
        self.assertEqual(len(self.workers), 2)
        self.assertIsNone(self.queue(base)["running"])

    def scene_service(self, lines):
        """A service with scenes, one Kokoro voice (it needs no recording), and a saved scene of `lines` lines for it."""
        from scene_store import SceneStore

        cast = temp_cast(self)
        character = cast.create_character("Nell", "")
        identity = cast.create_identity(character["id"], "kokoro", "Warm", {"voice": "af_heart"})
        cast.update_identity(identity["id"], {"status": "approved"})
        root = Path(tempfile.mkdtemp(prefix="voice-lab-queue-scenes-"))
        scenes = SceneStore(cast, self.pool.entries, root=root)
        base, runner = self.serve(cast, scenes)
        scene = scenes.create_scene({"schema_version": 1, "name": "Queue", "lines": [
            {"line_id": f"line-{n}", "line_type": "dialogue", "identity_id": identity["id"], "text": f"Line {n} of the batch.",
             "language": "English", "pause_after_ms": 0, "take_count": 1, "settings": {}} for n in range(1, lines + 1)]})
        return cast, identity, scenes, runner, scene, base

    def test_a_take_asked_for_during_a_batch_comes_after_the_current_line_and_the_batch_carries_on(self):
        self.delays = [0.8]
        cast, identity, scenes, runner, scene, base = self.scene_service(4)
        status, body = self.call(base, f"/api/scenes/{scene['scene_id']}/jobs", {})
        self.assertEqual(status, 202, body)
        job_id = body["job"]["id"]
        runner.start()
        self.wait_for(self.marker.exists, "the batch's first line to start")
        person, person_result = self.in_thread(base, "/api/performance", {
            "identity_id": identity["id"], "line_id": "a-person-asks", "text": "A take in the middle of the batch.",
            "language": "English", "wait_seconds": 60}, "person-take")
        person.join(60)
        self.assertEqual(person_result["status"], 200, person_result["body"])
        self.wait_for(lambda: scenes.get_job(job_id)["status"] == "completed", "the batch to finish", timeout=60)
        takes = sorted(cast.list_takes(), key=lambda take: take["created_at"])
        order = [take["line_id"].rsplit(":", 1)[-1] for take in takes]
        self.assertEqual(order[1], "a-person-asks", order)  # after the line being rendered, before the batch's next
        self.assertEqual(len(order), 5)
        self.assertEqual(scenes.get_job(job_id)["completed"], 4)

    def test_cancelling_a_batch_can_stop_the_line_it_is_rendering(self):
        self.delays = [60]
        cast, identity, scenes, runner, scene, base = self.scene_service(3)
        job_id = self.call(base, f"/api/scenes/{scene['scene_id']}/jobs", {})[1]["job"]["id"]
        runner.start()
        self.wait_for(self.marker.exists, "the batch's first line to start")
        self.wait_for(lambda: self.queue(base)["running"] is not None, "the line to hold the worker")
        running = self.queue(base)["running"]
        self.assertEqual((running["what"], running["priority"]), ("Take for a scene batch", "low"))
        self.assertTrue(running["id"].startswith("scene-row-"))
        stopped_at = time.monotonic()
        status, body = self.call(base, f"/api/scenes/jobs/{job_id}/cancel", {"stop_current": True})
        self.assertEqual(status, 200, body)
        self.wait_for(lambda: scenes.get_job(job_id)["status"] == "cancelled", "the batch to be cancelled", timeout=15)
        self.assertLess(time.monotonic() - stopped_at, 10)
        rows = scenes.get_job(job_id)["rows"]
        self.assertEqual([row["status"] for row in rows], ["cancelled"] * 3)
        self.assertEqual(rows[0]["error"], "Stopped when the batch was cancelled.")
        self.assertEqual(cast.list_takes(), [])
        status, body = self.call(base, "/api/speak", {"engine": "kokoro", "text": "after"})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
