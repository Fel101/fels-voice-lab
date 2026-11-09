"""tools/amd_build.py and hardware.amd_cards: AMD's ROCm build of PyTorch for the models the Lab runs only on a graphics card.
Nothing here reaches the network, pip, a graphics driver or PyTorch."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hardware  # noqa: E402
from tools import amd_build as amd  # noqa: E402
from tools import install_kokoro as base  # noqa: E402

# The mini PC the build was checked on: the Radeon 780M in its Ryzen processor, which may use 37 GB of the system's memory,
# and a Radeon AI PRO R9700 with 32 GB of its own.
RADEON_780M = {
    "name": "AMD Radeon 780M Graphics",
    "chip": "gfx1103",
    "memory_mib": 37247,
    "integrated": True,
    "bus": 201,
    "driver": "32.0.31041.1004",
}
R9700 = {
    "name": "AMD Radeon AI PRO R9700",
    "chip": "gfx1201",
    "memory_mib": 32624,
    "integrated": False,
    "bus": 3,
    "driver": "32.0.31041.1004",
}
RX_7600 = {"name": "AMD Radeon RX 7600", "chip": "gfx1102", "memory_mib": 8176, "integrated": False, "bus": 8, "driver": None}
RADEON_VII = {"name": "AMD Radeon VII", "chip": "gfx906", "memory_mib": 16368, "integrated": False, "bus": 4, "driver": None}
# PyTorch's list on that computer: it numbers the card built into the processor first.
DEVICES = [
    {"index": 0, "name": "AMD Radeon 780M Graphics", "chip": "gfx1103", "bus": 201, "memory_mib": 37247},
    {"index": 1, "name": "AMD Radeon AI PRO R9700", "chip": "gfx1201", "bus": 3, "memory_mib": 32624},
]
CHIPS = [
    "gfx1010",
    "gfx1011",
    "gfx1012",
    "gfx1030",
    "gfx1031",
    "gfx1032",
    "gfx1033",
    "gfx1034",
    "gfx1035",
    "gfx1036",
    "gfx1100",
    "gfx1101",
    "gfx1102",
    "gfx1103",
    "gfx1150",
    "gfx1151",
    "gfx1152",
    "gfx1153",
    "gfx1200",
    "gfx1201",
]
MODEL_LOCKS = ("requirements/qwen-nvidia.txt", "requirements/voxcpm-nvidia.txt", "requirements/breeze-nvidia.txt")
QWEN_LOCK = MODEL_LOCKS[0]


def lab(folder, settings=None):
    """A lab folder holding the shipped AMD list and Qwen's list, and runtime.local.json when settings are given."""
    root = Path(folder)
    (root / "requirements").mkdir()
    for name in (amd.LOCK, QWEN_LOCK):
        shutil.copyfile(ROOT / name, root / name)
    if settings is not None:
        (root / amd.SETTINGS).write_text(json.dumps(settings), "utf-8")
    return root


class ShippedListTests(unittest.TestCase):
    def test_every_line_pins_one_file_from_amds_index_by_its_sha256(self):
        lines = [line for line in (ROOT / amd.LOCK).read_text("utf-8").splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(lines), 48)
        for line in lines:
            with self.subTest(line=line[:60]):
                self.assertRegex(
                    line, r"^[a-z0-9-]+ @ https://repo\.amd\.com/rocm/whl-multi-arch/\S+ --hash=sha256:[0-9a-f]{64}$"
                )
        sources = [line.split()[0] for line in lines if not line.split()[2].endswith(".whl")]
        self.assertEqual(sources, [amd.SOURCE_PACKAGE])  # the one package pip builds, after the wheels

    def test_it_holds_pytorch_the_runtime_and_both_code_packages_of_each_chip(self):
        versions = {item["key"]: item["version"] for item in amd.lock_packages(ROOT)}
        self.assertEqual((versions["torch"], versions["torchaudio"]), ("2.12.0+rocm7.14.0", "2.11.0+rocm7.14.0"))
        for key in ("rocm", "rocm-sdk-core", "rocm-sdk-libraries"):
            self.assertEqual(versions[key], "7.14.0")
        self.assertIn("rocm-bootstrap", versions)
        self.assertEqual(amd.chips(ROOT), CHIPS)
        for chip in CHIPS:
            for key in amd.chip_packages(chip):
                self.assertIn(key, versions)
        self.assertEqual(sum(amd.is_device_package(key) for key in versions), 2 * len(CHIPS) + 2)

    def test_the_model_lists_pin_the_same_pytorch_release_and_the_setuptools_that_builds_rocm(self):
        ours = {item["key"]: item["version"] for item in amd.lock_packages(ROOT)}
        for lock in MODEL_LOCKS:
            with self.subTest(lock=lock):
                versions = {item["key"]: item["version"] for item in base.read_lock(ROOT / lock)}
                for key in amd.SWAPS:
                    self.assertEqual(versions[key].partition("+")[0], ours[key].partition("+")[0])
                self.assertIn("setuptools", versions)  # rocm is built with it, without build isolation


