"""Regression checks for the Sol review, using isolated stores and owned fake workers."""

import copy
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import serve
from cast_store import CastStore
from evaluation_store import EvaluationStore
from local_ownership import LocalOwnership
from performance_settings import effective_settings
from scene_store import SceneStore, SceneBatchRunner, digest_json
from tools.scene_export import (ExportError, load_takes, trimmed_captions, verify_caption_sets,
                           write_subtitles)
from worker_pool import EnginePool, WorkerBusy, read_catalog, process_is_running
from worker_process import WorkerProcess, WorkerUnavailable


class FixtureEngines:
    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {row["id"]: row for row in self.catalog["engines"]}
        self.api_entries = self.entries
        self.device = "cpu"
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.busy = threading.Event()

    def speak(self, req):
        self.calls.append(dict(req))
        if self.busy.is_set():
            raise WorkerBusy("Fixture second service owns the model")
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("Fixture generation release timed out")
        return np.sin(np.arange(12000) * 0.03).astype(np.float32) * 0.1, 24000, {"total_s": 0.1}


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-review-fix-")
        self.root = Path(self.temp.name)
        self.cast = CastStore(self.root)
        self.engines = FixtureEngines()
        self.character = self.cast.create_character("Review")
        self.identity = self.new_identity("qwen3-tts-1.7b-base")
        self.scenes = SceneStore(self.cast, self.engines.entries)
        self.evals = EvaluationStore(self.root / "eval.sqlite3", lab_root=self.root)
        self.server = serve.LocalThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.runner = SceneBatchRunner(self.scenes, self.base)
        self.server.RequestHandlerClass = serve.make_handler(self.engines, self.server.server_address[1],
            self.cast, self.scenes, self.runner, self.evals)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.engines.release.set()
        self.runner.close(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def new_identity(self, engine, label="Candidate"):
        identity = self.cast.create_identity(self.character["id"], engine, label)
        if engine != "kokoro":
            raw = io.BytesIO()
            sf.write(raw, np.sin(np.arange(24000) * 0.03) * 0.1, 24000, format="WAV")
            ref = self.cast.add_reference(identity["id"], "fixture.wav", raw.getvalue(),
                transcript="A reference line.", origin="synthetic tone", allowed_use="tests only")
            self.cast.update_identity(identity["id"], {"status": "approved", "reference_version_id": ref["id"]})
        return self.cast.get_generation_identity(identity["id"])

    def post(self, body):
        request = urllib.request.Request(self.base + "/api/performance", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=8)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read(), dict(response.headers)

    def selected_scene(self, use_pronunciations=False):
        scene = self.scenes.create_scene({"name": "Review", "lines": [{"line_id": "line-001",
            "identity_id": self.identity["id"], "text": "Old dialogue.", "settings": {"seed": 42},
            "use_pronunciations": use_pronunciations}]})
        line = scene["lines"][0]
        status, wav, headers = self.post({"identity_id": self.identity["id"],
            "line_id": scene["scene_id"][:12] + ":line-001", "text": line["text"],
            "language": "English", "settings": line["settings"], "use_pronunciations": use_pronunciations})
        self.assertEqual(status, 200, wav)
        return self.scenes.select_take(scene["scene_id"], line["line_id"], headers["X-Take-Id"])

    def test_drama_clone_take_retains_cpu_execution_and_clone_mode(self):
        identity=self.new_identity('drama-3')
        speak=self.engines.speak
        def cpu_result(request):
            wav,rate,info=speak(request)
            return wav,rate,{**info,'backend':'speech.cpp-cpu','device':'cpu','mode':'clone',
                             'model_sha256':'pinned-fixture','effective_settings':request}
        with patch.object(self.engines,'speak',side_effect=cpu_result):
            status,body,headers=self.post({'identity_id':identity['id'],'line_id':'cpu-clone',
                                          'text':'Keep the gate closed.','settings':{'seed':0}})
        self.assertEqual(status,200,body)
        model=self.cast.get_take(headers['X-Take-Id'])['model']
        self.assertEqual(model['mode'],'clone')
        self.assertEqual(model['backend'],'speech.cpp-cpu')
        self.assertEqual(model['backend_result']['device'],'cpu')
        self.assertEqual(model['effective_settings']['seed'],0)

    def test_edits_clear_stale_selections_but_pauses_keep_them(self):
        scene = self.selected_scene()
        take_id = scene["lines"][0]["selected_take_id"]
        scene["lines"][0]["pause_after_ms"] = 300
        scene = self.scenes.update_scene(scene["scene_id"], scene)
        self.assertEqual(scene["lines"][0]["selected_take_id"], take_id)
        self.scenes.assemble_preview(scene["scene_id"])
        for field, value in (("text", "New dialogue."), ("direction", "Speak quietly."),
                             ("settings", {"seed": 43}), ("language", "French")):
            with self.subTest(field=field):
                changed = copy.deepcopy(scene)
                changed["lines"][0][field] = value
                updated = self.scenes.update_scene(scene["scene_id"], changed)
                self.assertEqual(updated["lines"][0]["selected_takes"], {})
                self.assertIsNone(updated["lines"][0]["selected_take_id"])
                with self.assertRaisesRegex(ValueError, "requires a selected"):
                    self.scenes.assemble_preview(scene["scene_id"])
                # Restore the valid original selection through the normal update path.
                scene = self.scenes.update_scene(scene["scene_id"], scene)
        self.assertEqual(self.cast.get_take(take_id)["text"], "Old dialogue.")

    def test_import_preview_and_export_reject_stale_or_foreign_line_takes(self):
        scene = self.selected_scene()
        scene["lines"][0]["text"] = "Mismatched dialogue."
        with self.assertRaisesRegex(ValueError, "stale|different scene"):
            self.scenes.create_scene(scene)
        scene.pop("checksum")
        scene["checksum"] = digest_json(scene)
        with self.scenes._connect() as db:
            db.execute("UPDATE scenes SET manifest_json=?,checksum=? WHERE id=?",
                       (json.dumps(scene), scene["checksum"], scene["scene_id"]))
        with self.assertRaisesRegex(ValueError, "stale"):
            self.scenes.assemble_preview(scene["scene_id"])
        with self.assertRaisesRegex(ExportError, "stale"):
            load_takes(self.root, scene)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.scenes.select_take(scene["scene_id"], "line-001", scene["lines"][0]["selected_take_id"])

    def test_pronunciation_changes_require_new_selected_audio(self):
        scene = self.selected_scene(use_pronunciations=True)
        self.cast.save_pronunciation(character_id=self.character["id"], source="Old", replacement="Changed")
        with self.assertRaisesRegex(ValueError, "spoken text"):
            self.scenes.assemble_preview(scene["scene_id"])
        with self.assertRaisesRegex(ExportError, "spoken text"):
            load_takes(self.root, scene)
        updated = self.scenes.update_scene(scene["scene_id"], scene)
        self.assertEqual(updated["lines"][0]["selected_takes"], {})

    def test_reference_version_changes_invalidate_selected_audio(self):
        scene = self.selected_scene()
        reference_id = self.identity["reference"]["id"]
        new_reference = self.cast.add_reference_version(reference_id, {"start_seconds": 0.1,
            "end_seconds": 0.9, "transcript": "A changed reference line."})
        scene["lines"][0]["reference_version_id"] = new_reference["id"]
        edited = self.scenes.update_scene(scene["scene_id"], scene)
        self.assertEqual(edited["lines"][0]["selected_takes"], {})

    def test_unicode_headers_and_idempotent_replay_return_complete_wav(self):
        self.character = self.cast.create_character("雪\nCharacter")
        identity = self.new_identity("turbo", "声")
        body = {"identity_id": identity["id"], "line_id": "台詞", "text": "A line.",
                "language": "English", "settings": {}, "idempotency_key": "unicode-replay"}
        first = self.post(body)
        second = self.post(body)
        for status, wav, headers in (first, second):
            self.assertEqual(status, 200)
            self.assertTrue(wav.startswith(b"RIFF"))
            self.assertEqual(len(wav), int(headers["Content-Length"]))
            self.assertIn("X-Cast-Character", headers["X-Metadata-Encoding"])
            self.assertEqual(urllib.parse.unquote(headers["X-Cast-Identity"]), "声")
        self.assertEqual(first[1], second[1])
        self.assertEqual(second[2]["X-Idempotent-Replay"], "true")
        self.assertEqual(len(self.engines.calls), 1)

    def test_unsupported_settings_fail_before_generation_and_effective_values_are_saved(self):
        identity = self.new_identity("turbo")
        base = {"identity_id": identity["id"], "line_id": "controls", "text": "A line."}
        for settings in ({"seed": 42}, {"exaggeration": 0.9}, {"nonexistent_control": 123},
                         {"temperature": float("nan")}):
            status, body, _ = self.post({**base, "settings": settings})
            self.assertEqual(status, 400, body)
        self.assertEqual(self.engines.calls, [])
        status, _, headers = self.post({**base, "settings": {"temperature": 0.7}})
        self.assertEqual(status, 200)
        take = self.cast.get_take(headers["X-Take-Id"])
        applied = take["model"]["effective_settings"]
        self.assertEqual(applied, effective_settings("turbo", {"temperature": 0.7}))
        self.assertEqual(applied["temperature"], self.engines.calls[-1]["temperature"])
        for engine, bad in (("kokoro", {"seed": 1}), ("voxcpm2", {"profile": "eager"}),
                            ("qwen3-tts-1.7b-base", {"profile": "eager"})):
            with self.assertRaises(ValueError):
                effective_settings(engine, bad)

    def test_second_runner_does_not_reset_active_jobs_and_cancellation(self):
        scene = self.scenes.create_scene({"name": "Concurrent", "lines": [
            {"line_id": "line-001", "identity_id": self.identity["id"], "text": "A line."}]})
        job = self.scenes.create_job(scene["scene_id"])
        self.engines.release.clear()
        self.runner.start()
        self.assertTrue(self.engines.entered.wait(3))
        self.scenes.cancel(job["id"])
        second = SceneStore(self.cast, self.engines.entries)
        self.assertEqual(second.get_job(job["id"])["status"], "running")
        self.assertEqual(second.get_job(job["id"])["cancel_requested"], 1)
        self.assertEqual(second.recover_interrupted_jobs(), [])
        runner = SceneBatchRunner(second, self.base)
        try:
            runner.start()
            time.sleep(0.65)
            self.assertEqual(len(self.engines.calls), 1)
            self.assertEqual(second.get_job(job["id"])["running"], 1)
            self.engines.release.set()
            deadline = time.monotonic() + 3
            while self.scenes.get_job(job["id"])["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(second.get_job(job["id"])["status"], "cancelled")
        finally:
            runner.close(5)

    def test_abandoned_recovery_preserves_cancel_and_pause(self):
        for flag, expected in (("cancel_requested", "cancelled"), ("pause_requested", "paused"), (None, "queued")):
            scene = self.scenes.create_scene({"name": "Recovery", "lines": [
                {"line_id": "line-001", "identity_id": self.identity["id"], "text": "A line."}]})
            job = self.scenes.create_job(scene["scene_id"])
            self.scenes.start_queued_job(job["id"])
            row = self.scenes.claim_next_row(job["id"])
            if flag:
                with self.scenes._connect() as db:
                    db.execute(f"UPDATE jobs SET {flag}=1 WHERE id=?", (job["id"],))
            other = SceneStore(self.cast)
            self.assertEqual(other.get_job(job["id"])["status"], "running")
            self.assertIn(job["id"], other.recover_interrupted_jobs())
            result = other.get_job(job["id"])
            self.assertEqual(result["status"], expected)
            self.assertEqual(other.get_job_row(row)["status"], "cancelled" if flag == "cancel_requested" else "queued")
            if flag:
                self.assertEqual(result[flag], 1)

    def test_batch_waits_for_model_ownership_without_failing_rows(self):
        scene = self.scenes.create_scene({"name": "Busy model", "lines": [
            {"line_id": "line-001", "identity_id": self.identity["id"], "text": "A line."}]})
        job = self.scenes.create_job(scene["scene_id"])
        self.engines.busy.set()
        self.runner.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.scenes.get_job(job["id"])
            if "Waiting for model ownership" in result["rows"][0]["error"]:
                break
            time.sleep(0.02)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["rows"][0]["status"], "queued")
        self.engines.busy.clear()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.scenes.get_job(job["id"])
            if result["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.cast.list_takes()), 1)


class ReviewOwnershipTests(unittest.TestCase):
    def test_process_exit_releases_exclusive_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lease.lock"
            script = "from local_ownership import LocalOwnership; import sys; lease=LocalOwnership(sys.argv[1]); print(lease.acquire(),flush=True); sys.stdin.read()"
            # Test the lock-owning process itself, rather than a Windows venv
            # redirector whose child may still be exiting after launcher.wait().
            child = subprocess.Popen([getattr(sys,'_base_executable',sys.executable), "-u", "-c", script, str(path)], cwd=ROOT,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            lease = LocalOwnership(path)
            try:
                self.assertEqual(child.stdout.readline().strip(), "True")
                self.assertFalse(lease.acquire())
                child.terminate()
                child.wait(5)
                self.assertTrue(lease.acquire())
            finally:
                lease.release()
                if child.poll() is None:
                    child.kill()
                    child.wait(5)
                child.stdin.close()
                child.stdout.close()

    def test_engine_switch_stops_previous_worker_and_other_pool_waits(self):
        with tempfile.TemporaryDirectory() as folder:
            workers = []
            def factory(entry):
                worker = WorkerProcess([sys.executable, "-u", str(ROOT / "tests/fixtures/fake_worker.py"),
                    "--engine", entry["id"]], Path(folder) / f"{entry['id']}-{len(workers)}.log", cwd=ROOT)
                workers.append(worker)
                return worker
            path = Path(folder) / "gpu.lock"
            first = EnginePool("cpu", 0, worker_factory=factory, residency_lock_path=path)
            second = EnginePool("cpu", 0, worker_factory=factory, residency_lock_path=path)
            try:
                first.load("breeze-tts-2")
                previous_pid = first.health()["workers"][0]["pid"]
                with self.assertRaises(WorkerBusy):
                    second.load("kokoro")
                first.load("qwen3-tts-1.7b-base")
                self.assertEqual(first.loaded, ["qwen3-tts-1.7b-base"])
                self.assertFalse(workers[0].alive)
                self.assertFalse(process_is_running(previous_pid))
                first.unload()
                second.load("kokoro")
                self.assertEqual(second.loaded, ["kokoro"])
            finally:
                first.close()
                second.close()

    def test_failed_resource_admission_releases_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "gpu.lock"
            def fail(key):
                raise ValueError("Fixture RAM limit")
            pool = EnginePool("cpu", 0, worker_factory=lambda entry: None,
                              resource_check=fail, residency_lock_path=path)
            lease = LocalOwnership(path)
            try:
                with self.assertRaisesRegex(ValueError, "RAM"):
                    pool.load("kokoro")
                self.assertTrue(lease.acquire())
            finally:
                lease.release()
                pool.close()

    def test_failed_worker_stop_does_not_admit_a_second_model(self):
        with tempfile.TemporaryDirectory() as folder:
            def factory(entry):
                return WorkerProcess([sys.executable, "-u", str(ROOT / "tests/fixtures/fake_worker.py"),
                    "--engine", entry["id"]], Path(folder) / (entry["id"] + ".log"), cwd=ROOT)
            pool = EnginePool("cpu", 0, worker_factory=factory)
            try:
                pool.load("kokoro")
                worker = pool.workers["kokoro"]["worker"]
                with patch.object(worker, "close", return_value=None):
                    with self.assertRaisesRegex(WorkerUnavailable, "did not stop"):
                        pool.load("turbo")
                    self.assertEqual(set(pool.workers), {"kokoro"})
                pool.unload()
                pool.load("turbo")
            finally:
                pool.close()

    def test_chatterbox_worker_and_broker_share_containment_policy(self):
        from worker_runtime import EngineWorkerRuntime
        import reference_paths
        with tempfile.TemporaryDirectory() as folder, patch.object(reference_paths, "LAB", Path(folder)):
            for engine in ("orig", "turbo"):
                runtime = EngineWorkerRuntime(engine, "cpu")
                runtime.engine = type("Reference", (), {"prepare_reference": lambda self, *args, **kwargs: None})()
                for root in ("reference", "data/voices", "data/cast/media/references"):
                    path = Path(folder) / root / "fixture.wav"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    sf.write(path, np.sin(np.arange(144000) * 0.03) * 0.1, 24000)
                    self.assertTrue(runtime.prepare_reference({"path": str(path)})["prepared"])
                outside = Path(folder) / "outside.wav"
                outside.write_bytes(b"fixture")
                with self.assertRaisesRegex(ValueError, "under"):
                    runtime.prepare_reference({"path": str(outside)})
            short = Path(folder) / "data/voices/short.wav"
            sf.write(short, np.sin(np.arange(24000) * 0.03) * 0.1, 24000)
            with self.assertRaisesRegex(ValueError, "longer than 5 seconds"):
                reference_paths.resolve_reference_path("turbo", short)

    def test_private_dataset_inputs_are_ignored(self):
        result = subprocess.run(["git", "check-ignore", "--no-index", "data/datasets/review/audio.wav",
                                 "data/datasets/review/manifest.json"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.splitlines()), 2)


class ReviewCaptionTests(unittest.TestCase):
    def test_trimmed_caption_clock_and_verifier(self):
        captions = [{"line_id": "a", "text": "First", "character_name": "Review", "start_seconds": 0.0, "end_seconds": 4.32},
                    {"line_id": "b", "text": "Second", "character_name": "Review", "start_seconds": 4.47, "end_seconds": 8.31}]
        adjusted = trimmed_captions(captions, 8.56, 0.05, 0.05)
        self.assertAlmostEqual(adjusted[1]["start_seconds"], 4.42)
        self.assertAlmostEqual(adjusted[1]["end_seconds"], 8.26)
        clipped = trimmed_captions(captions, 8.56, 4.4, 0.5)
        self.assertEqual(len(clipped), 1)
        self.assertAlmostEqual(clipped[0]["start_seconds"], 0.07)
        self.assertAlmostEqual(clipped[0]["end_seconds"], 3.66)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "scene").mkdir()
            sets = [{"audio_path": "scene/raw.wav", "srt_path": "scene/scene.srt", "vtt_path": "scene/scene.vtt",
                     "trim_start_seconds": 0.0, "trim_end_seconds": 0.0},
                    {"audio_path": "scene/scene-mix.flac", "srt_path": "scene/flac.srt", "vtt_path": "scene/flac.vtt",
                     "trim_start_seconds": 0.05, "trim_end_seconds": 0.05}]
            manifest = {"scene_mix": {"path": "scene/raw.wav", "duration_seconds": 8.56, "lines": captions},
                        "assets": [{"asset_type": "scene_mix_raw_wav", "output_path": "scene/raw.wav"},
                                   {"asset_type": "scene_mix_flac", "output_path": "scene/scene-mix.flac"}],
                        "transforms": {"trim_start_seconds": 0.05, "trim_end_seconds": 0.05}, "caption_sets": sets}
            write_subtitles(root / sets[0]["srt_path"], root / sets[0]["vtt_path"], captions)
            write_subtitles(root / sets[1]["srt_path"], root / sets[1]["vtt_path"], adjusted)
            verify_caption_sets(root, manifest)
            write_subtitles(root / sets[1]["srt_path"], root / sets[1]["vtt_path"], captions)
            with self.assertRaisesRegex(ExportError, "timing/text"):
                verify_caption_sets(root, manifest)
            del manifest["caption_sets"]
            with self.assertRaisesRegex(ExportError, "Legacy trimmed"):
                verify_caption_sets(root, manifest)


if __name__ == "__main__":
    unittest.main()
