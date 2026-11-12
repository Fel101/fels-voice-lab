"""Which device the service asks for, and which one a worker really uses. Nothing here loads a model or needs a GPU."""

import json
import subprocess
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


def cuda_torch(
    capability, arch_list, *, version="2.6.0+cu126", cuda="12.6", hip=None, name="NVIDIA GeForce RTX 5070 Laptop GPU"
):
    """A CUDA build of PyTorch that sees one card, as PyTorch 2.6 sees an RTX 50 series card: available, with no code for it."""
    return types.SimpleNamespace(
        __version__=version,
        version=types.SimpleNamespace(cuda=cuda, hip=hip),
        cuda=types.SimpleNamespace(
            is_available=lambda: True,
            get_device_capability=lambda index=0: capability,
            get_arch_list=lambda: list(arch_list),
            get_device_name=lambda index=0: name,
        ),
    )


# What torch.cuda.get_arch_list() answers in the CUDA builds the installers pin.
ARCH_CU126 = ["sm_50", "sm_60", "sm_70", "sm_75", "sm_80", "sm_86", "sm_90"]
ARCH_CU130 = ["sm_75", "sm_80", "sm_86", "sm_90", "sm_100", "sm_120", "compute_120"]


class ServiceDeviceTests(unittest.TestCase):
    def pick(self, want="auto", *, setting=None, nvidia=False, amd=False, torch=no_torch):
        settings = {} if setting is None else {"device": setting}
        usable = amd if callable(amd) else (lambda: amd)
        with mock.patch.dict(common._settings, settings, clear=True), mock.patch.object(common, "torch_runtime", torch):
            return common.pick_device(want, nvidia_present=lambda: nvidia, amd_usable=usable)

    def test_an_amd_card_that_amds_build_runs_on_means_the_graphics_card_too(self):
        # AMD's ROCm build of PyTorch answers to "cuda" as well, and the service's .venv holds no PyTorch to ask.
        self.assertEqual(self.pick(amd=True), "cuda")
        self.assertEqual(self.pick(setting="cpu", amd=True), "cpu")
        self.assertEqual(self.pick("cpu", amd=True), "cpu")
        asked = mock.Mock(side_effect=AssertionError("asked AMD's driver"))
        self.assertEqual(self.pick(nvidia=True, amd=asked), "cuda")  # an NVIDIA driver decides first
        self.assertEqual(self.pick(setting="cuda", amd=asked), "cuda")

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

    def test_a_cuda_build_with_no_code_for_the_card_runs_on_the_cpu_and_names_the_card(self):
        # Chatterbox's PyTorch 2.6 on an RTX 50 series card: available, but every kernel launch would fail.
        device, note = self.usable("turbo", "cuda", lambda: cuda_torch((12, 0), ARCH_CU126))
        self.assertEqual(device, "cpu")
        self.assertIn(
            "PyTorch (2.6.0+cu126) has no code for the NVIDIA GeForce RTX 5070 Laptop GPU (compute capability 12.0)", note
        )
        # Kokoro's CUDA 13.0 build has code for it.
        self.assertEqual(
            self.usable("kokoro", "cuda", lambda: cuda_torch((12, 0), ARCH_CU130, version="2.12.0+cu130")), ("cuda", None)
        )

    def test_code_for_an_older_card_of_the_same_generation_or_ptx_runs_and_newer_code_does_not(self):
        self.assertTrue(worker_runtime.runs_on((8, 9), ARCH_CU126))  # an RTX 40 card runs the sm_86 code
        self.assertTrue(worker_runtime.runs_on((7, 5), ARCH_CU130))
        self.assertFalse(worker_runtime.runs_on((6, 1), ARCH_CU130))  # a GTX 10 card is older than CUDA 13.0 has code for
        self.assertFalse(worker_runtime.runs_on((8, 0), ["sm_86", "sm_90"]))  # sm_86 code does not run on an 8.0 card
        self.assertTrue(worker_runtime.runs_on((12, 0), ["sm_80", "compute_90"]))  # PTX is compiled for newer cards
        self.assertFalse(worker_runtime.runs_on((9, 0), ["sm_90a", "gfx1100"]))  # names it does not know are not guessed at

    def test_a_rocm_build_or_one_that_lists_no_architectures_is_left_to_pytorch(self):
        rocm = cuda_torch((11, 0), ["gfx1100"], version="2.6.0+rocm6.2", cuda=None, hip="6.2", name="AMD Radeon RX 7900")
        self.assertEqual(self.usable("kokoro", "cuda", lambda: rocm), ("cuda", None))
        self.assertEqual(self.usable("kokoro", "cuda", lambda: cuda_torch((12, 0), [])), ("cuda", None))

    def test_a_card_pytorch_cannot_read_runs_the_engine_on_the_cpu(self):
        broken = cuda_torch((12, 0), ARCH_CU130)
        broken.cuda.get_device_capability = mock.Mock(side_effect=RuntimeError("CUDA error: unknown error"))
        device, note = self.usable("kokoro", "cuda", lambda: broken)
        self.assertEqual(device, "cpu")
        self.assertIn("could not read the graphics card (RuntimeError: CUDA error: unknown error)", note)

    def test_a_worker_that_fell_back_reports_it_in_its_health(self):
        with mock.patch.object(worker_runtime, "torch_runtime", lambda: fake_torch(gpu=False)), mock.patch("sys.stdout"):
            runtime = worker_runtime.EngineWorkerRuntime("kokoro", "cuda")
            health = runtime.health()
        self.assertEqual((runtime.device, health["device"]), ("cpu", "cpu"))
        self.assertIn("cannot use a graphics card", health["device_note"])

    def test_miopen_is_switched_off_only_for_amds_build_on_the_card(self):
        def switched(device, *, hip):
            torch = types.SimpleNamespace(
                version=types.SimpleNamespace(hip=hip),
                backends=types.SimpleNamespace(cudnn=types.SimpleNamespace(enabled=True)),
            )
            with mock.patch.object(common, "torch_runtime", lambda: torch):
                common.skip_miopen(device)
            return torch.backends.cudnn.enabled

        self.assertFalse(switched("cuda", hip="7.14.60850"))  # MIOpen sits behind the cuDNN switch on ROCm
        self.assertTrue(switched("cuda", hip=None))  # NVIDIA's cuDNN stays on
        self.assertTrue(switched("cpu", hip="7.14.60850"))


