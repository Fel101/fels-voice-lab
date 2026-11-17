"""The sample scene (sample_project.py, POST /api/sample): made only when asked and only with Kokoro installed, rendered and
built like any scene, and removed in one action that leaves the library as it was; against the stand-in engines."""

import json
import sqlite3
import sys
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_service  # noqa: E402
import kokoro_support  # noqa: E402
import sample_project  # noqa: E402

VOICES = ["af_heart", "am_michael", "bf_emma"]


def library_state(root):
    """Every file of the library but the databases and locks, with its bytes, and every row of the Cast and Scenes
    databases: what "as it was" is checked against."""
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and ".sqlite3" not in path.name and not relative.startswith("out/runtime-locks/"):
            files[relative] = path.read_bytes()
    tables = {}
    for database in ("data/cast/cast.sqlite3", "data/scenes/scenes.sqlite3"):
        with closing(sqlite3.connect(root / database)) as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            for name in names:
                tables[f"{database}:{name}"] = sorted(map(repr, db.execute(f"SELECT * FROM {name}").fetchall()))
    return files, tables


class SampleCase(unittest.TestCase):
    def setUp(self):
        self.engines = api_service.FakeEngines()
        self.installed = {"kokoro"}
        self.engines.install_probe = lambda entry: {"installed": entry["id"] in self.installed}
        self.enterContext(mock.patch.object(kokoro_support, "discover_kokoro_voices", lambda: list(VOICES)))
        self.service = api_service.Service(self, self.engines)
        self.lab = self.service.server.RequestHandlerClass.lab
        self.scenes, self.cast = self.lab.scenes, self.lab.cast
        self.root = Path(self.cast.root)

    def call(self, method, path, body=None):
        status, _headers, raw = self.service.request(method, path, body)
        return status, json.loads(raw) if raw else None

    def open_sample(self):
        status, answer = self.call("POST", "/api/sample")
        self.assertEqual(status, 201, answer)
        return answer["sample"]

    def run_batches(self):
        self.lab.batches.start()
        self.addCleanup(self.lab.batches.close, 5)

    def wait_job(self, job_id, wanted=("completed",), timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.scenes.get_job(job_id)
            if job["status"] in wanted:
                return job
            time.sleep(0.05)
        self.fail(f"job stayed {job['status']}")


class OpenTests(SampleCase):
    def test_nothing_is_made_until_asked_and_then_two_characters_and_a_scene_are(self):
        self.assertEqual(self.call("GET", "/api/sample"), (200, {"sample": None, "can_make": True, "why_not": None}))
        self.assertEqual((self.cast.list_cast(), self.scenes.list_scenes()), ([], []))
        sample = self.open_sample()
        characters = self.cast.list_cast()
        self.assertEqual(sorted(item["name"] for item in characters), ["Edie (sample)", "Tomas (sample)"])
        self.assertEqual([item["name"] for item in sample["characters"]], ["Edie (sample)", "Tomas (sample)"])
        for character in characters:
            self.assertEqual(character["tags"], ["sample"])
            (voice,) = character["identities"]
            self.assertEqual((voice["engine_id"], voice["status"]), ("kokoro", "approved"))
        voices = {item["name"]: item["identities"][0]["engine_options"]["voice"] for item in characters}
        self.assertEqual(voices, {"Edie (sample)": "af_heart", "Tomas (sample)": "am_michael"})
        (scene,) = self.scenes.list_scenes()
        self.assertEqual((sample["scene_id"], sample["scene_name"]), (scene["scene_id"], sample_project.SCENE_NAME))
        self.assertEqual(len(scene["lines"]), 9)
        self.assertEqual(scene["lines"][0]["text"], "You're late. The kettle's been on since the tide turned.")
        speakers = [line["character_id"] for line in scene["lines"]]
        self.assertEqual(speakers[:2], [characters[0]["id"], characters[1]["id"]])
        self.assertEqual(len(set(speakers)), 2)
        record = json.loads(sample_project.record_path(self.scenes).read_text("utf-8"))
        self.assertEqual(record["scene_id"], scene["scene_id"])
        self.assertEqual(sorted(record["character_ids"]), sorted(item["id"] for item in characters))

    def test_asking_again_opens_the_same_sample_and_makes_nothing_more(self):
        first = self.open_sample()
        status, answer = self.call("POST", "/api/sample")
        self.assertEqual((status, answer["made"], answer["sample"]), (200, False, first))
        self.assertEqual((len(self.cast.list_cast()), len(self.scenes.list_scenes())), (2, 1))

    def test_without_kokoro_it_says_so_and_makes_nothing(self):
        self.installed = set()
        status, answer = self.call("GET", "/api/sample")
        self.assertEqual((status, answer["can_make"]), (200, False))
        self.assertIn("Kokoro 82M", answer["why_not"])
        status, answer = self.call("POST", "/api/sample")
        self.assertEqual((status, answer["code"]), (503, "model_not_installed"))
        self.assertEqual((self.cast.list_cast(), self.scenes.list_scenes()), ([], []))
        self.assertFalse(sample_project.record_path(self.scenes).exists())

    def test_a_failure_halfway_takes_back_what_was_made(self):
        with mock.patch.object(self.scenes, "create_scene", side_effect=RuntimeError("disk full")):
            status, _answer = self.call("POST", "/api/sample")
        self.assertEqual(status, 500)
        self.assertEqual((self.cast.list_cast(), self.scenes.list_scenes()), ([], []))
        self.assertFalse(sample_project.record_path(self.scenes).exists())
        self.open_sample()


class RemoveTests(SampleCase):
    def test_render_build_and_remove_leaves_the_library_as_it_was(self):
        # Something of the owner's own, which the sample must not touch.
        mine = self.service.add_voice("Nell", "Warm", approved=True)
        self.scenes.create_scene({"name": "Mine", "lines": [{"line_id": "l1", "identity_id": mine["id"], "text": "Mine."}]})
        self.run_batches()
        before = library_state(self.root)
        sample = self.open_sample()
        status, answer = self.call("POST", f"/api/scenes/{sample['scene_id']}/jobs", {})
        self.assertEqual(status, 202, answer)
        self.wait_job(answer["job"]["id"])
        self.assertEqual(self.call("POST", f"/api/scenes/{sample['scene_id']}/choose-newest", {})[0], 200)
        status, built = self.call("POST", f"/api/scenes/{sample['scene_id']}/preview", {})
        self.assertEqual(status, 201, built)
        # A take of a sample voice made in Perform goes with the sample too.
        edie = sample["characters"][0]["id"]
        voice = next(item for item in self.cast.list_cast() if item["id"] == edie)["identities"][0]
        status, _headers, _raw = self.service.request(
            "POST", "/api/performance", {"identity_id": voice["id"], "line_id": "perform-1", "text": "Hello."}
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(library_state(self.root), before)

        status, preview = self.call("GET", "/api/sample/remove/preview")
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["would_remove"], {"character": 2, "identity": 2, "reference": 0, "take": 10})
        self.assertEqual((preview["scene"]["name"], preview["scene"]["jobs"]), (sample_project.SCENE_NAME, 1))
        self.assertEqual(preview["used_elsewhere"], [])
        status, report = self.call("POST", "/api/sample/remove", {"confirm": True})
        self.assertEqual(status, 200, report)
        self.assertEqual(report["removed"], {"scene": 1, "jobs": 1, "character": 2, "identity": 2, "reference": 0, "take": 10})
        after = library_state(self.root)
        self.assertEqual(sorted(after[0]), sorted(before[0]))
        self.assertEqual(after, before)
        self.assertEqual(self.call("GET", "/api/sample")[1]["sample"], None)
        status, preview = self.call("GET", "/api/sample/remove/preview")
        self.assertEqual((status, preview["scene"], set(preview["would_remove"].values())), (200, None, {0}))
        self.assertEqual(self.call("POST", "/api/sample/remove", {"confirm": True})[0], 404)
        self.open_sample()  # and it can be made again

    def test_removing_needs_confirm(self):
        self.open_sample()
        status, answer = self.call("POST", "/api/sample/remove", {})
        self.assertEqual(status, 400)
        self.assertIn("Confirm", answer["error"])
        self.assertEqual(len(self.cast.list_cast()), 2)

    def test_a_sample_character_in_another_scene_stops_the_removal(self):
        sample = self.open_sample()
        line = self.scenes.get_scene(sample["scene_id"])["lines"][0]
        self.scenes.create_scene({"name": "My scene", "lines": [{"line_id": "l1", "identity_id": line["identity_id"],
                                                                  "text": "Borrowed."}]})  # fmt: skip
        status, preview = self.call("GET", "/api/sample/remove/preview")
        self.assertEqual((status, preview["used_elsewhere"]), (200, ["My scene"]))
        status, answer = self.call("POST", "/api/sample/remove", {"confirm": True})
        self.assertEqual(status, 400)
        self.assertIn("My scene", answer["error"])
        self.assertEqual((len(self.cast.list_cast()), len(self.scenes.list_scenes())), (2, 2))

    def test_a_batch_making_a_line_is_cancelled_and_the_removal_waits_for_it(self):
        sample = self.open_sample()
        self.engines.gate = threading.Event()
        self.addCleanup(self.engines.gate.set)
        self.run_batches()
        job = self.call("POST", f"/api/scenes/{sample['scene_id']}/jobs", {})[1]["job"]
        self.assertTrue(self.engines.started.wait(10))
        status, answer = self.call("POST", "/api/sample/remove", {"confirm": True})
        self.assertEqual((status, answer["code"]), (409, "busy"))
        self.assertTrue(self.scenes.get_job(job["id"])["cancel_requested"])
        self.assertEqual(len(self.cast.list_cast()), 2)
        self.engines.gate.set()
        self.wait_job(job["id"], ("cancelled",))
        status, report = self.call("POST", "/api/sample/remove", {"confirm": True})
        self.assertEqual(status, 200, report)
        self.assertEqual((report["removed"]["jobs"], report["removed"]["take"]), (1, 1))
        self.assertEqual((self.cast.list_cast(), self.scenes.list_scenes()), ([], []))

    def test_a_queued_batch_is_cancelled_and_removed_with_the_scene(self):
        sample = self.open_sample()
        job = self.call("POST", f"/api/scenes/{sample['scene_id']}/jobs", {})[1]["job"]  # no runner: it stays queued
        status, report = self.call("POST", "/api/sample/remove", {"confirm": True})
        self.assertEqual((status, report["removed"]["jobs"]), (200, 1))
        with self.assertRaises(KeyError):
            self.scenes.get_job(job["id"])


class DeleteSceneTests(SampleCase):
    def scene(self, name="A scene"):
        voice = self.service.add_voice("Nell", "Warm", approved=True)
        return self.scenes.create_scene({"name": name, "lines": [{"line_id": "l1", "identity_id": voice["id"], "text": "Hi."}]})

    def test_a_scene_goes_with_its_jobs_manifest_and_folder_and_its_takes_stay(self):
        scene = self.scene()
        self.run_batches()
        job = self.scenes.create_job(scene["scene_id"])
        self.wait_job(job["id"])
        take_id = self.scenes.get_job(job["id"])["rows"][0]["take_id"]
        folder = self.scenes.output_root / scene["scene_id"]
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.scenes.delete_scene(scene["scene_id"]), {"scene_id": scene["scene_id"], "jobs": 1})
        self.assertFalse(folder.exists())
        self.assertFalse(self.scenes._manifest_path(scene["scene_id"]).exists())
        self.assertEqual(self.scenes.list_scenes(), [])
        self.assertEqual(self.cast.get_take(take_id)["id"], take_id)
        with self.assertRaises(KeyError):
            self.scenes.delete_scene(scene["scene_id"])

    def test_a_queued_batch_stops_it(self):
        scene = self.scene()
        self.scenes.create_job(scene["scene_id"])
        with self.assertRaisesRegex(ValueError, "queued or rendering"):
            self.scenes.delete_scene(scene["scene_id"])
        self.assertEqual(len(self.scenes.list_scenes()), 1)

    def test_the_runner_skips_a_job_deleted_as_it_starts_and_goes_on(self):
        doomed, kept = self.scene("Doomed"), self.scene("Kept")
        first = self.scenes.create_job(doomed["scene_id"])
        second = self.scenes.create_job(kept["scene_id"])
        start = self.scenes.start_queued_job

        def deleted_first(job_id):
            if job_id == first["id"]:  # cancelled and deleted between the listing and the start
                self.scenes.cancel(job_id)
                self.scenes.delete_scene(doomed["scene_id"])
            return start(job_id)

        with mock.patch.object(self.scenes, "start_queued_job", deleted_first):
            self.run_batches()
            self.wait_job(second["id"])
        self.assertTrue(self.lab.batches._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
