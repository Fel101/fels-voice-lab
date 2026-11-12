"""Breeze service request and profile lifecycle checks without a GPU model load."""

import os
import json
import re
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from breeze_worker import OUT_OF_MEMORY, BreezeWorker, _integer, _number
import serve
from cast_store import CastStore
from library_fixture import temp_cast
from worker_pool import EnginePool
from worker_process import WorkerProcess
from tests.audio_fixtures import owned_reference

FAKE_WORKER = ROOT / "tests" / "fixtures" / "fake_worker.py"


class BreezeContractTests(unittest.TestCase):
    def new_worker(self, entry):
        log = Path(tempfile.gettempdir()) / "voice-lab-breeze-tests" / f"{os.getpid()}-{time.time_ns()}.log"
        command = [sys.executable, "-u", str(FAKE_WORKER), "--engine", entry["id"]]
        return WorkerProcess(command, log, cwd=ROOT)

    def test_sampling_bounds_reject_invalid_values(self):
        self.assertEqual(_integer({"seed": 42}, "seed", 1, 1, 100), 42)
        self.assertAlmostEqual(_number({"top_p": 0.8}, "top_p", 1, 0.01, 1), 0.8)
        for params, name in [({"seed": 0}, "seed"), ({"seed": 4.5}, "seed"),
                             ({"temperature": float("nan")}, "temperature"),
                             ({"top_p": 1.1}, "top_p")]:
            with self.assertRaises(ValueError):
                if name == "seed":
                    _integer(params, name, 1, 1, 100)
                else:
                    _number(params, name, 1, 0.01, 1)

    def test_a_line_that_runs_the_card_out_of_memory_says_to_split_it(self):
        class Module:
            def __init__(self, error):
                self.error = error

            def design_speech(self, *_args, **_kwargs):
                raise self.error

        worker = BreezeWorker("cuda")
        worker.bundle, worker.profile = object(), "eager"
        request = {"text": "A long line.", "voice_description": "A calm voice."}
        worker.module = Module(RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB (GPU 0; 7.96 GiB total)"))
        with self.assertRaises(RuntimeError) as caught:
            worker.generate(request)
        self.assertEqual(str(caught.exception), OUT_OF_MEMORY)
        self.assertIn("split it into shorter lines", OUT_OF_MEMORY)
        worker.module = Module(RuntimeError("HIP error: invalid device function"))
        with self.assertRaisesRegex(RuntimeError, "^HIP error: invalid device function$"):
            worker.generate(request)

    def test_reference_paths_are_confined_to_local_reference_folder(self):
        sample = owned_reference(self, ROOT)
        self.assertEqual(BreezeWorker._reference_path(sample), sample.resolve())
        with self.assertRaisesRegex(ValueError, "under voice-lab/reference"):
            BreezeWorker._reference_path(ROOT / "README.md")

    def test_profile_change_restarts_only_the_breeze_worker(self):
        workers = []

        def factory(entry):
            worker = self.new_worker(entry)
            workers.append(worker)
            return worker

        pool = EnginePool("cpu", 0, worker_factory=factory, idle_poll_seconds=0.1)
        try:
            first = pool.load("breeze-tts-2", profile="eager")
            second = pool.load("breeze-tts-2", profile="cuda_graphs")
            self.assertEqual(first["profile"], "eager")
            self.assertEqual(second["profile"], "cuda_graphs")
            self.assertEqual(len(workers), 2)
            self.assertIsNotNone(workers[0].returncode)
            self.assertTrue(workers[1].alive)
            self.assertEqual(pool.health()["workers"][0]["profile"], "cuda_graphs")
        finally:
            pool.close()
        self.assertIsNotNone(workers[1].returncode)

    def test_clone_request_uses_only_local_reference_and_keeps_profile(self):
        workers = []

        def factory(entry):
            worker = self.new_worker(entry)
            workers.append(worker)
            return worker

        pool = EnginePool("cpu", 0, worker_factory=factory, idle_poll_seconds=0.1)
        try:
            wav, sample_rate, info = pool.speak({
                "engine": "breeze-tts-2",
                "mode": "clone",
                "profile": "eager",
                "text": "A new line from this voice.",
                "reference_path": str(owned_reference(self, ROOT)),
                "reference_text": "This is a local reference transcript.",
            })
            self.assertEqual(sample_rate, 24000)
            self.assertGreater(wav.size, 0)
            self.assertAlmostEqual(info["total_s"], 0.1)
            self.assertEqual(pool.health()["workers"][0]["profile"], "eager")
        finally:
            pool.close()
        self.assertIsNotNone(workers[0].returncode)

    def test_saved_voice_assets_are_local_and_never_overwritten(self):
        workers = []

        def factory(entry):
            worker = self.new_worker(entry)
            workers.append(worker)
            return worker

        # Saved voices go under the lab folder; this test uses a temporary one, so it never writes to the real data/voices.
        library = Path(tempfile.mkdtemp(prefix="voice-lab-breeze-"))
        self.addCleanup(shutil.rmtree, library, True)
        temporary_lab = patch.object(serve, "LAB", library)
        temporary_lab.start()
        self.addCleanup(temporary_lab.stop)
        pool = EnginePool("cpu", 0, worker_factory=factory, idle_poll_seconds=0.1)
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        server.RequestHandlerClass = serve.make_handler(pool, server.server_address[1], CastStore(library))
        thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
        thread.start()
        name = f"contract-test-{os.getpid()}-{time.time_ns()}"
        asset = library / "data" / "voices" / f"{name}.wav"
        url = f"http://127.0.0.1:{server.server_address[1]}/api/speak"
        payload = {"engine": "breeze-tts-2", "mode": "design", "profile": "eager",
                   "text": "A local character voice test.", "voice_description": "A calm voice.",
                   "save_voice_as": name}
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read()
                self.assertEqual(Path(response.headers["X-Voice-Asset-Path"]), asset.resolve())
                self.assertTrue(body.startswith(b"RIFF"))
            self.assertTrue(asset.is_file())
            reference_request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/references")
            with urllib.request.urlopen(reference_request, timeout=10) as response:
                listed = json.loads(response.read().decode("utf-8"))["references"]
            self.assertTrue(any(item["path"] == str(asset.resolve()) for item in listed))
            collision = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                               headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(collision, timeout=10)
            self.assertEqual(rejected.exception.code, 409)
            unsafe = {**payload, "save_voice_as": "../escape"}
            bad = urllib.request.Request(url, data=json.dumps(unsafe).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as rejected_path:
                urllib.request.urlopen(bad, timeout=10)
            self.assertEqual(rejected_path.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            pool.close()
            thread.join(timeout=3)
        self.assertIsNotNone(workers[0].returncode)

    def test_studio_page_exposes_breeze_character_controls(self):
        pool = EnginePool("cpu", 0, worker_factory=self.new_worker, idle_poll_seconds=0.1)
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        server.RequestHandlerClass = serve.make_handler(pool, server.server_address[1], temp_cast(self))
        thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(f"{base}/", timeout=10) as response:
                page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
            # Controls and headers live in the scripts the page loads, so read each one as served.
            for name in re.findall(r'<script[^>]*\ssrc="/([^"]+)"', page):
                with urllib.request.urlopen(f"{base}/{name}", timeout=10) as response:
                    page += "\n" + response.read().decode("utf-8")
            for control in ("voiceDescription", "referenceText", "direction", "profile", "seed", "cfgScale",
                            "depthTemperature", "depthTopK", "depthTopP", "saveVoice", "X-Voice-Asset-Path"):
                self.assertIn(control, page)
        finally:
            server.shutdown()
            server.server_close()
            pool.close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