class PackageTests(unittest.TestCase):
    def test_a_model_gets_amds_pytorch_the_runtime_and_the_code_for_its_chip_alone(self):
        model = {item["key"]: item["version"] for item in base.read_lock(ROOT / QWEN_LOCK)}
        packages = amd.pinned_packages(ROOT, QWEN_LOCK, "gfx1201")
        keys = [item["key"] for item in packages]
        self.assertEqual(len(keys), len(set(keys)))
        versions = {item["key"]: item["version"] for item in packages}
        self.assertEqual((versions["torch"], versions["torchaudio"]), ("2.12.0+rocm7.14.0", "2.11.0+rocm7.14.0"))
        self.assertEqual(
            sorted(key for key in keys if amd.is_device_package(key)),
            ["amd-torch-device-gfx12-0", "amd-torch-device-gfx1201", "rocm-sdk-device-gfx1201"],
        )
        added = set(keys) - set(model)
        self.assertEqual(
            added,
            {
                "rocm",
                "rocm-bootstrap",
                "rocm-sdk-core",
                "rocm-sdk-libraries",
                "amd-torch-device-gfx12-0",
                "amd-torch-device-gfx1201",
                "rocm-sdk-device-gfx1201",
            },
        )
        for key, version in model.items():
            if key not in amd.SWAPS:
                self.assertEqual(versions[key], version)

    def test_a_chip_of_rdna_3_or_4_also_gets_its_familys_kernels(self):
        self.assertEqual(
            amd.chip_packages("gfx1103"), ["amd-torch-device-gfx11", "amd-torch-device-gfx1103", "rocm-sdk-device-gfx1103"]
        )
        self.assertEqual(
            amd.chip_packages("gfx1201"), ["amd-torch-device-gfx12-0", "amd-torch-device-gfx1201", "rocm-sdk-device-gfx1201"]
        )
        self.assertEqual(amd.chip_packages("gfx1030"), ["amd-torch-device-gfx1030", "rocm-sdk-device-gfx1030"])

    def test_the_sizes_add_the_shared_packages_and_the_chips_own_to_the_models_others(self):
        self.assertEqual(amd.chip_sizes_mb("gfx1100"), (680, 920))
        self.assertEqual(amd.chip_sizes_mb("gfx1201"), (520, 760))
        self.assertEqual(amd.chip_sizes_mb("gfx1030"), (100, 120))
        self.assertEqual(
            amd.sizes_mb("gfx1201", 160, 800), (160 + amd.SHARED_DOWNLOAD_MB + 520, 800 + amd.SHARED_DISK_MB + 760)
        )
        # Installed for the R9700, torch, torchaudio, the runtime and the chip's code took 4169 MB.
        self.assertGreaterEqual(amd.SHARED_DISK_MB + 760, 4169)

    def test_pip_reads_the_wheels_from_one_list_and_rocms_source_from_another(self):
        with tempfile.TemporaryDirectory() as folder:
            root = lab(folder)
            wheels, source = amd.write_requirements(root, QWEN_LOCK, "gfx1201", root / "out" / "qwen")
            self.assertEqual((wheels.name, source.name), ("requirements-amd.txt", "requirements-amd-source.txt"))
            written = base.lock_lines(wheels)
            self.assertEqual(
                sorted(written),
                sorted(item["key"] for item in amd.pinned_packages(root, QWEN_LOCK, "gfx1201") if item["key"] != "rocm"),
            )
            self.assertTrue(written["torch"].startswith("torch @ https://repo.amd.com/rocm/whl-multi-arch/"))
            self.assertEqual(written["transformers"], base.lock_lines(root / QWEN_LOCK)["transformers"])
            self.assertTrue(all("--hash=sha256:" in line for line in written.values()))
            self.assertEqual(base.lock_lines(source), {"rocm": base.lock_lines(root / amd.LOCK)["rocm"]})

    def run_install(self, codes):
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=codes[len(commands) - 1])

        with tempfile.TemporaryDirectory() as folder:
            root = lab(folder)
            amd.install_packages(root, ".venv-fqwen", QWEN_LOCK, "gfx1201", root / "out" / "qwen", runner=runner)
            return commands, str(root / ".venv-fqwen" / "Scripts" / "python.exe")

    def test_the_wheels_go_in_first_and_rocm_is_built_with_what_they_put_in(self):
        (wheels, source), python = self.run_install([0, 0])
        self.assertEqual((wheels[0], source[0]), (python, python))
        self.assertIn("--only-binary=:all:", wheels)
        self.assertTrue(wheels[-1].endswith("requirements-amd.txt"))
        for flag in ("--no-deps", "--require-hashes", "--no-build-isolation", "--no-binary=rocm"):
            self.assertIn(flag, source)
        self.assertTrue(source[-1].endswith("requirements-amd-source.txt"))

    def test_pip_failing_on_either_list_stops_with_what_to_do(self):
        with self.assertRaisesRegex(amd.InstallError, "pip stopped with exit code 1"):
            self.run_install([1])
        with self.assertRaisesRegex(amd.InstallError, "while it built rocm, the part of AMD's ROCm runtime"):
            self.run_install([0, 1])


class CardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-amd-")
        self.addCleanup(temp.cleanup)
        self.root = lab(temp.name)

    def problems(self, cards, environ=None, **options):
        return amd.card_problems(cards, model="VoxCPM2", root=self.root, environ=environ or {}, **options)

    def test_a_separate_card_comes_before_one_built_into_the_processor_then_the_most_memory(self):
        self.assertIs(amd.choose_card([RADEON_780M, R9700], CHIPS), R9700)
        self.assertIs(amd.choose_card([R9700, RADEON_780M], CHIPS), R9700)
        self.assertIs(amd.choose_card([RX_7600, R9700], CHIPS), R9700)
        self.assertIs(amd.choose_card([RADEON_780M], CHIPS), RADEON_780M)
        self.assertIs(amd.choose_card([RADEON_VII, RADEON_780M], CHIPS), RADEON_780M)
        self.assertIsNone(amd.choose_card([RADEON_VII], CHIPS))
        self.assertIsNone(amd.choose_card([], CHIPS))

    def test_a_card_a_setting_names_is_used_whatever_it_is(self):
        self.assertIs(amd.choose_card([RADEON_780M, R9700], CHIPS, visible=0), RADEON_780M)
        self.assertIs(amd.choose_card([RADEON_VII, R9700], CHIPS, visible=0), RADEON_VII)
        self.assertIsNone(amd.choose_card([R9700], CHIPS, visible=1))

    def test_the_setting_comes_from_hip_visible_devices_and_then_runtime_local_json(self):
        settings = self.root / amd.SETTINGS
        self.assertEqual(amd.visible_setting(self.root, {}), (None, None))
        settings.write_text('{"hip_visible_devices": "1"}', "utf-8")
        self.assertEqual(amd.visible_setting(self.root, {}), ("runtime.local.json", 1))
        self.assertEqual(amd.visible_setting(self.root, {"HIP_VISIBLE_DEVICES": "0,1"}), ("HIP_VISIBLE_DEVICES", 0))
        self.assertEqual(amd.visible_setting(self.root, {"HIP_VISIBLE_DEVICES": " "}), ("runtime.local.json", 1))
        self.assertEqual(amd.visible_setting(self.root, {"HIP_VISIBLE_DEVICES": "GPU-1"}), ("HIP_VISIBLE_DEVICES", None))
        settings.write_text('{"device": "cuda", "hip_visible_devices": 0}', "utf-8")
        self.assertEqual(amd.visible_setting(self.root, {}), ("runtime.local.json", 0))
        settings.write_text('{"device": "cuda"}', "utf-8")
        self.assertEqual(amd.visible_setting(self.root, {}), (None, None))
        settings.write_text("{not json", "utf-8")
        self.assertEqual(amd.visible_setting(self.root, {}), (None, None))

    def test_a_card_the_build_has_code_for_with_enough_memory_passes(self):
        self.assertEqual(self.problems([RADEON_780M, R9700]), [])
        self.assertEqual(self.problems([RADEON_780M, R9700], memory_gb=10), [])
        self.assertIs(amd.chosen_card([RADEON_780M, R9700], self.root, {}), R9700)

    def test_each_reason_a_card_cannot_take_the_build_says_what_to_do(self):
        [driver] = self.problems([])
        self.assertIn("AMD's driver did not say which chip it has", driver)
        self.assertIn("AMD Software: Adrenalin Edition", driver)
        [old] = self.problems([RADEON_VII])
        self.assertIn("AMD's ROCm 7.14 build of PyTorch 2.12 has no code for the AMD Radeon VII (gfx906)", old)
        self.assertIn("the Radeon RX 5000, 6000, 7000 and 9000 series", old)
        self.assertIn("gfx1201", old)
        [small] = self.problems([RX_7600], memory_gb=10)
        self.assertIn(
            "VoxCPM2 needs a graphics card with at least 10 GB of memory, and the AMD Radeon RX 7600 has 8.0 GB", small
        )
        [gone] = self.problems([R9700], {"HIP_VISIBLE_DEVICES": "3"})
        self.assertIn("HIP_VISIBLE_DEVICES names card 3, and AMD's driver lists these: 0 AMD Radeon AI PRO R9700", gone)
        [word] = self.problems([R9700], {"HIP_VISIBLE_DEVICES": "GPU-1"})
        self.assertIn("HIP_VISIBLE_DEVICES does not give a card number", word)
        [named] = self.problems([RADEON_VII, R9700], {"HIP_VISIBLE_DEVICES": "0"})
        self.assertIn("has no code for the AMD Radeon VII (gfx906)", named)
        for cards, environ in (
            ([], {}),
            ([RADEON_VII], {}),
            ([R9700], {"HIP_VISIBLE_DEVICES": "3"}),
            ([RADEON_VII, R9700], {"HIP_VISIBLE_DEVICES": "0"}),
        ):
            self.assertIsNone(amd.chosen_card(cards, self.root, environ))

    def test_the_service_uses_the_graphics_card_when_one_can_take_the_build(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(amd.VARIABLE, None)
            self.assertTrue(amd.usable_card(self.root, [RADEON_780M, R9700]))
            self.assertFalse(amd.usable_card(self.root, [RADEON_VII]))
            with mock.patch.object(hardware, "amd_listed", return_value=[]), mock.patch.object(hardware, "amd_cards") as cards:
                self.assertFalse(amd.usable_card(self.root))
            cards.assert_not_called()

    def test_an_installer_puts_in_nvidias_build_with_an_nvidia_driver_and_amds_for_an_amd_card(self):
        self.assertEqual(amd.graphics(nvidia_present=True), ("nvidia", []))
        self.assertEqual(amd.graphics(nvidia_present=False, amd_cards=[R9700]), ("amd", [R9700]))
        with mock.patch.object(hardware, "amd_listed", return_value=[]):
            self.assertEqual(amd.graphics(nvidia_present=False), (None, []))
        listed = [{"name": "AMD Radeon AI PRO R9700", "driver": "32.0.31041.1004"}]
        with (
            mock.patch.object(hardware, "amd_listed", return_value=listed),
            mock.patch.object(hardware, "amd_cards", return_value=[]),
        ):
            self.assertEqual(amd.graphics(nvidia_present=False), ("amd", []))  # card_problems then asks for the driver

    def test_the_plan_names_the_card_and_on_a_computer_with_two_says_which_one_the_lab_uses(self):
        self.assertEqual(amd.card_text(R9700), "AMD Radeon AI PRO R9700 (gfx1201)")
        self.assertEqual(amd.card_text(RADEON_780M), "AMD Radeon 780M Graphics (gfx1103, built into the processor)")
        one = amd.plan_note(R9700, [R9700])
        self.assertIn("under AMD's own licence terms", one)
        self.assertIn("checked with it on an AMD Radeon AI PRO R9700 (gfx1201)", one)
        self.assertNotIn("runtime.local.json", one)
        two = " ".join(amd.plan_note(R9700, [RADEON_780M, R9700]).split())
        self.assertIn(
            "This computer has 2 AMD graphics cards (AMD Radeon 780M Graphics, AMD Radeon AI PRO R9700), and the "
            "Lab uses the AMD Radeon AI PRO R9700",
            two,
        )
        self.assertIn('runtime.local.json ("hip_visible_devices")', two)


class NumberTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-amd-")
        self.addCleanup(temp.cleanup)
        self.root = lab(temp.name)
        self.settings = self.root / amd.SETTINGS

    def test_pytorch_numbers_the_card_found_on_its_pci_bus(self):
        self.assertEqual(amd.hip_number(DEVICES, R9700), 1)
        self.assertEqual(amd.hip_number(DEVICES, RADEON_780M), 0)
        self.assertEqual(amd.hip_number(DEVICES, {**R9700, "bus": None}), 1)  # by its chip and name
        self.assertIsNone(amd.hip_number(DEVICES, RX_7600))
        twins = [{**DEVICES[1], "index": 0, "bus": 3}, {**DEVICES[1], "index": 1, "bus": 7}]
        self.assertEqual(amd.hip_number(twins, {**R9700, "bus": 7}), 1)

    def test_with_two_cards_the_number_goes_into_runtime_local_json_beside_what_it_holds(self):
        self.settings.write_text('{"device": "cuda"}', "utf-8")
        number, note = amd.settle_number(self.root, DEVICES, R9700, environ={})
        self.assertEqual(number, 1)
        self.assertEqual(
            note,
            "This computer has 2 AMD graphics cards, and the Lab uses the AMD Radeon AI PRO R9700: "
            'runtime.local.json now says "hip_visible_devices": "1".',
        )
        self.assertEqual(json.loads(self.settings.read_text("utf-8")), {"device": "cuda", "hip_visible_devices": "1"})
        self.assertFalse(self.settings.with_name("runtime.local.json.tmp").exists())
        # The next model's install finds the card named, so it neither writes nor says anything.
        self.assertEqual(amd.settle_number(self.root, DEVICES, R9700, environ={}), (1, None))

    def test_with_one_card_nothing_is_written(self):
        self.assertEqual(amd.settle_number(self.root, [{**DEVICES[1], "index": 0}], R9700, environ={}), (0, None))
        self.assertFalse(self.settings.exists())

    def test_a_setting_that_names_another_card_stops_it_and_says_which_number_to_give(self):
        with self.assertRaises(amd.InstallError) as caught:
            amd.settle_number(self.root, DEVICES, R9700, environ={"HIP_VISIBLE_DEVICES": "0"})
        text = " ".join(str(caught.exception).split())
        self.assertIn(
            "HIP_VISIBLE_DEVICES names card 0, the AMD Radeon 780M Graphics in PyTorch's numbering, and the "
            "packages were installed for the AMD Radeon AI PRO R9700, card 1 in that numbering. Set it to 1, or remove "
            "it",
            text,
        )
        with self.assertRaisesRegex(amd.InstallError, "names card 5, a card PyTorch does not list"):
            amd.settle_number(self.root, DEVICES, R9700, environ={"HIP_VISIBLE_DEVICES": "5"})
        self.assertEqual(amd.settle_number(self.root, DEVICES, R9700, environ={"HIP_VISIBLE_DEVICES": "1"}), (1, None))
        self.assertFalse(self.settings.exists())

    def test_a_card_pytorch_does_not_list_stops_it_and_names_the_driver(self):
        with self.assertRaises(amd.InstallError) as caught:
            amd.settle_number(self.root, DEVICES, RX_7600, environ={})
        text = " ".join(str(caught.exception).split())
        self.assertIn("AMD's PyTorch does not list the AMD Radeon RX 7600 (gfx1102)", text)
        self.assertIn("It lists: 0 AMD Radeon 780M Graphics (gfx1103), 1 AMD Radeon AI PRO R9700 (gfx1201)", text)
        self.assertIn("Update the AMD graphics driver", text)

    def test_a_runtime_local_json_that_cannot_be_read_is_left_as_it_is(self):
        for content in ("{not json", "[1]"):
            self.settings.write_text(content, "utf-8")
            with self.assertRaises(amd.InstallError) as caught:
                amd.settle_number(self.root, DEVICES, R9700, environ={})
            self.assertIn("runtime.local.json", str(caught.exception))
            self.assertIn("Correct the file or delete it", str(caught.exception))
            self.assertEqual(self.settings.read_text("utf-8"), content)


class PytorchListTests(unittest.TestCase):
    def test_pytorch_lists_the_cards_with_no_setting_that_hides_one(self):
        seen = {}

        def runner(command, **kwargs):
            seen.update(command=command, env=kwargs["env"], timeout=kwargs["timeout"])
            listed = {"torch": "2.12.0+rocm7.14.0", "hip": "7.14.60850", "devices": DEVICES}
            return SimpleNamespace(returncode=0, stdout="a warning\n" + json.dumps(listed) + "\n", stderr="")

        with mock.patch.dict(os.environ, {"HIP_VISIBLE_DEVICES": "1", "ROCR_VISIBLE_DEVICES": "0"}):
            self.assertEqual(amd.hip_devices(Path("C:/lab"), ".venv-fqwen", runner=runner), DEVICES)
        self.assertEqual(seen["command"][:3], [str(Path("C:/lab/.venv-fqwen/Scripts/python.exe")), "-I", "-c"])
        self.assertIn("torch.cuda.get_device_properties", seen["command"][3])
        for name in hardware.VISIBILITY:
            self.assertNotIn(name, seen["env"])
        self.assertEqual(seen["timeout"], amd.CHECK_TIMEOUT)

    def test_a_pytorch_that_cannot_list_the_cards_shows_the_end_of_its_output(self):
        def runner(command, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="RuntimeError: HIP error: invalid device function\n")

        with self.assertRaises(amd.InstallError) as caught:
            amd.hip_devices(Path("C:/lab"), ".venv-fqwen", runner=runner)
        text = str(caught.exception)
        self.assertIn("The packages are in .venv-fqwen, but AMD's PyTorch could not list the graphics cards", text)
        self.assertIn("invalid device function", text)
        self.assertIn("Update the graphics driver (AMD Software: Adrenalin Edition", text)

    def test_a_hang_is_stopped(self):
        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with self.assertRaisesRegex(amd.InstallError, "did not list the graphics cards within 5 minutes"):
            amd.hip_devices(Path("C:/lab"), ".venv-fqwen", runner=runner)


class ReceiptAndHintTests(unittest.TestCase):
    def test_the_receipt_records_the_build_the_list_and_the_card(self):
        self.assertEqual(amd.receipt_fields(ROOT, None, None), {"build": "cuda"})
        self.assertEqual(
            amd.receipt_fields(ROOT, R9700, 1),
            {
                "build": "rocm",
                "amd_lock_sha256": hashlib.sha256((ROOT / amd.LOCK).read_bytes()).hexdigest(),
                "gpu": {
                    "name": "AMD Radeon AI PRO R9700",
                    "chip": "gfx1201",
                    "driver": "32.0.31041.1004",
                    "hip_visible_devices": 1,
                },
            },
        )

    def test_the_hint_names_the_likely_cause(self):
        self.assertIn("ran out of memory", amd.failure_hint("torch.OutOfMemoryError: HIP out of memory."))
        self.assertIn("Update the graphics driver", amd.failure_hint("RuntimeError: HIP error: no kernel image is available"))
        self.assertIn("Update the graphics driver", amd.failure_hint("hipErrorNoBinaryForGpu: Unable to find code object"))
        self.assertEqual(amd.failure_hint("ValueError: the text is empty"), "")


class ScriptTests(unittest.TestCase):
    def test_an_installer_run_as_a_script_raises_and_catches_the_same_installerror_as_this_module(self):
        # Run as tools\install_qwen.py, Python puts tools\ first on the path; an installer that then imported install_kokoro
        # under that name while this module imports tools.install_kokoro would hold two InstallErrors, and an error raised here
        # would end in a traceback instead of the installer's error line.
        for name in ("install_qwen", "install_voxcpm", "install_breeze", "install_chatterbox"):
            with self.subTest(installer=name):
                code = (
                    "import importlib, sys\n"
                    f"sys.path.insert(0, {str(ROOT / 'tools')!r})\n"
                    f"installer = importlib.import_module({name!r})\n"
                    "from tools import amd_build\n"
                    "print(installer.InstallError is amd_build.InstallError)\n"
                )
                done = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout.strip(), "True")


