"""tools/install_chatterbox.py: the pinned Chatterbox Turbo CPU install into its own environment. Nothing here reaches the real
network, pip, venv or the real Chatterbox."""
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chatterbox_support  # noqa: E402
import engine_checks  # noqa: E402
import soundfile as sf  # noqa: E402
from tests.test_amd_build import DEVICES, R9700, RADEON_780M, RADEON_VII  # noqa: E402
from tools import amd_build as amd  # noqa: E402
from tools import install_chatterbox as ic  # noqa: E402
from tools import install_kokoro as base  # noqa: E402
from tests.test_install_phonon import FileServer  # noqa: E402

REAL_MANIFEST = json.loads((ROOT / ic.MANIFEST).read_text("utf-8"))
CATALOG = {entry["id"]: entry for entry in json.loads((ROOT / "catalog" / "engines.json").read_text("utf-8"))["engines"]}
ENTRY = CATALOG["turbo"]
HASH = "ab" * 32


def sha(data):
    return hashlib.sha256(data).hexdigest()


def noise(length, seed=0):
    return hashlib.shake_256(seed.to_bytes(4, "little")).digest(length)


def core_pins():
    pins = {}
    for line in (ROOT / "requirements" / "core.txt").read_text("utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            name, version = line.strip().split("==")
            pins[base.canonical(name)] = version
    return pins


class FakeLab:
    """A lab folder holding a two-package lock, the shipped AMD list and small stand-ins for the Turbo files, at the real
    revision."""
    CONTENTS = {name: noise(2_000 + index * 37, index) for index, name in enumerate(chatterbox_support.TURBO_FILES)}
    CONTENTS["README.md"] = b"---\nlicense: mit\n---\n"
    LOCK = (f"# a stand-in for requirements/chatterbox-cpu.txt\nchatterbox-tts==0.1.7 --hash=sha256:{HASH}\n"
            f"torch==2.6.0 --hash=sha256:{'cd' * 32}\n")

    def __init__(self, folder):
        self.root = Path(folder)
        (self.root / "catalog").mkdir()
        (self.root / "requirements").mkdir()
        self.manifest = {**{key: REAL_MANIFEST[key] for key in ("schema", "repo", "revision", "license", "license_source")},
                         "files": [{"path": path, "size": len(data), "sha256": sha(data)} for path, data in self.CONTENTS.items()]}
        (self.root / ic.MANIFEST).write_text(json.dumps(self.manifest), "utf-8")
        (self.root / ic.LOCK).write_text(self.LOCK, "utf-8")
        (self.root / amd.LOCK).write_bytes((ROOT / amd.LOCK).read_bytes())

    def served(self):
        prefix = f"/{self.manifest['repo']}/resolve/{self.manifest['revision']}/"
        return {prefix + path: data for path, data in self.CONTENTS.items()}

    def snapshot(self):
        return base.snapshot_dir(self.root, self.manifest)


class ShippedFilesTests(unittest.TestCase):
    def test_the_file_list_pins_the_revision_the_catalog_uses(self):
        self.assertEqual((REAL_MANIFEST["repo"], REAL_MANIFEST["revision"]), (ENTRY["checkpoint"], ENTRY["local_revision"]))
        self.assertEqual(chatterbox_support.turbo_snapshot_dir(ROOT), base.snapshot_dir(ROOT, REAL_MANIFEST))

    def test_the_file_list_holds_what_the_adapter_loads_and_the_model_card(self):
        paths = [item["path"] for item in REAL_MANIFEST["files"]]
        self.assertEqual(sorted(paths), sorted([*chatterbox_support.TURBO_FILES, "README.md"]))
        self.assertEqual(len(paths), len(set(paths)))
        for item in REAL_MANIFEST["files"]:
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$", item["path"])
            self.assertIsInstance(item["size"], int)
            self.assertGreater(item["size"], 0)
            self.assertNotIn("..", Path(item["path"]).parts)
            self.assertFalse(item["path"].startswith("/"))

    def test_the_files_left_out_are_ones_turbo_never_reads(self):
        left_out = {item["path"] for item in REAL_MANIFEST["not_fetched"]}
        self.assertEqual(left_out, {".gitattributes", "s3gen.safetensors", "t3_turbo_v1.yaml"})
        self.assertFalse(left_out & set(chatterbox_support.TURBO_FILES))
        self.assertTrue(all(item["why"] for item in REAL_MANIFEST["not_fetched"]))

    def test_the_big_files_are_the_two_halves_of_the_model(self):
        biggest = sorted(REAL_MANIFEST["files"], key=lambda item: item["size"])[-2:]
        self.assertEqual([item["path"] for item in biggest], ["s3gen_meanflow.safetensors", "t3_turbo_v1.safetensors"])
        total = sum(item["size"] for item in REAL_MANIFEST["files"])
        self.assertTrue(2.9e9 < total < 3.1e9, total)

    def test_the_licence_is_recorded_from_the_model_card(self):
        self.assertEqual(REAL_MANIFEST["license"], "MIT")
        self.assertIn("model card", REAL_MANIFEST["license_source"])
        self.assertIn("MIT", ENTRY["license"]["weights"])
        self.assertIn("Perth", ENTRY["license"]["watermark"])

    def test_the_package_list_pins_the_versions_the_catalog_names(self):
        packages = {item["key"]: item for item in base.read_lock(ROOT / ic.LOCK)}
        self.assertEqual(packages["chatterbox-tts"]["version"], ENTRY["backend"]["package_version"])
        self.assertEqual(packages["transformers"]["version"], ENTRY["backend"]["transformers_version"])
        self.assertEqual((packages["torch"]["version"], packages["torchaudio"]["version"]), ("2.6.0", "2.6.0"))
        self.assertNotIn("+", packages["torch"]["version"], "the CPU build has no GPU suffix such as +rocm or +cu")
        self.assertEqual(packages["setuptools"]["version"], "81.0.0", "the Perth watermarker imports pkg_resources")
        for name in ("librosa", "s3tokenizer", "resemble-perth", "conformer", "diffusers", "omegaconf", "onnx", "safetensors"):
            self.assertIn(name, packages)

    def test_the_package_list_leaves_out_what_turbo_never_imports(self):
        keys = {item["key"] for item in base.read_lock(ROOT / ic.LOCK)}
        for name in ("gradio", "pykakasi", "spacy-pkuseg", "pre-commit", "antlr4-python3-runtime"):
            self.assertNotIn(name, keys)

    def test_every_line_of_the_package_list_pins_one_wheel_and_nothing_points_at_another_index(self):
        lines = [line.strip() for line in (ROOT / ic.LOCK).read_text("utf-8").splitlines()]
        lines = [line for line in lines if line and not line.startswith("#")]
        self.assertEqual(len(lines), len(base.read_lock(ROOT / ic.LOCK)))
        self.assertGreater(len(lines), 60)
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-9][A-Za-z0-9._-]*==\S+ --hash=sha256:[0-9a-f]{64}$")

    def test_the_package_list_repeats_the_core_requirements_at_the_same_versions(self):
        # The worker runs the lab's own modules in .venv-chatterbox, so numpy and soundfile behave as they do in .venv.
        core = core_pins()
        packages = {item["key"]: item["version"] for item in base.read_lock(ROOT / ic.LOCK)}
        self.assertTrue(core)
        for name, version in core.items():
            self.assertEqual(packages.get(name), version, name)

    def test_the_catalog_runs_every_chatterbox_engine_in_its_own_environment(self):
        for engine in ("turbo", "orig", "chatterbox-vc"):
            self.assertEqual(CATALOG[engine]["runtime"]["python"], ic.PYTHON, engine)
            self.assertEqual(CATALOG[engine]["runtime"]["environment"], ic.ENVIRONMENT, engine)
        # Kokoro and Phonon have environments of their own too, so no two installers ever share one.
        guided = {CATALOG[engine]["runtime"]["environment"] for engine in ("kokoro", "phonon-31m-en", "turbo")}
        self.assertEqual(guided, {".venv-kokoro", ".venv-phonon", ".venv-chatterbox"})

    def test_the_batch_file_runs_the_installer_with_the_labs_own_python(self):
        data = (ROOT / "setup-chatterbox.bat").read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "batch files keep CRLF line ends")
        text = data.decode("utf-8")
        self.assertIn('".venv\\Scripts\\python.exe" tools\\install_chatterbox.py %*', text)
        self.assertIn("Run setup.bat first.", text)

    def test_the_setup_window_waits_for_a_key_so_a_double_click_can_be_read(self):
        lines = [line.strip() for line in (ROOT / "setup-chatterbox.bat").read_text("utf-8").splitlines() if line.strip()]
        last_run = max(index for index, line in enumerate(lines) if "tools\\" in line)
        self.assertIn("pause", lines[last_run + 1:])

    def test_the_first_setup_offers_the_installer(self):
        from tools import first_setup
        self.assertEqual(first_setup.MODELS["chatterbox"],
                         {"engine": "turbo", "script": "tools/install_chatterbox.py", "gpu": True})
        self.assertIn("setup-chatterbox.bat", first_setup.menu_text(16, first_setup.model_sizes()))

    def test_the_guides_describe_the_installer_with_the_numbers_it_ships(self):
        setup = " ".join((ROOT / "docs" / "SETUP.md").read_text("utf-8").split())
        for needed in ("setup-chatterbox.bat", "tools\\install_chatterbox.py", "--dry-run", "--yes", "--checkpoint-only",
                       "requirements/chatterbox-cpu.txt", "catalog/chatterbox_turbo_snapshot.json", ".venv-chatterbox",
                       "Visual C++ Redistributable", "Perth"):
            self.assertIn(needed, setup)
        packages = len(base.read_lock(ROOT / ic.LOCK))
        total = sum(item["size"] for item in REAL_MANIFEST["files"])
        for number in (f"{packages} pinned Python packages", f"{ic.PACKAGES_DOWNLOAD_MB} MB", ic.size_gb(total),
                       f"{ENTRY['local_revision'][:7]}"):
            self.assertIn(number, setup)
        for name in ("README.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "MODEL_CAPABILITIES.md"):
            text = " ".join((ROOT / name).read_text("utf-8").split())
            self.assertTrue("setup-chatterbox.bat" in text or "tools/install_chatterbox.py" in text, name)

    def test_the_guides_and_the_tables_agree_on_the_free_ram_turbo_needs(self):
        import serve
        import worker_runtime

        self.assertEqual(serve.MIN_FREE_RAM_GB["turbo"], worker_runtime.MIN_FREE_RAM_GB["turbo"])
        setup = " ".join((ROOT / "docs" / "SETUP.md").read_text("utf-8").split())
        self.assertIn(f"asks for {serve.MIN_FREE_RAM_GB['turbo']} GB of free RAM before it loads it", setup)

    def test_the_installer_prints_only_plain_ascii(self):
        self.assertTrue((ROOT / "tools" / "install_chatterbox.py").read_bytes().isascii())
        packages = base.read_lock(ROOT / ic.LOCK)
        files = REAL_MANIFEST["files"]
        for todo, pending, exists, only, card in ((len(packages), files, False, False, None), (3, files, True, False, None),
                                                  (0, [], True, False, None), (0, files, True, True, None),
                                                  (len(packages), files, False, False, R9700)):
            text = ic.plan_text(REAL_MANIFEST, packages, todo, pending, environment_exists=exists, checkpoint_only=only, card=card,
                                cards=[RADEON_780M, R9700])
            self.assertTrue(text.isascii())
            self.assertLessEqual(max(len(line) for line in text.splitlines()), 110)

    def test_amds_build_swaps_torch_and_torchaudio_and_adds_its_runtime_and_the_code_for_the_cards_chip(self):
        cpu = base.read_lock(ROOT / ic.LOCK)
        packages = ic.pinned_packages(ROOT, card=R9700)
        builds = {item["key"]: item["version"] for item in packages if item["key"] in ("torch", "torchaudio")}
        self.assertEqual(builds, {"torch": "2.12.0+rocm7.14.0", "torchaudio": "2.11.0+rocm7.14.0"})
        self.assertEqual([item for item in packages[:len(cpu)] if item["key"] not in builds],
                         [item for item in cpu if item["key"] not in builds])
        self.assertEqual(sorted(item["key"] for item in packages[len(cpu):]),
                         ["amd-torch-device-gfx12-0", "amd-torch-device-gfx1201", "rocm", "rocm-bootstrap", "rocm-sdk-core",
                          "rocm-sdk-device-gfx1201", "rocm-sdk-libraries"])
        # The same torch and torchaudio as the NVIDIA build, PyTorch 2.12 and torchaudio 2.11, in AMD's ROCm build.
        nvidia = {item["key"]: item["version"] for item in ic.pinned_packages(ROOT, nvidia=True) if item["key"] in builds}
        self.assertEqual({key: version.split("+")[0] for key, version in nvidia.items()},
                         {key: version.split("+")[0] for key, version in builds.items()})


class EnvironmentTests(unittest.TestCase):
    def problems(self, **options):
        defaults = {"system": "win32", "machine": "AMD64", "version": (3, 12, 10), "prefix": str(ROOT / ".venv"), "bits": 64,
                    "base_python": sys.executable}
        return ic.interpreter_problems(ROOT, **{**defaults, **options})

    def test_only_a_64_bit_windows_python_312_in_the_labs_own_environment_passes(self):
        self.assertEqual(self.problems(), [])
        self.assertRegex(self.problems(system="linux")[0], "64-bit Windows")
        self.assertRegex(self.problems(machine="arm64")[0], "64-bit Windows")
        self.assertRegex(self.problems(bits=32)[0], "64-bit Windows")
        self.assertRegex(self.problems(version=(3, 11, 9))[0], "Python 3.11; the pinned packages are built for Python 3.12")
        self.assertRegex(self.problems(prefix=str(ROOT / ".venv-chatterbox"))[0], r"own Python.*install_chatterbox\.py")

    def test_the_python_that_venv_was_made_from_must_still_be_there(self):
        problem = self.problems(base_python=str(ROOT / "no-such-python.exe"))
        self.assertEqual(len(problem), 1)
        self.assertRegex(problem[0], r"Python 3\.12 that \.venv was made from is gone.*setup\.bat")

    def test_other_versions_already_installed_are_named_and_the_way_out_is_given(self):
        packages = {item["key"]: item for item in base.read_lock(ROOT / ic.LOCK)}
        self.assertEqual(ic.package_problems([]), [])
        problem = ic.package_problems([(packages["torch"], "2.6.0+rocm6.4")])
        self.assertEqual(len(problem), 1)
        self.assertIn("torch 2.6.0+rocm6.4 (the list pins 2.6.0)", problem[0])
        self.assertIn("--checkpoint-only", problem[0])
        self.assertIn("delete the .venv-chatterbox folder", problem[0])
        many = ic.package_problems([(packages["torch"], "1")] * 7)
        self.assertIn("and 2 more", many[0])

    def test_an_environment_without_pip_is_reported(self):
        self.assertRegex(ic.package_problems([], has_pip=False)[0], "has no pip.*makes a new one")

    def test_checkpoint_only_needs_an_environment_that_has_chatterbox(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertRegex(ic.checkpoint_only_problems(folder, {})[0], "there is no .venv-chatterbox here")
            python = ic.environment_python(folder)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            self.assertRegex(ic.checkpoint_only_problems(folder, {"torch": "2.6.0"})[0], "chatterbox-tts is not among them")
            self.assertEqual(ic.checkpoint_only_problems(folder, {"chatterbox-tts": "0.1.7"}), [])

    def test_a_folder_path_too_long_for_windows_is_refused_unless_long_paths_are_on(self):
        self.assertEqual(ic.long_path_problems(Path("C:/voice-lab"), REAL_MANIFEST, long_paths=False), [])
        deep = Path("C:/" + "d" * 120)
        problem = ic.long_path_problems(deep, REAL_MANIFEST, long_paths=False)
        self.assertEqual(len(problem), 1)
        self.assertRegex(problem[0], r"too long for Windows.*up to \d+ characters.*stops at 259.*C:\\voice-lab")
        self.assertEqual(ic.long_path_problems(deep, REAL_MANIFEST, long_paths=True), [])


class EnvironmentVersionsTests(unittest.TestCase):
    def test_no_environment_means_no_packages_and_nothing_is_run(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(ic.environment_versions(folder, runner=mock.Mock(side_effect=AssertionError("ran"))), {})

    def test_the_versions_are_read_by_the_environments_own_python_in_isolated_mode(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, 'noise\n{"Chatterbox_TTS": "0.1.7", "torch": "2.6.0"}\n', "")

        with tempfile.TemporaryDirectory() as folder:
            python = ic.environment_python(folder)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            self.assertEqual(ic.environment_versions(folder, runner=runner), {"chatterbox-tts": "0.1.7", "torch": "2.6.0"})
            self.assertEqual(calls[0][:3], [str(python), "-I", "-c"])

    def test_an_environment_whose_python_does_not_run_says_to_delete_it(self):
        with tempfile.TemporaryDirectory() as folder:
            python = ic.environment_python(folder)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            broken = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "No Python at 'C:\\gone'")  # noqa: E731
            with self.assertRaisesRegex(ic.InstallError, r"(?s)does not run:.*No Python.*Delete the \.venv-chatterbox folder"):
                ic.environment_versions(folder, runner=broken)


class PlanTests(unittest.TestCase):
    def plan(self, todo=73, pending=True, exists=False, only=False, nvidia=False):
        packages = ic.pinned_packages(ROOT, nvidia=nvidia)
        files = REAL_MANIFEST["files"] if pending else []
        return " ".join(ic.plan_text(REAL_MANIFEST, packages, todo, files, environment_exists=exists, checkpoint_only=only,
                                     nvidia=nvidia).split())

    def test_the_plan_says_what_is_installed_where_and_how_much_to_download(self):
        text = self.plan()
        for part in ("Chatterbox Turbo 350M on the CPU", "a Python environment of its own, .venv-chatterbox",
                     "nothing another model installs can clash with Chatterbox's packages",
                     "73 of 73 to install there", "PyTorch 2.6 for the CPU",
                     "About 400 MB to download and 2.0 GB on disk", "requirements/chatterbox-cpu.txt", "revision 749d1c1",
                     "huggingface.co/ResembleAI/chatterbox-turbo", "the 1.9 GB language model", "the 1.1 GB speech decoder",
                     "3.0 GB to download", "catalog/chatterbox_turbo_snapshot.json", "nothing system-wide",
                     "pip keeps its usual download cache in your user profile", "clones that recording", "without using the network",
                     "On a CPU this takes a few minutes."):
            self.assertIn(part, text)
        self.assertNotIn("NVIDIA", text)

    def test_the_nvidia_plan_names_the_cuda_builds_the_cards_they_run_on_and_that_they_are_untried(self):
        text = self.plan(nvidia=True)
        for part in ("Chatterbox Turbo 350M on an NVIDIA graphics card",
                     "the CUDA 13.0 builds of PyTorch 2.12 and torchaudio 2.11 for NVIDIA graphics cards (not yet tried on one)",
                     f"About {base.mb_text(ic.PACKAGES_DOWNLOAD_MB + ic.NVIDIA_EXTRA_DOWNLOAD_MB)} to download",
                     "requirements/chatterbox-cpu.txt and requirements/chatterbox-nvidia.txt", "It runs on the graphics card.",
                     "GeForce GTX 16 or RTX 20 series card or newer, RTX 50 included", "driver of version 580 or later",
                     "checked to speak and clone a voice with PyTorch 2.12", "hardware report on GitHub"):
            self.assertIn(part, text)

    def test_the_cuda_13_build_runs_on_an_rtx_50_card_and_is_refused_on_old_cards_and_drivers(self):
        rtx_5070 = {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": (12, 0), "driver": (581, 80)}
        for fine in (rtx_5070, {"name": "NVIDIA GeForce RTX 2060", "capability": (7, 5), "driver": (581, 80)},
                     {"name": "NVIDIA GeForce RTX 4090", "capability": None, "driver": None}):
            with self.subTest(card=fine["name"]):
                self.assertEqual(ic.nvidia_problems(present=True, cards=[fine]), [])
        old_card = ic.nvidia_problems(present=True, cards=[{"name": "NVIDIA GeForce GTX 1080", "capability": (6, 1),
                                                            "driver": (581, 80)}])
        self.assertEqual(len(old_card), 1)
        self.assertIn("Chatterbox's NVIDIA build (PyTorch 2.12 with CUDA 13.0) has no code for the NVIDIA GeForce GTX 1080 "
                      "(compute capability 6.1); it runs on GeForce GTX 16 and RTX 20 series cards and newer. Leave out --nvidia",
                      old_card[0])
        old_driver = ic.nvidia_problems(present=True, cards=[{**rtx_5070, "driver": (572, 83)}])
        self.assertIn("needs NVIDIA driver 580 or later, and this computer has 572.83", old_driver[0])
        self.assertIn("no NVIDIA driver", ic.nvidia_problems(present=False)[0])
        self.assertEqual(ic.card_problems([rtx_5070]), [])    # what setup.bat and the Models page ask

    def test_a_failed_check_on_the_card_names_the_cards_and_driver_the_build_needs(self):
        hint = base.check_failure_hint("RuntimeError: CUDA error: no kernel image is available", nvidia=True,
                                       cuda_needs=ic.CUDA_NEEDS)
        self.assertIn("CUDA 13.0 needs an NVIDIA driver of version 580 or later and a GTX 16 or RTX 20 series card", hint)

    def test_the_nvidia_plan_for_a_cpu_install_says_it_replaces_pytorch_and_torchaudio_only(self):
        packages = ic.pinned_packages(ROOT, nvidia=True)
        cpu = {item["key"]: item for item in base.read_lock(ROOT / ic.LOCK)}
        gpu = {item["key"]: item for item in packages}
        switching = [(gpu["torch"], cpu["torch"]["version"]), (gpu["torchaudio"], cpu["torchaudio"]["version"])]
        text = " ".join(ic.plan_text(REAL_MANIFEST, packages, 2, [], environment_exists=True, checkpoint_only=False,
                                     nvidia=True, switching=switching).split())
        self.assertIn("Chatterbox is already installed in .venv-chatterbox, and its CPU build of PyTorch is replaced by the CUDA "
                      "13.0 builds of PyTorch 2.12 and torchaudio 2.11 for NVIDIA graphics cards (not yet tried on one): torch "
                      "2.6.0 becomes 2.12.0+cu130, torchaudio 2.6.0 becomes 2.11.0+cu130. About 1.9 GB to download", text)

    def test_the_amd_plan_names_the_card_its_chip_amds_list_and_which_card_the_lab_uses(self):
        packages = ic.pinned_packages(ROOT, card=R9700)
        text = " ".join(ic.plan_text(REAL_MANIFEST, packages, len(packages), REAL_MANIFEST["files"], environment_exists=False,
                                     checkpoint_only=False, card=R9700, cards=[RADEON_780M, R9700]).split())
        download, disk = ic.packages_mb(card=R9700)
        self.assertEqual((download, disk), amd.sizes_mb("gfx1201", ic.OTHER_DOWNLOAD_MB, ic.OTHER_DISK_MB))
        for part in ("Chatterbox Turbo 350M on an AMD graphics card",
                     "AMD's ROCm 7.14 builds of PyTorch 2.12 and torchaudio 2.11 with the code for the AMD Radeon AI PRO R9700 "
                     "(gfx1201)", f"About {base.mb_text(download)} to download and {base.mb_text(disk)} on disk",
                     "requirements/chatterbox-cpu.txt and requirements/amd-rocm.txt", "It runs on the AMD Radeon AI PRO R9700.",
                     "AMD Software: Adrenalin Edition", "This computer has 2 AMD graphics cards"):
            self.assertIn(part, text)
        self.assertNotIn("NVIDIA", text)
        self.assertNotIn("On a CPU", text)

    def test_the_amd_plan_for_a_cpu_install_replaces_pytorch_and_torchaudio_and_adds_amds_runtime(self):
        cpu = base.read_lock(ROOT / ic.LOCK)
        packages = ic.pinned_packages(ROOT, card=R9700)
        installed = {item["key"]: item["version"] for item in cpu}
        missing, different = base.package_state(packages, installed)
        switching, conflicts, switch_only = base.gpu_switch(packages, cpu, installed)
        self.assertEqual(sorted(item["key"] for item, _have in switching), ["torch", "torchaudio"])
        self.assertEqual((conflicts, switch_only), ([], True))
        text = " ".join(ic.plan_text(ORIGINAL_MANIFEST, packages, len(missing) + len(different), [], environment_exists=True,
                                     checkpoint_only=False, switching=switching, switch_only=switch_only, original=True,
                                     card=R9700, cards=[R9700]).split())
        download, disk = ic.switch_mb(card=R9700)
        self.assertIn("Chatterbox is already installed in .venv-chatterbox, and its CPU build of PyTorch is replaced by AMD's ROCm "
                      "7.14 builds of PyTorch 2.12 and torchaudio 2.11 with the code for the AMD Radeon AI PRO R9700 (gfx1201): "
                      "torch 2.6.0 becomes 2.12.0+rocm7.14.0, torchaudio 2.6.0 becomes 2.11.0+rocm7.14.0. About "
                      f"{base.mb_text(download)} to download and {base.mb_text(disk)} more on disk.", text)
        self.assertIn("Chatterbox 500M and Voice Conversion on an AMD graphics card", text)
        self.assertIn("then Voice Conversion says that clip again, which you can play afterwards. It runs on the AMD Radeon AI PRO "
                      "R9700.", text)

    def test_amds_build_runs_on_a_card_whose_chip_it_has_code_for(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)
            self.assertEqual(ic.amd_card_problems([RADEON_780M, R9700], root=lab.root, environ={}), [])
            [problem] = ic.amd_card_problems([RADEON_VII], root=lab.root, environ={})
        self.assertIn("has no code for the AMD Radeon VII (gfx906)", problem)

    def test_the_plan_says_which_licence_covers_what_and_that_every_clip_is_watermarked(self):
        text = self.plan()
        for part in ("Turbo weights are MIT", "Perth watermark", "does not remove it", "soxr", "LGPL-2.1", "certifi and tqdm (MPL-2.0)",
                     "not part of this repository"):
            self.assertIn(part, text)

    def test_the_plan_says_the_voice_match_comes_with_it(self):
        self.assertIn("speaker encoder also gives the Lab its voice match", self.plan())

    def test_the_plan_reflects_what_is_already_done(self):
        self.assertIn(".venv-chatterbox is already here. Python packages: 5 of 73", self.plan(todo=5, exists=True))
        self.assertIn("all 73 are already installed in .venv-chatterbox", self.plan(todo=0, exists=True))
        self.assertIn("Everything is already in place", self.plan(pending=False))
        self.assertIn("left alone (--checkpoint-only)", self.plan(todo=0, exists=True, only=True))


class MakeEnvironmentTests(unittest.TestCase):
    def test_the_environment_is_made_by_the_python_that_venv_was_made_from_without_the_launchers_variables(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            python = ic.environment_python(folder)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"__PYVENV_LAUNCHER__": "x", "VIRTUAL_ENV": "y", "KEPT": "z"}):
            ic.make_environment(folder, runner=runner)
        command, kwargs = calls[0]
        self.assertEqual(command, [getattr(sys, "_base_executable", sys.executable), "-m", "venv", str(Path(folder) / ".venv-chatterbox")])
        self.assertNotIn("__PYVENV_LAUNCHER__", kwargs["env"])
        self.assertNotIn("VIRTUAL_ENV", kwargs["env"])
        self.assertEqual(kwargs["env"]["KEPT"], "z")

    def test_a_venv_that_fails_or_leaves_no_python_says_to_delete_it_and_run_again(self):
        with tempfile.TemporaryDirectory() as folder:
            for code in (0, 2):
                with self.assertRaisesRegex(ic.InstallError, f"could not make .venv-chatterbox \\(exit code {code}\\).*run the installer again"):
                    ic.make_environment(folder, runner=lambda command, code=code, **kwargs: subprocess.CompletedProcess(command, code))


class InstallPackagesTests(unittest.TestCase):
    def test_pip_of_the_new_environment_gets_the_locked_file_with_hashes_required_and_nothing_resolved(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        ic.install_packages(ROOT, runner=runner)
        command, kwargs = calls[0]
        self.assertEqual(command[:4], [str(ROOT / ".venv-chatterbox" / "Scripts" / "python.exe"), "-m", "pip", "install"])
        for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:", "--no-input", "--disable-pip-version-check"):
            self.assertIn(flag, command)
        self.assertEqual(Path(command[command.index("-r") + 1]), ROOT / "requirements" / "chatterbox-cpu.txt")
        self.assertEqual(kwargs["cwd"], ROOT)
        self.assertNotIn("index-url", " ".join(command))

    def test_for_nvidia_pip_gets_the_cpu_list_with_the_cuda_torch_and_torchaudio_in_their_place(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)  # its lock pins chatterbox-tts and torch
            cuda = f"torch @ https://example.invalid/torch-2.6.0%2Bcu126-cp312-cp312-win_amd64.whl --hash=sha256:{'ef' * 32}"
            (lab.root / ic.NVIDIA_LOCK).write_text(f"# a stand-in for requirements/chatterbox-nvidia.txt\n{cuda}\n", "utf-8")
            ic.install_packages(lab.root, nvidia=True,
                                runner=lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0))
            written = Path(calls[0][calls[0].index("-r") + 1])
            self.assertEqual(written, lab.root / "out" / "chatterbox" / "requirements-nvidia.txt")
            lines = [line for line in written.read_text("utf-8").splitlines() if not line.startswith("#")]
        self.assertEqual(lines, [f"chatterbox-tts==0.1.7 --hash=sha256:{HASH}", cuda])

    def test_the_nvidia_list_swaps_only_torch_and_torchaudio_for_their_cuda_13_builds(self):
        cpu = {item["key"]: item for item in base.read_lock(ROOT / ic.LOCK)}
        gpu = base.read_lock(ROOT / ic.NVIDIA_LOCK)
        self.assertEqual([(item["key"], item["version"]) for item in gpu], [("torch", "2.12.0+cu130"), ("torchaudio", "2.11.0+cu130")])
        for line in base.lock_lines(ROOT / ic.NVIDIA_LOCK).values():
            self.assertRegex(line, r"^torch(audio)? @ https://download\.pytorch\.org/whl/cu130/torch(audio)?-2\.1[12]\.0%2Bcu130-"
                                   r"cp312-cp312-win_amd64\.whl --hash=sha256:[0-9a-f]{64}$")
        # The same PyTorch file as Kokoro's NVIDIA build, so pip's cache serves both.
        self.assertEqual(base.lock_lines(ROOT / ic.NVIDIA_LOCK)["torch"], base.lock_lines(ROOT / base.NVIDIA_LOCK)["torch"])
        swapped = ic.pinned_packages(ROOT, nvidia=True)
        self.assertEqual([item["key"] for item in swapped], list(cpu))

    def test_only_a_cpu_build_of_the_swapped_packages_is_replaced(self):
        cpu = base.read_lock(ROOT / ic.LOCK)
        gpu = base.read_lock(ROOT / ic.NVIDIA_LOCK)
        packages = ic.pinned_packages(ROOT, nvidia=True)
        installed = {item["key"]: item["version"] for item in cpu}
        _missing, different = base.package_state(packages, installed)
        self.assertEqual(sorted(item["key"] for item, _have in base.build_switch(different, cpu, gpu)), ["torch", "torchaudio"])
        installed.update({"torch": "2.6.0+rocm6.4", "numpy": "2.0.0"})    # someone's own PyTorch, and another numpy
        _missing, different = base.package_state(packages, installed)
        self.assertEqual([item["key"] for item, _have in base.build_switch(different, cpu, gpu)], ["torchaudio"])

    def test_for_an_amd_card_pip_gets_the_wheels_then_builds_amds_source_package(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)
            ic.install_packages(lab.root, card=R9700,
                                runner=lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0))
            wheels, source = (Path(command[command.index("-r") + 1]) for command in calls)
            self.assertEqual((wheels, source), (lab.root / "out" / "chatterbox" / amd.WHEELS,
                                                lab.root / "out" / "chatterbox" / amd.SOURCE))
            lines = [line for line in wheels.read_text("utf-8").splitlines() if not line.startswith("#")]
        self.assertEqual(lines[0], f"chatterbox-tts==0.1.7 --hash=sha256:{HASH}")
        self.assertTrue(lines[1].startswith("torch @ https://repo.amd.com/rocm/whl-multi-arch/"), lines[1])
        self.assertIn("amd-torch-device-gfx1201", " ".join(lines))
        self.assertIn("--no-build-isolation", calls[1])

    def test_a_pip_failure_says_to_fix_it_and_run_again(self):
        with self.assertRaisesRegex(ic.InstallError, "pip stopped with exit code 3.*run the installer again"):
            ic.install_packages(ROOT, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 3))


