"""Versioned-scene, resumable-batch, and scene-assembly acceptance tests."""

import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve
from cast_store import CastStore
from scene_store import SceneBatchRunner, SceneStore, segment_text
from worker_pool import read_catalog


def tone_wav(seconds=1.0, sample_rate=24_000, amplitude=0.1):
    time_axis = np.arange(round(seconds * sample_rate), dtype=np.float32) / sample_rate
    audio = amplitude * np.sin(2 * np.pi * 220 * time_axis)
    result = io.BytesIO()
    sf.write(result, audio, sample_rate, format="WAV", subtype="PCM_16")
    return result.getvalue()


class FakeEngines:
    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"
        self.calls = []
        self.fail_once_for = set()

    def speak(self, req):
        self.calls.append(dict(req))
        if req["text"] in self.fail_once_for:
            self.fail_once_for.remove(req["text"])
            raise RuntimeError("injected one-shot generation failure")
        time_axis = np.arange(12_000, dtype=np.float32) / 24_000
        audio = (0.1 * np.sin(2 * np.pi * 260 * time_axis)).astype(np.float32)
        return audio, 24_000, {"total_s": 0.5, "first_chunk_s": None, "mode": "clone",
                              "backend": "fake-qwen", "checkpoint": "Qwen/test", "checkpoint_revision": "test-revision"}


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-scenes-")
        self.cast = CastStore(self.temp.name)
        self.character = self.cast.create_character("Mira", "Warm and steady")
        self.identity = self.cast.create_identity(self.character["id"], "qwen3-tts-1.7b-base", "Mira Base")
        self.reference = self.cast.add_reference(self.identity["id"], "mira.wav", tone_wav(),
            transcript="A clear reference line.", origin="local test fixture", allowed_use="tests only",
            edits={"target_sample_rate": 24_000})
        self.cast.update_identity(self.identity["id"], {"status": "approved", "reference_version_id": self.reference["id"]})
        self.engines = FakeEngines()
        self.scenes = SceneStore(self.cast, self.engines.entries)
        self.runner = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        self.runner = SceneBatchRunner(self.scenes, f"http://127.0.0.1:{self.server.server_address[1]}")
        self.server.RequestHandlerClass = serve.make_handler(self.engines, self.server.server_address[1],
                                                              self.cast, self.scenes, self.runner)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        if self.runner:
            self.runner.close(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def scene_payload(self, lines=None, name="Test scene"):
        return {"schema_version": 1, "name": name, "lines": lines or [{
            "line_id": "line-001", "line_type": "dialogue", "character_id": self.character["id"],
            "identity_id": self.identity["id"], "reference_version_id": self.reference["id"],
            "text": "The road is clear.", "language": "English", "direction": "",
            "pause_before_ms": 0, "pause_after_ms": 75, "take_count": 1,
            "settings": {"seed": 42}, "use_pronunciations": False,
        }]}

    def request(self, path, body=None, raw=None, method=None):
        data = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data,
            headers={"Content-Type": "application/json"} if data is not None else {}, method=method)
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read()
            try:
                parsed = json.loads(payload.decode("utf-8")) if "json" in response.headers.get("Content-Type", "") else payload
            except json.JSONDecodeError:
                parsed = payload
            return response.status, parsed, response.headers

    def wait_job(self, job_id, expected, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.scenes.get_job(job_id)
            if job["status"] in expected:
                return job
            time.sleep(0.025)
        self.fail(f"Batch job {job_id} did not reach {expected}: {self.scenes.get_job(job_id)['status']}")

    def test_scene_schema_rejects_bad_ids_characters_languages_and_text_before_generation(self):
        invalid = [
            self.scene_payload(lines=[self.scene_payload()["lines"][0], self.scene_payload()["lines"][0]]),
            self.scene_payload(lines=[{**self.scene_payload()["lines"][0], "identity_id": "0" * 32}]),
            self.scene_payload(lines=[{**self.scene_payload()["lines"][0], "language": "Klingon"}]),
            self.scene_payload(lines=[{**self.scene_payload()["lines"][0], "text": "x" * 10_001}]),
        ]
        for payload in invalid:
            with self.subTest(line=payload["lines"][0]):
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    self.request("/api/scenes", payload)
                self.assertEqual(rejected.exception.code, 400)
        self.assertEqual(self.engines.calls, [])

    def test_json_csv_round_trip_and_checksums_keep_unicode_order_and_scene_version(self):
        payload = self.scene_payload(lines=[
            {**self.scene_payload()["lines"][0], "line_id": "open", "text": "Élodie, wait!", "pause_after_ms": 100},
            {**self.scene_payload()["lines"][0], "line_id": "reply", "text": "I'm here.", "line_type": "narration"},
        ])
        status, result, _ = self.request("/api/scenes", payload)
        self.assertEqual(status, 201)
        scene = result["scene"]
        self.assertEqual([line["line_id"] for line in scene["lines"]], ["open", "reply"])
        self.assertEqual(scene["checksum"], self.scenes.get_scene(scene["scene_id"])["checksum"])
        self.assertTrue((self.scenes.manifest_root / f"{scene['scene_id']}.json").is_file())
        _, imported_json, _ = self.request(f"/api/scenes/{scene['scene_id']}/export.json")
        self.assertEqual(imported_json["checksum"], scene["checksum"])
        _, csv_bytes, _ = self.request(f"/api/scenes/{scene['scene_id']}/export.csv")
        csv_payload = self.scenes.import_csv(csv_bytes)
        csv_scene = self.scenes.create_scene(csv_payload)
        self.assertEqual([line["text"] for line in csv_scene["lines"]], ["Élodie, wait!", "I'm here."])
        self.assertEqual([line["line_id"] for line in csv_scene["lines"]], ["open", "reply"])
        changed = dict(imported_json)
        changed["lines"] = [dict(line, pause_after_ms=250) if line["line_id"] == "open" else line for line in changed["lines"]]
        updated = self.scenes.update_scene(scene["scene_id"], changed, expected_revision=1)
        self.assertEqual(updated["revision"], 2)
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            self.scenes.update_scene(scene["scene_id"], changed, expected_revision=1)

    def test_restart_retries_inflight_row_by_idempotency_key_without_regenerating_audio(self):
        scene = self.scenes.create_scene(self.scene_payload())
        job = self.scenes.create_job(scene["scene_id"])
        self.scenes.start_queued_job(job["id"])
        row_id = self.scenes.claim_next_row(job["id"])
        row = self.scenes.get_job_row(row_id)
        request = {"identity_id": row["identity_id"], "reference_version_id": row["reference_version_id"],
            "line_id": row["cast_line_id"], "text": row["text"], "language": row["language"],
            "direction": row["direction"], "settings": row["settings"],
            "use_pronunciations": row["use_pronunciations"], "idempotency_key": row["request_key"]}
        _, wav, headers = self.request("/api/performance", request)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(len(self.engines.calls), 1)
        recovered = SceneStore(self.cast, self.engines.entries)
        self.assertEqual(recovered.get_job(job["id"])["status"], "running")
        self.runner.close(timeout=5)
        recovered.recover_interrupted_jobs()
        self.assertEqual(recovered.get_job(job["id"])["status"], "queued")
        self.runner = SceneBatchRunner(recovered, self.base)
        self.runner.start()
        completed = self.wait_job(job["id"], {"completed"})
        self.assertEqual(completed["completed"], 1)
        self.assertEqual(completed["rows"][0]["take_id"], headers["X-Take-Id"])
        self.assertEqual(len(self.engines.calls), 1, "recovered performance must replay its persisted take")
        valid, detail = recovered.verify_job_manifest(job["id"])
        self.assertTrue(valid, detail)

    def test_retired_take_and_voice_are_refused_by_scene_selection_assembly_and_rendering(self):
        scene = self.scenes.create_scene(self.scene_payload())
        job = self.scenes.create_job(scene["scene_id"])
        self.runner.start()
        completed = self.wait_job(job["id"], {"completed"})
        take_id = completed["rows"][0]["take_id"]
        self.scenes.select_take(scene["scene_id"], "line-001", take_id)
        self.cast.retire("take", take_id, reason="Bad take.")
        with self.assertRaisesRegex(ValueError, "retired"):
            self.scenes.assemble_preview(scene["scene_id"])
        with self.assertRaisesRegex(ValueError, "retired"):
            self.scenes.select_take(scene["scene_id"], "line-001", take_id)
        self.assertEqual(self.scenes.get_job(job["id"])["rows"][0]["retirement"]["reason"], "Bad take.")
        self.cast.restore("take", take_id)
        self.cast.retire("identity", self.identity["id"], reason="Permission withdrawn.")
        with self.assertRaisesRegex(ValueError, "retired"):
            self.scenes.create_job(scene["scene_id"])

    def test_failed_rows_retry_and_regeneration_is_limited_to_requested_line_ids(self):
        lines = [dict(self.scene_payload()["lines"][0], line_id=f"line-{index:03d}", text=f"Utterance {index}.")
                 for index in range(1, 4)]
        self.engines.fail_once_for.add("Utterance 2.")
        scene = self.scenes.create_scene(self.scene_payload(lines=lines))
        job = self.scenes.create_job(scene["scene_id"])
        self.runner.start()
        failed = self.wait_job(job["id"], {"failed"})
        self.assertEqual((failed["completed"], failed["failed"]), (2, 1))
        self.scenes.retry_failed(job["id"])
        self.runner.wake()
        retried = self.wait_job(job["id"], {"completed"})
        self.assertEqual(retried["completed"], 3)
        self.assertEqual(retried["failed"], 0)
        self.assertEqual({row["waveform_checks"]["status"] for row in retried["rows"]}, {"checked"})
        second_job = self.scenes.create_job(scene["scene_id"], ["line-002"])
        self.runner.wake()
        regenerated = self.wait_job(second_job["id"], {"completed"})
        self.assertEqual(regenerated["total"], 1)
        self.assertEqual(regenerated["rows"][0]["line_id"], "line-002")
        self.assertNotEqual(regenerated["rows"][0]["take_id"], retried["rows"][1]["take_id"])
        self.assertEqual(len(self.engines.calls), 5)

    def test_a_batch_renders_one_models_lines_before_the_next_and_assembles_in_script_order(self):
        # Correction 1 of the plan: create_job sorts a batch by model, then reference, then script order, so one model renders
        # all of its lines before the next model loads. Without the sort these lines would alternate between the two models.
        other = self.cast.create_identity(self.character["id"], "turbo", "Mira Turbo")
        reference = self.cast.add_reference(other["id"], "mira-turbo.wav", tone_wav(), transcript="A clear reference line.",
                                            origin="local test fixture", allowed_use="tests only",
                                            edits={"target_sample_rate": 24_000})
        self.cast.update_identity(other["id"], {"status": "approved", "reference_version_id": reference["id"]})
        base = self.scene_payload()["lines"][0]
        lines = []
        for index in range(1, 7):
            turbo = index % 2 == 0
            lines.append(dict(base, line_id=f"line-{index:03d}", text=f"Line number {index}.",
                              identity_id=other["id"] if turbo else self.identity["id"],
                              reference_version_id=reference["id"] if turbo else self.reference["id"],
                              settings={} if turbo else base["settings"]))    # Turbo has no seed
        scene = self.scenes.create_scene(self.scene_payload(lines=lines))
        job = self.scenes.create_job(scene["scene_id"])
        self.assertEqual([row["engine_id"] for row in job["rows"]], ["qwen3-tts-1.7b-base"] * 3 + ["turbo"] * 3)
        self.assertEqual([part["engine_id"] for part in job["models"]], ["qwen3-tts-1.7b-base", "turbo"])
        self.assertEqual([(part["total"], part["done"]) for part in job["models"]], [(3, 0), (3, 0)])
        self.runner.start()
        completed = self.wait_job(job["id"], {"completed"})
        self.assertEqual([call["engine"] for call in self.engines.calls], ["qwen3-tts-1.7b-base"] * 3 + ["turbo"] * 3)
        self.assertEqual([call["text"] for call in self.engines.calls],
                         ["Line number 1.", "Line number 3.", "Line number 5.",
                          "Line number 2.", "Line number 4.", "Line number 6."])
        self.assertEqual([(part["total"], part["done"]) for part in completed["models"]], [(3, 3), (3, 3)])
        for row in completed["rows"]:
            self.scenes.select_take(scene["scene_id"], row["line_id"], row["take_id"], row["segment_id"])
        preview = self.scenes.assemble_preview(scene["scene_id"])
        self.assertEqual([entry["line_id"] for entry in preview["lines"]], [line["line_id"] for line in lines])

    def test_cancel_queued_job_marks_every_pending_row_cancelled_without_generation(self):
        lines = [dict(self.scene_payload()["lines"][0], line_id=f"cancel-{index}", text=f"Do not render {index}.")
                 for index in range(3)]
        scene = self.scenes.create_scene(self.scene_payload(lines=lines))
        _, created, _ = self.request(f"/api/scenes/{scene['scene_id']}/jobs", {})
        job_id = created["job"]["id"]
        _, cancelled, _ = self.request(f"/api/scenes/jobs/{job_id}/cancel", {})
        self.assertEqual(cancelled["job"]["status"], "cancelled")
        self.assertEqual(cancelled["job"]["cancelled"], 3)
        self.assertEqual(self.engines.calls, [])

    def test_take_selection_assembly_and_long_text_preview_preserve_segment_order(self):
        long_line = ("Dr. Vale paused. [laugh] " + "quiet words, " * 150).strip()
        self.cast.save_pronunciation(character_id=self.character["id"], source="quiet words", replacement="kwy-et words")
        segments = segment_text(long_line)
        self.assertGreater(len(segments), 1)
        self.assertTrue(any("Dr. Vale" in segment for segment in segments))
        self.assertTrue(any("[laugh]" in segment for segment in segments))
        self.assertTrue(all(len(segment) <= 1200 for segment in segments))
        scene = self.scenes.create_scene(self.scene_payload(lines=[
            dict(self.scene_payload()["lines"][0], line_id="long", text=long_line, pause_before_ms=50, pause_after_ms=50,
                 use_pronunciations=True),
            dict(self.scene_payload()["lines"][0], line_id="short", text="After the pause.", pause_before_ms=100, pause_after_ms=0),
        ]))
        self.assertGreater(len(scene["lines"][0]["segments"]), 1)
        for left, right in zip(scene["lines"][0]["segments"], scene["lines"][0]["segments"][1:]):
            self.assertFalse(left["text"].endswith("quiet") and right["text"].startswith("words"),
                             "segmentation must keep a dictionary pronunciation span together")
        job = self.scenes.create_job(scene["scene_id"])
        self.runner.start()
        completed = self.wait_job(job["id"], {"completed"})
        for row in completed["rows"]:
            self.scenes.select_take(scene["scene_id"], row["line_id"], row["take_id"], row["segment_id"])
        preview = self.scenes.assemble_preview(scene["scene_id"])
        self.assertGreater(preview["duration_seconds"], len(completed["rows"]) * 0.5)
        self.assertEqual([entry["line_id"] for entry in preview["lines"]],
                         [row["line_id"] for row in completed["rows"]])
        expected_duration = (sum(entry["duration_seconds"] for entry in preview["lines"]) +
                             sum(line["pause_before_ms"] + line["pause_after_ms"] for line in scene["lines"]) / 1000)
        self.assertAlmostEqual(preview["duration_seconds"], expected_duration, delta=1 / 24_000,
                               msg="line pauses apply once per line, not again between speech segments")
        self.assertTrue(Path(preview["audio_path"]).is_file())
        self.assertTrue(Path(preview["mastered_audio_path"]).is_file())
        self.assertEqual(sf.info(preview["audio_path"]).samplerate, 24_000)
        mastered = sf.read(preview["mastered_audio_path"], dtype="float32")[0]
        self.assertLessEqual(float(np.max(np.abs(mastered))), 10 ** (-1.0 / 20.0) + 1e-4)
        self.assertEqual(preview["mastering"]["peak_ceiling_dbfs"], -1.0)
        before = self.scenes.get_scene(scene["scene_id"])
        before_first_segment = before["lines"][0]["selected_takes"]["seg-001"]
        failing_segment = before["lines"][0]["segments"][1]
        self.engines.fail_once_for.add(self.cast.apply_pronunciations(failing_segment["text"], self.character["id"]))
        segment_job = self.scenes.create_job(scene["scene_id"], segment_ids=["long/seg-002"])
        self.assertEqual(segment_job["total"], 1, "segment regeneration must leave other segments untouched")
        self.runner.wake()
        failed = self.wait_job(segment_job["id"], {"failed"})
        self.assertEqual((failed["failed"], failed["total"]), (1, 1))
        with self.assertRaisesRegex(ValueError, "failed row"):
            self.scenes.assemble_preview(scene["scene_id"])
        self.scenes.retry_failed(segment_job["id"])
        self.runner.wake()
        retried = self.wait_job(segment_job["id"], {"completed"})
        new_take = retried["rows"][0]["take_id"]
        self.scenes.select_take(scene["scene_id"], "long", new_take, "seg-002")
        after = self.scenes.get_scene(scene["scene_id"])
        self.assertEqual(after["lines"][0]["selected_takes"]["seg-001"], before_first_segment)
        self.assertEqual(after["lines"][0]["selected_takes"]["seg-002"], new_take)
        self.scenes.assemble_preview(scene["scene_id"])


if __name__ == "__main__":
    unittest.main()