class HardwareTests(unittest.TestCase):
    def test_the_nvidia_driver_is_found_by_its_dll_in_system32(self):
        with tempfile.TemporaryDirectory() as windows:
            self.assertFalse(hardware.nvidia_driver_present(windows))
            (Path(windows) / "System32").mkdir()
            (Path(windows) / "System32" / "nvcuda.dll").write_bytes(b"")
            self.assertEqual(hardware.nvidia_driver_present(windows), sys.platform == "win32")

    def test_nvidia_driver_versions_are_read_from_the_last_five_digits_of_the_windows_version(self):
        self.assertEqual(hardware.nvidia_driver_version("32.0.15.8180"), (581, 80))
        self.assertEqual(hardware.nvidia_driver_version("31.0.15.5222"), (552, 22))
        self.assertEqual(hardware.nvidia_driver_version("32.0.15.7283"), (572, 83))
        for unreadable in (None, "", "31.0.101.5333x", "581.80"):
            self.assertIsNone(hardware.nvidia_driver_version(unreadable))
        self.assertEqual(hardware.driver_text((581, 8)), "581.08")
        self.assertEqual((hardware.version_pair(" 12.0 "), hardware.version_pair("[N/A]")), ((12, 0), None))

    def smi(self, *answers):
        """Stands in for subprocess.run with nvidia-smi: each call takes the next (exit code, output) in turn."""
        calls, answers = [], list(answers)

        def run(command, **kwargs):
            calls.append(command)
            code, output = answers.pop(0)
            return types.SimpleNamespace(returncode=code, stdout=output, stderr="")

        return run, calls

    def test_nvidia_smi_names_each_card_its_generation_and_the_driver(self):
        run, calls = self.smi(
            (0, "NVIDIA GeForce RTX 5070 Laptop GPU, 12.0, 581.80, 8151 MiB\nNVIDIA GeForce RTX 3060, 8.6, 581.80, [N/A]\n")
        )
        cards = hardware.nvidia_cards(run=run, smi=Path("nvidia-smi.exe"), listed=[], probe=False)
        self.assertEqual(
            cards,
            [
                {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": (12, 0), "driver": (581, 80), "memory_mib": 8151},
                {"name": "NVIDIA GeForce RTX 3060", "capability": (8, 6), "driver": (581, 80), "memory_mib": None},
            ],
        )
        self.assertEqual(calls[0][1:], ["--query-gpu=name,compute_cap,driver_version,memory.total", "--format=csv,noheader"])

    def test_an_nvidia_smi_too_old_for_compute_cap_is_asked_for_the_rest(self):
        run, calls = self.smi(
            (6, 'Field "compute_cap" is not a valid field to query.'), (0, "NVIDIA GeForce GTX 1080, 472.12, 8192 MiB\n")
        )
        cards = hardware.nvidia_cards(run=run, smi=Path("nvidia-smi.exe"), listed=[], probe=False)
        self.assertEqual(
            cards, [{"name": "NVIDIA GeForce GTX 1080", "capability": None, "driver": (472, 12), "memory_mib": 8192}]
        )
        self.assertEqual(len(calls), 2)

    def test_without_nvidia_smi_the_cards_windows_lists_give_the_names_and_drivers(self):
        listed = [
            {"name": "AMD Radeon 780M Graphics", "driver": "32.0.12011.1036"},
            {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "driver": "32.0.15.8180"},
        ]
        run, _calls = self.smi((1, "NVIDIA-SMI has failed"), (1, "NVIDIA-SMI has failed"))
        for smi in (False, Path("nvidia-smi.exe")):
            with self.subTest(smi=smi):
                self.assertEqual(
                    hardware.nvidia_cards(run=run, smi=smi, listed=listed, probe=False),
                    [
                        {
                            "name": "NVIDIA GeForce RTX 5070 Laptop GPU",
                            "capability": None,
                            "driver": (581, 80),
                            "memory_mib": None,
                        }
                    ],
                )
        self.assertEqual(hardware.nvidia_cards(smi=False, listed=listed[:1], probe=False), [])

    # The RTX 5070 laptop, whose Radeon 890M drives the display, as NVIDIA's driver answered with its card switched off and on.
    LAPTOP = [
        {"name": "AMD Radeon(TM) 890M Graphics", "driver": "32.0.31035.1003"},
        {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "driver": "32.0.16.1714"},
    ]
    OFF = {
        "error": "CUDA_ERROR_NO_DEVICE: no CUDA-capable device is detected",
        "code": 100,
        "driver_cuda": (13, 4),
        "cards": [],
    }
    ON = {
        "error": None,
        "code": None,
        "driver_cuda": (13, 4),
        "cards": [{"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": (12, 0), "memory_mib": 8151}],
    }

    def test_a_card_the_driver_cannot_start_cuda_on_is_marked_unavailable(self):
        # Switched off, the card stays in Windows' list with its driver, and nvidia-smi blames permissions.
        failed = (4, "NVIDIA-SMI has failed because you do not have sufficient permissions.")
        run, _calls = self.smi(failed, failed)
        self.assertEqual(
            hardware.nvidia_cards(run=run, smi=Path("nvidia-smi.exe"), listed=self.LAPTOP, probe=self.OFF),
            [
                {
                    "name": "NVIDIA GeForce RTX 5070 Laptop GPU",
                    "capability": None,
                    "driver": (617, 14),
                    "memory_mib": None,
                    "unavailable": "CUDA_ERROR_NO_DEVICE: no CUDA-capable device is detected",
                }
            ],
        )
        # nvidia-smi can list a card that CUDA is kept from (CUDA_VISIBLE_DEVICES=-1): the driver decides.
        run, _calls = self.smi((0, "NVIDIA GeForce RTX 5070 Laptop GPU, 12.0, 617.14, 8151 MiB\n"))
        [card] = hardware.nvidia_cards(run=run, smi=Path("nvidia-smi.exe"), listed=[], probe=self.OFF)
        self.assertEqual((card["memory_mib"], card["unavailable"]), (8151, self.OFF["error"]))
        # A driver with no card Windows lists still has one to name.
        [card] = hardware.nvidia_cards(smi=False, listed=[], probe=self.OFF)
        self.assertEqual((card["name"], card["unavailable"]), ("NVIDIA graphics card", self.OFF["error"]))

    def test_without_nvidia_smi_the_driver_gives_the_generation_and_memory(self):
        self.assertEqual(
            hardware.nvidia_cards(smi=False, listed=self.LAPTOP, probe=self.ON),
            [{"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": (12, 0), "memory_mib": 8151, "driver": (617, 14)}],
        )
        # An nvidia-smi from before compute_cap: the driver fills in the generation.
        run, _calls = self.smi(
            (6, 'Field "compute_cap" is not a valid field to query.'),
            (0, "NVIDIA GeForce RTX 5070 Laptop GPU, 617.14, [N/A]\n"),
        )
        [card] = hardware.nvidia_cards(run=run, smi=Path("nvidia-smi.exe"), listed=[], probe=self.ON)
        self.assertEqual((card["capability"], card["memory_mib"], card["driver"]), ((12, 0), 8151, (617, 14)))
        self.assertNotIn("unavailable", card)

    @unittest.skipUnless(sys.platform == "win32", "only Windows has NVIDIA's nvcuda.dll for the probe to ask")
    def test_the_driver_probe_is_read_from_its_last_line_and_anything_else_is_no_answer(self):
        def answer(code, output):
            return lambda command, **kwargs: types.SimpleNamespace(returncode=code, stdout=output, stderr="")

        def hang(command, **kwargs):
            raise subprocess.TimeoutExpired(command, hardware.CUDA_TIMEOUT)

        off = {"error": "CUDA_ERROR_NO_DEVICE: no CUDA-capable device is detected", "code": 100, "driver_cuda": [13, 4]}
        on = {
            "error": None,
            "code": None,
            "driver_cuda": [13, 4],
            "cards": [{"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": [12, 0], "memory_mib": 8151}],
        }
        with tempfile.TemporaryDirectory() as windows:
            self.assertIsNone(hardware.cuda_probe(run=answer(0, json.dumps(on)), system_root=windows))  # no driver: not asked
            (Path(windows) / "System32").mkdir()
            (Path(windows) / "System32" / "nvcuda.dll").write_bytes(b"")
            asked = hardware.cuda_probe(
                run=answer(0, "a warning\n" + json.dumps({**off, "cards": []}) + "\n"), system_root=windows
            )
            self.assertEqual(asked, self.OFF)
            self.assertEqual(hardware.cuda_probe(run=answer(0, json.dumps(on)), system_root=windows), self.ON)
            # A driver that starts CUDA but lists no card has none to use.
            empty = hardware.cuda_probe(run=answer(0, json.dumps({**on, "cards": []})), system_root=windows)
            self.assertEqual((empty["code"], empty["cards"]), (100, []))
            for broken in (answer(3, json.dumps(on)), answer(0, "no answer"), answer(0, json.dumps(off)), hang):
                self.assertIsNone(hardware.cuda_probe(run=broken, system_root=windows))

    @unittest.skipUnless(sys.platform == "win32", "the probe calls NVIDIA's Windows driver")
    def test_the_driver_probe_itself_runs_here_and_answers_in_one_line(self):
        done = subprocess.run(
            [sys.executable, "-I", "-c", hardware.CUDA_PROBE], capture_output=True, text=True, encoding="utf-8", timeout=60
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        found = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(set(found), {"error", "code", "driver_cuda", "cards"})
        if not hardware.nvidia_driver_present():
            self.assertIn("nvcuda.dll could not be loaded", found["error"])
            self.assertIsNone(hardware.cuda_probe())

    def test_nvidia_smi_is_looked_for_in_system32_then_in_program_files(self):
        with (
            tempfile.TemporaryDirectory() as windows,
            tempfile.TemporaryDirectory() as programs,
            mock.patch.object(hardware.shutil, "which", lambda name: None),
        ):
            self.assertIsNone(hardware.nvidia_smi_path(windows, programs))
            old = Path(programs) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"")
            self.assertEqual(hardware.nvidia_smi_path(windows, programs), old)
            new = Path(windows) / "System32" / "nvidia-smi.exe"
            new.parent.mkdir()
            new.write_bytes(b"")
            self.assertEqual(hardware.nvidia_smi_path(windows, programs), new)

    @unittest.skipUnless(sys.platform == "win32", "Windows names its processor and graphics cards in the registry")
    def test_this_computer_names_its_processor_and_only_real_graphics_cards(self):
        self.assertTrue(hardware.processor_name())
        cards = hardware.graphics_cards()
        self.assertIsInstance(cards, list)
        for card in cards:
            self.assertEqual(set(card), {"name", "driver"})
            self.assertFalse(card["name"].startswith("Microsoft "), card)
        self.assertEqual(len(cards), len({(card["name"], card["driver"]) for card in cards}))
        for card in hardware.nvidia_cards():
            self.assertLessEqual({"name", "capability", "driver", "memory_mib"}, set(card))
            self.assertLessEqual(set(card), {"name", "capability", "driver", "memory_mib", "unavailable"})


if __name__ == "__main__":
    unittest.main()
