"""Dubbing jobs retain their local progress and ignore unfinished watch inputs."""

import json
import time
import unittest

import dubbing
from dub_queue import DubQueue
from tests import test_dubbing as fixture


class DubQueueTests(fixture.DubbingTests):
    @unittest.skipUnless(fixture.HAS_FFMPEG, "Needs FFmpeg")
    def test_queue_recreates_its_worker_and_reuses_takes_after_restart(self):
        self.start_server()
        scene = self.scene()
        lab = self.server.RequestHandlerClass.lab
        lab.batches.start()
        self.runner = lab.batches
        queue = lab.dub_jobs
        item = {
            "scene_id": scene["scene_id"],
            "render_missing": True,
            "export": {"expected_revision": scene["revision"], "target_language": "es"},
        }
        first = self.checked_json("/api/dub-queue", 202, {"items": [item]})["jobs"][0]
        queue.tick()
        self.assertEqual(queue.snapshot()["jobs"][0]["status"], "waiting_for_takes")
        restarted = DubQueue(self.scenes, lab.batches)
        self.assertEqual(restarted.enqueue([item])[0]["id"], first["id"])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            restarted.tick()
            done = restarted.snapshot()["jobs"][0]
            self.assertNotEqual(done["status"], "failed", done)
            if done["status"] == "complete":
                break
            time.sleep(0.03)
        self.assertEqual(done["status"], "complete")
        self.assertEqual(len(self.scenes.list_jobs(scene["scene_id"])), 1)
        self.assertTrue(dubbing.verify(self.scenes, scene["scene_id"], done["delivery_id"])["ok"])

    @unittest.skipUnless(fixture.HAS_FFMPEG, "Needs FFmpeg")
    def test_watch_waits_for_file_copies_dedupes_after_restart_and_keeps_good_jobs(self):
        self.start_server()
        scene, _ = self.render(self.scene()["scene_id"])
        watched = self.root / "watched"
        watched.mkdir()
        queue = self.server.RequestHandlerClass.lab.dub_jobs
        queue.add_watch(str(watched))
        spec = {
            "scene_id": scene["scene_id"],
            "media": "clip.wav",
            "export": {"expected_revision": scene["revision"], "target_language": "en"},
        }
        (watched / "job.dubjob.json").write_text(json.dumps(spec), encoding="utf-8")
        (watched / "invalid.dubjob.json").write_text(json.dumps({**spec, "media": "../forbidden.wav"}), encoding="utf-8")
        source = watched / "clip.wav"
        source.write_bytes(b"partially written")
        queue.tick()
        queue.tick()
        self.assertFalse(queue.snapshot()["jobs"])
        source.write_bytes(fixture.tone_wav(2))
        queue.tick()
        self.assertFalse(queue.snapshot()["jobs"])
        queue.tick()
        jobs = queue.snapshot()["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "complete", jobs)
        self.assertIn("in this folder", queue.snapshot()["watches"][0]["error"])
        reopened = DubQueue(self.scenes, self.server.RequestHandlerClass.lab.batches)
        for _ in range(4):
            reopened.tick()
        self.assertEqual(len(reopened.snapshot()["jobs"]), 1)
        self.assertEqual(reopened.snapshot()["jobs"][0]["delivery_id"], jobs[0]["delivery_id"])
