"""tools/install_breeze.py: Breeze TTS 2 for an NVIDIA graphics card, into its own environment, with its non-commercial licence
said first. Nothing here reaches the real network, pip, venv, a graphics card or the real Breeze."""

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

import breeze_worker  # noqa: E402
import engine_checks  # noqa: E402
import engine_install  # noqa: E402
from tests.test_install_phonon import FileServer  # noqa: E402
from tools import install_breeze as ib  # noqa: E402
from tools import install_kokoro as base  # noqa: E402

CATALOG = {entry["id"]: entry for entry in json.loads((ROOT / "catalog" / "engines.json").read_text("utf-8"))["engines"]}
ENTRY = CATALOG["breeze-tts-2"]
MANIFEST = json.loads((ROOT / ib.MANIFEST).read_text("utf-8"))


def loader_constants():
    """The literal settings of breeze/loader.py, read from its source: importing it needs PyTorch, which the lab's .venv lacks."""
    import ast

    tree = ast.parse((ROOT / "breeze" / "loader.py").read_text("utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    try:
                        found[target.id] = ast.literal_eval(node.value)
                    except ValueError:
                        pass
    return found


LOADER = loader_constants()
RTX_4080 = {"name": "NVIDIA GeForce RTX 4080", "capability": (8, 9), "driver": (581, 57), "memory_mib": 16376}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def noise(length, seed=0):
    return hashlib.shake_256(seed.to_bytes(4, "little")).digest(length)


class ShippedFilesTests(unittest.TestCase):
    def test_the_file_list_pins_the_revision_the_catalog_and_the_worker_use_into_the_folder_the_loader_reads(self):
        self.assertEqual((MANIFEST["repo"], MANIFEST["revision"]), (ENTRY["checkpoint"], ENTRY["local_revision"]))
        self.assertEqual(MANIFEST["revision"], breeze_worker.CHECKPOINT_REVISION)
        self.assertEqual(MANIFEST["repo"], LOADER["MIRROR_REPO_ID"])
        # Without ComfyUI the loader looks in breeze/models/<MODEL_FOLDER_NAME>/<repo id with "/" made "_">.
        folder = f"breeze/models/{LOADER['MODEL_FOLDER_NAME']}/{LOADER['MIRROR_REPO_ID'].replace('/', '_')}"
        self.assertEqual(MANIFEST["folder"], folder)
        self.assertEqual(MANIFEST["folder"], engine_checks.BREEZE_FOLDER)
        self.assertEqual(base.snapshot_dir(ROOT, MANIFEST), ROOT / folder)
        self.assertEqual(ENTRY["runtime"]["environment"], ib.ENVIRONMENT)

    def test_the_file_list_holds_the_bf16_model_and_what_the_loader_needs_and_leaves_out_the_int8_builds(self):
        paths = {item["path"] for item in MANIFEST["files"]}
        self.assertLessEqual(set(engine_checks.BREEZE_FILES), paths)
        self.assertIn(ib.WEIGHTS, paths)
        for pattern in LOADER["SMALL_FILE_PATTERNS"]:
            if "*" not in pattern:
                self.assertIn(pattern, paths)
        self.assertFalse(any("int8" in path for path in paths))
        total = sum(item["size"] for item in MANIFEST["files"])
        self.assertTrue(7.6e9 < total < 7.8e9, total)

    def test_the_licence_is_the_non_commercial_one_from_the_model_card(self):
        self.assertEqual(MANIFEST["license"], "BreezeBlue Research and Non-Commercial License")
        self.assertTrue(MANIFEST["license_link"].startswith("https://huggingface.co/BreezeBlue/Breeze-TTS-2/"))
        self.assertIn("RESONIA", MANIFEST["license_source"])
        self.assertIn("Non-Commercial", ENTRY["capabilities"]["license_note"])

    def test_the_package_list_is_the_nvidia_build_of_what_breeze_imports(self):
        packages = {item["key"]: item for item in base.read_lock(ROOT / ib.LOCK)}
        self.assertEqual((packages["torch"]["version"], packages["torchaudio"]["version"]), ("2.12.0+cu130", "2.11.0+cu130"))
        self.assertEqual(packages["transformers"]["version"], ENTRY["backend"]["transformers_version"])
        for name in ("accelerate", "safetensors", "soundfile", "numpy", "huggingface-hub", "tokenizers"):
            self.assertIn(name, packages)
        for name in ("comfy-kitchen", "flash-attn", "sageattention", "gradio"):
            self.assertNotIn(name, packages)

    def test_the_launcher_and_the_models_page_run_the_installer(self):
        text = (ROOT / "setup-breeze.bat").read_text("utf-8")
        self.assertIn('".venv\\Scripts\\python.exe" tools\\install_breeze.py %*', text)
        self.assertIn("non-commercial", text)
        self.assertIn(b"\r\n", (ROOT / "setup-breeze.bat").read_bytes())
        self.assertEqual(engine_install.INSTALLERS["breeze-tts-2"], "tools/install_breeze.py")
        self.assertEqual(engine_install.GPU_BUILDS["breeze-tts-2"], "tools.install_breeze")
        self.assertEqual(engine_checks.GUIDED_INSTALLERS["breeze-tts-2"], "setup-breeze.bat")


class ProblemAndPlanTests(unittest.TestCase):
    def test_an_8_gb_card_is_refused_before_anything_is_downloaded_and_a_16_gb_one_passes(self):
        laptop = {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": (12, 0), "driver": (581, 57), "memory_mib": 8151}
        problems = ib.nvidia_problems(present=True, cards=[laptop])
        self.assertEqual(len(problems), 1)
        self.assertIn(
            "needs a graphics card with at least 10 GB of memory, and the NVIDIA GeForce RTX 5070 Laptop GPU has 8.0 GB",
            problems[0],
        )
        self.assertEqual(ib.nvidia_problems(present=True, cards=[RTX_4080]), [])
        self.assertEqual(ib.nvidia_problems(present=False), [ib.NO_NVIDIA_DRIVER])

    def test_the_plan_says_the_licence_first_then_what_it_installs(self):
        packages = base.read_lock(ROOT / ib.LOCK)
        text = ib.plan_text(
            MANIFEST, packages, len(packages), MANIFEST["files"], environment_exists=False, checkpoint_only=False
        )
        title, licence = text.split("\n\n")[:2]
        self.assertEqual(title, "Fel's Voice Lab: Breeze TTS 2 on an NVIDIA graphics card")
        licence = " ".join(licence.split())
        self.assertTrue(
            licence.startswith("Licence: Breeze TTS 2's weights, these converted files and what you make with them")
        )
        self.assertIn("research and non-commercial use only", licence)
        self.assertIn("Installing them means you accept it", licence)
        flat = " ".join(text.split())
        self.assertIn(f"{len(packages)} of {len(packages)} to install there", flat)
        self.assertIn("the 7.0 GB BF16 model, the 0.7 GB audio tokenizer", flat)
        self.assertIn("into breeze\\models\\breezetts2\\drbaph_Breeze-TTS-2-comfyui", flat)
        self.assertIn("at least 10 GB of memory", flat)
        self.assertIn("INT8 builds are left out", flat)
        text.encode("ascii")


class SpeechCheckTests(unittest.TestCase):
    def test_the_check_runs_offline_in_its_environment_through_the_labs_worker(self):
        calls = []
        good = (
            '{"problem": null, "load_seconds": 60.1, "total_seconds": 8.2, "audio_seconds": 2.9, "torch": "2.12.0+cu130", '
            '"device": "cuda", "device_name": "NVIDIA GeForce RTX 4080", "peak_gpu_gb": 7.6}'
        )

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, good + "\n", "")

        with tempfile.TemporaryDirectory() as folder:
            result = ib.run_speech_check(folder, runner=runner)
            command, kwargs = calls[0]
            self.assertEqual(command[:2], [str(Path(folder) / ".venv-breeze" / "Scripts" / "python.exe"), "-c"])
            self.assertIn('BreezeWorker("cuda")', command[2])
            self.assertIn('worker.load("eager")', command[2])
            self.assertEqual(command[3:], [folder, str(Path(folder) / "out" / "breeze" / "install-check.wav")])
            self.assertEqual(kwargs["env"]["HF_HUB_OFFLINE"], "1")
            self.assertEqual(result["peak_gpu_gb"], 7.6)

    def test_running_out_of_graphics_memory_says_how_much_breeze_needs(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "torch.OutOfMemoryError: CUDA out of memory.")

        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ib.InstallError, "needs about 10 GB of it"):
            ib.run_speech_check(folder, runner=runner)

    def test_the_names_the_check_script_uses_exist_in_the_lab(self):
        self.assertTrue(callable(breeze_worker.BreezeWorker.load) and callable(breeze_worker.BreezeWorker.generate))
        request = engine_checks.smoke_request("breeze-tts-2", {}, root=ROOT)
        self.assertEqual(request["mode"], "design")
        compile(ib.SPEECH_CHECK, "<speech check>", "exec")


class RunTests(unittest.TestCase):
    """The whole install with venv, pip, the model host, the NVIDIA checks and Breeze replaced by stand-ins."""

    @classmethod
    def setUpClass(cls):
        cls.server = FileServer()
        cls.addClassCleanup(cls.server.close)

    def setUp(self):
        self.server.reset()
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-breeze-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / "catalog").mkdir()
        (self.base / "requirements").mkdir()
        self.contents = {item["path"]: noise(700 + index * 29, index) for index, item in enumerate(MANIFEST["files"])}
        self.manifest = {
            **{key: value for key, value in MANIFEST.items() if key not in ("files", "not_fetched")},
            "files": [{"path": path, "size": len(data), "sha256": sha(data)} for path, data in self.contents.items()],
        }
        (self.base / ib.MANIFEST).write_text(json.dumps(self.manifest), "utf-8")
        (self.base / ib.LOCK).write_text(f"transformers==5.17.0 --hash=sha256:{'ab' * 32}\n", "utf-8")
        prefix = f"/{MANIFEST['repo']}/resolve/{MANIFEST['revision']}/"
        self.server.files = {prefix + path: data for path, data in self.contents.items()}
        self.installed = None
        self.calls = []
        self.nvidia = []
        self.check = {
            "problem": None,
            "load_seconds": 60.1,
            "total_seconds": 8.2,
            "audio_seconds": 2.9,
            "torch": "2.12.0+cu130",
            "device": "cuda",
            "device_name": "NVIDIA GeForce RTX 4080",
            "peak_gpu_gb": 7.6,
            "wav": self.base / "out" / "breeze" / "install-check.wav",
        }

        def make(root, **kwargs):
            self.calls.append("venv")
            python = ib.environment_python(root)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            self.installed = {"pip": "25.0.1"}

        def pip(root, **kwargs):
            self.calls.append("pip")
            self.installed.update({item["key"]: item["version"] for item in base.read_lock(root / ib.LOCK)})

        def speak(root, **kwargs):
            self.calls.append("check")
            (root / ib.OUTPUT).mkdir(parents=True, exist_ok=True)
            return self.check

        for owner, name, value in (
            (ib, "ROOT", self.base),
            (base, "HOST", self.server.address),
            (ib, "make_environment", make),
            (ib, "environment_versions", lambda root: dict(self.installed or {})),
            (ib, "install_packages", pip),
            (ib, "run_speech_check", speak),
            (ib, "nvidia_problems", lambda: list(self.nvidia)),
            (ib, "interpreter_problems", lambda root: []),
            (ib, "long_path_problems", lambda root, manifest: []),
            (base, "space_problems", lambda root, need: []),
        ):
            patch = mock.patch.object(owner, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO()):
            status = ib.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def test_a_dry_run_shows_the_licence_and_changes_nothing(self):
        status, out, err = self.run_main("--dry-run")
        self.assertEqual((status, err), (0, ""))
        self.assertIn("research and non-commercial use only", " ".join(out.split()))
        self.assertEqual((self.calls, self.server.requests), ([], []))

    def test_the_whole_install_puts_the_files_where_the_loader_reads_them_and_writes_a_receipt(self):
        status, out, err = self.run_main("--yes")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["venv", "pip", "check"])
        folder = self.base / "breeze" / "models" / "breezetts2" / "drbaph_Breeze-TTS-2-comfyui"
        self.assertEqual(
            (folder / "audio_tokenizer" / "model.safetensors").read_bytes(), self.contents["audio_tokenizer/model.safetensors"]
        )
        self.assertFalse((self.base / "breeze" / "models" / "refs").exists())  # a plain folder: no Hugging Face refs
        self.assertIn("Breeze TTS 2 is installed. It loaded in 60 s on the NVIDIA GeForce RTX 4080", " ".join(out.split()))
        self.assertIn("for research and non-commercial use", " ".join(out.split()))
        receipt = json.loads((self.base / "out" / "breeze" / "installation.json").read_text("utf-8"))
        self.assertEqual((receipt["license"], receipt["revision"]), (MANIFEST["license"], MANIFEST["revision"]))
        site = self.base / ".venv-breeze" / "Lib" / "site-packages"
        for name in ("transformers-5.17.0", "accelerate-1.15.0"):
            (site / f"{name}.dist-info").mkdir(parents=True)
        self.assertEqual(engine_checks.installation(ENTRY, self.base)["missing"], [])

    def test_a_card_that_cannot_take_it_stops_it_before_any_change(self):
        self.nvidia = ["Breeze TTS 2's NVIDIA build needs a graphics card with at least 10 GB of memory."]
        status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("at least 10 GB of memory", err)
        self.assertEqual((self.calls, self.server.requests), ([], []))


if __name__ == "__main__":
    unittest.main()
