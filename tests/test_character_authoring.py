"""Preset identities, effective settings, scene authorship, and restart checks."""

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve
from cast_store import CAST_SCHEMA_VERSION, CastStore
from performance_settings import performance_controls
from scene_store import SceneBatchRunner, SceneStore
from worker_pool import read_catalog


CUSTOM = "qwen3-tts-1.7b-custom-voice"


def fake_audio():
    time_axis = np.arange(12_000, dtype=np.float32) / 24_000
    buffer = __import__("io").BytesIO()
    sf.write(buffer, (0.08 * np.sin(2 * np.pi * 260 * time_axis)).astype(np.float32),
             24_000, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


class FakeEngines:
    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"
        self.loaded = []
        self.calls = []

    def loaded_model_info(self, engine_id):
        if engine_id != CUSTOM:
            return None
        entry = self.entries[engine_id]
        return {"engine": engine_id, "model_loaded": True, "checkpoint": entry["checkpoint"],
                "checkpoint_revision": entry["local_revision"], "supported_speakers": ["Vivian", "Serena"],
                "supported_languages": ["English", "French"]}

    def speak(self, request):
        self.calls.append(dict(request))
        entry = self.entries[request["engine"]]
        return np.frombuffer(fake_audio()[44:], dtype="<i2").astype(np.float32) / 32768, 24_000, {
            "total_s": 0.5, "mode": request.get("mode", "fixed_voice"),
            "backend": "fake-worker", "checkpoint": entry["checkpoint"],
            "checkpoint_revision": entry["local_revision"],
        }


def preset_options(engines, speaker="Vivian", language="English", direction="Speak calmly."):
    entry = engines.entries[CUSTOM]
    capabilities = entry["capabilities"]
    schema = performance_controls(CUSTOM, capabilities)
    return {
        "checkpoint": entry["checkpoint"], "checkpoint_revision": entry["local_revision"],
        "speaker": speaker, "language": language, "supported_speakers": ["Vivian", "Serena"],
        "supported_languages": ["English", "French"], "default_direction": direction,
        "settings_schema": schema["controls"],
        "capability_snapshot": {"voice_direction": schema["direction"],
                                "sampling_controls": capabilities["sampling_controls"],
                                "acting_controls": capabilities["acting_controls"]},
    }


class CharacterAuthoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-authoring-")
        self.root = Path(self.temp.name)
        self.cast = CastStore(self.root)
        self.engines = FakeEngines()
        self.server = serve.LocalThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        self.server.RequestHandlerClass = serve.make_handler(self.engines, self.server.server_address[1], self.cast)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def post(self, path, body):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read()
            return response.status, json.loads(payload.decode("utf-8")), response.headers

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8")), response.headers

    def make_preset(self, name="Mira", speaker="Vivian", language="English", direction="Speak calmly."):
        character = self.cast.create_character(name, "Stable identity description.")
        identity = self.cast.create_identity(character["id"], CUSTOM, name + " preset",
                                             preset_options(self.engines, speaker, language, direction))
        return character, identity

    def test_customvoice_preset_take_is_reference_free_effective_and_reloadable(self):
        status, settings_response, _ = self.get("/api/performance/settings")
        self.assertEqual(status, 200)
        controls = next(item for item in settings_response["engines"] if item["engine_id"] == CUSTOM)
        self.assertIn("temperature", [item["id"] for item in controls["controls"]])
        self.assertEqual(controls["direction"]["status"], "supported")

        _, result, _ = self.post("/api/characters", {"name": "Élodie", "description": "A stage actor."})
        character = result["character"]
        _, result, _ = self.post(f"/api/characters/{character['id']}/identities", {
            "engine_id": CUSTOM, "label": "French narrator", "speaker": "Vivian", "language": "French",
            "default_direction": "Speak gently and clearly.",
        })
        identity = result["identity"]
        self.assertEqual(identity["kind"], "preset_voice")
        self.assertEqual(identity["engine_options"]["speaker"], "Vivian")
        self.assertEqual(identity["engine_options"]["language"], "French")
        self.assertEqual(identity["engine_options"]["checkpoint_revision"], self.engines.entries[CUSTOM]["local_revision"])
        self.assertEqual(identity["references"], [])
        _, approved, _ = self.post(f"/api/identities/{identity['id']}/update", {"status": "approved"})
        self.assertEqual(approved["identity"]["status"], "approved")

        request = {"identity_id": identity["id"], "line_id": "scene-voice-01", "text": "Bienvenue à la maison.",
                   "settings": {"seed": 73, "temperature": 0.75, "top_k": 64}}
        _, wav, headers = self.post_audio("/api/performance", request)
        take_id = headers["X-Take-Id"]
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(self.engines.calls[-1]["mode"], "custom_voice")
        self.assertEqual(self.engines.calls[-1]["speaker"], "Vivian")
        self.assertEqual(self.engines.calls[-1]["language"], "French")
        self.assertEqual(self.engines.calls[-1]["direction"], "Speak gently and clearly.")

        reopened = CastStore(self.root)
        take = reopened.get_take(take_id)
        self.assertEqual(take["model"]["mode"], "custom_voice")
        self.assertEqual(take["model"]["effective_settings"]["seed"], 73)
        self.assertEqual(take["model"]["effective_settings"]["temperature"], 0.75)
        self.assertEqual(take["settings"]["top_k"], 64)
        self.assertEqual(take["provenance"]["identity_type"], "preset_voice")
        self.assertEqual(take["provenance"]["identity_options"]["speaker"], "Vivian")
        self.assertEqual(take["model"]["voice_identity"]["engine_options"]["language"], "French")
        self.assertIsNone(take["reference_version_id"])
        self.assertEqual(reopened.get_generation_identity(identity["id"])["reference"], None)
        with self.assertRaisesRegex(ValueError, "does not use a reference"):
            reopened.get_generation_identity(identity["id"], "0" * 32)
        with self.assertRaisesRegex(ValueError, "immutable"):
            reopened.update_identity(identity["id"], {"engine_options": {"speaker": "Serena"}})

    def test_model_controls_are_filtered_by_capability_status(self):
        schema = performance_controls(CUSTOM, {
            "sampling_controls": {"status": "worker_verified", "controls": ["temperature", "top_k"]},
            "voice_direction": {"status": "experimental", "input": "optional_instruct"},
        })
        self.assertEqual([item["id"] for item in schema["controls"]], ["temperature", "top_k"])
        self.assertEqual(schema["direction"]["status"], "experimental")

    def post_audio(self, path, body):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read(), response.headers

    def test_scene_identity_snapshot_unknown_fields_revision_and_invalidation(self):
        character, identity = self.make_preset()
        scenes = SceneStore(self.cast, self.engines.entries)
        payload = {"name": "Dialogue", "future_scene": {"keep": True}, "lines": [{
            "line_id": "intro", "character_id": character["id"], "identity_id": identity["id"],
            "text": "The first line.", "language": "English", "direction": "Speak softly.",
            "settings": {"seed": 42}, "selected_takes": {}, "future_line": {"keep": True},
        }]}
        scene = scenes.create_scene(payload)
        self.assertEqual(scene["future_scene"], {"keep": True})
        self.assertEqual(scene["lines"][0]["future_line"], {"keep": True})
        self.assertEqual(scene["lines"][0]["voice_identity"]["engine_options"]["speaker"], "Vivian")
        changed = dict(scene)
        changed["lines"] = [dict(scene["lines"][0], text="Edited line.", settings={"seed": 44})]
        updated = scenes.update_scene(scene["scene_id"], changed, expected_revision=1)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["lines"][0]["line_id"], "intro")
        self.assertEqual(updated["lines"][0]["future_line"], {"keep": True})
        self.assertEqual(updated["future_scene"], {"keep": True})
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            scenes.update_scene(scene["scene_id"], changed, expected_revision=1)

    def test_small_mixed_character_batch_selects_and_assembles_then_clears_stale_take(self):
        first_char, first_identity = self.make_preset("Mira", "Vivian", "English", "Speak warmly.")
        second_char = self.cast.create_character("Serena", "A lower register voice.")
        second_identity = self.cast.create_identity(second_char["id"], CUSTOM, "Serena preset",
                                                   preset_options(self.engines, "Serena", "French", "Speak firmly."))
        scenes = SceneStore(self.cast, self.engines.entries)
        runner = SceneBatchRunner(scenes, self.base)
        self.server.RequestHandlerClass = serve.make_handler(self.engines, self.server.server_address[1],
                                                              self.cast, scenes, runner)
        scene = scenes.create_scene({"name": "Two voices", "lines": [
            {"line_id": "mira-01", "identity_id": first_identity["id"], "text": "You came back.",
             "language": "English", "settings": {"seed": 21}},
            {"line_id": "serena-01", "identity_id": second_identity["id"], "text": "I promised.",
             "language": "French", "settings": {"seed": 22}},
        ]})
        try:
            runner.start()
            _, result, _ = self.post(f"/api/scenes/{scene['scene_id']}/jobs", {})
            job_id = result["job"]["id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                job = scenes.get_job(job_id)
                if job["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.025)
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["completed"], 2)
            self.assertEqual({call["speaker"] for call in self.engines.calls}, {"Vivian", "Serena"})
            for row in job["rows"]:
                scenes.select_take(scene["scene_id"], row["line_id"], row["take_id"], row["segment_id"])
            preview = scenes.assemble_preview(scene["scene_id"])
            self.assertTrue(Path(preview["audio_path"]).is_file())
            self.assertEqual(len(preview["lines"]), 2)

            saved = scenes.get_scene(scene["scene_id"])
            saved["lines"][0]["settings"] = {"seed": 99}
            changed = scenes.update_scene(scene["scene_id"], saved, expected_revision=saved["revision"])
            self.assertEqual(changed["lines"][0]["selected_takes"], {})
            with self.assertRaisesRegex(ValueError, "selected successful take for every"):
                scenes.assemble_preview(scene["scene_id"])
        finally:
            runner.close(timeout=5)


class CastSchemaUpgradeTests(unittest.TestCase):
    def test_parallel_first_access_initializes_one_cast_database(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-authoring-first-access-") as folder:
            stores = [CastStore(folder) for _ in range(12)]
            barrier = threading.Barrier(len(stores))

            def first_access(store):
                barrier.wait(timeout=5)
                return store.list_cast()

            with ThreadPoolExecutor(max_workers=len(stores)) as workers:
                self.assertEqual(list(workers.map(first_access, stores)), [[] for _ in stores])
            db = sqlite3.connect(Path(folder) / "data" / "cast" / "cast.sqlite3")
            try:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], CAST_SCHEMA_VERSION)
            finally:
                db.close()

    def test_v2_cast_identity_rows_survive_preset_kind_migration(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-authoring-migration-") as folder:
            db_path = Path(folder) / "data" / "cast" / "cast.sqlite3"
            db_path.parent.mkdir(parents=True)
            db = sqlite3.connect(db_path)
            try:
                db.executescript("""
                    CREATE TABLE characters(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',
                        tags_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                    CREATE TABLE reference_versions(id TEXT PRIMARY KEY);
                    CREATE TABLE takes(id TEXT PRIMARY KEY);
                    CREATE TABLE identities(id TEXT PRIMARY KEY,character_id TEXT NOT NULL REFERENCES characters(id),
                        engine_id TEXT NOT NULL,label TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('candidate','approved','rejected')),
                        kind TEXT NOT NULL CHECK(kind IN ('reference','fixed_voice')),engine_options_json TEXT NOT NULL DEFAULT '{}',
                        notes TEXT NOT NULL DEFAULT '',approved_reference_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                        FOREIGN KEY(approved_reference_id) REFERENCES reference_versions(id));
                    CREATE UNIQUE INDEX one_approved_identity_per_engine ON identities(character_id,engine_id) WHERE status='approved';
                    INSERT INTO characters VALUES('c','Old character','','[]','now','now');
                    INSERT INTO identities VALUES('i','c','kokoro','Old fixed voice','candidate','fixed_voice',
                        '{"voice":"af_heart"}', '', NULL, 'now', 'now');
                    PRAGMA user_version=2;
                """)
            finally:
                db.close()
            store = CastStore(folder)
            with store._connect() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], CAST_SCHEMA_VERSION)
                self.assertEqual(db.execute("SELECT kind FROM identities WHERE id='i'").fetchone()[0], "fixed_voice")
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
