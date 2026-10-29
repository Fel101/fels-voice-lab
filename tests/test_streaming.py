"""Native-stream contracts; fake workers exercise transport and real adapter checks use a tiny generator."""

import json
from importlib.util import find_spec
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve
from cast_store import CastStore
from library_fixture import temp_cast
from qwen_worker import QwenWorker
from worker_pool import EnginePool, read_catalog
from worker_process import WorkerCancelled, WorkerProcess

FAKE_STREAM_WORKER = ROOT / "tests" / "fixtures" / "stream_worker.py"


class TinyStreamingModel:
    def generate_voice_design_streaming(self, **kwargs):
        assert kwargs["chunk_size"] == 8
        yield np.asarray([0.0, 0.1, 0.2], dtype=np.float32), 24000, {"chunk": 0}
        yield np.asarray([0.3, 0.4], dtype=np.float32), 24000, {"chunk": 1}
        yield np.asarray([0.5, 0.6, 0.7, 0.8], dtype=np.float32), 24000, {"chunk": 2}


class StreamingTests(unittest.TestCase):
    def new_worker(self, *, delay=0.04):
        log = Path(tempfile.gettempdir()) / "voice-lab-stream-tests" / f"worker-{os.getpid()}-{time.time_ns()}.log"
        return WorkerProcess([sys.executable, "-u", str(FAKE_STREAM_WORKER), "--delay", str(delay)],
                             log, cwd=ROOT, timeouts={"generate_stream": 10})

    def test_catalog_distinguishes_native_streaming_from_full_file_and_assembly(self):
        catalog = read_catalog()
        service = catalog["service_streaming"]
        self.assertEqual(service["endpoint"], "/api/stream")
        self.assertEqual(service["native_generator_engines"], [
            "qwen3-tts-1.7b-voice-design", "qwen3-tts-1.7b-base",
            "qwen3-tts-0.6b-base", "qwen3-tts-1.7b-custom-voice",
        ])
        self.assertEqual(service["sentence_at_a_time"]["status"], "not_implemented")
        self.assertEqual(service["full_file"]["endpoint"], "/api/speak")
        engines = {entry["id"]: entry for entry in catalog["engines"]}
        self.assertEqual(engines["qwen3-tts-1.7b-voice-design"]["capabilities"]["streaming"]["status"],
                         "real_gpu_smoke_verified")
        self.assertEqual(engines["qwen3-tts-1.7b-base"]["capabilities"]["streaming"]["status"],
                         "implemented_real_inference_pending")
        self.assertEqual(engines["kokoro"]["capabilities"]["streaming"]["api_output"], "complete_wav")

    @unittest.skipUnless(find_spec("torch"), "optional Torch runtime required for adapter seed controls")
    def test_adapter_forwards_exact_generator_samples_in_order(self):
        adapter = QwenWorker("qwen3-tts-1.7b-voice-design", "cuda")
        adapter.model = TinyStreamingModel()
        adapter.sample_rate = 24000
        adapter.runtime_identity = "fixture-runtime"
        adapter.supported_languages = ["English"]
        captured = []
        result = adapter.generate_stream({
            "text": "A short line.", "mode": "design", "language": "English",
            "voice_description": "A calm, clear voice.", "seed": 41,
        }, lambda audio, rate, info: captured.append((audio.copy(), rate, info)))
        restored = np.concatenate([row[0] for row in captured])
        np.testing.assert_array_equal(restored, np.arange(9, dtype=np.float32) / 10)
        self.assertEqual([row[1] for row in captured], [24000, 24000, 24000])
        self.assertEqual(result["backend_chunk_count"], 3)
        self.assertEqual(result["sample_count"], 9)
        self.assertEqual(result["streaming_kind"], "native_generator")
        self.assertGreaterEqual(result["generator_first_chunk_s"], 0)

    @unittest.skipUnless(find_spec("torch"), "optional Torch runtime required for adapter seed controls")
    def test_adapter_rejects_missing_native_stream_generator(self):
        adapter = QwenWorker("qwen3-tts-1.7b-voice-design", "cuda")
        adapter.model = object()
        with self.assertRaisesRegex(RuntimeError, "no VoiceDesign streaming generator"):
            adapter.generate_stream({"text": "hello", "mode": "design", "language": "English",
                                     "voice_description": "A voice"}, lambda *_: None)

    def test_worker_stream_orders_chunks_and_cancel_stops_owned_worker(self):
        worker = self.new_worker()
        worker.start()
        worker.call("load")
        packets = []
        result = worker.call_stream("generate_stream", packets.append, text="hello", mode="design")
        self.assertEqual([item["sequence"] for item in packets], [0, 1, 2])
        self.assertEqual([item["sample_start"] for item in packets], [0, 5, 10])
        self.assertEqual(b"".join(item["pcm"] for item in packets),
                         b"".join(struct.pack("<5h", base, base + 1, base + 2, base + 3, base + 4)
                                  for base in (100, 200, 300)))
        self.assertEqual(result["sample_count"], 15)
        self.assertEqual(result["chunk_count"], 3)
        worker.close(force=False)

        worker = self.new_worker(delay=0.25)
        worker.start()
        worker.call("load")
        received = []

        def cancel_after_first(packet):
            received.append(packet)
            return False

        try:
            with self.assertRaises(WorkerCancelled):
                worker.call_stream("generate_stream", cancel_after_first, text="interrupt me", mode="design")
            self.assertEqual(len(received), 1)
            self.assertFalse(worker.alive)
        finally:
            worker.close(force=True)

    def test_http_stream_delivers_audio_lines_before_complete_with_contiguous_offsets(self):
        workers = []

        def factory(_entry):
            worker = self.new_worker()
            workers.append(worker)
            return worker

        pool = EnginePool("cpu", 0, worker_factory=factory, idle_poll_seconds=0.1)
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        server.RequestHandlerClass = serve.make_handler(pool, server.server_address[1], temp_cast(self))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/stream",
            data=json.dumps({"engine": "qwen3-tts-1.7b-voice-design", "mode": "design",
                             "text": "An early line.", "voice_description": "A warm narrator."}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        events = []
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("X-Voice-Lab-Stream"), "native-generator")
                while True:
                    line = response.readline()
                    if not line:
                        break
                    events.append(json.loads(line))
            self.assertEqual(events[0]["type"], "start")
            audio = [event for event in events if event["type"] == "audio"]
            self.assertEqual([event["sequence"] for event in audio], [0, 1, 2])
            self.assertEqual([event["sample_start"] for event in audio], [0, 5, 10])
            self.assertEqual(events[-1]["type"], "complete")
            self.assertEqual(events[-1]["sample_count"], 15)
            self.assertLess(events[0]["service_first_byte_s"], events[-1]["service_total_s"])
            self.assertGreater(events[1]["generator_first_chunk_s"], 0)
        finally:
            server.shutdown()
            server.server_close()
            pool.close()
        self.assertTrue(workers)
        self.assertTrue(all(worker.returncode is not None for worker in workers))

    def test_http_voice_design_stream_saves_a_reusable_wav_inside_voice_root(self):
        workers = []

        def factory(_entry):
            worker = self.new_worker()
            workers.append(worker)
            return worker

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            pool = EnginePool("cpu", 0, worker_factory=factory, idle_poll_seconds=0.1)
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
            server.RequestHandlerClass = serve.make_handler(pool, server.server_address[1], CastStore(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/stream",
                data=json.dumps({"engine": "qwen3-tts-1.7b-voice-design", "mode": "design",
                                 "text": "Reusable voice.", "voice_description": "A warm voice.",
                                 "save_voice_as": "stream-test-voice"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            events = []
            try:
                with patch.object(serve, "LAB", root):
                    with urllib.request.urlopen(request, timeout=10) as response:
                        while True:
                            line = response.readline()
                            if not line:
                                break
                            events.append(json.loads(line))
                complete = events[-1]
                asset = root / "data" / "voices" / "stream-test-voice.wav"
                self.assertEqual(Path(complete["saved_asset"]), asset)
                self.assertTrue(asset.is_file())
                with wave.open(str(asset), "rb") as saved:
                    self.assertEqual((saved.getnchannels(), saved.getframerate(), saved.getnframes()), (1, 24000, 15))
                    self.assertEqual(saved.getsampwidth(), 2)
                    raw = saved.readframes(15)
                self.assertEqual(len(raw), 30)
                self.assertEqual(complete["sample_count"], 15)
            finally:
                server.shutdown()
                server.server_close()
                pool.close()
        self.assertTrue(workers)
        self.assertTrue(all(worker.returncode is not None for worker in workers))


if __name__ == "__main__":
    unittest.main()