class HardwareTests(unittest.TestCase):
    LISTED = [
        {"name": "AMD Radeon 780M Graphics", "driver": "32.0.31041.1004"},
        {"name": "AMD Radeon AI PRO R9700", "driver": "32.0.31041.1004"},
        {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "driver": "32.0.15.8157"},
    ]

    def probe(self, stdout, code=0):
        seen = {}

        def run(command, **kwargs):
            seen.update(command=command, env=kwargs["env"])
            return SimpleNamespace(returncode=code, stdout=stdout, stderr="")

        with mock.patch.dict(os.environ, {"HIP_VISIBLE_DEVICES": "1"}), mock.patch.object(hardware.sys, "platform", "win32"):
            return hardware.amd_cards(run=run, python="python.exe", listed=self.LISTED), seen

    def test_windows_lists_the_amd_cards_by_name(self):
        self.assertEqual(
            [card["name"] for card in hardware.amd_listed(self.LISTED)], ["AMD Radeon 780M Graphics", "AMD Radeon AI PRO R9700"]
        )
        self.assertEqual(len(hardware.amd_listed([{"name": "Radeon RX 580 Series", "driver": "31.0.21921.1000"}])), 1)
        self.assertEqual(hardware.amd_listed(self.LISTED[2:]), [])

    def test_amds_driver_gives_each_cards_chip_memory_and_bus_and_windows_its_driver(self):
        found = {
            "cards": [
                {key: card[key] for key in ("name", "chip", "memory_mib", "integrated", "bus")} for card in (RADEON_780M, R9700)
            ]
        }
        cards, seen = self.probe("the driver says hello\n" + json.dumps(found) + "\n")
        self.assertEqual(cards, [RADEON_780M, R9700])
        self.assertEqual(seen["command"][:3], ["python.exe", "-I", "-c"])
        self.assertIn("clGetDeviceInfo", seen["command"][3])
        self.assertNotIn("HIP_VISIBLE_DEVICES", seen["env"])

    def test_what_the_driver_does_not_give_is_none_and_a_card_without_a_chip_is_left_out(self):
        found = {
            "cards": [
                {"name": "", "chip": "gfx1201", "memory_mib": "lots", "integrated": 1, "bus": None},
                {"name": "Something", "chip": "not a chip"},
                "junk",
            ]
        }
        cards, _seen = self.probe(json.dumps(found))
        self.assertEqual(
            cards, [{"name": "gfx1201", "chip": "gfx1201", "memory_mib": None, "integrated": None, "bus": None, "driver": None}]
        )
        self.assertEqual(self.probe(json.dumps(found), code=1)[0], [])
        self.assertEqual(self.probe("no answer")[0], [])

    def test_without_an_amd_card_in_windows_list_the_driver_is_not_asked(self):
        run = mock.Mock(side_effect=AssertionError("asked the driver"))
        with mock.patch.object(hardware.sys, "platform", "win32"):
            self.assertEqual(hardware.amd_cards(run=run, listed=self.LISTED[2:]), [])
            self.assertEqual(hardware.amd_cards(run=mock.Mock(side_effect=OSError("no Python")), listed=self.LISTED), [])


if __name__ == "__main__":
    unittest.main()
