"""The GPU/CPU switch on each Models page card: which PyTorch build an environment holds, what the switch shows, where the choice
is kept, which device a worker is asked for, and the route that changes it.

Nothing here loads a model or starts a worker: environments are folders with dist-info names, and workers are stand-ins.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine_checks
import engine_devices
import worker_pool
from engine_devices import EngineDeviceStore, device_facts, torch_build
from worker_pool import EnginePool, WorkerBusy

GPU_OK = (True, None)
NO_BUILD = (False, "The app has no GPU build of it to install; it runs on the CPU.")


def entry(engine="kokoro", python=".venv-kokoro/Scripts/python.exe"):
    return {"id": engine, "name": engine.title(), "runtime": {"python": python}, "integration": {"service_api": True}}


class TorchBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-devices-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def environment(self, *names):
        site = self.root / ".venv-kokoro" / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        for name in names:
            (site / name).mkdir()

    def test_the_build_is_read_from_the_dist_info_folder_name(self):
        for folder, expected in (
            ("torch-2.12.0+cu130.dist-info", ("cuda", "2.12.0+cu130")),
            ("torch-2.12.0+rocm7.14.0.dist-info", ("rocm", "2.12.0+rocm7.14.0")),
            ("torch-2.6.0.dist-info", ("cpu", "2.6.0")),
            ("torch-2.7.1+cpu.dist-info", ("cpu", "2.7.1+cpu")),
        ):
            with self.subTest(folder=folder):
                self.temp.cleanup()
                self.root.mkdir()
                self.environment("torchaudio-2.11.0+cu130.dist-info", folder, "numpy-1.26.4.dist-info")
                self.assertEqual(torch_build(self.root, entry()), expected)

    def test_no_pytorch_or_no_environment_is_none(self):
        self.assertEqual(torch_build(self.root, entry()), (None, None))
        self.environment("torchaudio-2.11.0.dist-info", "ptts-0.1.0.dist-info")
        self.assertEqual(torch_build(self.root, entry()), (None, None))


class DeviceFactsTests(unittest.TestCase):
    def facts(self, engine="kokoro", *, service="cuda", installed=True, build="cuda", choice=None, gpu_install=GPU_OK):
        return device_facts(
            engine, service_device=service, installed=installed, build=build, choice=choice, gpu_install=gpu_install
        )

    def test_a_gpu_build_runs_on_the_card_until_the_switch_keeps_it_on_the_cpu(self):
        facts = self.facts()
        self.assertEqual((facts["device"], facts["switch"], facts["choice"]), ("cuda", "gpu", None))
        self.assertEqual(facts["gpu"], {"choosable": True, "reason": None, "build": "cuda", "install": False, "cpu": None})
        facts = self.facts(choice="cpu")
        self.assertEqual((facts["device"], facts["switch"], facts["choice"]), ("cpu", "cpu", "cpu"))
        for build in ("rocm", None):  # an AMD build, or one set up by hand that cannot be read: the worker finds out itself
            with self.subTest(build=build):
                self.assertEqual(self.facts(build=build)["device"], "cuda")

    def test_a_cpu_build_runs_on_the_cpu_and_offers_its_gpu_build_when_switched(self):
        facts = self.facts(build="cpu")
        self.assertEqual((facts["device"], facts["switch"]), ("cpu", "cpu"))
        self.assertEqual(facts["gpu"], {"choosable": True, "reason": None, "build": "cpu", "install": False, "cpu": None})
        facts = self.facts(build="cpu", choice="gpu")
        self.assertEqual((facts["device"], facts["switch"]), ("cpu", "gpu"))
        self.assertTrue(facts["gpu"]["install"])
        facts = self.facts(build="cpu", choice="gpu", gpu_install=NO_BUILD)
        self.assertEqual((facts["device"], facts["switch"]), ("cpu", "cpu"))
        self.assertEqual(
            facts["gpu"], {"choosable": False, "reason": NO_BUILD[1], "build": "cpu", "install": False, "cpu": None}
        )

    def test_a_model_that_is_not_installed_gets_the_build_its_switch_shows(self):
        facts = self.facts(installed=False, build=None)
        self.assertEqual((facts["switch"], facts["gpu"]["install"]), ("gpu", True))
        facts = self.facts(installed=False, build=None, choice="cpu")
        self.assertEqual((facts["switch"], facts["gpu"]["install"]), ("cpu", False))
        facts = self.facts(installed=False, build=None, gpu_install=NO_BUILD)
        self.assertEqual((facts["switch"], facts["gpu"]["choosable"], facts["gpu"]["reason"]), ("cpu", False, NO_BUILD[1]))

    def test_a_service_on_the_cpu_keeps_every_model_there(self):
        for choice in (None, "gpu", "cpu"):
            with self.subTest(choice=choice):
                facts = self.facts(service="cpu", choice=choice)
                self.assertEqual((facts["device"], facts["switch"], facts["gpu"]["choosable"]), ("cpu", "cpu", False))
                self.assertEqual(facts["gpu"]["reason"], engine_devices.NO_GPU)

    def test_phonon_and_drama_have_no_switch_and_keep_the_services_device(self):
        for engine in engine_devices.CPU_ONLY:
            with self.subTest(engine=engine):
                facts = self.facts(engine, build=None, choice="gpu")
                self.assertEqual((facts["device"], facts["switch"], facts["choice"]), ("cuda", "cpu", None))
                self.assertEqual(facts["gpu"]["reason"], engine_devices.CPU_ONLY[engine])


class GpuOnlyTests(unittest.TestCase):
    """Qwen3-TTS, VoxCPM2 and Breeze run only on a graphics card: their switch stays on GPU and CPU is never offered."""

    def facts(self, engine="qwen3-tts-1.7b-base", **kwargs):
        options = {"service_device": "cuda", "installed": False, "build": None, "choice": None, "gpu_install": GPU_OK, **kwargs}
        return device_facts(engine, **options)

    def test_cpu_is_never_offered_and_install_puts_in_the_gpu_build(self):
        for engine in engine_devices.GPU_ONLY:
            with self.subTest(engine=engine):
                facts = self.facts(engine, choice="cpu")
                self.assertEqual((facts["device"], facts["switch"], facts["choice"]), ("cuda", "gpu", None))
                self.assertEqual(
                    facts["gpu"],
                    {"choosable": True, "reason": None, "build": None, "install": True, "cpu": engine_devices.GPU_ONLY[engine]},
                )
        installed = self.facts(installed=True, build="cuda")
        self.assertEqual((installed["gpu"]["choosable"], installed["gpu"]["install"]), (True, False))

    def test_without_a_gpu_build_to_install_or_a_card_it_cannot_run_and_says_why(self):
        facts = self.facts(gpu_install=NO_BUILD)
        self.assertEqual(
            (facts["gpu"]["choosable"], facts["gpu"]["reason"], facts["gpu"]["install"]), (False, NO_BUILD[1], False)
        )
        facts = self.facts(service_device="cpu")
        self.assertEqual(
            (facts["device"], facts["gpu"]["choosable"], facts["gpu"]["reason"]), ("cpu", False, engine_devices.NO_GPU)
        )

    def test_the_cpu_and_gpu_only_lists_do_not_overlap_and_name_catalog_engines(self):
        catalog = {entry["id"] for entry in worker_pool.read_catalog()["engines"]}
        self.assertFalse(set(engine_devices.CPU_ONLY) & set(engine_devices.GPU_ONLY))
        self.assertLessEqual(set(engine_devices.CPU_ONLY) | set(engine_devices.GPU_ONLY), catalog)


class DeviceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-devices-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "engines" / "devices.json"

    def test_a_choice_is_kept_on_disk_and_forgotten_on_request(self):
        store = EngineDeviceStore(self.path)
        self.assertIsNone(store.get("kokoro"))
        store.set("kokoro", "cpu")
        store.set("turbo", "gpu")
        self.assertEqual(json.loads(self.path.read_text("utf-8")), {"schema": 1, "devices": {"kokoro": "cpu", "turbo": "gpu"}})
        self.assertEqual(EngineDeviceStore(self.path).get("kokoro"), "cpu")
        store.set("kokoro", None)
        self.assertIsNone(EngineDeviceStore(self.path).get("kokoro"))
        with self.assertRaisesRegex(ValueError, "Choose gpu or cpu"):
            store.set("kokoro", "vulkan")

    def test_a_file_that_cannot_be_read_counts_as_no_choices(self):
        self.path.parent.mkdir(parents=True)
        for text in (
            "not json",
            json.dumps({"schema": 2, "devices": {"kokoro": "cpu"}}),
            json.dumps({"schema": 1, "devices": {"kokoro": "fast", "turbo": "cpu"}}),
        ):
            with self.subTest(text=text):
                self.path.write_text(text, "utf-8")
                store = EngineDeviceStore(self.path)
                self.assertIsNone(store.get("kokoro"))
        self.assertEqual(store.get("turbo"), "cpu")

    def test_a_failed_write_keeps_the_choice_in_memory_and_says_why(self):
        self.path.parent.mkdir(parents=True)
        self.path.with_name("devices.json.tmp").mkdir()  # a folder where the temporary file must go
        store = EngineDeviceStore(self.path)
        store.set("kokoro", "cpu")
        self.assertEqual(store.get("kokoro"), "cpu")
        self.assertIsNotNone(store.write_error)


class FakeWorker:
    def __init__(self):
        self.alive, self.pid, self.calls = True, None, []

    def call(self, method, **params):
        self.calls.append(method)
        return {"released": True}

    def close(self, force=False):
        self.alive = False


class PoolDeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-devices-")
        self.addCleanup(self.temp.cleanup)
        self.store = EngineDeviceStore(Path(self.temp.name) / "devices.json")
        self.builds = {"kokoro": ("cuda", "2.12.0+cu130"), "turbo": ("cpu", "2.6.0")}

    def pool(self, device="cuda"):
        pool = EnginePool(
            device,
            0,
            worker_factory=lambda entry: None,
            idle_poll_seconds=0.1,
            residency_lock_path=None,
            device_store=self.store,
            build_probe=lambda entry: self.builds.get(entry["id"], (None, None)),
            install_probe=lambda entry: {"installed": True, "missing": [], "environment": "x", "checkpoint": "y"},
            fingerprint_probe=lambda entry: "pin",
        )
        self.addCleanup(pool.close)
        return pool

    def test_each_worker_is_asked_for_its_own_device(self):
        pool = self.pool()
        self.assertEqual(pool.device_for("kokoro"), "cuda")
        self.assertEqual(pool.device_for("turbo"), "cpu")  # its PyTorch is the CPU build
        self.assertEqual(pool.device_for("phonon-31m-en"), "cuda")  # as before: Phonon chooses for itself
        self.store.set("kokoro", "cpu")
        self.assertEqual(pool.device_for("kokoro"), "cpu")
        self.assertEqual(self.pool("cpu").device_for("orig"), "cpu")

    def test_a_worker_is_started_with_the_device_its_card_chose(self):
        pool = self.pool()
        self.store.set("kokoro", "cpu")
        with mock.patch.object(worker_pool, "WorkerProcess") as process, mock.patch.object(Path, "is_file", return_value=True):
            pool.worker_factory = None
            pool._new_worker("kokoro", pool.entries["kokoro"])
        command = process.call_args.args[0]
        self.assertEqual(command[command.index("--device") + 1], "cpu")

    def test_choosing_unloads_the_model_so_its_next_load_uses_the_new_device(self):
        pool = self.pool()
        worker = FakeWorker()
        pool.workers["kokoro"] = {"worker": worker, "loaded": True, "runtime_pid": None}
        with mock.patch.object(worker_pool, "process_is_running", return_value=False):
            answer = pool.choose_device("kokoro", "cpu")
        self.assertEqual(answer, {"engine": "kokoro", "choice": "cpu", "device": "cpu", "unloaded": True})
        self.assertEqual(worker.calls, ["unload"])
        self.assertEqual(pool.workers, {})
        self.assertEqual(self.store.get("kokoro"), "cpu")
        self.assertEqual(
            pool.choose_device("kokoro", "gpu"), {"engine": "kokoro", "choice": "gpu", "device": "cuda", "unloaded": False}
        )

    def test_choices_that_cannot_be_made_are_refused(self):
        pool = self.pool()
        for engine, choice, words in (
            ("kokoro", "vulkan", "Choose gpu or cpu"),
            ("phonon-31m-en", "cpu", "only on the CPU"),
            ("qwen3-tts-0.6b-base", "cpu", "only on a graphics card"),
            ("not-an-engine", "cpu", "Unknown engine"),
        ):
            with self.subTest(engine=engine), self.assertRaisesRegex(ValueError, words):
                pool.choose_device(engine, choice)
        with self.assertRaisesRegex(ValueError, "runs every model on the CPU"):
            self.pool("cpu").choose_device("kokoro", "gpu")
        pool.hold("Kokoro 82M is being installed")
        try:
            with self.assertRaisesRegex(WorkerBusy, "being installed"):
                pool.choose_device("kokoro", "cpu")
        finally:
            pool.release_hold()
        self.assertIsNone(self.store.get("kokoro"))

    def test_the_status_gives_each_engine_its_switch_and_its_check_on_that_device(self):
        pool = self.pool()
        pool.gpu_install = lambda key: GPU_OK
        store = engine_checks.EngineCheckStore(Path(self.temp.name) / "checks.json")
        pool.check_store = store
        cpu_check = engine_checks.check_record(
            status="passed", device="cuda", ran_on="cpu", fingerprint="pin", total_s=1, audio_seconds=2
        )
        store.record_check("turbo", "cuda", cpu_check)  # made before the switch: asked for the card, ran on the CPU
        store.record_check("kokoro", "cuda", engine_checks.check_record(status="passed", device="cuda", fingerprint="pin"))
        rows = {row["id"]: row for row in pool.engine_status()["engines"]}
        self.assertEqual(
            (rows["kokoro"]["device"], rows["kokoro"]["switch"], rows["kokoro"]["state"]), ("cuda", "gpu", "usable")
        )
        self.assertEqual((rows["turbo"]["device"], rows["turbo"]["switch"]), ("cpu", "cpu"))
        self.assertEqual(rows["turbo"]["check"]["ran_on"], "cpu")  # still counts, as a check on the CPU
        self.assertEqual(rows["turbo"]["state"], "usable")
        self.assertEqual(
            rows["turbo"]["gpu"], {"choosable": True, "reason": None, "build": "cpu", "install": False, "cpu": None}
        )
        self.assertEqual(rows["phonon-31m-en"]["gpu"]["reason"], engine_devices.CPU_ONLY["phonon-31m-en"])
        self.store.set("kokoro", "cpu")
        kokoro = next(row for row in pool.engine_status()["engines"] if row["id"] == "kokoro")
        self.assertEqual((kokoro["device"], kokoro["state"], kokoro["check"]), ("cpu", "not_checked", None))

    def test_a_gpu_only_model_names_its_launcher_only_where_the_app_can_install_it(self):
        pool = self.pool()
        missing = {"installed": False, "missing": ["x"], "environment": "e", "checkpoint": "c", "installer": "setup-qwen.bat"}
        pool.install_probe = lambda entry: dict(missing)
        refused = (False, "The app installs GPU builds for NVIDIA graphics cards only, and this computer has no NVIDIA driver.")
        for answer, named in ((GPU_OK, True), (refused, False)):
            with self.subTest(answer=answer):
                pool.gpu_install = lambda key, answer=answer: answer
                rows = {row["id"]: row for row in pool.engine_status()["engines"]}
                qwen = rows["qwen3-tts-0.6b-base"]
                self.assertEqual("installer" in qwen["installation"], named)
                self.assertEqual(qwen["gpu"]["install"], named)
                self.assertIn("installer", rows["kokoro"]["installation"])  # a model with a CPU build keeps its launcher

    def test_a_check_is_kept_under_the_device_the_engine_ran_on(self):
        pool = self.pool()
        pool.check_store = engine_checks.EngineCheckStore(Path(self.temp.name) / "checks.json")
        self.store.set("kokoro", "cpu")
        with (
            mock.patch.object(pool, "load", return_value={"load_seconds": 1.0}),
            mock.patch.object(
                pool,
                "speak",
                return_value=(__import__("numpy").full(24000, 0.1, "float32"), 24000, {"total_s": 0.5, "device": "cpu"}),
            ),
        ):
            record = pool.check("kokoro")
        self.assertEqual((record["status"], record["device"], record["ran_on"]), ("passed", "cpu", "cpu"))
        self.assertEqual(set(pool.check_store.checks_for("kokoro")), {"cpu"})


if __name__ == "__main__":
    unittest.main()
