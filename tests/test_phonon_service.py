"""Phonon through the service: the voice list, Cast identities, performances, scene lines, the pool's request checks and the
worker's branch. A stand-in replaces the model, so the real ptts and the real voices are not needed."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import phonon_support  # noqa: E402
import serve  # noqa: E402
import worker_runtime  # noqa: E402
from library_fixture import temp_cast  # noqa: E402
from phonon_fixtures import Lab  # noqa: E402
from scene_store import SceneStore  # noqa: E402
from worker_pool import EnginePool, read_catalog  # noqa: E402

PHONON = phonon_support.PHONON_ENGINE_ID


def fake_audio():
    axis = np.arange(12_000, dtype=np.float32) / 24_000
    buffer = __import__("io").BytesIO()
    sf.write(buffer, (0.08 * np.sin(2 * np.pi * 260 * axis)).astype(np.float32), 24_000, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


class FakeEngines:
    """What the service asks of its engine pool: the catalog, and speak() answering with a short clip and a record of the call."""

    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items() if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"
        self.loaded = []
        self.calls = []

    def speak(self, request):
        self.calls.append(dict(request))
        entry = self.entries[request["engine"]]
        samples = np.frombuffer(fake_audio()[44:], dtype="<i2").astype(np.float32) / 32768
        return samples, 24_000, {"total_s": 0.5, "backend": "ptts", "backend_version": "0.5.0", "device": "cpu",
                                 "checkpoint": entry["checkpoint"], "checkpoint_revision": entry["local_revision"],
                                 "speaker": request.get("voice")}


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-phonon-svc-")
        self.addCleanup(self.temp.cleanup)
        self.lab_root = Path(self.temp.name) / "lab"
        self.lab_root.mkdir()
        self.lab = Lab(self.lab_root)
        patch = mock.patch.object(phonon_support, "LAB", self.lab_root)
        patch.start()
        self.addCleanup(patch.stop)
        self.cast = temp_cast(self)
        self.engines = FakeEngines()
        self.server = serve.LocalThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        self.server.RequestHandlerClass = serve.make_handler(self.engines, self.server.server_address[1], self.cast)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, path, body=None):
        request = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = response.read()
                return response.status, (json.loads(payload) if response.headers.get_content_type() == "application/json" else payload), \
                    response.headers
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read()), error.headers

    def character(self, name="Mira"):
        return self.call("/api/characters", {"name": name, "description": "A stage actor."})[1]["character"]

    def identity(self, speaker="zoey", **more):
        character = self.character()
        status, result, _ = self.call(f"/api/characters/{character['id']}/identities",
                                      {"engine_id": PHONON, "label": "Zoey candidate", "speaker": speaker, **more})
        self.assertEqual(status, 201, result)
        return character, result["identity"]


class VoiceListAndSettingsTests(ServiceCase):
    def test_info_lists_the_installed_voices_grouped_and_in_order(self):
        status, info, _ = self.call("/api/info")
        self.assertEqual(status, 200)
        voices = info["phonon_voices"]
        self.assertEqual([item["name"] for item in voices],
                         ["easton", "Harper", "Zoey", "Alfie", "Maeve", "Azgorath", "Old Mortlake"])
        self.assertEqual({item["group"] for item in voices}, {"American English", "British English", "Characters"})
        self.assertEqual(set(voices[0]), {"name", "group", "character", "gender", "age", "locale", "description"})
        self.assertIn("voices", info, "Kokoro's list is still there")

    def test_info_lists_no_voices_when_none_are_installed(self):
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        Lab(empty, records=None)
        with mock.patch.object(phonon_support, "LAB", empty):
            self.assertEqual(self.call("/api/info")[1]["phonon_voices"], [])

    def test_the_settings_schema_offers_seed_temperature_and_pace_and_no_direction(self):
        status, result, _ = self.call("/api/performance/settings")
        self.assertEqual(status, 200)
        schema = next(item for item in result["engines"] if item["engine_id"] == PHONON)
        self.assertEqual([(item["id"], item["default"]) for item in schema["controls"]],
                         [("seed", 42), ("temperature", 0.3), ("duration_delta", 0.0)])
        pace = next(item for item in schema["controls"] if item["id"] == "duration_delta")
        self.assertEqual((pace["minimum"], pace["maximum"]), (-0.5, 0.5))
        self.assertEqual(schema["direction"]["status"], "unsupported")
        self.assertEqual(schema["languages"], ["English"])


class IdentityTests(ServiceCase):
    def test_a_phonon_voice_becomes_a_reference_free_preset_identity_with_the_pins_it_was_chosen_under(self):
        _character, identity = self.identity("zoey")
        self.assertEqual(identity["kind"], "preset_voice")
        self.assertEqual(identity["references"], [])
        options = identity["engine_options"]
        entry = self.engines.entries[PHONON]
        self.assertEqual((options["speaker"], options["language"]), ("Zoey", "English"))
        self.assertEqual((options["checkpoint"], options["checkpoint_revision"]), (entry["checkpoint"], entry["local_revision"]))
        self.assertEqual(options["voices_revision"], entry["backend"]["voices_revision"])
        self.assertEqual(options["supported_speakers"], phonon_support.voice_names())
        self.assertEqual(options["supported_languages"], ["English"])
        self.assertEqual(options["voice_details"]["group"], "American English")

    def test_an_identity_cannot_be_changed_afterwards_but_can_be_approved(self):
        _character, identity = self.identity()
        status, result, _ = self.call(f"/api/identities/{identity['id']}/update", {"engine_options": {"speaker": "Harper"}})
        self.assertEqual(status, 400)
        self.assertIn("immutable", result["error"])
        status, result, _ = self.call(f"/api/identities/{identity['id']}/update", {"status": "approved"})
        self.assertEqual((status, result["identity"]["status"]), (200, "approved"))

    def test_a_voice_that_is_not_there_or_not_chosen_or_a_language_phonon_lacks_is_refused_in_words(self):
        character = self.character()
        path = f"/api/characters/{character['id']}/identities"
        for extra, message in (({"speaker": "Nobody"}, "no installed voice named 'Nobody'"),
                               ({"speaker": ""}, "Choose a Phonon voice"),
                               ({}, "Choose a Phonon voice"),
                               ({"speaker": "Zoey", "language": "French"}, "English only")):
            with self.subTest(extra=extra):
                status, result, _ = self.call(path, {"engine_id": PHONON, "label": "Candidate", **extra})
                self.assertEqual(status, 400)
                self.assertIn(message, result["error"])

    def test_nothing_installed_means_the_install_instruction(self):
        character = self.character()
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        Lab(empty, records=None)
        with mock.patch.object(phonon_support, "LAB", empty):
            status, result, _ = self.call(f"/api/characters/{character['id']}/identities",
                                          {"engine_id": PHONON, "label": "Candidate", "speaker": "Zoey"})
        self.assertEqual(status, 400)
        self.assertRegex(result["error"], r"not installed.*Install on its card.*setup-phonon\.bat")


class PerformanceTests(ServiceCase):
    def perform(self, identity, **more):
        body = {"identity_id": identity["id"], "line_id": "intro", "text": "Hello there.", **more}
        return self.call("/api/performance", body)

    def test_a_performance_sends_the_voice_language_and_settings_and_saves_a_fixed_voice_take(self):
        _character, identity = self.identity()
        status, wav, headers = self.perform(identity, settings={"seed": 9, "temperature": 0.6, "duration_delta": -0.2})
        self.assertEqual(status, 200)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(self.engines.calls[-1], {"engine": PHONON, "text": "Hello there.", "voice": "Zoey", "language": "English",
                                                  "seed": 9, "temperature": 0.6, "duration_delta": -0.2})
        self.assertEqual(headers["X-Generation-Mode"], "fixed_voice")
        take = self.cast.get_take(headers["X-Take-Id"])
        self.assertEqual(take["language"], "English")
        self.assertEqual(take["model"]["mode"], "fixed_voice")
        self.assertEqual(take["model"]["effective_settings"], {"seed": 9, "temperature": 0.6, "duration_delta": -0.2})
        self.assertEqual(take["model"]["backend_result"]["speaker"], "Zoey")
        self.assertEqual(take["provenance"]["identity_type"], "preset_voice")
        self.assertEqual(take["provenance"]["identity_options"]["speaker"], "Zoey")
        self.assertIsNone(take["reference_version_id"])

    def test_without_settings_the_defaults_are_used(self):
        _character, identity = self.identity()
        self.assertEqual(self.perform(identity)[0], 200)
        call = self.engines.calls[-1]
        self.assertEqual((call["seed"], call["temperature"], call["duration_delta"]), (42, 0.3, 0.0))

    def test_what_phonon_cannot_do_is_refused_before_the_model_is_asked(self):
        _character, identity = self.identity()
        for extra, message in (({"direction": "Whisper it."}, "no text-direction control"),
                               ({"settings": {"speed": 1.2}}, "does not support performance setting"),
                               ({"settings": {"temperature": "warm"}}, "must be a finite number"),
                               ({"settings": []}, "settings must be an object"),
                               ({"reference_version_id": "0" * 32}, "does not use a reference")):
            with self.subTest(extra=extra):
                status, result, _ = self.perform(identity, **extra)
                self.assertEqual(status, 400)
                self.assertIn(message, result["error"])
        self.assertEqual(self.engines.calls, [], "no refused request reached the engine")

    def test_the_same_key_replays_the_same_take_and_a_changed_pace_is_a_different_request(self):
        _character, identity = self.identity()
        first = self.perform(identity, idempotency_key="phonon-1", settings={"duration_delta": 0.1})
        again = self.perform(identity, idempotency_key="phonon-1", settings={"duration_delta": 0.1})
        self.assertEqual((first[0], again[0]), (200, 200))
        self.assertEqual(again[2]["X-Idempotent-Replay"], "true")
        self.assertEqual(len(self.engines.calls), 1)
        changed = self.perform(identity, idempotency_key="phonon-1", settings={"duration_delta": 0.2})
        self.assertEqual(changed[0], 400)
        self.assertIn("different inputs", changed[1]["error"])


class SceneTests(ServiceCase):
    def test_a_scene_line_takes_english_and_nothing_else_and_keeps_the_voice_it_was_made_with(self):
        character, identity = self.identity("harper")
        scenes = SceneStore(self.cast, self.engines.entries)
        line = {"line_id": "intro", "character_id": character["id"], "identity_id": identity["id"], "text": "The first line.",
                "language": "english", "settings": {"seed": 5, "duration_delta": 0.1}, "selected_takes": {}}
        scene = scenes.create_scene({"name": "Dialogue", "lines": [line]})
        saved = scene["lines"][0]
        self.assertEqual(saved["language"], "English")
        self.assertEqual(saved["engine_id"], PHONON)
        self.assertEqual(saved["voice_identity"]["engine_options"]["speaker"], "Harper")
        with self.assertRaisesRegex(ValueError, "does not support language 'French'; supported values: English"):
            scenes.create_scene({"name": "French", "lines": [dict(line, line_id="fr", language="French")]})
        with self.assertRaisesRegex(ValueError, "no verified text-direction control"):
            scenes.create_scene({"name": "Directed", "lines": [dict(line, line_id="dir", direction="Softly.")]})


class PoolChecksTests(unittest.TestCase):
    """EnginePool.speak refuses a bad Phonon request before any worker is started."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-phonon-pool-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "lab"
        root.mkdir()
        Lab(root)
        patch = mock.patch.object(phonon_support, "LAB", root)
        patch.start()
        self.addCleanup(patch.stop)
        self.pool = EnginePool("cpu", 0, worker_factory=lambda entry: self.fail("an invalid request started a worker"),
                               idle_poll_seconds=0.1)
        self.addCleanup(self.pool.close)

    def test_a_request_the_engine_cannot_take_is_refused_in_words(self):
        for extra, message in (({"reference_path": "x.wav"}, r"Unsupported Phonon parameter\(s\): reference_path"),
                               ({"speed": 1.1, "mode": "clone"}, r"Unsupported Phonon parameter\(s\): mode, speed"),
                               ({"voice": "Nobody"}, "no installed voice named 'Nobody'"),
                               ({"language": "German"}, "English only"),
                               ({"temperature": 1.5}, "temperature must be between 0 and 1"),
                               ({"seed": -3}, "seed must be between"),
                               ({"duration_delta": 0.9}, "duration_delta must be between -0.5 and 0.5"),
                               ({"seed": 1.5}, "whole number")):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, message):
                self.pool.speak({"engine": PHONON, "text": "Hello.", **extra})
        self.assertEqual(self.pool.loaded, [])

    def test_text_is_checked_like_every_other_engine(self):
        for text, message in (("", "Type something to say"), ("x" * 1201, "under 1200 characters")):
            with self.assertRaisesRegex(ValueError, message):
                self.pool.speak({"engine": PHONON, "text": text})


