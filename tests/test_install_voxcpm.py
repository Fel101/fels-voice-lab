"""tools/install_voxcpm.py: VoxCPM2 for an NVIDIA graphics card, into its own environment, with VoxCPM's source files at the
pinned commit. Nothing here reaches the real network, pip, venv, a graphics card or the real VoxCPM2."""

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine_checks  # noqa: E402
import engine_install  # noqa: E402
import voxcpm_worker  # noqa: E402
from tests.test_install_phonon import FileServer  # noqa: E402
from tools import install_kokoro as base  # noqa: E402
from tools import install_voxcpm as iv  # noqa: E402

CATALOG = {entry["id"]: entry for entry in json.loads((ROOT / "catalog" / "engines.json").read_text("utf-8"))["engines"]}
ENTRY = CATALOG["voxcpm2"]
MANIFEST = json.loads((ROOT / iv.MANIFEST).read_text("utf-8"))
SOURCE = json.loads((ROOT / iv.SOURCE_MANIFEST).read_text("utf-8"))
RTX_5070 = {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": (12, 0), "driver": (581, 57)}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def noise(length, seed=0):
    return hashlib.shake_256(seed.to_bytes(4, "little")).digest(length)


class FakeLab:
    """A lab folder with a two-package lock, three stand-in source files (one of them empty) and small stand-ins for the model
    files, at the real commit and revision."""

    LOCK = (
        f"# a stand-in for requirements/voxcpm-nvidia.txt\ntransformers==5.15.1 --hash=sha256:{'ab' * 32}\n"
        "torch @ https://download.pytorch.org/whl/cu130/torch-2.12.0%2Bcu130-cp312-cp312-win_amd64.whl "
        f"--hash=sha256:{'cd' * 32}\n"
    )

    def __init__(self, folder):
        self.root = Path(folder)
        self.model = {item["path"]: noise(900 + index * 31, index) for index, item in enumerate(MANIFEST["files"])}
        self.code = {
            "LICENSE": b"Apache License\n",
            "src/voxcpm/__init__.py": b"from .core import VoxCPM\n",
            "src/voxcpm/modules/__init__.py": b"",
        }
        (self.root / "catalog").mkdir()
        (self.root / "requirements").mkdir()
        self.manifest = {
            **{key: MANIFEST[key] for key in ("schema", "repo", "revision", "cache", "license", "license_source")},
            "files": [{"path": path, "size": len(data), "sha256": sha(data)} for path, data in self.model.items()],
        }
        self.source = {
            **{key: SOURCE[key] for key in ("schema", "repo", "commit", "license", "license_source")},
            "files": [{"path": path, "size": len(data), "sha256": sha(data)} for path, data in self.code.items()],
        }
        (self.root / iv.MANIFEST).write_text(json.dumps(self.manifest), "utf-8")
        (self.root / iv.SOURCE_MANIFEST).write_text(json.dumps(self.source), "utf-8")
        (self.root / iv.LOCK).write_text(self.LOCK, "utf-8")

    def served(self):
        model = f"/{self.manifest['repo']}/resolve/{self.manifest['revision']}/"
        code = f"/{self.source['repo']}/{self.source['commit']}/"
        return {
            **{model + path: data for path, data in self.model.items()},
            **{code + path: data for path, data in self.code.items() if data},
        }

    def snapshot(self):
        return base.snapshot_dir(self.root, self.manifest)


class ShippedFilesTests(unittest.TestCase):
    def test_the_file_lists_pin_what_the_catalog_and_the_worker_load(self):
        self.assertEqual((MANIFEST["repo"], MANIFEST["revision"]), (ENTRY["checkpoint"], ENTRY["local_revision"]))
        self.assertEqual(MANIFEST["revision"], voxcpm_worker.MODEL_REVISION)
        self.assertEqual(SOURCE["commit"], voxcpm_worker.SOURCE_COMMIT)
        self.assertEqual(SOURCE["commit"], ENTRY["backend"]["source_commit"])
        self.assertEqual(base.snapshot_dir(ROOT, MANIFEST), voxcpm_worker.MODEL)
        self.assertEqual(ROOT / iv.SOURCE / "src", voxcpm_worker.SOURCE)
        self.assertEqual(ENTRY["runtime"]["environment"], iv.ENVIRONMENT)

    def test_the_checkpoint_list_holds_the_model_its_audio_decoder_and_tokenizer(self):
        paths = {item["path"] for item in MANIFEST["files"]}
        self.assertLessEqual(set(engine_checks.REQUIRED_FILES["voxcpm2"]), paths)
        self.assertLessEqual({"README.md", "special_tokens_map.json", "tokenizer_config.json"}, paths)
        total = sum(item["size"] for item in MANIFEST["files"])
        self.assertTrue(4.9e9 < total < 5.0e9, total)
        self.assertEqual(MANIFEST["cache"], "hf-cache")
        self.assertEqual((MANIFEST["license"], SOURCE["license"]), ("Apache-2.0", "Apache-2.0"))

    def test_the_source_list_holds_what_inference_imports_and_leaves_out_the_rest(self):
        paths = {item["path"] for item in SOURCE["files"]}
        for path in (
            "LICENSE",
            "src/voxcpm/__init__.py",
            "src/voxcpm/core.py",
            "src/voxcpm/model/voxcpm2.py",
            "src/voxcpm/modules/audiovae/audio_vae_v2.py",
            "src/voxcpm/modules/minicpm4/model.py",
        ):
            self.assertIn(path, paths)
        left_out = {item["path"] for item in SOURCE["not_fetched"]}
        self.assertIn("src/voxcpm/zipenhancer.py", left_out)
        self.assertIn("src/voxcpm/utils/text_normalize.py", left_out)
        self.assertFalse(paths & left_out)
        self.assertFalse(any(path.startswith("src/voxcpm/training/") for path in paths))
        for item in SOURCE["files"]:
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("..", Path(item["path"]).parts)

    def test_the_package_list_is_the_nvidia_build_and_what_inference_imports(self):
        packages = {item["key"]: item for item in base.read_lock(ROOT / iv.LOCK)}
        self.assertEqual((packages["torch"]["version"], packages["torchaudio"]["version"]), ("2.12.0+cu130", "2.11.0+cu130"))
        self.assertEqual(packages["transformers"]["version"], "5.15.1")
        for name in ("librosa", "einops", "pydantic", "safetensors", "tqdm", "huggingface-hub", "numpy", "soundfile"):
            self.assertIn(name, packages)
        for name in ("gradio", "modelscope", "funasr", "datasets", "matplotlib", "torchcodec", "voxcpm"):
            self.assertNotIn(name, packages)
        qwen = base.lock_lines(ROOT / "requirements" / "qwen-nvidia.txt")
        lines = base.lock_lines(ROOT / iv.LOCK)
        self.assertEqual((lines["torch"], lines["torchaudio"]), (qwen["torch"], qwen["torchaudio"]))

    def test_the_launcher_and_the_models_page_run_the_installer(self):
        text = (ROOT / "setup-voxcpm.bat").read_text("utf-8")
        self.assertIn('".venv\\Scripts\\python.exe" tools\\install_voxcpm.py %*', text)
        self.assertIn(b"\r\n", (ROOT / "setup-voxcpm.bat").read_bytes())
        self.assertEqual(engine_install.INSTALLERS["voxcpm2"], "tools/install_voxcpm.py")
        self.assertEqual(engine_install.GPU_BUILDS["voxcpm2"], "tools.install_voxcpm")
        self.assertEqual(engine_checks.GUIDED_INSTALLERS["voxcpm2"], "setup-voxcpm.bat")

    def test_an_8_gb_card_is_big_enough_for_the_worker(self):
        voxcpm_worker.check_gpu_memory("NVIDIA GeForce RTX 5070 Laptop GPU", 8151 * 2**20)  # what an 8 GB card reports
        self.assertEqual(iv.GPU_MEMORY_GB, voxcpm_worker.MIN_GPU_GIB)


class ProblemAndPlanTests(unittest.TestCase):
    def test_without_an_nvidia_card_it_stops_and_old_cards_and_drivers_are_refused(self):
        self.assertEqual(iv.nvidia_problems(present=False), [iv.NO_NVIDIA_DRIVER])
        self.assertIn("--checkpoint-only", iv.NO_NVIDIA_DRIVER)
        self.assertEqual(iv.nvidia_problems(present=True, cards=[RTX_5070]), [])
        old = iv.nvidia_problems(present=True, cards=[{**RTX_5070, "name": "GTX 1080", "capability": (6, 1)}])
        self.assertIn("cannot run on this card", old[0])
        driver = iv.nvidia_problems(present=True, cards=[{**RTX_5070, "driver": (560, 94)}])
        self.assertIn("Update the driver", driver[0])

    def test_the_plan_names_the_source_the_model_the_memory_it_needs_and_the_licences(self):
        packages = base.read_lock(ROOT / iv.LOCK)
        text = " ".join(
            iv.plan_text(
                MANIFEST,
                SOURCE,
                packages,
                len(packages),
                MANIFEST["files"],
                SOURCE["files"],
                environment_exists=False,
                checkpoint_only=False,
            ).split()
        )
        self.assertIn("Fel's Voice Lab: VoxCPM2 on an NVIDIA graphics card", text)
        self.assertIn(f"{len(packages)} of {len(packages)} to install there", text)
        self.assertIn("About 2.1 GB to download and 5.1 GB on disk", text)
        self.assertIn(f"the {len(SOURCE['files'])} files its inference imports", text)
        self.assertIn("github.com/OpenBMB/VoxCPM at commit f772e49, into out\\voxcpm2\\source", text)
        self.assertIn("the 4.6 GB model, the 0.4 GB audio decoder", text)
        self.assertIn("5.0 GB to download", text)
        self.assertIn("at least 7 GB of memory", text)
        self.assertIn("VoxCPM2 weights and VoxCPM's code are Apache-2.0", text)
        text.encode("ascii")
        text = " ".join(
            iv.plan_text(MANIFEST, SOURCE, packages, 0, [], [], environment_exists=True, checkpoint_only=True).split()
        )
        self.assertIn("left alone (--checkpoint-only)", text)
        self.assertIn("Everything is already in place", text)


class SpeechCheckTests(unittest.TestCase):
    GOOD = (
        '{"problem": null, "load_seconds": 20.5, "total_seconds": 9.1, "audio_seconds": 2.6, "torch": "2.12.0+cu130", '
        '"device": "cuda", "device_name": "NVIDIA GeForce RTX 5070 Laptop GPU", "peak_gpu_gb": 5.6}'
    )

    def check(self, stdout="", stderr="", code=0):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, code, stdout, stderr)

        with tempfile.TemporaryDirectory() as folder:
            return iv.run_speech_check(folder, runner=runner), calls, Path(folder)

    def test_the_check_runs_offline_in_its_environment_through_the_labs_worker(self):
        result, calls, folder = self.check(stdout=self.GOOD + "\n")
        command, kwargs = calls[0]
        self.assertEqual(command[:2], [str(folder / ".venv-voxcpm" / "Scripts" / "python.exe"), "-c"])
        self.assertIn('VoxCPMWorker("cuda")', command[2])
        self.assertIn('smoke_request("voxcpm2", loaded, root=root)', command[2])
        self.assertIn('.venv-fqwen" / "Lib" / "site-packages"', command[2])  # only for an environment without PyTorch
        self.assertEqual(command[3:], [str(folder), str(folder / "out" / "voxcpm2" / "install-check.wav")])
        self.assertEqual(kwargs["env"]["HF_HUB_OFFLINE"], "1")
        self.assertEqual((result["peak_gpu_gb"], result["audio_seconds"]), (5.6, 2.6))

    def test_a_failure_shows_the_end_of_the_output_and_what_to_do(self):
        with self.assertRaisesRegex(iv.InstallError, r"(?s)VoxCPM2 did not load or speak:.*ran out of memory"):
            self.check(stderr="torch.OutOfMemoryError: CUDA out of memory.", code=1)
        with self.assertRaisesRegex(iv.InstallError, "not speech: generated silence"):
            self.check(stdout=self.GOOD.replace("null", '"generated silence"'))

    def test_the_names_the_check_script_uses_exist_in_the_lab(self):
        self.assertTrue(callable(voxcpm_worker.VoxCPMWorker.load) and callable(voxcpm_worker.VoxCPMWorker.generate))
        request = engine_checks.smoke_request("voxcpm2", {}, root=ROOT)
        self.assertEqual((request["mode"], request["voice_description"]), ("design", engine_checks.CHECK_VOICE))
        compile(iv.SPEECH_CHECK, "<speech check>", "exec")


