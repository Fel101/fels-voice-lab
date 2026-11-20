"""The Lab's side of the GPU lock of Fel's Studio (contract 8; fels_suite.GpuLock): the model pool holds it shared while a
model is on the graphics card, beside another app holding it shared, as Altera's chat would; it refuses with gpu_busy while
NPC Forge has the card to itself (One model at a time) and loads again once Forge lets go; POST /api/unload says what the Lab
is doing when it is busy, and counts a whole scene as busy; GET /api/gpu shows it all."""

import json
import os
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fels_suite  # noqa: E402
import serve  # noqa: E402
from library_fixture import temp_cast  # noqa: E402
from local_ownership import LocalOwnership  # noqa: E402
from worker_pool import EnginePool, GpuBusy, WorkerBusy  # noqa: E402
from worker_process import WorkerProcess  # noqa: E402

FAKE_WORKER = ROOT / "tests" / "fixtures" / "fake_worker.py"

# Another app of Fel's Studio in a process of its own, holding the lock as it is told on its standard input.
STAND_IN = """
import os
import sys
sys.path.insert(0, sys.argv[1])
import fels_suite
lock = fels_suite.GpuLock(sys.argv[3], {"FELS_SUITE_DIR": sys.argv[2]})
print("ready", os.getpid(), flush=True)
for line in sys.stdin:
    command, _, what = line.strip().partition(" ")
    if command == "shared":
        print(lock.acquire(what, timeout=0), flush=True)
    elif command == "alone":
        print(lock.acquire(what, alone=True, timeout=30, poll=0.05), flush=True)
    elif command == "release":
        lock.release()
        print("released", flush=True)
"""


