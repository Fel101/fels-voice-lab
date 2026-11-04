"""Which device the service asks for, and which one a worker really uses. Nothing here loads a model or needs a GPU."""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import common  # noqa: E402
import hardware  # noqa: E402
import worker_runtime  # noqa: E402


def fake_torch(*, gpu):
    return types.SimpleNamespace(
        __version__="2.12.0+cu130" if gpu else "2.12.0+cpu", cuda=types.SimpleNamespace(is_available=lambda: gpu)
    )


def no_torch():
    raise ModuleNotFoundError("No module named 'torch'", name="torch")


class ServiceDeviceTests(unittest.TestCase):
    def pick(self, want="auto", *, setting=None, nvidia=False, torch=no_torch):
        settings = {} if setting is None else {"device": setting}
        with mock.patch.dict(common._settings, settings, clear=True), mock.patch.object(common, "torch_runtime", torch):
            return common.pick_device(want, nvidia_present=lambda: nvidia)

    def test_an_explicit_choice_is_kept(self):
        self.assertEqual(self.pick("cpu", nvidia=True), "cpu")
        self.assertEqual(self.pick("cuda"), "cuda")

    def test_auto_takes_the_setting_in_runtime_local_json_first(self):
        self.assertEqual(self.pick(setting="cpu", nvidia=True), "cpu")
        self.assertEqual(self.pick(setting="cuda"), "cuda")
        self.assertEqual(self.pick(setting="auto", nvidia=True), "cuda")
        with self.assertRaisesRegex(ValueError, r'"device" must be one of auto, cpu, cuda, not \'gpu\''):
            self.pick(setting="gpu")

    def test_an_nvidia_driver_means_the_graphics_card_even_without_pytorch_in_the_service(self):
        # The service's .venv holds no PyTorch: the driver is what tells a computer with an NVIDIA card.
        self.assertEqual(self.pick(nvidia=True), "cuda")
        self.assertEqual(self.pick(nvidia=False), "cpu")

    def test_without_an_nvidia_driver_a_gpu_build_of_pytorch_in_the_service_still_counts(self):
        # An older install, or an AMD card with a ROCm build of PyTorch in .venv, which answers to "cuda" too.
        self.assertEqual(self.pick(torch=lambda: fake_torch(gpu=True)), "cuda")
        self.assertEqual(self.pick(torch=lambda: fake_torch(gpu=False)), "cpu")

    def test_the_service_and_the_desktop_launcher_accept_the_same_devices(self):
        self.assertEqual(common.DEVICES, ("auto", "cpu", "cuda"))
        self.assertIn('choices=("auto", "cpu", "cuda")', (ROOT / "desktop.py").read_text(encoding="utf-8"))
        self.assertIn("choices=DEVICES", (ROOT / "serve.py").read_text(encoding="utf-8"))


class WorkerDeviceTests(unittest.TestCase):
    def usable(self, engine, device, torch):
        with mock.patch.object(worker_runtime, "torch_runtime", torch):
            return worker_runtime.usable_device(engine, device)

    def test_a_pytorch_that_can_use_the_card_gets_it(self):
        self.assertEqual(self.usable("kokoro", "cuda", lambda: fake_torch(gpu=True)), ("cuda", None))

    def test_a_cpu_build_of_pytorch_runs_on_the_cpu_and_says_why(self):
        device, note = self.usable("turbo", "cuda", lambda: fake_torch(gpu=False))
        self.assertEqual(device, "cpu")
        self.assertIn("PyTorch (2.12.0+cpu) cannot use a graphics card", note)
        self.assertEqual(self.usable("kokoro", "cuda", no_torch), ("cpu", "PyTorch is not installed in this environment"))

    def test_the_cpu_and_the_engines_that_do_not_run_on_pytorch_are_left_as_asked(self):
        broken = mock.Mock(side_effect=AssertionError("looked at PyTorch"))
        self.assertEqual(self.usable("kokoro", "cpu", broken), ("cpu", None))
        self.assertEqual(self.usable("phonon-31m-en", "cuda", broken), ("cuda", None))
        self.assertEqual(self.usable("drama-3", "cuda", broken), ("cuda", None))
        self.assertEqual(worker_runtime.TORCH_ENGINE_IDS, worker_runtime.ENGINE_IDS - {"phonon-31m-en", "drama-3"})

    def test_a_worker_that_fell_back_reports_it_in_its_health(self):
        with mock.patch.object(worker_runtime, "torch_runtime", lambda: fake_torch(gpu=False)), mock.patch("sys.stdout"):
            runtime = worker_runtime.EngineWorkerRuntime("kokoro", "cuda")
            health = runtime.health()
        self.assertEqual((runtime.device, health["device"]), ("cpu", "cpu"))
        self.assertIn("cannot use a graphics card", health["device_note"])


class HardwareTests(unittest.TestCase):
    def test_the_nvidia_driver_is_found_by_its_dll_in_system32(self):
        with tempfile.TemporaryDirectory() as windows:
            self.assertFalse(hardware.nvidia_driver_present(windows))
            (Path(windows) / "System32").mkdir()
            (Path(windows) / "System32" / "nvcuda.dll").write_bytes(b"")
            self.assertEqual(hardware.nvidia_driver_present(windows), sys.platform == "win32")

    @unittest.skipUnless(sys.platform == "win32", "Windows names its processor and graphics cards in the registry")
    def test_this_computer_names_its_processor_and_only_real_graphics_cards(self):
        self.assertTrue(hardware.processor_name())
        cards = hardware.graphics_cards()
        self.assertIsInstance(cards, list)
        for card in cards:
            self.assertEqual(set(card), {"name", "driver"})
            self.assertFalse(card["name"].startswith("Microsoft "), card)
        self.assertEqual(len(cards), len({(card["name"], card["driver"]) for card in cards}))


if __name__ == "__main__":
    unittest.main()