class SpeechCheckTests(unittest.TestCase):
    GOOD = ('{"problem": null, "load_seconds": 9.9, "reference_seconds": 12.7, "reference_total_seconds": 31.0, '
            '"prepare_seconds": 1.0, "total_seconds": 6.0, "audio_seconds": 2.2, "torch": "2.6.0+cpu"}')

    def check(self, stdout="", stderr="", code=0, error=None, device="cpu", **options):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if error:
                raise error
            return subprocess.CompletedProcess(command, code, stdout, stderr)

        with tempfile.TemporaryDirectory() as folder:
            return ic.run_speech_check(folder, device=device, runner=runner, **options), calls, Path(folder)

    def test_on_an_amd_card_both_checks_are_given_its_number_and_a_failure_points_at_amds_driver(self):
        _result, calls, _folder = self.check(stdout=self.GOOD, device="cuda", amd_card=True, number=1)
        self.assertEqual((calls[0][0][-1], calls[0][1]["env"]["HIP_VISIBLE_DEVICES"]), ("cuda", "1"))
        with self.assertRaises(ic.InstallError) as caught:
            self.check(stderr="RuntimeError: HIP error: invalid device function", code=1, device="cuda", amd_card=True)
        self.assertIn("Update the graphics driver (AMD Software: Adrenalin Edition", str(caught.exception))
        self.assertNotIn("nvidia.com", str(caught.exception))
        seen = []

        def runner(command, **kwargs):
            seen.append(kwargs["env"].get("HIP_VISIBLE_DEVICES"))
            return subprocess.CompletedProcess(command, 1, "", "RuntimeError: HIP error: invalid device function")

        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ic.InstallError, "AMD Software: Adrenalin Edition"):
            ic.run_original_check(folder, device="cuda", amd_card=True, number=1, runner=runner)
        self.assertEqual(seen, ["1"])

    def test_the_clips_are_made_offline_by_the_new_environment_and_their_numbers_come_back(self):
        result, calls, folder = self.check(stdout="loaded PerthNet\n" + self.GOOD + "\n", stderr="a warning\n")
        command, kwargs = calls[0]
        self.assertEqual(command[:2], [str(folder / ".venv-chatterbox" / "Scripts" / "python.exe"), "-c"])
        self.assertIn("from engines import ChatterboxTurbo", command[2])
        self.assertIn('engine_checks.smoke_request("turbo")', command[2])
        self.assertEqual(command[3:], [str(folder), str(folder / "out" / "chatterbox" / "install-reference.wav"),
                                       str(folder / "out" / "chatterbox" / "install-check.wav"), "cpu"])
        self.assertEqual((kwargs["env"]["HF_HUB_OFFLINE"], kwargs["env"]["TRANSFORMERS_OFFLINE"]), ("1", "1"))
        self.assertEqual((result["audio_seconds"], result["torch"]), (2.2, "2.6.0+cpu"))
        self.assertEqual(result["wav"], folder / "out" / "chatterbox" / "install-check.wav")

    def test_a_crash_shows_the_end_of_the_output(self):
        with self.assertRaisesRegex(ic.InstallError, r"in place, but Chatterbox Turbo did not load or speak:\n(.*\n)*last line"):
            self.check(stdout="first\n", stderr="\n".join(f"line {n}" for n in range(30)) + "\nlast line", code=1)

    def test_a_missing_dll_points_at_the_visual_c_runtime(self):
        with self.assertRaisesRegex(ic.InstallError, r"(?s)WinError 126.*https://aka\.ms/vs/17/release/vc_redist\.x64\.exe"):
            self.check(stderr="OSError: [WinError 126] The specified module could not be found. c10.dll", code=1)

    def test_a_clip_that_is_not_speech_is_reported(self):
        with self.assertRaisesRegex(ic.InstallError, "a check clip was not speech: generated silence"):
            self.check(stdout=self.GOOD.replace("null", '"generated silence"'))

    def test_no_result_line_is_a_failure_and_a_hang_is_stopped(self):
        with self.assertRaisesRegex(ic.InstallError, "did not load or speak"):
            self.check(stdout="no json here\n")
        with self.assertRaisesRegex(ic.InstallError, "more than 30 minutes"):
            self.check(error=subprocess.TimeoutExpired("python", ic.CHECK_TIMEOUT))

    def test_the_check_script_speaks_clones_and_reports_both_clips(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = Path(folder) / "lab"
            lab.mkdir()
            (lab / "torch.py").write_text("__version__ = '2.6.0+cpu'\n", "utf-8")
            (lab / "common.py").write_text("REFERENCE_TEXT = 'a paragraph long enough to clone'\n", "utf-8")
            (lab / "engines.py").write_text(
                "import numpy as np\n"
                "class ChatterboxTurbo:\n"
                "    def __init__(self, device):\n"
                "        assert device == 'cpu'\n"
                "        self.prepared = None\n"
                "    def prepare_reference(self, path):\n"
                "        self.prepared = path\n"
                "    def speak(self, text, ref_wav=None):\n"
                "        assert (ref_wav is None) == (self.prepared is None) and ref_wav == self.prepared\n"
                "        seconds = 6 if ref_wav is None else 2\n"
                "        t = np.arange(24000 * seconds) / 24000\n"
                "        return (0.1 * np.sin(2 * np.pi * 220 * t)).astype('float32'), 24000, {'total_s': 0.5}\n", "utf-8")
            (lab / "engine_checks.py").write_text(
                "def smoke_request(engine):\n    assert engine == 'turbo'\n    return {'engine': engine, 'text': 'check'}\n"
                "def audible(samples, rate):\n    return None\n", "utf-8")
            reference, wav = Path(folder) / "reference.wav", Path(folder) / "clip.wav"
            done = subprocess.run([sys.executable, "-c", ic.SPEECH_CHECK, str(lab), str(reference), str(wav), "cpu"],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            result = base.last_json_line(done.stdout)
            self.assertEqual((result["problem"], result["torch"], result["total_seconds"], result["device"]),
                             (None, "2.6.0+cpu", 0.5, "cpu"))
            self.assertAlmostEqual(result["reference_seconds"], 6.0)
            self.assertAlmostEqual(result["audio_seconds"], 2.0)
            self.assertEqual(sf.info(str(reference)).frames, 6 * 24000)
            self.assertEqual(sf.info(str(wav)).frames, 2 * 24000)

    def test_the_names_the_check_script_uses_exist_in_the_lab(self):
        import common
        import engines

        self.assertTrue(callable(engine_checks.smoke_request) and callable(engine_checks.audible))
        self.assertTrue(hasattr(engines, "ChatterboxTurbo") and hasattr(engines.ChatterboxTurbo, "prepare_reference"))
        self.assertGreater(len(common.REFERENCE_TEXT), 150, "the reference must make more than five seconds of speech")
        compile(ic.SPEECH_CHECK, "<speech check>", "exec")


class VoiceMatchTests(unittest.TestCase):
    def test_the_clone_is_compared_with_its_reference_by_the_studios_voice_match(self):
        import speaker_similarity

        check = {"reference": "a.wav", "wav": "b.wav"}
        with mock.patch.object(speaker_similarity, "similarity", return_value=0.912345) as similarity:
            self.assertEqual(ic.voice_match(check), {"score": 0.9123, "band": "likely_same"})
        similarity.assert_called_once_with("a.wav", "b.wav")

    def test_a_voice_match_that_fails_is_reported_and_not_raised(self):
        import speaker_similarity

        with mock.patch.object(speaker_similarity, "similarity", side_effect=FileNotFoundError("ve.safetensors")):
            self.assertEqual(ic.voice_match({"reference": "a", "wav": "b"}), {"error": "FileNotFoundError: ve.safetensors"})


class ReceiptTests(unittest.TestCase):
    def test_the_receipt_records_what_was_installed_and_how_it_spoke(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)
            (lab.root / "out" / "chatterbox").mkdir(parents=True)
            packages = base.read_lock(lab.root / ic.LOCK)
            check = {"problem": None, "load_seconds": 9.9, "reference_seconds": 12.7, "reference_total_seconds": 31.0,
                     "prepare_seconds": 1.0, "total_seconds": 6.0, "audio_seconds": 2.2, "torch": "2.6.0+cpu",
                     "reference": "x", "wav": "y"}
            receipt = json.loads(ic.write_receipt(lab.root, lab.manifest, packages, check, {"score": 0.9, "band": "likely_same"})
                                 .read_text("utf-8"))
            self.assertEqual(receipt["lock_sha256"], sha((lab.root / ic.LOCK).read_bytes()))
        self.assertEqual((receipt["schema"], receipt["environment"], receipt["torch"]), (1, ".venv-chatterbox", "2.6.0+cpu"))
        self.assertEqual((receipt["repo"], receipt["revision"], receipt["license"]),
                         (REAL_MANIFEST["repo"], REAL_MANIFEST["revision"], "MIT"))
        self.assertEqual(receipt["packages_pinned"], 2)
        self.assertEqual(len(receipt["files"]), len(FakeLab.CONTENTS))
        self.assertEqual(receipt["checkpoint_bytes"], sum(len(data) for data in FakeLab.CONTENTS.values()))
        self.assertEqual(receipt["speech_check"], {"load_seconds": 9.9, "reference_seconds": 12.7, "prepare_seconds": 1.0,
                                                   "total_seconds": 6.0, "audio_seconds": 2.2})
        self.assertEqual(receipt["voice_match"], {"score": 0.9, "band": "likely_same"})
        self.assertRegex(receipt["installed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertNotIn(folder, json.dumps(receipt), "no folder names of the person's computer in the receipt")
        self.assertNotIn("build", receipt)

    def test_an_amd_install_records_the_build_amds_list_and_the_card(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)
            (lab.root / "out" / "chatterbox").mkdir(parents=True)
            check = {"problem": None, "load_seconds": 39.0, "total_seconds": 2.0, "audio_seconds": 2.5, "vc_load_seconds": 4.0,
                     "vc_seconds": 1.0, "vc_audio_seconds": 2.5, "torch": "2.12.0+rocm7.14.0", "device": "cuda"}
            receipt = json.loads(ic.write_receipt(lab.root, lab.manifest, ic.pinned_packages(lab.root, card=R9700), check,
                                                  original=True, card=R9700, number=1).read_text("utf-8"))
        self.assertEqual((receipt["build"], receipt["device"], receipt["packages_pinned"]), ("rocm", "cuda", 9))
        self.assertEqual(receipt["gpu"]["hip_visible_devices"], 1)


class RunTests(unittest.TestCase):
    """The whole install with venv, pip, the model host, Chatterbox and the voice match replaced by stand-ins."""

    @classmethod
    def setUpClass(cls):
        cls.server = FileServer()
        cls.addClassCleanup(cls.server.close)

    def setUp(self):
        self.server.reset()
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-chatterbox-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.lab = FakeLab(self.base)
        self.server.files = self.lab.served()
        self.installed = None    # None: no .venv-chatterbox yet
        self.calls = []
        self.check_options = []
        self.graphics = ("nvidia", [])
        self.match = {"score": 0.9, "band": "likely_same"}
        folder = self.base / "out" / "chatterbox"
        self.check = {"problem": None, "load_seconds": 9.9, "reference_seconds": 12.7, "reference_total_seconds": 31.0,
                      "prepare_seconds": 1.0, "total_seconds": 6.0, "audio_seconds": 2.2, "torch": "2.6.0+cpu",
                      "reference": folder / "install-reference.wav", "wav": folder / "install-check.wav"}
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(amd.VARIABLE, None)

        def make(root, **kwargs):
            self.calls.append("venv")
            python = ic.environment_python(root)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            self.installed = {"pip": "25.0.1"}

        def pip(root, *, nvidia=False, card=None, **kwargs):
            self.calls.append(("pip", card["chip"]) if card else "pip")
            self.installed.update({item["key"]: item["version"] for item in ic.pinned_packages(root, nvidia=nvidia, card=card)})

        def speak(root, **kwargs):
            self.calls.append("check")
            self.check_options.append(kwargs)
            folder.mkdir(parents=True, exist_ok=True)
            return self.check

        for owner, name, value in ((ic, "ROOT", self.base), (base, "HOST", self.server.address), (ic, "make_environment", make),
                                   (ic, "environment_versions", lambda root: dict(self.installed or {})),
                                   (ic, "install_packages", pip), (ic, "run_speech_check", speak),
                                   (ic, "voice_match", lambda check: self.match), (ic, "interpreter_problems", lambda root: []),
                                   (ic, "long_path_problems", lambda root, manifest: []),
                                   (ic, "nvidia_problems", lambda: []),
                                   (base, "graphics", lambda: (self.graphics[0], list(self.graphics[1]))),
                                   (amd, "hip_devices", lambda root, environment: [dict(device) for device in DEVICES]),
                                   (base, "space_problems", lambda root, need: [])):
            patch = mock.patch.object(owner, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def own_environment(self, versions):
        python = ic.environment_python(self.base)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"")
        self.installed = dict(versions)

    def args(self, **options):
        return argparse.Namespace(**{"yes": False, "dry_run": False, "checkpoint_only": False, **options})

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO()):
            status = ic.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def untouched(self):
        self.assertEqual(self.calls, [])
        self.assertFalse((self.base / "hf-cache").exists())
        self.assertFalse((self.base / "out").exists())

    def test_a_dry_run_shows_the_plan_and_changes_nothing(self):
        status, out, err = self.run_main("--dry-run")
        self.assertEqual((status, err), (0, ""))
        self.assertIn("Chatterbox Turbo 350M on the CPU", out)
        self.assertIn("This was a dry run, so nothing was changed.", out)
        self.untouched()
        self.assertFalse((self.base / ".venv-chatterbox").exists())
        self.assertEqual(self.server.requests, [])

    def test_without_a_terminal_and_without_yes_nothing_happens(self):
        status, _out, err = self.run_main()
        self.assertEqual(status, 1)
        self.assertIn("not an interactive terminal", err)
        self.untouched()

    def test_asked_in_a_terminal_only_yes_goes_ahead(self):
        lines = []
        for answer in ("", "no", "y please"):
            self.assertEqual(ic.run(self.args(), say=lines.append, ask=lambda prompt, answer=answer: answer, interactive=True), 1)
        self.assertIn("Nothing was changed.", lines)
        self.untouched()
        self.assertEqual(ic.run(self.args(), say=lines.append, ask=lambda prompt: " Yes ", interactive=True), 0)
        self.assertEqual(self.calls, ["venv", "pip", "check"])

    def test_the_whole_install_runs_in_order_and_ends_with_a_receipt(self):
        status, out, err = self.run_main("--yes")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["venv", "pip", "check"])
        self.assertIn("Making .venv-chatterbox:", out)
        self.assertIn("Installing the Python packages into .venv-chatterbox", out)
        self.assertIn(f"[1/{len(FakeLab.CONTENTS)}] added_tokens.json", out)
        self.assertIn("Chatterbox Turbo is installed. It loaded in 10 s, spoke 12.7 s in its built-in voice, cloned that recording "
                      "and spoke 2.2 s in the cloned voice in 6.0 s.", out)
        self.assertIn("Listen to them: out\\chatterbox\\install-reference.wav and out\\chatterbox\\install-check.wav", out)
        self.assertIn("open Voice cloning and choose Chatterbox Turbo", out)
        self.assertNotIn("voice match could not", out)
        receipt = json.loads((self.base / "out" / "chatterbox" / "installation.json").read_text("utf-8"))
        self.assertEqual((receipt["torch"], receipt["voice_match"]), ("2.6.0+cpu", self.match))
        self.assertEqual((self.lab.snapshot().parent.parent / "refs" / "main").read_text("utf-8"), REAL_MANIFEST["revision"])
        self.assertEqual((self.lab.snapshot() / "ve.safetensors").read_bytes(), FakeLab.CONTENTS["ve.safetensors"])
        self.assertEqual(self.check_options, [{"device": "cpu", "amd_card": False, "number": None}])

    def test_gpu_on_an_amd_card_installs_amds_builds_and_checks_turbo_on_that_card(self):
        self.graphics = ("amd", [RADEON_780M, R9700])
        self.check = {**self.check, "torch": "2.12.0+rocm7.14.0", "device": "cuda"}
        status, out, err = self.run_main("--yes", "--gpu")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["venv", ("pip", "gfx1201"), "check"])
        self.assertEqual(self.check_options, [{"device": "cuda", "amd_card": True, "number": 1}])
        text = " ".join(out.split())
        self.assertIn("Chatterbox Turbo 350M on an AMD graphics card", text)
        self.assertIn('runtime.local.json now says "hip_visible_devices": "1".', text)
        self.assertIn("Checking that Chatterbox Turbo speaks and clones a voice on the AMD Radeon AI PRO R9700:", text)
        receipt = json.loads((self.base / "out" / "chatterbox" / "installation.json").read_text("utf-8"))
        self.assertEqual((receipt["build"], receipt["gpu"]["name"]), ("rocm", "AMD Radeon AI PRO R9700"))

    def test_gpu_on_a_cpu_install_replaces_pytorch_and_torchaudio_with_amds_builds(self):
        self.own_environment({"pip": "25.0.1", **{item["key"]: item["version"] for item in base.read_lock(self.base / ic.LOCK)}})
        self.graphics = ("amd", [R9700])
        with mock.patch.object(amd, "hip_devices", lambda root, environment: [{**DEVICES[1], "index": 0}]):
            status, out, err = self.run_main("--yes", "--gpu")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, [("pip", "gfx1201"), "check"])
        self.assertIn("Chatterbox is already installed in .venv-chatterbox, and its CPU build of PyTorch is replaced by AMD's ROCm "
                      "7.14 builds", " ".join(out.split()))
        self.assertEqual(self.installed["torch"], "2.12.0+rocm7.14.0")

    def test_gpu_on_an_amd_card_the_build_has_no_code_for_is_refused_before_any_change(self):
        self.graphics = ("amd", [RADEON_VII])
        for argv, name in ((["--yes", "--gpu"], "Chatterbox Turbo"), (["--yes", "--gpu", "--original"],
                                                                      "Chatterbox 500M and Voice Conversion")):
            with self.subTest(name=name):
                status, out, err = self.run_main(*argv)
                self.assertEqual((status, out), (1, ""))
                text = " ".join(err.split())
                self.assertIn(f"This computer cannot take the GPU build of {name} yet: - AMD's ROCm 7.14 build of PyTorch 2.12 "
                              "has no code for the AMD Radeon VII (gfx906)", text)
                self.assertIn("Leave out --gpu to install the CPU build", text)
                self.untouched()

    def test_the_lab_counts_turbo_as_installed_afterwards(self):
        self.run_main("--yes")
        (self.base / ".venv-chatterbox" / "Lib" / "site-packages" / "chatterbox_tts-0.1.7.dist-info").mkdir(parents=True)
        # the stand-in files sit at the catalog's revision, so the lab's file check agrees that Turbo is there
        report = engine_checks.installation(ENTRY, self.base)
        self.assertEqual((report["installed"], report["missing"]), (True, []))
        (self.lab.snapshot() / "conds.pt").unlink()
        report = engine_checks.installation(ENTRY, self.base)
        self.assertEqual(report["missing"], [f"{report['checkpoint']}/conds.pt"])
        self.assertEqual(report["installer"], "setup-chatterbox.bat")

    def test_an_environment_that_has_every_package_is_left_alone(self):
        self.own_environment({"pip": "25.0.1", "chatterbox-tts": "0.1.7", "torch": "2.6.0"})
        status, out, _err = self.run_main("--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["check"])
        self.assertIn("all 2 are already installed in .venv-chatterbox", " ".join(out.split()))

    def test_an_environment_that_lacks_some_packages_gets_only_those_and_is_not_made_again(self):
        self.own_environment({"pip": "25.0.1", "torch": "2.6.0"})
        status, out, _err = self.run_main("--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["pip", "check"])
        self.assertIn(".venv-chatterbox is already here. Python packages: 1 of 2", " ".join(out.split()))

    def test_a_different_version_of_a_pinned_package_stops_everything_before_any_change(self):
        self.own_environment({"pip": "25.0.1", "torch": "2.6.0+rocm6.4"})
        status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("This computer cannot take the plan yet:", err)
        self.assertIn("torch 2.6.0+rocm6.4 (the list pins 2.6.0)", " ".join(err.split()))
        self.untouched()
        self.assertEqual(self.server.requests, [])

    def test_the_model_files_alone_can_be_added_to_an_environment_with_its_own_pytorch(self):
        self.own_environment({"pip": "25.0.1", "chatterbox-tts": "0.1.7", "torch": "2.6.0+rocm6.4"})
        status, out, _err = self.run_main("--yes", "--checkpoint-only")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["check"])
        self.assertIn("left alone (--checkpoint-only)", out)
        self.assertTrue((self.lab.snapshot() / "t3_turbo_v1.safetensors").is_file())

    def test_checkpoint_only_without_an_environment_is_refused_before_any_change(self):
        status, _out, err = self.run_main("--yes", "--checkpoint-only")
        self.assertEqual(status, 1)
        self.assertIn("there is no .venv-chatterbox here", " ".join(err.split()))
        self.untouched()

    def test_a_computer_that_fails_a_check_is_told_why_and_nothing_is_changed(self):
        with mock.patch.object(ic, "interpreter_problems", lambda root: ["Run it with this folder's own Python."]), \
                mock.patch.object(base, "space_problems", lambda root, need: ["About 6 GB of free disk space is needed."]), \
                mock.patch.object(ic, "long_path_problems", lambda root, manifest: ["The path is too long."]):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("  - Run it with this folder's own Python.\n  - The path is too long.\n  - About 6 GB of free disk space is needed.", err)
        self.untouched()

    def test_the_space_needed_counts_the_packages_and_the_files_still_to_fetch(self):
        needs = []
        with mock.patch.object(base, "space_problems", lambda root, need: needs.append(need) or []):
            self.run_main("--dry-run")
            self.own_environment({"pip": "25.0.1", "chatterbox-tts": "0.1.7", "torch": "2.6.0"})
            self.run_main("--dry-run")
        files = sum(len(data) for data in FakeLab.CONTENTS.values())
        margin = ic.SPACE_MARGIN_MB * 10**6
        self.assertEqual(needs, [(ic.PACKAGES_DOWNLOAD_MB + ic.PACKAGES_DISK_MB) * 10**6 + files + margin, files + margin])

    def test_a_pip_failure_stops_before_the_model_is_fetched(self):
        def failing(root, **kwargs):
            raise ic.InstallError("pip stopped with exit code 1.")

        with mock.patch.object(ic, "install_packages", failing):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("error: pip stopped with exit code 1.", err)
        self.assertFalse((self.base / "hf-cache").exists())

    def test_pip_that_finishes_without_installing_everything_is_caught(self):
        with mock.patch.object(ic, "install_packages", lambda root, **kwargs: None):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("pip finished but 2 packages are still missing, for example chatterbox-tts", err)

    def test_a_failed_speech_check_leaves_the_files_and_writes_no_receipt(self):
        def silent(root, **kwargs):
            raise ic.InstallError("The packages and model files are in place, but Chatterbox Turbo did not load or speak:\nboom")

        with mock.patch.object(ic, "run_speech_check", silent):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("did not load or speak", err)
        self.assertTrue((self.lab.snapshot() / "s3gen_meanflow.safetensors").is_file())
        self.assertFalse((self.base / "out" / "chatterbox" / "installation.json").exists())

    def test_a_voice_match_that_fails_is_said_and_the_install_still_counts(self):
        self.match = {"error": "FileNotFoundError: ve.safetensors"}
        status, out, _err = self.run_main("--yes")
        self.assertEqual(status, 0)
        self.assertIn("The voice match could not compare them in .venv (FileNotFoundError: ve.safetensors)", out)

    def test_a_second_run_installs_and_downloads_nothing_new_and_a_damaged_file_is_fetched_again(self):
        self.run_main("--yes")
        asked, self.calls[:] = len(self.server.requests), []
        status, _out, _err = self.run_main("--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["check"])
        self.assertEqual(len(self.server.requests), asked)
        damaged = self.lab.snapshot() / "conds.pt"
        damaged.write_bytes(b"damaged")
        self.server.requests.clear()
        self.run_main("--yes")
        self.assertEqual(damaged.read_bytes(), FakeLab.CONTENTS["conds.pt"])
        self.assertEqual(self.server.requests, [f"/{REAL_MANIFEST['repo']}/resolve/{REAL_MANIFEST['revision']}/conds.pt"])

    def test_a_file_the_system_will_not_let_it_write_ends_with_an_error_line_not_a_traceback(self):
        with mock.patch.object(ic, "write_receipt", side_effect=PermissionError(13, "Access is denied", "installation.json")):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("error: [Errno 13] Access is denied", err)
        self.assertNotIn("Traceback", err)

    def test_control_c_stops_politely(self):
        def interrupted(root, **kwargs):
            raise KeyboardInterrupt

        with mock.patch.object(ic, "install_packages", interrupted):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 130)
        self.assertIn("Run the installer again to continue", err)


ORIGINAL_MANIFEST = json.loads((ROOT / ic.ORIGINAL_MANIFEST).read_text("utf-8"))


class OriginalTests(unittest.TestCase):
    """--original: Chatterbox 500M and Chatterbox Voice Conversion, from the ResembleAI/chatterbox checkpoint."""

    def test_the_file_list_pins_the_revision_both_engines_use_and_holds_what_they_load(self):
        for engine in ("orig", "chatterbox-vc"):
            self.assertEqual((ORIGINAL_MANIFEST["repo"], ORIGINAL_MANIFEST["revision"]),
                             (CATALOG[engine]["checkpoint"], CATALOG[engine]["local_revision"]))
        self.assertEqual(chatterbox_support.original_snapshot_dir(ROOT), base.snapshot_dir(ROOT, ORIGINAL_MANIFEST))
        paths = [item["path"] for item in ORIGINAL_MANIFEST["files"]]
        self.assertEqual(sorted(paths), sorted([*chatterbox_support.ORIGINAL_FILES, "README.md"]))
        self.assertLessEqual(set(chatterbox_support.VC_FILES), set(chatterbox_support.ORIGINAL_FILES))
        for item in ORIGINAL_MANIFEST["files"]:
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$", item["path"])
        self.assertFalse({item["path"] for item in ORIGINAL_MANIFEST["not_fetched"]} & set(paths))
        self.assertTrue(all(item["why"] for item in ORIGINAL_MANIFEST["not_fetched"]))
        self.assertTrue(3.1e9 < sum(item["size"] for item in ORIGINAL_MANIFEST["files"]) < 3.3e9)
        self.assertEqual(ORIGINAL_MANIFEST["license"], "MIT")
        # The speaker encoder is the same file as Turbo's.
        turbo = {item["path"]: item for item in REAL_MANIFEST["files"]}
        original = {item["path"]: item for item in ORIGINAL_MANIFEST["files"]}
        self.assertEqual(original["ve.safetensors"]["sha256"], turbo["ve.safetensors"]["sha256"])

    def test_the_plan_names_both_models_their_files_and_licence(self):
        packages = ic.pinned_packages(ROOT)
        text = " ".join(ic.plan_text(ORIGINAL_MANIFEST, packages, 0, ORIGINAL_MANIFEST["files"], environment_exists=True,
                                     checkpoint_only=False, original=True).split())
        for part in ("Chatterbox 500M and Voice Conversion on the CPU", "share .venv-chatterbox with Chatterbox Turbo",
                     "all 73 are already installed in .venv-chatterbox", "revision 5bb1f6e",
                     "huggingface.co/ResembleAI/chatterbox:", "the 2.1 GB language model",
                     "the 1.1 GB speech decoder that Voice Conversion uses too", "3.2 GB to download",
                     "catalog/chatterbox_snapshot.json", "then Voice Conversion says that clip again",
                     "Chatterbox 500M weights are MIT", "Perth watermark"):
            self.assertIn(part, text)
        self.assertNotIn("voice match", text)

    def test_the_check_script_speaks_then_converts_the_clip_with_itself_as_the_target(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = Path(folder) / "lab"
            (lab / "chatterbox").mkdir(parents=True)
            (lab / "torch.py").write_text("__version__ = '2.6.0+cpu'\n", "utf-8")
            (lab / "common.py").write_text("import numpy as np\ndef to_numpy(audio):\n    return np.asarray(audio, 'float32').reshape(-1)\n",
                                           "utf-8")
            (lab / "chatterbox_support.py").write_text("def original_snapshot_dir(root):\n    return root / 'snapshot'\n", "utf-8")
            (lab / "chatterbox" / "__init__.py").write_text("", "utf-8")
            (lab / "chatterbox" / "vc.py").write_text(
                "import numpy as np\nimport soundfile\n"
                "class ChatterboxVC:\n"
                "    sr = 24000\n"
                "    @classmethod\n"
                "    def from_local(cls, folder, device):\n"
                "        assert folder.name == 'snapshot' and device == 'cpu'\n"
                "        return cls()\n"
                "    def generate(self, audio, target_voice_path=None):\n"
                "        assert audio == target_voice_path\n"
                "        data, rate = soundfile.read(audio, dtype='float32')\n"
                "        return data[None, :]\n", "utf-8")
            (lab / "engines.py").write_text(
                "import numpy as np\n"
                "class ChatterboxOriginal:\n"
                "    def __init__(self, device):\n"
                "        assert device == 'cpu'\n"
                "    def speak(self, text):\n"
                "        t = np.arange(24000 * 3) / 24000\n"
                "        return (0.1 * np.sin(2 * np.pi * 220 * t)).astype('float32'), 24000, {'total_s': 1.5}\n", "utf-8")
            (lab / "engine_checks.py").write_text(
                "def smoke_request(engine):\n    assert engine == 'orig'\n    return {'engine': engine, 'text': 'check'}\n"
                "def audible(samples, rate):\n    return None\n", "utf-8")
            wav, converted = Path(folder) / "original.wav", Path(folder) / "vc.wav"
            done = subprocess.run([sys.executable, "-c", ic.ORIGINAL_CHECK, str(lab), str(wav), str(converted), "cpu"],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            result = base.last_json_line(done.stdout)
            self.assertEqual((result["problem"], result["total_seconds"], result["torch"]), (None, 1.5, "2.6.0+cpu"))
            self.assertAlmostEqual(result["audio_seconds"], 3.0)
            self.assertAlmostEqual(result["vc_audio_seconds"], 3.0)
            self.assertEqual(sf.info(str(converted)).frames, 3 * 24000)
        import engines
        from chatterbox_support import original_snapshot_dir    # the names the script imports exist in the lab
        self.assertTrue(hasattr(engines, "ChatterboxOriginal") and callable(original_snapshot_dir))
        compile(ic.ORIGINAL_CHECK, "<original check>", "exec")

    def test_a_failed_check_names_both_models(self):
        runner = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "boom")  # noqa: E731
        with tempfile.TemporaryDirectory() as folder, \
                self.assertRaisesRegex(ic.InstallError, "Chatterbox 500M or Voice Conversion did not load or speak"):
            ic.run_original_check(folder, runner=runner)

    def test_the_whole_install_fetches_the_original_files_and_writes_its_own_receipt(self):
        server = FileServer()
        self.addCleanup(server.close)
        with tempfile.TemporaryDirectory(prefix="voice-lab-chatterbox-") as folder:
            root = Path(folder)
            contents = {name: noise(1_500 + index * 41, index + 50) for index, name in enumerate(chatterbox_support.ORIGINAL_FILES)}
            contents["README.md"] = b"---\nlicense: mit\n---\n"
            manifest = {**{key: ORIGINAL_MANIFEST[key] for key in ("schema", "repo", "revision", "license", "license_source")},
                        "files": [{"path": path, "size": len(data), "sha256": sha(data)} for path, data in contents.items()]}
            (root / "catalog").mkdir()
            (root / "requirements").mkdir()
            (root / ic.ORIGINAL_MANIFEST).write_text(json.dumps(manifest), "utf-8")
            (root / ic.LOCK).write_text(FakeLab.LOCK, "utf-8")
            prefix = f"/{manifest['repo']}/resolve/{manifest['revision']}/"
            server.files = {prefix + path: data for path, data in contents.items()}
            installed = {"pip": "25.0.1", **{item["key"]: item["version"] for item in base.read_lock(root / ic.LOCK)}}
            python = ic.environment_python(root)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            out_dir = root / "out" / "chatterbox"
            check = {"problem": None, "load_seconds": 20.0, "total_seconds": 7.5, "audio_seconds": 2.5, "vc_load_seconds": 4.0,
                     "vc_seconds": 3.0, "vc_audio_seconds": 2.5, "torch": "2.6.0+cpu",
                     "wav": out_dir / "install-original.wav", "converted": out_dir / "install-vc.wav"}
            calls = []

            def original_check(root, **kwargs):
                calls.append(("original", kwargs))
                out_dir.mkdir(parents=True, exist_ok=True)
                return check

            patches = ((ic, "ROOT", root), (base, "HOST", server.address), (ic, "environment_versions", lambda root: installed),
                       (ic, "run_original_check", original_check),
                       (ic, "run_speech_check", lambda *a, **k: self.fail("Turbo's check ran")),
                       (ic, "interpreter_problems", lambda root: []), (ic, "long_path_problems", lambda root, manifest: []),
                       (base, "space_problems", lambda root, need: []))
            with contextlib.ExitStack() as stack:
                for owner, name, value in patches:
                    stack.enter_context(mock.patch.object(owner, name, value))
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    status = ic.main(["--original", "--yes"])
            self.assertEqual((status, err.getvalue()), (0, ""))
            text = out.getvalue()
            self.assertIn("Chatterbox 500M and Voice Conversion on the CPU", text)
            self.assertIn("Chatterbox 500M and Voice Conversion are installed. Chatterbox 500M loaded in 20 s and spoke 2.5 s in "
                          "its built-in voice in 7.5 s; Voice Conversion said it again in 3.0 s.", text)
            self.assertIn("out\\chatterbox\\install-original.wav and out\\chatterbox\\install-vc.wav", text)
            self.assertEqual(calls, [("original", {"device": "cpu", "amd_card": False, "number": None})])
            snapshot = base.snapshot_dir(root, manifest)
            self.assertEqual((snapshot / "t3_cfg.safetensors").read_bytes(), contents["t3_cfg.safetensors"])
            receipt = json.loads((out_dir / "installation-original.json").read_text("utf-8"))
            self.assertEqual((receipt["repo"], receipt["revision"]), (manifest["repo"], manifest["revision"]))
            self.assertEqual(receipt["speech_check"], {key: check[key] for key in (
                "load_seconds", "total_seconds", "audio_seconds", "vc_load_seconds", "vc_seconds", "vc_audio_seconds")})
            self.assertNotIn("voice_match", receipt)
            self.assertFalse((out_dir / "installation.json").exists())    # Turbo's receipt is not touched
            (root / ".venv-chatterbox" / "Lib" / "site-packages" / "chatterbox_tts-0.1.7.dist-info").mkdir(parents=True)
            for engine in ("orig", "chatterbox-vc"):
                with self.subTest(engine=engine):
                    self.assertEqual(engine_checks.installation(CATALOG[engine], root)["missing"], [])


class ManifestTests(unittest.TestCase):
    def test_the_shipped_file_list_loads_with_the_shared_reader(self):
        self.assertEqual(base.load_manifest(ROOT / ic.MANIFEST), REAL_MANIFEST)

    def test_a_missing_or_incomplete_file_list_is_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.json"
            with self.assertRaisesRegex(ic.InstallError, "missing or unreadable"):
                base.load_manifest(path)
            path.write_text('{"repo": "a/b"}', "utf-8")
            with self.assertRaisesRegex(ic.InstallError, "missing or unreadable"):
                base.load_manifest(path)

    def test_the_revision_is_written_once_for_the_service(self):
        # speaker_similarity, the adapter and the installation check all take Turbo's snapshot from the catalog.
        import speaker_similarity

        self.assertEqual(speaker_similarity.ENCODER_WEIGHTS, chatterbox_support.turbo_snapshot_dir(ROOT) / "ve.safetensors")
        self.assertNotIn(ENTRY["local_revision"], (ROOT / "speaker_similarity.py").read_text("utf-8"))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", ENTRY["local_revision"]))


if __name__ == "__main__":
    unittest.main()
