"""tools/install_qwen.py: one of the four Qwen3-TTS models, for an NVIDIA or AMD graphics card, into the .venv-fqwen they share.
Nothing here reaches the real network, pip, venv, a graphics card or the real Qwen3-TTS."""

import contextlib
import hashlib
import io
import json
import os
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
import qwen_support  # noqa: E402
import qwen_worker  # noqa: E402
import soundfile as sf  # noqa: E402
from tests.test_amd_build import DEVICES, R9700, RADEON_780M, RADEON_VII  # noqa: E402
from tests.test_install_phonon import FileServer  # noqa: E402
from tools import amd_build as amd  # noqa: E402
from tools import install_kokoro as base  # noqa: E402
from tools import install_qwen as iq  # noqa: E402

CATALOG = {entry["id"]: entry for entry in json.loads((ROOT / "catalog" / "engines.json").read_text("utf-8"))["engines"]}
MANIFESTS = {engine: json.loads((ROOT / spec["manifest"]).read_text("utf-8")) for engine, spec in iq.MODELS.items()}
RTX_5070 = {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": (12, 0), "driver": (581, 57)}
DESIGN = "qwen3-tts-1.7b-voice-design"
SMALL = "qwen3-tts-0.6b-base"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def noise(length, seed=0):
    return hashlib.shake_256(seed.to_bytes(4, "little")).digest(length)


class FakeLab:
    """A lab folder holding a two-package lock, the AMD list, and small stand-ins for one Qwen checkpoint's files, at its real
    revision."""

    LOCK = (
        f"# a stand-in for requirements/qwen-nvidia.txt\nfaster-qwen3-tts==0.4.0 --hash=sha256:{'ab' * 32}\n"
        "torch @ https://download.pytorch.org/whl/cu130/torch-2.12.0%2Bcu130-cp312-cp312-win_amd64.whl "
        f"--hash=sha256:{'cd' * 32}\n"
    )

    def __init__(self, folder, engine=DESIGN):
        self.root = Path(folder)
        self.engine = engine
        real = MANIFESTS[engine]
        self.contents = {item["path"]: noise(1_000 + index * 37, index) for index, item in enumerate(real["files"])}
        (self.root / "catalog").mkdir()
        (self.root / "requirements").mkdir()
        self.manifest = {
            **{key: real[key] for key in ("schema", "repo", "revision", "license", "license_source")},
            "files": [{"path": path, "size": len(data), "sha256": sha(data)} for path, data in self.contents.items()],
        }
        for spec in iq.MODELS.values():
            other = {**MANIFESTS[next(key for key, value in iq.MODELS.items() if value is spec)]}
            (self.root / spec["manifest"]).write_text(json.dumps(other), "utf-8")
        (self.root / iq.MODELS[engine]["manifest"]).write_text(json.dumps(self.manifest), "utf-8")
        (self.root / iq.LOCK).write_text(self.LOCK, "utf-8")
        (self.root / amd.LOCK).write_bytes((ROOT / amd.LOCK).read_bytes())

    def served(self):
        prefix = f"/{self.manifest['repo']}/resolve/{self.manifest['revision']}/"
        return {prefix + path: data for path, data in self.contents.items()}

    def snapshot(self):
        return base.snapshot_dir(self.root, self.manifest)


class ShippedFilesTests(unittest.TestCase):
    def test_each_file_list_pins_the_revision_the_catalog_and_the_worker_load(self):
        self.assertEqual(set(iq.MODELS), set(qwen_support.QWEN_MODELS))
        self.assertEqual(set(iq.MODELS), set(qwen_worker.ENGINES))
        for engine, manifest in MANIFESTS.items():
            with self.subTest(engine=engine):
                self.assertEqual(
                    (manifest["repo"], manifest["revision"]), (CATALOG[engine]["checkpoint"], CATALOG[engine]["local_revision"])
                )
                self.assertEqual(
                    (manifest["repo"], manifest["revision"]),
                    (qwen_worker.ENGINES[engine]["repo_id"], qwen_worker.ENGINES[engine]["revision"]),
                )
                self.assertEqual(CATALOG[engine]["runtime"]["environment"], iq.ENVIRONMENT)
                self.assertEqual(iq.MODELS[engine]["short"], qwen_support.QWEN_MODELS[engine])

    def test_each_file_list_holds_what_the_worker_loads_the_speech_tokenizer_and_the_model_card(self):
        for engine, manifest in MANIFESTS.items():
            with self.subTest(engine=engine):
                paths = [item["path"] for item in manifest["files"]]
                self.assertLessEqual(
                    {
                        *qwen_support.QWEN_FILES,
                        "README.md",
                        "generation_config.json",
                        "preprocessor_config.json",
                        "speech_tokenizer/configuration.json",
                        "speech_tokenizer/preprocessor_config.json",
                    },
                    set(paths),
                )
                self.assertEqual(len(paths), len(set(paths)))
                self.assertEqual([item["path"] for item in manifest["not_fetched"]], [".gitattributes"])
                for item in manifest["files"]:
                    self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$", item["path"])
                    self.assertGreater(item["size"], 0)
                    self.assertNotIn("..", Path(item["path"]).parts)
                    self.assertFalse(item["path"].startswith("/"))

    def test_the_sizes_are_the_models_and_one_speech_tokenizer_shared_by_all_four(self):
        tokenizers = {
            next(item["sha256"] for item in manifest["files"] if item["path"] == "speech_tokenizer/model.safetensors")
            for manifest in MANIFESTS.values()
        }
        self.assertEqual(len(tokenizers), 1, "the four checkpoints carry the same speech tokenizer")
        totals = {engine: sum(item["size"] for item in manifest["files"]) for engine, manifest in MANIFESTS.items()}
        self.assertTrue(2.4e9 < totals[SMALL] < 2.6e9, totals[SMALL])
        for engine in set(totals) - {SMALL}:
            self.assertTrue(4.4e9 < totals[engine] < 4.6e9, (engine, totals[engine]))

    def test_the_licence_is_recorded_from_the_model_card(self):
        for engine, manifest in MANIFESTS.items():
            with self.subTest(engine=engine):
                self.assertEqual(manifest["license"], "Apache-2.0")
                self.assertIn("model card", manifest["license_source"])

    def test_the_package_list_is_the_nvidia_build_of_the_versions_the_catalog_names(self):
        packages = {item["key"]: item for item in base.read_lock(ROOT / iq.LOCK)}
        backend = CATALOG[SMALL]["backend"]
        self.assertEqual(packages["faster-qwen3-tts"]["version"], backend["package_version"])
        self.assertEqual(packages["transformers"]["version"], backend["transformers_version"])
        self.assertEqual((packages["torch"]["version"], packages["torchaudio"]["version"]), ("2.12.0+cu130", "2.11.0+cu130"))
        self.assertEqual(packages["qwen-tts-hf"]["version"], "0.1.1.post1")
        for name in ("librosa", "onnxruntime", "accelerate", "einops", "soundfile", "sox", "numpy", "huggingface-hub"):
            self.assertIn(name, packages)
        for name in ("gradio", "fastapi", "pandas", "uvicorn", "pydantic"):
            self.assertNotIn(name, packages, "the upstream demo app and what only it needs stay out")
        self.assertEqual(len(packages), 66)

    def test_the_cuda_builds_are_the_same_files_as_chatterboxs(self):
        lines = base.lock_lines(ROOT / iq.LOCK)
        chatterbox = base.lock_lines(ROOT / "requirements" / "chatterbox-nvidia.txt")
        self.assertEqual((lines["torch"], lines["torchaudio"]), (chatterbox["torch"], chatterbox["torchaudio"]))

    def test_every_line_of_the_package_list_pins_one_wheel_and_nothing_points_at_another_index(self):
        text = (ROOT / iq.LOCK).read_text("utf-8")
        self.assertNotIn("--index-url", text)
        self.assertNotIn("--extra-index-url", text)
        for line in text.splitlines():
            if line and not line.startswith("#"):
                self.assertRegex(
                    line, r"^\S+(==\S+| @ https://download\.pytorch\.org/whl/cu130/\S+\.whl) --hash=sha256:[0-9a-f]{64}$"
                )

    def test_the_batch_file_runs_the_installer_with_the_labs_own_python_and_waits_for_a_key(self):
        text = (ROOT / "setup-qwen.bat").read_text("utf-8")
        self.assertIn('".venv\\Scripts\\python.exe" tools\\install_qwen.py %*', text)
        self.assertIn("Run setup.bat first.", text)
        self.assertEqual(text.strip().splitlines()[-1], "pause")
        self.assertIn(b"\r\n", (ROOT / "setup-qwen.bat").read_bytes())

    def test_the_models_page_runs_the_launchers_command_for_each_model(self):
        for engine, model in qwen_support.QWEN_MODELS.items():
            with self.subTest(engine=engine):
                self.assertEqual(engine_install.INSTALLERS[engine], ("tools/install_qwen.py", "--model", model))
                self.assertEqual(engine_checks.GUIDED_INSTALLERS[engine], f"setup-qwen.bat --model {model}")
                self.assertEqual(engine_install.GPU_BUILDS[engine], "tools.install_qwen")
                self.assertEqual(iq.engine_id(model), engine)
                self.assertEqual(iq.engine_id(engine), engine)
        with self.assertRaisesRegex(iq.InstallError, "no Qwen3-TTS model called '2b'. Choose one of: 0.6b-base"):
            iq.engine_id("2b")

    def test_the_installer_prints_only_plain_ascii(self):
        for engine in iq.MODELS:
            text = iq.plan_text(
                engine,
                MANIFESTS[engine],
                base.read_lock(ROOT / iq.LOCK),
                66,
                MANIFESTS[engine]["files"],
                environment_exists=False,
                checkpoint_only=False,
            )
            text.encode("ascii")
        for problem in [iq.NO_NVIDIA_DRIVER, *iq.reference_problems(Path(tempfile.gettempdir()) / "no-lab-here", SMALL)]:
            problem.encode("ascii")


class ProblemTests(unittest.TestCase):
    def test_a_switched_off_card_stops_it_before_any_download_and_is_not_called_too_old(self):
        off = {**RTX_5070, "memory_mib": None, "unavailable": "CUDA_ERROR_NO_DEVICE: no CUDA-capable device is detected"}
        [problem] = iq.nvidia_problems(present=True, cards=[off])
        self.assertIn("CUDA cannot use the NVIDIA GeForce RTX 5070 Laptop GPU", problem)
        self.assertIn("switched off", problem)
        for wrong in ("cannot run on this card", "Update the driver", "leave out --nvidia"):
            self.assertNotIn(wrong, problem)

    def test_without_an_nvidia_driver_or_an_amd_card_it_stops_and_says_there_is_no_cpu_build(self):
        problems = iq.nvidia_problems(present=False)
        self.assertEqual(problems, [iq.NO_NVIDIA_DRIVER])
        self.assertEqual(iq.gpu_problems(None, []), [iq.NO_NVIDIA_DRIVER])
        text = " ".join(problems[0].split())
        self.assertIn("only on a graphics card, an NVIDIA card or an AMD Radeon", text)
        self.assertIn("AMD Software: Adrenalin Edition", text)
        self.assertIn("--checkpoint-only", text)
        self.assertNotIn("CPU build", text)

    def test_an_amd_card_is_judged_by_the_chip_amds_build_has_code_for(self):
        with tempfile.TemporaryDirectory() as folder:
            FakeLab(folder)
            with mock.patch.dict(os.environ):
                os.environ.pop(amd.VARIABLE, None)
                self.assertEqual(iq.gpu_problems("amd", [RADEON_780M, R9700], folder), [])
                self.assertEqual(iq.amd_card_problems([RADEON_780M], root=folder), [])  # Qwen3-TTS asks no more memory
                [old] = iq.gpu_problems("amd", [RADEON_VII], folder)
            self.assertIn("has no code for the AMD Radeon VII (gfx906)", old)

    def test_an_rtx_50_card_passes_and_old_cards_and_drivers_are_refused_with_what_to_do(self):
        self.assertEqual(iq.nvidia_problems(present=True, cards=[RTX_5070]), [])
        self.assertEqual(iq.card_problems([RTX_5070]), [])
        old = iq.nvidia_problems(
            present=True, cards=[{"name": "NVIDIA GeForce GTX 1080", "capability": (6, 1), "driver": (581, 57)}]
        )
        self.assertEqual(len(old), 1)
        self.assertIn("no code for the NVIDIA GeForce GTX 1080 (compute capability 6.1)", old[0])
        self.assertIn("cannot run on this card", old[0])
        driver = iq.nvidia_problems(present=True, cards=[{**RTX_5070, "driver": (560, 94)}])
        self.assertIn("needs NVIDIA driver 580 or later", driver[0])
        self.assertIn("Update the driver", driver[0])
        for problem in old + driver:
            self.assertNotIn("CPU build", problem)

    def test_the_base_models_need_the_reference_clip_kokoro_makes_and_the_others_do_not(self):
        with tempfile.TemporaryDirectory() as folder:
            for engine in ("qwen3-tts-0.6b-base", "qwen3-tts-1.7b-base"):
                problems = iq.reference_problems(folder, engine)
                self.assertEqual(len(problems), 1)
                self.assertIn("reference\\af_heart.wav", problems[0])
                self.assertIn("Install Kokoro first", problems[0])
            for engine in (DESIGN, "qwen3-tts-1.7b-custom-voice"):
                self.assertEqual(iq.reference_problems(folder, engine), [])
            (Path(folder) / "reference").mkdir()
            (Path(folder) / "reference" / "af_heart.wav").write_bytes(b"RIFF")
            self.assertEqual(iq.reference_problems(folder, SMALL), [])

    def test_checkpoint_only_needs_an_environment_that_has_faster_qwen(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIn("there is no .venv-fqwen here", iq.checkpoint_only_problems(folder, {})[0])
            python = iq.environment_python(folder)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            self.assertIn("faster-qwen3-tts is not among them", iq.checkpoint_only_problems(folder, {"torch": "2.12.0"})[0])
            self.assertEqual(iq.checkpoint_only_problems(folder, {"faster-qwen3-tts": "0.4.0"}), [])


class PlanTests(unittest.TestCase):
    def plan(self, engine=DESIGN, **options):
        manifest = MANIFESTS[engine]
        settings = {"todo": 66, "pending": manifest["files"], "environment_exists": False, "checkpoint_only": False, **options}
        todo, pending = settings.pop("todo"), settings.pop("pending")
        return " ".join(iq.plan_text(engine, manifest, base.read_lock(ROOT / iq.LOCK), todo, pending, **settings).split())

    def test_the_plan_names_the_model_what_it_does_its_files_and_where_they_go(self):
        text = self.plan(SMALL)
        self.assertIn("Fel's Voice Lab: Qwen3-TTS 0.6B Base on an NVIDIA graphics card", text)
        self.assertIn("clones a voice from a short recording, and is the smallest and fastest of the four", text)
        self.assertIn("First a Python environment of its own, .venv-fqwen", text)
        self.assertIn("66 of 66 to install there", text)
        self.assertIn("CUDA 13.0 builds of PyTorch 2.12 and torchaudio 2.11", text)
        self.assertIn("About 2.1 GB to download and 5.3 GB on disk", text)
        self.assertIn("the 1.8 GB model, the 0.7 GB speech tokenizer", text)
        self.assertIn("2.5 GB to download", text)
        self.assertIn("catalog/qwen3_tts_0.6b_base_snapshot.json", text)
        self.assertIn("in a voice cloned from reference\\af_heart.wav", text)
        self.assertIn("(.venv-fqwen, hf-cache and out\\qwen)", text)

    def test_the_plan_says_which_cards_and_driver_the_build_needs_where_it_was_checked_and_the_licences(self):
        text = self.plan()
        self.assertIn("GeForce GTX 16 or RTX 20 series card or newer, RTX 50 included", text)
        self.assertIn("driver of version 580 or later", text)
        self.assertIn("They were checked on a GeForce RTX 5070 Laptop GPU (8 GB)", text)
        self.assertIn("Qwen3-TTS weights and qwen-tts-hf are Apache-2.0", text)
        self.assertIn("faster-qwen3-tts is MIT", text)
        self.assertIn("in a voice made from a short description", text)

    def test_on_an_amd_card_the_plan_names_amds_build_the_card_and_which_card_the_lab_uses(self):
        packages = iq.pinned_packages(ROOT, R9700)
        manifest = MANIFESTS[DESIGN]
        text = " ".join(
            iq.plan_text(
                DESIGN,
                manifest,
                packages,
                len(packages),
                manifest["files"],
                environment_exists=False,
                checkpoint_only=False,
                card=R9700,
                cards=[RADEON_780M, R9700],
            ).split()
        )
        self.assertIn("Fel's Voice Lab: Qwen3-TTS 1.7B VoiceDesign on an AMD graphics card", text)
        self.assertIn(f"{len(packages)} of {len(packages)} to install there", text)
        self.assertIn(
            "AMD's ROCm 7.14 builds of PyTorch 2.12 and torchaudio 2.11 with the code for the AMD Radeon AI PRO "
            "R9700 (gfx1201)",
            text,
        )
        self.assertIn("About 1.6 GB to download and 5.1 GB on disk", text)
        self.assertIn("requirements/qwen-nvidia.txt and requirements/amd-rocm.txt", text)
        self.assertIn(
            "A speech check on the AMD Radeon AI PRO R9700: loads the model without using the network, records "
            "its HIP graphs (AMD's CUDA graphs)",
            text,
        )
        self.assertIn("This computer has 2 AMD graphics cards", text)
        self.assertIn("under AMD's own licence terms", text)
        self.assertNotIn("CUDA 13.0", text)
        self.assertNotIn("RTX 5070 Laptop GPU", text)
        text.encode("ascii")

    def test_the_plan_reflects_what_is_already_done(self):
        text = self.plan(todo=0, pending=[], environment_exists=True)
        self.assertIn("all 66 are already installed in .venv-fqwen", text)
        self.assertIn("Everything is already in place", text)
        text = self.plan(todo=3, environment_exists=True)
        self.assertIn(".venv-fqwen is already here. Python packages: 3 of 66", text)
        text = self.plan(checkpoint_only=True)
        self.assertIn("the ones in .venv-fqwen are left alone (--checkpoint-only)", text)
        self.assertIn("on your own graphics card build", text)
        self.assertNotIn("RTX 5070 Laptop GPU", text)


class SpeechCheckTests(unittest.TestCase):
    GOOD = (
        '{"problem": null, "load_seconds": 41.2, "total_seconds": 3.1, "audio_seconds": 2.5, "torch": "2.12.0+cu130", '
        '"device": "cuda", "device_name": "NVIDIA GeForce RTX 5070 Laptop GPU", "peak_gpu_gb": 5.2}'
    )

    def check(self, stdout="", stderr="", code=0, error=None, engine=DESIGN, number=None):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if error:
                raise error
            return subprocess.CompletedProcess(command, code, stdout, stderr)

        with tempfile.TemporaryDirectory() as folder:
            return iq.run_speech_check(folder, engine, number=number, runner=runner), calls, Path(folder)

    def test_the_check_runs_offline_in_the_shared_environment_and_its_numbers_come_back(self):
        result, calls, folder = self.check(stdout="Warning: flash-attn is not installed.\n" + self.GOOD + "\n")
        command, kwargs = calls[0]
        self.assertEqual(command[:2], [str(folder / ".venv-fqwen" / "Scripts" / "python.exe"), "-c"])
        self.assertIn("from qwen_worker import QwenWorker", command[2])
        self.assertIn('QwenWorker(engine, "cuda")', command[2])
        self.assertEqual(command[3:], [str(folder), DESIGN, str(folder / "out" / "qwen" / "install-1.7b-voice-design.wav")])
        self.assertEqual((kwargs["env"]["HF_HUB_OFFLINE"], kwargs["env"]["TRANSFORMERS_OFFLINE"]), ("1", "1"))
        self.assertEqual((result["audio_seconds"], result["peak_gpu_gb"]), (2.5, 5.2))
        self.assertEqual(result["wav"], folder / "out" / "qwen" / "install-1.7b-voice-design.wav")

    def test_on_an_amd_card_the_check_runs_on_the_card_the_lab_uses_and_a_failure_names_amds_driver(self):
        good = self.GOOD.replace("2.12.0+cu130", "2.12.0+rocm7.14.0")
        _result, calls, _folder = self.check(stdout=good + "\n", number=1)
        self.assertEqual(calls[0][1]["env"]["HIP_VISIBLE_DEVICES"], "1")
        with mock.patch.dict(os.environ):
            os.environ.pop(amd.VARIABLE, None)
            _result, calls, _folder = self.check(stdout=good + "\n")
            self.assertNotIn(amd.VARIABLE, calls[0][1]["env"])
        with self.assertRaisesRegex(iq.InstallError, r"Update the graphics driver \(AMD Software: Adrenalin Edition") as caught:
            self.check(stderr="RuntimeError: HIP error: invalid device function", code=1, number=1)
        self.assertNotIn("nvidia.com", str(caught.exception))
        with self.assertRaisesRegex(iq.InstallError, "ran out of memory"):
            self.check(stderr="torch.OutOfMemoryError: HIP out of memory.", code=1, number=0)

    def test_a_crash_shows_the_end_of_the_output_and_names_the_model(self):
        with self.assertRaisesRegex(
            iq.InstallError, r"in place, but Qwen3-TTS 0.6B Base did not load or speak:\n(.*\n)*last line"
        ):
            self.check(stderr="\n".join(f"line {n}" for n in range(30)) + "\nlast line", code=1, engine=SMALL)

    def test_a_card_out_of_memory_or_unusable_says_what_to_do_and_never_offers_the_cpu(self):
        with self.assertRaisesRegex(iq.InstallError, "ran out of memory. Close other programs") as caught:
            self.check(stderr="torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", code=1)
        self.assertNotIn("without --nvidia", str(caught.exception))
        with self.assertRaisesRegex(iq.InstallError, "has no code for this graphics card: CUDA 13.0 needs") as caught:
            self.check(stderr="RuntimeError: CUDA error: no kernel image is available", code=1)
        self.assertNotIn("without --gpu or --nvidia", str(caught.exception))
        with self.assertRaisesRegex(iq.InstallError, "driver is too old for this build. CUDA 13.0 needs") as caught:
            self.check(stderr="RuntimeError: The NVIDIA driver on your system is too old (found version 12080).", code=1)
        self.assertNotIn("without --gpu or --nvidia", str(caught.exception))
        # What the laptop printed with its NVIDIA card switched off: the hint is about the card, not the driver.
        with self.assertRaisesRegex(iq.InstallError, "(?s)PyTorch found no NVIDIA card it could use.*Code 45") as caught:
            self.check(stderr="ValueError: CUDA graphs require CUDA device", code=1)
        self.assertNotIn("update the driver", str(caught.exception))
        with self.assertRaisesRegex(iq.InstallError, r"(?s)WinError 126.*vc_redist\.x64\.exe"):
            self.check(stderr="OSError: [WinError 126] The specified module could not be found. c10.dll", code=1)

    def test_a_clip_that_is_not_speech_or_no_result_line_is_a_failure_and_a_hang_is_stopped(self):
        with self.assertRaisesRegex(iq.InstallError, "the check clip was not speech: generated silence"):
            self.check(stdout=self.GOOD.replace("null", '"generated silence"'))
        with self.assertRaisesRegex(iq.InstallError, "did not load or speak"):
            self.check(stdout="no json here\n")
        with self.assertRaisesRegex(iq.InstallError, "more than 30 minutes"):
            self.check(error=subprocess.TimeoutExpired("python", iq.CHECK_TIMEOUT))

    def test_the_check_script_loads_the_labs_worker_on_the_card_and_speaks_the_models_page_request(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = Path(folder) / "lab"
            lab.mkdir()
            (lab / "torch.py").write_text(
                "__version__ = '2.12.0+cu130'\n"
                "class cuda:\n"
                "    @staticmethod\n"
                "    def is_available():\n        return True\n"
                "    @staticmethod\n"
                "    def max_memory_reserved():\n        return 5 * 2**30\n",
                "utf-8",
            )
            (lab / "qwen_worker.py").write_text(
                "import numpy as np\n"
                "class QwenWorker:\n"
                "    def __init__(self, engine, device):\n"
                "        assert (engine, device) == ('qwen3-tts-1.7b-custom-voice', 'cuda')\n"
                "    def load(self):\n"
                "        return {'device_name': 'NVIDIA GeForce RTX 5070', 'supported_speakers': ['Serena']}\n"
                "    texts = []\n"
                "    def generate(self, params):\n"
                "        QwenWorker.texts.append(params['text'])\n"
                "        assert params == {'text': params['text'], 'speaker': 'Serena'}, params\n"
                "        assert QwenWorker.texts == ['Hello there.', 'check'][:len(QwenWorker.texts)], QwenWorker.texts\n"
                "        t = np.arange(24000 * 2) / 24000\n"
                "        return (0.1 * np.sin(2 * np.pi * 220 * t)).astype('float32'), 24000, {'total_s': 0.8}\n",
                "utf-8",
            )
            (lab / "engine_checks.py").write_text(
                "def smoke_request(engine, loaded, root=None):\n"
                "    assert root is not None\n"
                "    return {'engine': engine, 'text': 'check', 'speaker': loaded['supported_speakers'][0]}\n"
                "def audible(samples, rate):\n    return None\n",
                "utf-8",
            )
            wav = Path(folder) / "clip.wav"
            done = subprocess.run(
                [sys.executable, "-c", iq.SPEECH_CHECK, str(lab), "qwen3-tts-1.7b-custom-voice", str(wav)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            result = base.last_json_line(done.stdout)
            self.assertEqual(
                (result["problem"], result["torch"], result["total_seconds"], result["device"]),
                (None, "2.12.0+cu130", 0.8, "cuda"),
            )
            self.assertEqual((result["device_name"], result["peak_gpu_gb"]), ("NVIDIA GeForce RTX 5070", 5.0))
            self.assertAlmostEqual(result["audio_seconds"], 2.0)
            self.assertEqual(sf.info(str(wav)).frames, 2 * 24000)

    def test_the_names_the_check_script_uses_exist_in_the_lab(self):
        self.assertTrue(callable(qwen_worker.QwenWorker.load) and callable(qwen_worker.QwenWorker.generate))
        for engine in iq.MODELS:
            loaded = {"supported_speakers": ["Serena"]}
            request = engine_checks.smoke_request(engine, loaded, root=ROOT)
            self.assertEqual(request["engine"], engine)
        compile(iq.SPEECH_CHECK, "<speech check>", "exec")

    def test_the_worker_refuses_the_cpu_with_the_reason_the_card_gives(self):
        with self.assertRaisesRegex(
            RuntimeError, "runs only on a graphics card in the Lab, because its fast decoder records CUDA"
        ):
            qwen_worker.QwenWorker(SMALL, "cpu").load()


class RunTests(unittest.TestCase):
    """The whole install with venv, pip, the model host, the graphics card checks and Qwen3-TTS replaced by stand-ins. The
    computer has an NVIDIA driver unless a test gives it AMD cards (self.graphics)."""

    @classmethod
    def setUpClass(cls):
        cls.server = FileServer()
        cls.addClassCleanup(cls.server.close)

    def setUp(self):
        self.server.reset()
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-qwen-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.lab = FakeLab(self.base)
        self.server.files = self.lab.served()
        self.installed = None  # None: no .venv-fqwen yet
        self.calls = []
        self.nvidia = []
        self.graphics = ("nvidia", [])
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(amd.VARIABLE, None)
        folder = self.base / "out" / "qwen"
        self.check = {
            "problem": None,
            "load_seconds": 41.2,
            "total_seconds": 3.1,
            "audio_seconds": 2.5,
            "torch": "2.12.0+cu130",
            "device": "cuda",
            "device_name": "NVIDIA GeForce RTX 5070 Laptop GPU",
            "peak_gpu_gb": 5.2,
            "wav": folder / "install-1.7b-voice-design.wav",
        }

        def make(root, **kwargs):
            self.calls.append("venv")
            python = iq.environment_python(root)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            self.installed = {"pip": "25.0.1"}

        def pip(root, *, card=None, **kwargs):
            self.calls.append(("pip", card["chip"]) if card else "pip")
            self.installed.update({item["key"]: item["version"] for item in iq.pinned_packages(root, card)})

        def speak(root, engine, *, number=None, **kwargs):
            self.calls.append(("check", engine) if number is None else ("check", engine, number))
            folder.mkdir(parents=True, exist_ok=True)
            return self.check

        def torch_cuda(root, environment, **kwargs):
            self.calls.append("cuda")
            return {
                "torch": "2.12.0+cu130",
                "arches": ["sm_75", "sm_120"],
                "cards": [{"name": RTX_5070, "capability": [12, 0]}],
            }

        for owner, name, value in (
            (iq, "ROOT", self.base),
            (base, "HOST", self.server.address),
            (base, "check_torch_cuda", torch_cuda),
            (iq, "make_environment", make),
            (iq, "environment_versions", lambda root: dict(self.installed or {})),
            (iq, "install_packages", pip),
            (iq, "run_speech_check", speak),
            (iq, "graphics", lambda: (self.graphics[0], list(self.graphics[1]))),
            (amd, "hip_devices", lambda root, environment: [dict(device) for device in DEVICES]),
            (iq, "nvidia_problems", lambda: list(self.nvidia)),
            (iq, "interpreter_problems", lambda root: []),
            (iq, "long_path_problems", lambda root, manifest: []),
            (base, "space_problems", lambda root, need: []),
        ):
            patch = mock.patch.object(owner, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def own_environment(self, versions):
        python = iq.environment_python(self.base)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"")
        self.installed = dict(versions)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO()):
            status = iq.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def untouched(self):
        self.assertEqual(self.calls, [])
        self.assertFalse((self.base / "hf-cache").exists())
        self.assertFalse((self.base / "out").exists())

    def test_a_model_must_be_named(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            iq.main([])
        self.assertIn("--model", err.getvalue())
        self.untouched()

    def test_a_dry_run_shows_the_plan_and_changes_nothing(self):
        status, out, err = self.run_main("--model", "1.7b-voice-design", "--dry-run")
        self.assertEqual((status, err), (0, ""))
        self.assertIn("Qwen3-TTS 1.7B VoiceDesign on an NVIDIA graphics card", out)
        self.assertIn("This was a dry run, so nothing was changed.", out)
        self.untouched()
        self.assertEqual(self.server.requests, [])

    def test_the_models_page_passing_nvidia_changes_nothing(self):
        status, out, _err = self.run_main("--model", DESIGN, "--nvidia", "--dry-run")
        self.assertEqual(status, 0)
        self.assertIn("on an NVIDIA graphics card", out)

    def test_without_a_terminal_and_without_yes_nothing_happens(self):
        status, _out, err = self.run_main("--model", "1.7b-voice-design")
        self.assertEqual(status, 1)
        self.assertIn("not an interactive terminal", err)
        self.untouched()

    def test_the_whole_install_runs_in_order_and_ends_with_a_receipt(self):
        status, out, err = self.run_main("--model", "1.7b-voice-design", "--yes")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["venv", "pip", "cuda", ("check", DESIGN)])
        self.assertIn("Making .venv-fqwen:", out)
        self.assertIn(f"[1/{len(self.lab.contents)}] README.md", out)
        self.assertIn(
            "Qwen3-TTS 1.7B VoiceDesign is installed. It loaded and warmed up in 41 s on the NVIDIA GeForce RTX 5070 "
            "Laptop GPU and "
            "spoke 2.5 s in a voice made from a short description in 3.1 s, using 5.2 GB of graphics memory at most.",
            " ".join(out.split()),
        )
        self.assertIn("Listen to it: out\\qwen\\install-1.7b-voice-design.wav", out)
        receipt = json.loads((self.base / "out" / "qwen" / "installation-1.7b-voice-design.json").read_text("utf-8"))
        self.assertEqual(
            (receipt["engine"], receipt["torch"], receipt["device_name"]),
            (DESIGN, "2.12.0+cu130", "NVIDIA GeForce RTX 5070 Laptop GPU"),
        )
        self.assertEqual(receipt["speech_check"]["peak_gpu_gb"], 5.2)
        self.assertEqual(receipt["revision"], MANIFESTS[DESIGN]["revision"])
        self.assertEqual(
            (self.lab.snapshot().parent.parent / "refs" / "main").read_text("utf-8"), self.lab.manifest["revision"]
        )
        tokenizer = self.lab.snapshot() / "speech_tokenizer" / "model.safetensors"
        self.assertEqual(tokenizer.read_bytes(), self.lab.contents["speech_tokenizer/model.safetensors"])

    def test_the_lab_counts_the_model_as_installed_afterwards(self):
        self.run_main("--model", DESIGN, "--yes")
        (self.base / ".venv-fqwen" / "Lib" / "site-packages" / "faster_qwen3_tts-0.4.0.dist-info").mkdir(parents=True)
        report = engine_checks.installation(CATALOG[DESIGN], self.base)
        self.assertEqual((report["installed"], report["missing"]), (True, []))
        (self.lab.snapshot() / "speech_tokenizer" / "model.safetensors").unlink()
        report = engine_checks.installation(CATALOG[DESIGN], self.base)
        self.assertEqual(report["missing"], [f"{report['checkpoint']}/speech_tokenizer/model.safetensors"])
        self.assertEqual(report["installer"], "setup-qwen.bat --model 1.7b-voice-design")

    def test_a_computer_without_a_usable_nvidia_card_is_told_why_before_any_change(self):
        self.nvidia = [iq.NO_NVIDIA_DRIVER]
        status, out, err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual(status, 1)
        self.assertIn("This computer cannot take the plan yet", err)
        self.assertIn("only on a graphics card, an NVIDIA card or an AMD Radeon", " ".join(err.split()))
        self.assertIn("Qwen3-TTS 1.7B VoiceDesign", out)
        self.untouched()

    def test_a_computer_with_neither_card_is_told_so(self):
        self.graphics = (None, [])
        status, _out, err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual(status, 1)
        self.assertIn("neither an NVIDIA driver", " ".join(err.split()))
        self.untouched()

    def test_on_an_amd_card_amds_build_goes_in_and_the_card_is_noted_for_the_lab(self):
        self.graphics = ("amd", [RADEON_780M, R9700])
        self.check = {**self.check, "torch": "2.12.0+rocm7.14.0", "device_name": "AMD Radeon AI PRO R9700"}
        status, out, err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["venv", ("pip", "gfx1201"), ("check", DESIGN, 1)])
        text = " ".join(out.split())
        self.assertIn("Qwen3-TTS 1.7B VoiceDesign on an AMD graphics card", text)
        self.assertIn("with the code for the AMD Radeon AI PRO R9700 (gfx1201)", text)
        self.assertIn(
            "This computer has 2 AMD graphics cards, and the Lab uses the AMD Radeon AI PRO R9700: runtime.local.json "
            'now says "hip_visible_devices": "1".',
            text,
        )
        self.assertIn(
            "Qwen3-TTS 1.7B VoiceDesign is installed. It loaded and warmed up in 41 s on the AMD Radeon AI PRO R9700", text
        )
        self.assertEqual(json.loads((self.base / "runtime.local.json").read_text("utf-8")), {"hip_visible_devices": "1"})
        receipt = json.loads((self.base / "out" / "qwen" / "installation-1.7b-voice-design.json").read_text("utf-8"))
        self.assertEqual((receipt["build"], receipt["torch"], receipt["packages_pinned"]), ("rocm", "2.12.0+rocm7.14.0", 9))
        self.assertEqual(
            receipt["gpu"],
            {"name": "AMD Radeon AI PRO R9700", "chip": "gfx1201", "driver": "32.0.31041.1004", "hip_visible_devices": 1},
        )
        # A second run finds everything in place and the card already named.
        self.calls.clear()
        status, out, _err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual((status, self.calls), (0, [("check", DESIGN, 1)]))
        self.assertNotIn("now says", out)

    def test_an_amd_card_amds_build_has_no_code_for_is_refused_with_no_plan_and_no_change(self):
        self.graphics = ("amd", [RADEON_VII])
        status, out, err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual((status, out), (1, ""))
        text = " ".join(err.split())
        self.assertIn(
            "This computer cannot take Qwen3-TTS 1.7B VoiceDesign yet: - AMD's ROCm 7.14 build of PyTorch 2.12 has "
            "no code for the AMD Radeon VII (gfx906)",
            text,
        )
        self.untouched()

    def test_an_amd_card_named_by_a_setting_that_pytorch_numbers_otherwise_stops_before_the_download(self):
        self.graphics = ("amd", [RADEON_780M, R9700])
        os.environ[amd.VARIABLE] = "1"
        with mock.patch.object(
            amd, "hip_devices", lambda root, environment: [{**DEVICES[0], "index": 1}, {**DEVICES[1], "index": 0}]
        ):
            status, _out, err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual(status, 1)
        self.assertIn(
            "HIP_VISIBLE_DEVICES names card 1, the AMD Radeon 780M Graphics in PyTorch's numbering", " ".join(err.split())
        )
        self.assertEqual(self.calls, ["venv", ("pip", "gfx1201")])
        self.assertFalse((self.base / "hf-cache").exists())

    def test_a_base_model_without_the_reference_clip_is_refused_before_any_change(self):
        (self.base / "small").mkdir()
        lab = FakeLab(self.base / "small", SMALL)
        with mock.patch.object(iq, "ROOT", lab.root):
            status, _out, err = self.run_main("--model", "0.6b-base", "--yes")
        self.assertEqual(status, 1)
        self.assertIn("Install Kokoro first", " ".join(err.split()))
        self.assertEqual(self.calls, [])

    def test_an_environment_that_has_every_package_is_left_alone(self):
        self.own_environment({"pip": "25.0.1", "faster-qwen3-tts": "0.4.0", "torch": "2.12.0+cu130"})
        status, out, _err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["cuda", ("check", DESIGN)])  # PyTorch is asked to start CUDA before the files every time
        self.assertIn("all 2 are already installed in .venv-fqwen", " ".join(out.split()))

    def test_a_different_version_of_a_pinned_package_stops_everything_before_any_change(self):
        self.own_environment({"pip": "25.0.1", "faster-qwen3-tts": "0.4.0", "torch": "2.12.0+rocm7.14.0"})
        status, _out, err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual(status, 1)
        self.assertIn("torch 2.12.0+rocm7.14.0 (the list pins 2.12.0+cu130)", " ".join(err.split()))
        self.assertIn("--checkpoint-only", err)
        self.assertEqual(self.calls, [])

    def test_the_model_files_alone_go_into_an_environment_with_its_own_gpu_build_without_asking_for_nvidia(self):
        self.nvidia = [iq.NO_NVIDIA_DRIVER]  # an AMD card, say: --checkpoint-only does not need NVIDIA
        self.own_environment({"pip": "25.0.1", "faster-qwen3-tts": "0.4.0", "torch": "2.12.0+rocm7.14.0"})
        status, out, err = self.run_main("--model", DESIGN, "--checkpoint-only", "--yes")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, [("check", DESIGN)])  # its own build: no NVIDIA check before the files
        self.assertIn("left alone (--checkpoint-only)", " ".join(out.split()))

    def test_checkpoint_only_without_an_environment_is_refused_before_any_change(self):
        status, _out, err = self.run_main("--model", DESIGN, "--checkpoint-only", "--yes")
        self.assertEqual(status, 1)
        self.assertIn("there is no .venv-fqwen here", " ".join(err.split()))
        self.untouched()

    def test_a_failed_speech_check_leaves_the_files_and_writes_no_receipt(self):
        def fail(root, engine, **kwargs):
            raise iq.InstallError("The packages and model files are in place, but Qwen3-TTS 1.7B VoiceDesign did not load")

        with mock.patch.object(iq, "run_speech_check", fail):
            status, _out, err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual(status, 1)
        self.assertIn("did not load", err)
        self.assertTrue((self.lab.snapshot() / "model.safetensors").is_file())
        self.assertFalse((self.base / "out" / "qwen" / "installation-1.7b-voice-design.json").exists())

    def test_pip_that_finishes_without_installing_everything_is_caught(self):
        with mock.patch.object(iq, "install_packages", lambda root, **kwargs: self.calls.append("pip")):
            status, _out, err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual(status, 1)
        self.assertIn("pip finished but 2 packages are still missing, for example faster-qwen3-tts", err)

    def test_a_second_run_downloads_nothing_new(self):
        self.run_main("--model", DESIGN, "--yes")
        requests = len(self.server.requests)
        status, out, _err = self.run_main("--model", DESIGN, "--yes")
        self.assertEqual(status, 0)
        self.assertEqual(len(self.server.requests), requests)
        self.assertIn("already in place", out)

    def test_the_space_needed_counts_the_packages_and_the_files_still_to_fetch(self):
        asked = []
        with mock.patch.object(base, "space_problems", lambda root, need: asked.append(need) or []):
            self.run_main("--model", DESIGN, "--dry-run")
        files = sum(len(data) for data in self.lab.contents.values())
        self.assertEqual(asked, [(iq.PACKAGES_DOWNLOAD_MB + iq.PACKAGES_DISK_MB + iq.SPACE_MARGIN_MB) * 10**6 + files])


if __name__ == "__main__":
    unittest.main()