class StandIn:
    def __init__(self, test, app):
        self.process = subprocess.Popen(
            [sys.executable, "-c", STAND_IN, str(ROOT), test.folder, app],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        test.addCleanup(self.close)
        ready, pid = self.process.stdout.readline().split()
        test.assertEqual(ready, "ready")
        self.pid = int(pid)  # a virtual environment's python.exe starts the real one: its pid is the holder's

    def tell(self, line):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def answer(self):
        return self.process.stdout.readline().strip()

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(30)
        self.process.stdout.close()


class PoolGpuLockTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.environ = {"FELS_SUITE_DIR": self.folder}
        self.workers = []

    def pool(self, **more):
        def factory(entry):
            log_path = Path(self.folder) / "logs" / f"{entry['id']}-{len(self.workers)}.log"
            worker = WorkerProcess([sys.executable, "-u", str(FAKE_WORKER), "--engine", entry["id"]], log_path, cwd=ROOT)
            self.workers.append(worker)
            return worker

        pool = EnginePool(
            "cuda",
            0,
            worker_factory=factory,
            build_probe=lambda entry: ("cuda", "2.9.0"),
            gpu_lock=fels_suite.GpuLock("voice-lab", self.environ),
            **more,
        )
        self.addCleanup(pool.close)
        return pool

    def holders(self):
        return [(entry["app"], entry["what"], entry["alone"]) for entry in fels_suite.gpu_holders(self.environ)]

    def test_the_lab_and_a_chat_model_hold_the_card_together(self):
        altera = StandIn(self, "altera")
        altera.tell("shared a chat reply")
        self.assertEqual(altera.answer(), "True")
        pool = self.pool()
        pool.load("kokoro")
        self.assertTrue(pool.holds_gpu)
        self.assertCountEqual(self.holders(), [("altera", "a chat reply", False), ("voice-lab", "Kokoro 82M", False)])
        state = pool.gpu_state()
        self.assertEqual((state["device"], state["held"], state["alone"]), ("cuda", True, None))
        mine = {entry["app"]: entry["this_service"] for entry in state["holders"]}
        self.assertEqual(mine, {"altera": False, "voice-lab": True})
        self.assertEqual({entry["name"] for entry in state["holders"]}, {"Altera", "Voice Lab"})
        pool.load("turbo")
        self.assertIn(("voice-lab", "Chatterbox Turbo 350M", False), self.holders(), "the holder file names the model now")
        pool.unload()
        self.assertFalse(pool.holds_gpu)
        self.assertEqual(self.holders(), [("altera", "a chat reply", False)])

    def test_forge_alone_makes_the_lab_refuse_with_gpu_busy_until_it_lets_go(self):
        forge = StandIn(self, "npc-forge")
        forge.tell("alone pictures")
        self.assertEqual(forge.answer(), "True")
        lease = Path(self.folder) / "model-residency.lock"
        pool = self.pool(residency_lock_path=lease)
        with self.assertRaises(GpuBusy) as refused:
            pool.load("kokoro")
        self.assertEqual(refused.exception.error_code, "gpu_busy")
        self.assertEqual(refused.exception.status_code, 409)
        self.assertRegex(
            str(refused.exception),
            r"^NPC Forge is using the whole GPU for pictures \(One model at a time\) since \d\d:\d\d\. "
            r"Try again when it is done\.$",
        )
        holder = refused.exception.answer_fields["holder"]
        self.assertEqual((holder["app"], holder["pid"], holder["what"]), ("npc-forge", forge.pid, "pictures"))
        self.assertEqual(pool.loaded, [])
        self.assertEqual(self.workers, [], "no worker started")
        other = LocalOwnership(lease)
        self.assertTrue(other.acquire(), "a refusal lets go of the model lock between copies of the Lab")
        other.release()
        self.assertEqual(pool.gpu_state()["alone"]["app"], "npc-forge")

        pool.load("phonon-31m-en")  # Phonon runs on the CPU and takes nothing
        self.assertEqual(pool.loaded, ["phonon-31m-en"])
        self.assertFalse(pool.holds_gpu)
        with self.assertRaises(GpuBusy):
            pool.load("kokoro")
        self.assertEqual(pool.loaded, ["phonon-31m-en"], "a refusal leaves loaded what was loaded")

        forge.tell("release")
        self.assertEqual(forge.answer(), "released")
        pool.load("kokoro")
        self.assertEqual(pool.loaded, ["kokoro"])
        self.assertEqual(self.holders(), [("voice-lab", "Kokoro 82M", False)])
        self.assertIsNone(pool.gpu_state()["alone"])

    def test_forge_wanting_the_card_alone_waits_for_the_lab_to_unload(self):
        pool = self.pool()
        pool.load("kokoro")
        forge = StandIn(self, "npc-forge")
        forge.tell("alone pictures")
        deadline = time.monotonic() + 20
        while fels_suite.alone_wanted(self.environ) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(pool.gpu_state()["alone"]["app"], "npc-forge")
        pool.load("kokoro")  # the loaded model keeps working while Forge asks the Lab to unload
        self.assertTrue(pool.holds_gpu)
        pool.unload()
        self.assertEqual(forge.answer(), "True")
        self.assertEqual(self.holders(), [("npc-forge", "pictures", True)])
        with self.assertRaises(GpuBusy):
            pool.load("kokoro")

    def test_a_model_on_the_cpu_takes_nothing(self):
        def factory(entry):
            return WorkerProcess(
                [sys.executable, "-u", str(FAKE_WORKER), "--engine", entry["id"]], Path(self.folder) / "cpu.log", cwd=ROOT
            )

        pool = EnginePool("cpu", 0, worker_factory=factory, gpu_lock=fels_suite.GpuLock("voice-lab", self.environ))
        self.addCleanup(pool.close)
        pool.load("kokoro")
        self.assertFalse(pool.holds_gpu)
        self.assertEqual(self.holders(), [])
        self.assertEqual(pool.gpu_state()["device"], "cpu")

    def test_what_the_worker_is_doing_is_said_in_words(self):
        pool = self.pool()
        self.assertIsNone(pool.doing())
        with pool.queued(what="Clip"):
            pool._acquire_job("kokoro", "Clip")
            try:
                self.assertEqual(pool.doing(), "making a clip with Kokoro 82M")
            finally:
                pool._release_job()
        with pool.queued(what="Take for a scene batch", priority="low"):
            pool._acquire_job("turbo", "Take")
            try:
                self.assertEqual(pool.doing(), "speaking a line of a scene with Chatterbox Turbo 350M")
            finally:
                pool._release_job()
        pool.hold("Kokoro 82M is being installed", doing="installing Kokoro 82M")
        try:
            self.assertEqual(pool.doing(), "installing Kokoro 82M")
        finally:
            pool.release_hold()


class UnloadForTheSuiteTests(unittest.TestCase):
    """POST /api/unload and GET /api/gpu over HTTP."""

    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.environ = {"FELS_SUITE_DIR": self.folder}
        self.marker = Path(self.folder) / "started"
        self.delay = 0

        def factory(entry):
            return WorkerProcess(
                [
                    sys.executable,
                    "-u",
                    str(FAKE_WORKER),
                    "--engine",
                    entry["id"],
                    "--delay",
                    str(self.delay),
                    "--started-marker",
                    str(self.marker),
                ],
                Path(self.folder) / f"{entry['id']}.log",
                cwd=ROOT,
            )

        self.pool = EnginePool(
            "cuda",
            0,
            worker_factory=factory,
            build_probe=lambda entry: ("cuda", "2.9.0"),
            gpu_lock=fels_suite.GpuLock("voice-lab", self.environ),
        )
        self.addCleanup(self.pool.close)
        self.base, self.handler, _ = self.start(temp_cast(self))

    def start(self, cast, scenes=None):
        """A service on the pool: its address, its handler class, and its scene batch runner when there are scenes."""
        from scene_store import SceneBatchRunner

        server = serve.LocalThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        runner = SceneBatchRunner(scenes, base) if scenes is not None else None
        server.RequestHandlerClass = handler = serve.make_handler(self.pool, port, cast, scenes, runner)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            if runner is not None:
                runner.close(timeout=20)
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)

        self.addCleanup(stop)
        return base, handler, runner

    def wait_for(self, condition, what, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def call(self, path, body=None, base=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            (base or self.base) + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, (raw if raw.startswith(b"RIFF") else json.loads(raw))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_the_gpu_route_shows_the_holders(self):
        self.assertEqual(self.call("/api/gpu"), (200, {"device": "cuda", "held": False, "holders": [], "alone": None}))
        self.assertEqual(self.call("/api/load", {"engine": "kokoro"})[0], 200)
        status, answer = self.call("/api/gpu")
        self.assertEqual((status, answer["held"]), (200, True))
        [holder] = answer["holders"]
        self.assertEqual(
            {key: holder[key] for key in ("app", "name", "what", "alone", "this_service")},
            {"app": "voice-lab", "name": "Voice Lab", "what": "Kokoro 82M", "alone": False, "this_service": True},
        )
        self.assertEqual(self.call("/api/unload", {}), (200, {"ok": True}))
        self.assertEqual(self.call("/api/gpu")[1]["holders"], [])

    def test_a_scene_batch_waits_for_the_card_while_forge_has_it_and_then_finishes(self):
        from scene_store import SceneStore

        cast = temp_cast(self)
        character = cast.create_character("Nell", "")
        identity = cast.create_identity(character["id"], "kokoro", "Warm", {"voice": "af_heart"})
        cast.update_identity(identity["id"], {"status": "approved"})
        scenes = SceneStore(cast, self.pool.entries, root=Path(self.folder) / "scenes")
        base, _, runner = self.start(cast, scenes)
        lines = [
            {
                "line_id": f"line-{n}",
                "line_type": "dialogue",
                "identity_id": identity["id"],
                "text": f"Line {n}.",
                "language": "English",
                "pause_after_ms": 0,
                "take_count": 1,
                "settings": {},
            }
            for n in (1, 2)
        ]
        scene = scenes.create_scene({"schema_version": 1, "name": "Pictures first", "lines": lines})
        forge = StandIn(self, "npc-forge")
        forge.tell("alone pictures")
        self.assertEqual(forge.answer(), "True")
        status, body = self.call(f"/api/scenes/{scene['scene_id']}/jobs", {}, base=base)
        self.assertEqual(status, 202, body)
        job_id = body["job"]["id"]
        runner.start()
        waiting = "Waiting for the GPU: NPC Forge is using the whole GPU for pictures (One model at a time)"
        self.wait_for(
            lambda: any(row["error"].startswith(waiting) for row in scenes.get_job(job_id)["rows"]),
            "a line to say it waits for the GPU",
        )
        self.assertEqual(scenes.get_job(job_id)["failed"], 0, "waiting for the card is not a failure")
        forge.tell("release")
        self.assertEqual(forge.answer(), "released")
        self.wait_for(lambda: scenes.get_job(job_id)["status"] == "completed", "the batch to finish", timeout=60)
        self.assertEqual(scenes.get_job(job_id)["completed"], 2)

    def test_a_load_refused_for_forge_answers_gpu_busy_and_the_holder(self):
        forge = StandIn(self, "npc-forge")
        forge.tell("alone pictures")
        self.assertEqual(forge.answer(), "True")
        status, answer = self.call("/api/load", {"engine": "kokoro", "wait_seconds": 30})
        self.assertEqual((status, answer["code"], answer["holder"]["app"]), (409, "gpu_busy", "npc-forge"))
        self.assertIn("NPC Forge is using the whole GPU for pictures", answer["error"])
        status, answer = self.call("/api/gpu")
        self.assertEqual(
            (answer["alone"]["app"], answer["alone"]["name"], answer["alone"]["alone"]), ("npc-forge", "NPC Forge", True)
        )

    def test_a_busy_unload_says_what_the_lab_is_doing(self):
        self.delay = 2
        result = {}
        clip = threading.Thread(
            target=lambda: result.update(
                answer=self.call("/api/speak", {"engine": "kokoro", "text": "a slow line", "voice": "af_heart"})
            ),
            daemon=True,
        )
        clip.start()
        deadline = time.monotonic() + 30
        while not self.marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        status, answer = self.call("/api/unload", {})
        self.assertEqual(status, 409)
        self.assertEqual(
            answer,
            {
                "error": "Voice Lab is making a clip with Kokoro 82M. Try again when it is done.",
                "code": "busy",
                "doing": "making a clip with Kokoro 82M",
            },
        )
        clip.join(60)
        self.assertEqual(result["answer"][0], 200)

    def test_a_scene_being_spoken_on_the_card_counts_as_busy_from_first_line_to_last(self):
        progress = {"done": 12, "total": 40, "waiting": 0}
        scenes = self.handler.lab.scenes
        with patch.object(scenes, "rendering_progress", return_value=progress):
            self.assertEqual(self.call("/api/unload", {}), (200, {"ok": True}), "no model on the card: nothing is cut")
            self.assertEqual(self.call("/api/load", {"engine": "kokoro"})[0], 200)
            status, answer = self.call("/api/unload", {"wait_seconds": 30})
            self.assertEqual(status, 409)
            self.assertEqual(answer["doing"], "speaking a scene, 12 of 40 lines")
            self.assertEqual(answer["error"], "Voice Lab is speaking a scene, 12 of 40 lines. Try again when it is done.")
            self.assertTrue(self.pool.holds_gpu)
            progress.update(total=1, done=0, waiting=2)
            self.assertEqual(
                self.call("/api/unload", {})[1]["doing"], "speaking a scene, 0 of 1 line, with 2 more scenes after it"
            )
        self.assertEqual(self.call("/api/unload", {}), (200, {"ok": True}))
        self.assertFalse(self.pool.holds_gpu)


class SceneProgressTests(unittest.TestCase):
    def test_the_batch_rendering_counts_its_lines(self):
        from scene_store import SceneStore

        store = SceneStore(temp_cast(self))
        self.assertIsNone(store.rendering_progress())
        with store._connect() as db:
            for job, status, created in (
                ("a" * 32, "running", "2026-11-20T10:00:00Z"),
                ("b" * 32, "queued", "2026-11-20T09:00:00Z"),
            ):
                db.execute(
                    "INSERT INTO scenes (id,name,revision,manifest_json,checksum,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (job, "Scene", 1, "{}", "x", created, created),
                )
                db.execute(
                    "INSERT INTO jobs (id,scene_id,scene_revision,scene_checksum,scene_snapshot_json,status,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (job, job, 1, "x", "{}", status, created, created),
                )
            # Two takes of line 1, both made; line 2's second take still running; line 3 waiting; line 4 given up.
            rows = [
                ("line-1", 0, "completed"),
                ("line-1", 1, "completed"),
                ("line-2", 0, "completed"),
                ("line-2", 1, "running"),
                ("line-3", 0, "queued"),
                ("line-4", 0, "failed"),
            ]
            for number, (line, take, status) in enumerate(rows):
                db.execute(
                    "INSERT INTO job_rows (id,job_id,ordinal,scene_order,line_id,segment_id,segment_index,take_index,"
                    "cast_line_id,identity_id,text,language,direction,settings_json,use_pronunciations,status,"
                    "request_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"row-{number}",
                        "a" * 32,
                        number,
                        number,
                        line,
                        "s",
                        0,
                        take,
                        line,
                        "voice",
                        "Words.",
                        "English",
                        "",
                        "{}",
                        0,
                        status,
                        f"key-{number}",
                        "2026-11-20T10:00:00Z",
                        "2026-11-20T10:00:00Z",
                    ),
                )
        self.assertEqual(store.rendering_progress(), {"done": 2, "total": 4, "waiting": 1})


class TwoLabsTests(unittest.TestCase):
    def test_two_lab_services_still_wait_for_each_other_and_share_the_suite_lock_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            environ = {"FELS_SUITE_DIR": folder}

            def factory(entry):
                return WorkerProcess(
                    [sys.executable, "-u", str(FAKE_WORKER), "--engine", entry["id"]],
                    Path(folder) / f"{entry['id']}-{time.time_ns()}.log",
                    cwd=ROOT,
                )

            lease = Path(folder) / "model-residency.lock"
            first = EnginePool(
                "cuda",
                0,
                worker_factory=factory,
                residency_lock_path=lease,
                build_probe=lambda entry: ("cuda", "2.9.0"),
                gpu_lock=fels_suite.GpuLock("voice-lab", environ),
            )
            second = EnginePool(
                "cuda",
                0,
                worker_factory=factory,
                residency_lock_path=lease,
                build_probe=lambda entry: ("cuda", "2.9.0"),
                gpu_lock=fels_suite.GpuLock("voice-lab", environ, pid=os.getpid() + 100000, alive=lambda pid: True),
            )
            try:
                first.load("kokoro")
                with self.assertRaises(WorkerBusy) as refused:
                    second.load("kokoro")
                self.assertNotIsInstance(refused.exception, GpuBusy)
                self.assertIn("Another voice-lab service", str(refused.exception))
                self.assertFalse(second.holds_gpu, "the second waits on the model lock without taking the GPU lock")
                first.unload()
                second.load("kokoro")
                self.assertTrue(second.holds_gpu)
                self.assertEqual(
                    [entry["pid"] for entry in fels_suite.gpu_holders(environ, alive=lambda pid: True)], [os.getpid() + 100000]
                )
            finally:
                first.close()
                second.close()


if __name__ == "__main__":
    unittest.main()