class WorkerBranchTests(unittest.TestCase):
    """EngineWorkerRuntime loads and runs Phonon through engines.build_engine; here that is a stand-in."""

    class Engine:
        sr = 24_000
        device = "cpu"
        version = "0.5.0"
        voices = ["Harper", "Zoey"]
        pins = {"model": ("gradium/phonon-31m-en", "rev-a"), "voices": ("gradium/phonon-voices", "rev-b")}

        def __init__(self):
            self.calls = []

        def speak(self, text, **options):
            self.calls.append((text, options))
            return np.full(2400, 0.2, dtype=np.float32), 24_000, {"total_s": 0.1, "speaker": options.get("voice")}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-phonon-worker-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = self.Engine()
        for owner, name, value in ((worker_runtime, "LAB", self.root), (worker_runtime, "require_ram", lambda needed, engine: None)):
            patch = mock.patch.object(owner, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch("engines.build_engine", lambda key, device: self.engine)
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_loads_on_the_cpu_warms_up_with_the_default_voice_and_reports_the_voices(self):
        runtime = worker_runtime.EngineWorkerRuntime(PHONON, "cuda")
        loaded = runtime.load()
        self.assertEqual((loaded["device"], loaded["sample_rate"], loaded["model_loaded"]), ("cpu", 24_000, True))
        self.assertEqual(loaded["supported_speakers"], ["Harper", "Zoey"])
        self.assertEqual(loaded["supported_languages"], ["English"])
        self.assertEqual((loaded["checkpoint"], loaded["checkpoint_revision"]), ("gradium/phonon-31m-en", "rev-a"))
        self.assertEqual(self.engine.calls[0], ("Hello there.", {}))
        self.assertEqual(runtime.health()["model_info"]["backend"], "ptts")
        self.assertEqual(worker_runtime.MIN_FREE_RAM_GB[PHONON], 2)

    def test_a_clip_is_made_with_the_voice_and_settings_the_pool_checked(self):
        runtime = worker_runtime.EngineWorkerRuntime(PHONON, "cpu")
        runtime.load()
        output = self.root / "out" / "workers" / f"{PHONON}-{__import__('os').getpid()}" / "clip.npy"
        result = runtime.generate({"text": "Hello [laugh] there.", "voice": "Zoey", "seed": 7, "temperature": 0.5,
                                   "duration_delta": -0.1, "language": "English", "output_file": str(output)})
        text, options = self.engine.calls[-1]
        self.assertEqual(options, {"voice": "Zoey", "seed": 7, "temperature": 0.5, "duration_delta": -0.1})
        self.assertNotIn("[laugh]", text, "tags the engine cannot perform are not read aloud")
        self.assertEqual((result["sample_rate"], result["sample_count"], result["info"]["speaker"]), (24_000, 2400, "Zoey"))
        self.assertEqual(np.load(result["audio_file"]).shape, (2400,))

    def test_it_takes_no_reference_audio(self):
        runtime = worker_runtime.EngineWorkerRuntime(PHONON, "cpu")
        runtime.load()
        with self.assertRaisesRegex(ValueError, "preset voices and does not accept reference audio"):
            runtime.prepare_reference({"path": "x.wav"})
        with self.assertRaisesRegex(RuntimeError, "Load the model"):
            worker_runtime.EngineWorkerRuntime(PHONON, "cpu").prepare_reference({"path": "x.wav"})

    def test_a_worker_without_pytorch_says_so_instead_of_naming_an_import_error(self):
        runtime = worker_runtime.EngineWorkerRuntime(PHONON, "cpu")
        with mock.patch.object(worker_runtime, "torch_runtime", side_effect=ModuleNotFoundError("No module named 'torch'", name="torch")):
            memory = runtime.health()["memory"]
        self.assertEqual((memory["allocated_bytes"], memory["reserved_bytes"]), (0, 0))
        self.assertEqual(memory["memory_note"], "PyTorch is not installed here, so GPU memory is not tracked.")
        with mock.patch.object(worker_runtime, "torch_runtime", side_effect=ModuleNotFoundError("No module named 'other'", name="other")):
            self.assertIn("No module named 'other'", runtime.health()["memory"]["memory_note"])


if __name__ == "__main__":
    unittest.main()