class RunTests(unittest.TestCase):
    """The whole install with venv, pip, both hosts, the NVIDIA checks and VoxCPM2 replaced by stand-ins."""

    @classmethod
    def setUpClass(cls):
        cls.server = FileServer()
        cls.addClassCleanup(cls.server.close)

    def setUp(self):
        self.server.reset()
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-voxcpm-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.lab = FakeLab(self.base)
        self.server.files = self.lab.served()
        self.installed = None
        self.calls = []
        self.nvidia = []
        self.check = {
            "problem": None,
            "load_seconds": 20.5,
            "total_seconds": 9.1,
            "audio_seconds": 2.6,
            "torch": "2.12.0+cu130",
            "device": "cuda",
            "device_name": "NVIDIA GeForce RTX 5070 Laptop GPU",
            "peak_gpu_gb": 5.6,
            "wav": self.base / "out" / "voxcpm2" / "install-check.wav",
        }

        def make(root, **kwargs):
            self.calls.append("venv")
            python = iv.environment_python(root)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            self.installed = {"pip": "25.0.1"}

        def pip(root, **kwargs):
            self.calls.append("pip")
            self.installed.update({item["key"]: item["version"] for item in base.read_lock(root / iv.LOCK)})

        def speak(root, **kwargs):
            self.calls.append("check")
            (root / iv.OUTPUT).mkdir(parents=True, exist_ok=True)
            return self.check

        for owner, name, value in (
            (iv, "ROOT", self.base),
            (base, "HOST", self.server.address),
            (iv, "RAW_HOST", self.server.address),
            (iv, "make_environment", make),
            (iv, "environment_versions", lambda root: dict(self.installed or {})),
            (iv, "install_packages", pip),
            (iv, "run_speech_check", speak),
            (iv, "nvidia_problems", lambda: list(self.nvidia)),
            (iv, "interpreter_problems", lambda root: []),
            (iv, "long_path_problems", lambda root, manifest, source: []),
            (base, "space_problems", lambda root, need: []),
        ):
            patch = mock.patch.object(owner, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO()):
            status = iv.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def test_a_dry_run_changes_nothing(self):
        status, out, err = self.run_main("--dry-run")
        self.assertEqual((status, err), (0, ""))
        self.assertIn("This was a dry run", out)
        self.assertEqual((self.calls, self.server.requests), ([], []))
        self.assertFalse((self.base / "out").exists())

    def test_the_whole_install_fetches_source_and_model_checks_and_writes_a_receipt(self):
        status, out, err = self.run_main("--yes")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["venv", "pip", "check"])
        self.assertIn("3 of 3 source files fetched", out)
        source = self.base / iv.SOURCE
        self.assertEqual((source / "src" / "voxcpm" / "__init__.py").read_bytes(), self.lab.code["src/voxcpm/__init__.py"])
        self.assertEqual((source / "src" / "voxcpm" / "modules" / "__init__.py").read_bytes(), b"")
        self.assertEqual((self.lab.snapshot() / "audiovae.pth").read_bytes(), self.lab.model["audiovae.pth"])
        self.assertEqual(self.lab.snapshot().parents[2], self.base / "hf-cache")  # the top of hf-cache, not hub
        self.assertIn(
            "VoxCPM2 is installed. It loaded in 20 s on the NVIDIA GeForce RTX 5070 Laptop GPU and spoke 2.6 s in a "
            "voice made from a short description in 9.1 s, using 5.6 GB of graphics memory at most.",
            " ".join(out.split()),
        )
        receipt = json.loads((self.base / "out" / "voxcpm2" / "installation.json").read_text("utf-8"))
        self.assertEqual((receipt["source"]["commit"], receipt["revision"]), (SOURCE["commit"], MANIFEST["revision"]))
        self.assertEqual(receipt["speech_check"]["peak_gpu_gb"], 5.6)
        # and the Lab counts it as installed
        site = self.base / ".venv-voxcpm" / "Lib" / "site-packages"
        for name in ("torch-2.12.0+cu130", "transformers-5.15.1", "librosa-1.0.0"):
            (site / f"{name}.dist-info").mkdir(parents=True)
        self.assertEqual(engine_checks.installation(ENTRY, self.base)["missing"], [])

    def test_a_second_run_fetches_nothing_and_a_changed_source_file_is_fetched_again(self):
        self.run_main("--yes")
        requests = len(self.server.requests)
        status, out, _err = self.run_main("--yes")
        self.assertEqual((status, len(self.server.requests)), (0, requests))
        (self.base / iv.SOURCE / "src" / "voxcpm" / "__init__.py").write_bytes(b"changed\n")
        status, out, _err = self.run_main("--yes")
        self.assertEqual(status, 0)
        self.assertIn("1 of 3 source files fetched", out)
        self.assertEqual(
            (self.base / iv.SOURCE / "src" / "voxcpm" / "__init__.py").read_bytes(), self.lab.code["src/voxcpm/__init__.py"]
        )

    def test_no_nvidia_card_stops_it_before_any_change_but_checkpoint_only_goes_ahead(self):
        self.nvidia = [iv.NO_NVIDIA_DRIVER]
        status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("only on an NVIDIA graphics card", " ".join(err.split()))
        self.assertEqual(self.calls, [])
        status, _out, err = self.run_main("--yes", "--checkpoint-only")
        self.assertEqual(status, 1)
        self.assertIn("there is no .venv-voxcpm here", " ".join(err.split()))
        python = iv.environment_python(self.base)
        python.parent.mkdir(parents=True)
        python.write_bytes(b"")
        self.installed = {"pip": "25.0.1"}  # an environment made by hand, which borrows .venv-fqwen's packages
        status, _out, err = self.run_main("--yes", "--checkpoint-only")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["check"])

    def test_a_source_file_that_does_not_match_its_hash_is_not_kept(self):
        # the same length as the real file, so only its SHA-256 can tell
        self.server.files = {**self.lab.served(), f"/{SOURCE['repo']}/{SOURCE['commit']}/LICENSE": b"Apache Licence\n"}
        status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("LICENSE does not match the published SHA-256", err)
        self.assertFalse((self.base / iv.SOURCE / "LICENSE").exists())


if __name__ == "__main__":
    unittest.main()
