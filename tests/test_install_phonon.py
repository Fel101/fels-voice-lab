"""tools/install_phonon.py: the pinned Phonon CPU install. Nothing here reaches the real network, pip or the real Phonon."""
import argparse
import contextlib
import hashlib
import http.server
import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine_checks  # noqa: E402
import phonon_support  # noqa: E402
import soundfile as sf  # noqa: E402
from tools import install_kokoro as base  # noqa: E402
from tools import install_phonon as ip  # noqa: E402

REAL_MANIFEST = json.loads((ROOT / ip.MANIFEST).read_text("utf-8"))
ENTRY = next(entry for entry in json.loads((ROOT / "catalog" / "engines.json").read_text("utf-8"))["engines"]
             if entry["id"] == phonon_support.PHONON_ENGINE_ID)
HASH = "ab" * 32


def sha(data):
    return hashlib.sha256(data).hexdigest()


def noise(length, seed=0):
    return hashlib.shake_256(seed.to_bytes(4, "little")).digest(length)


class FileServer:
    """A local web server for the downloads: serves bytes by path."""

    def __init__(self):
        self.files, self.requests = {}, []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urllib.parse.unquote(self.path.split("?")[0])
                server.requests.append(path)
                body = server.files.get(path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.files, self.requests = {}, []

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FileServer()
        cls.addClassCleanup(cls.server.close)

    def setUp(self):
        self.server.reset()
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-phonon-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)


class FakeLab:
    """A lab folder holding a two-package lock and small stand-ins for the two repositories, at the real revisions."""
    CONTENTS = [
        {"config.json": b'{"lang": "en"}', "default-voice.safetensors": noise(3_000, 1), "model.q8.gguf": noise(40_000, 2),
         "tokenizer.json": b'{"model": {}}'},
        {"voices.jsonl": b'{"name": "Harper"}\n', "voices/aaa.safetensors": noise(5_000, 3), "voices/bbb.safetensors": noise(5_001, 4)},
    ]
    LOCK = f"# a stand-in for requirements/phonon.txt\nnumpy==1.26.4 --hash=sha256:{HASH}\nptts==0.5.0 --hash=sha256:{HASH}\n"

    def __init__(self, folder):
        self.root = Path(folder)
        (self.root / "catalog").mkdir()
        (self.root / "requirements").mkdir()
        repos = []
        for real, contents in zip(REAL_MANIFEST["repos"], self.CONTENTS):
            repos.append({**{key: real[key] for key in ("repo", "revision", "license", "license_source")},
                          "files": [{"path": path, "size": len(data), "sha256": sha(data)} for path, data in contents.items()]})
        self.manifest = {"schema": 1, "repos": repos}
        (self.root / ip.MANIFEST).write_text(json.dumps(self.manifest), "utf-8")
        (self.root / ip.LOCK).write_text(self.LOCK, "utf-8")

    def served(self):
        served = {}
        for repo, contents in zip(self.manifest["repos"], self.CONTENTS):
            prefix = f"/{repo['repo']}/resolve/{repo['revision']}/"
            served.update({prefix + path: data for path, data in contents.items()})
        return served

    def snapshot(self, index):
        return base.snapshot_dir(self.root, self.manifest["repos"][index])


class ShippedFilesTests(unittest.TestCase):
    def test_the_file_list_pins_the_revisions_the_catalog_uses(self):
        model, voices = REAL_MANIFEST["repos"]
        self.assertEqual((model["repo"], model["revision"]), (ENTRY["checkpoint"], ENTRY["local_revision"]))
        self.assertEqual((voices["repo"], voices["revision"]),
                         (ENTRY["backend"]["voices_checkpoint"], ENTRY["backend"]["voices_revision"]))
        self.assertEqual((model["repo"], voices["repo"]), ("gradium/phonon-31m-en", "gradium/phonon-voices"))

    def test_the_file_list_holds_what_the_adapter_loads(self):
        model, voices = REAL_MANIFEST["repos"]
        self.assertEqual(sorted(item["path"] for item in model["files"]), sorted(phonon_support.MODEL_FILES))
        paths = [item["path"] for item in voices["files"]]
        self.assertIn(phonon_support.VOICE_CATALOG, paths)
        voice_files = [path for path in paths if path.startswith("voices/")]
        self.assertEqual(len(voice_files), ENTRY["capabilities"]["voice_selection"]["count"])
        self.assertEqual(len(paths), len(set(paths)))
        for repo in REAL_MANIFEST["repos"]:
            for item in repo["files"]:
                self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$", item["path"])
                self.assertIsInstance(item["size"], int)
                self.assertGreater(item["size"], 0)
                self.assertNotIn("..", Path(item["path"]).parts)
                self.assertFalse(item["path"].startswith("/"))

    def test_the_big_model_file_is_the_quantized_weights_the_adapter_asks_for(self):
        biggest = max(REAL_MANIFEST["repos"][0]["files"], key=lambda item: item["size"])
        self.assertEqual(biggest["path"], "model.q8.gguf")
        self.assertEqual(ENTRY["backend"]["quantization"], "q8")

    def test_the_licence_of_each_repository_is_recorded_as_found_and_the_gap_is_not_papered_over(self):
        model, voices = REAL_MANIFEST["repos"]
        self.assertEqual(model["license"], "Apache-2.0")
        self.assertIsNone(voices["license"])
        self.assertIn("no", voices["license_source"].lower())
        self.assertTrue(model["license_source"])

    def test_the_package_list_pins_the_runtime_the_catalog_names_and_no_pytorch(self):
        lines = [line.strip() for line in (ROOT / ip.LOCK).read_text("utf-8").splitlines()]
        lines = [line for line in lines if line and not line.startswith("#")]
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-9][A-Za-z0-9._-]*==\S+ --hash=sha256:[0-9a-f]{64}$")
        packages = base.read_lock(ROOT / ip.LOCK)
        self.assertEqual(len(lines), len(packages))
        self.assertEqual(ip.runtime_package(packages)["version"], ENTRY["backend"]["package_version"])
        self.assertNotIn("torch", {item["key"] for item in packages})

    def test_the_package_list_is_the_runtime_and_the_core_requirements_at_the_same_versions(self):
        # The worker runs the lab's own modules in .venv-phonon, so numpy and soundfile behave as they do in .venv.
        core = {base.canonical(re.split(r"==", line.strip())[0]): re.split(r"==", line.strip())[1]
                for line in (ROOT / "requirements" / "core.txt").read_text("utf-8").splitlines()
                if line.strip() and not line.startswith("#")}
        packages = {item["key"]: item["version"] for item in base.read_lock(ROOT / ip.LOCK)}
        self.assertIn("numpy", core)
        self.assertEqual(packages, {**core, "ptts": ENTRY["backend"]["package_version"]})

    def test_the_catalog_runs_phonon_in_its_own_environment(self):
        self.assertEqual(ENTRY["runtime"]["environment"], ip.ENVIRONMENT)
        self.assertEqual(ENTRY["runtime"]["python"], f"{ip.ENVIRONMENT}/Scripts/python.exe")
        self.assertEqual(ip.environment_python(ROOT), ROOT / ENTRY["runtime"]["python"])

    def test_the_batch_file_runs_the_installer_with_the_labs_own_python(self):
        data = (ROOT / "setup-phonon.bat").read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "batch files keep CRLF line ends")
        text = data.decode("utf-8")
        self.assertIn('".venv\\Scripts\\python.exe" tools\\install_phonon.py %*', text)
        self.assertIn("Run setup.bat first.", text)

    def test_the_setup_window_waits_for_a_key_so_a_double_click_can_be_read(self):
        lines = [line.strip() for line in (ROOT / "setup-phonon.bat").read_text("utf-8").splitlines() if line.strip()]
        last_run = max(index for index, line in enumerate(lines) if "tools\\" in line)
        self.assertIn("pause", lines[last_run + 1:])

    def test_the_first_setup_offers_the_installer(self):
        from tools import first_setup
        self.assertEqual(first_setup.MODELS["phonon"],
                         {"engine": "phonon-31m-en", "script": "tools/install_phonon.py", "gpu": False})
        self.assertIn("setup-phonon.bat", first_setup.menu_text(16, first_setup.model_sizes()))

    def test_the_guides_describe_the_installer_with_the_numbers_it_ships(self):
        setup = " ".join((ROOT / "docs" / "SETUP.md").read_text("utf-8").split())
        for needed in ("setup-phonon.bat", "tools\\install_phonon.py", "--dry-run", "--yes", "--checkpoint-only",
                       "requirements/phonon.txt", "catalog/phonon_snapshot.json", "Visual C++ Redistributable"):
            self.assertIn(needed, setup)
        model, voices = REAL_MANIFEST["repos"]
        weights = max(model["files"], key=lambda item: item["size"])
        everything = sum(item["size"] for repo in REAL_MANIFEST["repos"] for item in repo["files"])
        for number in (f"{ENTRY['capabilities']['voice_selection']['count']} English preset voices",
                       base.size_text(weights["size"]), base.size_text(sum(item["size"] for item in voices["files"])),
                       f"{ip.PACKAGES_DOWNLOAD_MB} MB", f"{ip.PACKAGES_DISK_MB} MB", ".venv-phonon"):
            self.assertIn(number, setup)
        self.assertLess(everything, 100 * 10**6, "the whole download stays under a hundred megabytes")
        for name in ("README.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md"):
            text = " ".join((ROOT / name).read_text("utf-8").split())
            self.assertTrue("setup-phonon.bat" in text or "tools/install_phonon.py" in text, name)

    def test_the_installer_prints_only_plain_ascii(self):
        self.assertTrue((ROOT / "tools" / "install_phonon.py").read_bytes().isascii())
        packages = base.read_lock(ROOT / ip.LOCK)
        pending = [(repo, item) for repo in REAL_MANIFEST["repos"] for item in repo["files"]]
        for todo, left, only, exists in ((len(packages), pending, False, False), (1, pending, False, True),
                                         (0, pending, False, True), (0, [], False, True), (0, pending, True, True)):
            text = ip.plan_text(REAL_MANIFEST, packages, todo, left, checkpoint_only=only, environment_exists=exists)
            self.assertTrue(text.isascii())
            self.assertLessEqual(max(len(line) for line in text.splitlines()), 110)


class EnvironmentTests(unittest.TestCase):
    def problems(self, **options):
        defaults = {"system": "win32", "machine": "AMD64", "version": (3, 12, 10), "prefix": str(ROOT / ".venv"), "bits": 64}
        return ip.interpreter_problems(ROOT, **{**defaults, **options})

    def test_only_a_64_bit_windows_python_312_in_the_labs_own_environment_passes(self):
        self.assertEqual(self.problems(), [])
        self.assertRegex(self.problems(system="linux")[0], "64-bit Windows")
        self.assertRegex(self.problems(machine="arm64")[0], "64-bit Windows")
        self.assertRegex(self.problems(bits=32)[0], "64-bit Windows")
        self.assertRegex(self.problems(version=(3, 11, 9))[0], "Python 3.11; the pinned packages are built for Python 3.12")
        self.assertRegex(self.problems(prefix=str(ROOT / "other"))[0], r"own Python.*install_phonon\.py")

    def test_the_environment_needs_pip_and_its_runtime_is_not_replaced_at_another_version(self):
        runtime = ip.runtime_package(base.read_lock(ROOT / ip.LOCK))
        self.assertEqual(ip.package_problems([], has_pip=True), [])
        self.assertIn(".venv-phonon has no pip", ip.package_problems([], has_pip=False)[0])
        other = ip.package_problems([(runtime, "0.4.0")], has_pip=True)
        self.assertEqual(len(other), 1)
        self.assertIn(".venv-phonon already has other versions", other[0])
        self.assertIn("ptts 0.4.0 (the list pins 0.5.0)", other[0])
        self.assertIn("--checkpoint-only", other[0])

    def test_the_model_files_alone_need_an_environment_that_already_has_the_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIn("there is no .venv-phonon here", ip.checkpoint_only_problems(folder, {})[0])
            python = ip.environment_python(folder)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            self.assertIn("ptts is not among them", ip.checkpoint_only_problems(folder, {"numpy": "1.26.4"})[0])
            self.assertEqual(ip.checkpoint_only_problems(folder, {"ptts": "0.5.0"}), [])

    def test_a_folder_path_too_long_for_windows_is_refused_unless_long_paths_are_on(self):
        manifest = REAL_MANIFEST
        self.assertEqual(ip.long_path_problems(Path("C:/voice-lab"), manifest, long_paths=False), [])
        deep = Path("C:/" + "d" * 150)
        problem = ip.long_path_problems(deep, manifest, long_paths=False)
        self.assertEqual(len(problem), 1)
        self.assertRegex(problem[0], r"too long for Windows.*up to \d+ characters.*stops at 259.*C:\\voice-lab")
        self.assertEqual(ip.long_path_problems(deep, manifest, long_paths=True), [])

    def test_the_longest_file_below_the_lab_folder_leaves_room_for_an_ordinary_path(self):
        longest = max(len(base.SNAPSHOT.format(repo=repo["repo"].replace("/", "--"), revision=repo["revision"])) + 1 + len(item["path"])
                      for repo in REAL_MANIFEST["repos"] for item in repo["files"])
        self.assertLess(longest, 140, "a lab folder of about 120 characters or fewer still fits under Windows' limit")


class PlanTests(unittest.TestCase):
    PENDING = [(repo, item) for repo in REAL_MANIFEST["repos"] for item in repo["files"]]

    PACKAGES = len(base.read_lock(ROOT / ip.LOCK))

    def plan(self, todo=None, pending=True, only=False, exists=False):
        packages = base.read_lock(ROOT / ip.LOCK)
        return " ".join(ip.plan_text(REAL_MANIFEST, packages, self.PACKAGES if todo is None else todo,
                                     self.PENDING if pending else [], checkpoint_only=only, environment_exists=exists).split())

    def test_the_plan_says_what_is_installed_where_and_how_much_to_download(self):
        text = self.plan()
        for part in ("Phonon 31M English on the CPU", "First a Python environment of its own, .venv-phonon",
                     "changes no other model's packages", f"{self.PACKAGES} of {self.PACKAGES} to install there", "ptts 0.5.0",
                     "7 MB wheel", "needs only NumPy", f"About {ip.PACKAGES_DOWNLOAD_MB} MB to download and "
                     f"{ip.PACKAGES_DISK_MB} MB on disk", "there is no PyTorch", "requirements/phonon.txt",
                     "revision 8c64b55", "huggingface.co/gradium/phonon-31m-en", "the 65 MB model", "37 English preset voices",
                     "revision da6c9ec", "huggingface.co/gradium/phonon-voices (10 MB)", "75 MB to download", "SHA-256",
                     "(.venv-phonon, hf-cache and out\\phonon)", "nothing system-wide",
                     "pip keeps its usual download cache in your user profile", "speech check"):
            self.assertIn(part, text)

    def test_the_plan_says_which_licence_covers_what_and_that_the_voices_state_none(self):
        text = self.plan()
        for part in ("weights, tokenizer and default voice are Apache-2.0", "states no licence", "no model card and no LICENSE file",
                     "Ask Gradium before you publish", "MIT OR Apache-2.0"):
            self.assertIn(part, text)

    def test_the_plan_no_longer_warns_that_other_models_need_testing_again(self):
        # Phonon has its own environment now, so installing it changes no other model's packages or checks.
        self.assertNotIn("test the other models", self.plan())

    def test_the_plan_reflects_what_is_already_done(self):
        self.assertIn(f"all {self.PACKAGES} are already installed in .venv-phonon, ptts 0.5.0 among them", self.plan(todo=0))
        self.assertIn(".venv-phonon is already here. Python packages: 1 of", self.plan(todo=1, exists=True))
        self.assertIn("Everything is already in place", self.plan(pending=False))
        self.assertIn("the ones in .venv-phonon are left alone (--checkpoint-only)", self.plan(only=True))


class InstallPackagesTests(unittest.TestCase):
    def test_pip_gets_the_locked_file_with_hashes_required_and_nothing_resolved(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        ip.install_packages(ROOT, runner=runner)
        command, kwargs = calls[0]
        self.assertEqual(command[:4], [str(ROOT / ".venv-phonon" / "Scripts" / "python.exe"), "-m", "pip", "install"])
        for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:", "--no-input", "--disable-pip-version-check"):
            self.assertIn(flag, command)
        self.assertEqual(Path(command[command.index("-r") + 1]), ROOT / "requirements" / "phonon.txt")
        self.assertEqual(kwargs["cwd"], ROOT)
        self.assertNotIn("--index-url", " ".join(command))
        self.assertNotIn("--extra-index-url", " ".join(command))

    def test_a_pip_failure_says_to_fix_it_and_run_again(self):
        with self.assertRaisesRegex(ip.InstallError, "pip stopped with exit code 3.*run the installer again"):
            ip.install_packages(ROOT, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 3))


class SpeechCheckTests(unittest.TestCase):
    GOOD = '{"voice": "Harper", "problem": null, "load_seconds": 1.2, "total_seconds": 0.4, "audio_seconds": 2.6, "ptts": "0.5.0"}'

    def check(self, stdout="", stderr="", code=0, error=None):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if error:
                raise error
            return subprocess.CompletedProcess(command, code, stdout, stderr)

        with tempfile.TemporaryDirectory() as folder:
            return ip.run_speech_check(folder, runner=runner), calls, Path(folder)

    def test_the_clip_is_made_offline_by_the_new_environment_and_its_numbers_come_back(self):
        result, calls, folder = self.check(stdout="runtime noise\n" + self.GOOD + "\n", stderr="a warning\n")
        command, kwargs = calls[0]
        self.assertEqual(command[:2], [str(folder / ".venv-phonon" / "Scripts" / "python.exe"), "-c"])
        self.assertIn('engine_checks.smoke_request("phonon-31m-en")', command[2])
        self.assertEqual(command[3:], [str(folder), str(folder / "out" / "phonon" / "install-check.wav")])
        self.assertEqual((kwargs["env"]["HF_HUB_OFFLINE"], kwargs["env"]["TRANSFORMERS_OFFLINE"]), ("1", "1"))
        self.assertEqual(result["voice"], "Harper")
        self.assertEqual(result["wav"], folder / "out" / "phonon" / "install-check.wav")

    def test_a_crash_shows_the_end_of_the_output(self):
        with self.assertRaisesRegex(ip.InstallError, r"in place, but Phonon did not load or speak:\n(.*\n)*last line"):
            self.check(stdout="first\n", stderr="\n".join(f"line {n}" for n in range(30)) + "\nlast line", code=1)

    def test_a_missing_dll_points_at_the_visual_c_runtime(self):
        with self.assertRaisesRegex(ip.InstallError, r"(?s)WinError 126.*https://aka\.ms/vs/17/release/vc_redist\.x64\.exe"):
            self.check(stderr="OSError: [WinError 126] The specified module could not be found. ptts.pyd", code=1)

    def test_a_clip_that_is_not_speech_is_reported(self):
        with self.assertRaisesRegex(ip.InstallError, "was not speech: generated silence"):
            self.check(stdout=self.GOOD.replace("null", '"generated silence"'))

    def test_no_result_line_is_a_failure_and_a_hang_is_stopped(self):
        with self.assertRaisesRegex(ip.InstallError, "did not load or speak"):
            self.check(stdout="no json here\n")
        with self.assertRaisesRegex(ip.InstallError, "more than 5 minutes"):
            self.check(error=subprocess.TimeoutExpired("python", 300))

    def test_the_check_script_writes_the_clip_and_reports_it(self):
        with tempfile.TemporaryDirectory() as folder:
            lab, wav = Path(folder) / "lab", Path(folder) / "clip.wav"
            lab.mkdir()
            (lab / "engines.py").write_text(
                "import numpy as np\n"
                "class Phonon:\n"
                "    version = '0.5.0'\n"
                "    def __init__(self, device):\n"
                "        assert device == 'cpu'\n"
                "    def speak(self, text, voice=None):\n"
                "        t = np.arange(24000) / 24000\n"
                "        return (0.1 * np.sin(2 * np.pi * 220 * t)).astype('float32'), 24000, {'total_s': 0.25, 'voice': voice}\n", "utf-8")
            (lab / "engine_checks.py").write_text(
                "def smoke_request(engine):\n    assert engine == 'phonon-31m-en'\n    return {'engine': engine, 'text': 'check', 'voice': 'Harper'}\n"
                "def audible(samples, rate):\n    return None\n", "utf-8")
            done = subprocess.run([sys.executable, "-c", ip.SPEECH_CHECK, str(lab), str(wav)], capture_output=True, text=True,
                                  timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            result = base.last_json_line(done.stdout)
            self.assertEqual((result["voice"], result["problem"], result["total_seconds"], result["ptts"]),
                             ("Harper", None, 0.25, "0.5.0"))
            self.assertAlmostEqual(result["audio_seconds"], 1.0)
            samples, rate = sf.read(str(wav))
            self.assertEqual((rate, len(samples)), (24000, 24000))

    def test_the_names_the_check_script_uses_exist_in_the_lab(self):
        import engines
        self.assertTrue(callable(engine_checks.smoke_request) and callable(engine_checks.audible))
        self.assertTrue(hasattr(engines, "Phonon"))
        compile(ip.SPEECH_CHECK, "<speech check>", "exec")


class ReceiptTests(unittest.TestCase):
    def test_the_receipt_records_what_was_installed_and_how_it_spoke(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)
            (lab.root / "out" / "phonon").mkdir(parents=True)
            packages = base.read_lock(lab.root / ip.LOCK)
            check = {"voice": "Harper", "problem": None, "load_seconds": 1.2, "total_seconds": 0.4, "audio_seconds": 2.6, "wav": "x",
                     "ptts": "0.5.0"}
            receipt = json.loads(ip.write_receipt(lab.root, lab.manifest, packages, check).read_text("utf-8"))
            self.assertEqual(receipt["lock_sha256"], sha((lab.root / ip.LOCK).read_bytes()))
        self.assertEqual((receipt["schema"], receipt["ptts"], receipt["environment"], receipt["packages_pinned"]),
                         (1, "0.5.0", ".venv-phonon", 2))
        self.assertEqual([item["revision"] for item in receipt["repos"]], [repo["revision"] for repo in REAL_MANIFEST["repos"]])
        self.assertEqual([item["license"] for item in receipt["repos"]], ["Apache-2.0", None])
        self.assertEqual(receipt["files"][0], "gradium/phonon-31m-en/config.json")
        self.assertEqual(len(receipt["files"]), sum(len(contents) for contents in FakeLab.CONTENTS))
        self.assertEqual(receipt["checkpoint_bytes"], sum(len(data) for contents in FakeLab.CONTENTS for data in contents.values()))
        self.assertEqual(receipt["speech_check"], {"voice": "Harper", "load_seconds": 1.2, "total_seconds": 0.4, "audio_seconds": 2.6})
        self.assertRegex(receipt["installed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertNotIn(folder, json.dumps(receipt), "no folder names of the person's computer in the receipt")


class RunTests(ServerCase):
    """The whole install with venv, pip, the model host and Phonon replaced by stand-ins."""

    def setUp(self):
        super().setUp()
        self.lab = FakeLab(self.base)
        self.server.files = self.lab.served()
        self.installed = {}
        self.calls = []
        wav = self.base / "out" / "phonon" / "install-check.wav"
        self.check = {"voice": "Harper", "problem": None, "load_seconds": 4.2, "total_seconds": 1.1, "audio_seconds": 2.6, "wav": wav,
                      "ptts": "0.5.0"}

        def venv(root, **kwargs):
            self.calls.append("venv")
            self.make_environment({"pip": "26.2.2"})

        def pip(root, **kwargs):
            self.calls.append("pip")
            self.installed.update({"numpy": "1.26.4", "ptts": "0.5.0"})

        def speak(root, **kwargs):
            self.calls.append("check")
            wav.parent.mkdir(parents=True, exist_ok=True)
            return self.check

        for owner, name, value in ((ip, "ROOT", self.base), (base, "HOST", self.server.address),
                                   (ip, "environment_versions", lambda root, **kwargs: dict(self.installed)),
                                   (ip, "make_environment", venv), (ip, "install_packages", pip),
                                   (ip, "run_speech_check", speak), (ip, "interpreter_problems", lambda root: []),
                                   (base, "space_problems", lambda root, need: [])):
            patch = mock.patch.object(owner, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def make_environment(self, packages):
        python = self.base / ".venv-phonon" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"")
        self.installed.update(packages)

    def args(self, **options):
        return argparse.Namespace(**{"yes": False, "dry_run": False, "checkpoint_only": False, **options})

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO()):
            status = ip.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def untouched(self):
        self.assertEqual(self.calls, [])
        self.assertFalse((self.base / "hf-cache").exists())
        self.assertFalse((self.base / "out").exists())
        self.assertFalse((self.base / ".venv-phonon").exists())

    def test_a_dry_run_shows_the_plan_and_changes_nothing(self):
        status, out, err = self.run_main("--dry-run")
        self.assertEqual((status, err), (0, ""))
        self.assertIn("Phonon 31M English on the CPU", out)
        self.assertIn("This was a dry run, so nothing was changed.", out)
        self.untouched()
        self.assertEqual(self.server.requests, [])

    def test_without_a_terminal_and_without_yes_nothing_happens(self):
        status, _out, err = self.run_main()
        self.assertEqual(status, 1)
        self.assertIn("not an interactive terminal", err)
        self.assertIn("Add --yes", err)
        self.untouched()

    def test_asked_in_a_terminal_only_yes_goes_ahead(self):
        lines = []
        for answer in ("", "no", "y please"):
            self.assertEqual(ip.run(self.args(), say=lines.append, ask=lambda prompt, answer=answer: answer, interactive=True), 1)
        self.assertIn("Nothing was changed.", lines)
        self.untouched()
        self.assertEqual(ip.run(self.args(), say=lines.append, ask=lambda prompt: " Yes ", interactive=True), 0)
        self.assertEqual(self.calls, ["venv", "pip", "check"])

    def test_the_whole_install_runs_in_order_and_ends_with_a_receipt(self):
        status, out, err = self.run_main("--yes")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["venv", "pip", "check"])
        self.assertIn("Making .venv-phonon:", out)
        self.assertIn("Installing the Python packages into .venv-phonon", out)
        self.assertIn("[1/4] config.json", out)
        self.assertIn("[1/3] voices.jsonl", out)
        self.assertIn("Phonon is installed and spoke a test sentence (2.6 s of audio in 1.1 s after a 4.2 s load).", out)
        self.assertIn("Listen to it: out\\phonon\\install-check.wav", out)
        self.assertIn("press Test.", out)
        receipt = json.loads((self.base / "out" / "phonon" / "installation.json").read_text("utf-8"))
        self.assertEqual(len(receipt["files"]), 7)
        self.assertEqual(receipt["ptts"], "0.5.0")
        for index in (0, 1):
            self.assertEqual((self.lab.snapshot(index).parent.parent / "refs" / "main").read_text("utf-8"),
                             self.lab.manifest["repos"][index]["revision"])
        self.assertEqual((self.lab.snapshot(0) / "model.q8.gguf").read_bytes(), FakeLab.CONTENTS[0]["model.q8.gguf"])
        self.assertTrue((self.lab.snapshot(1) / "voices" / "aaa.safetensors").is_file())

    def test_the_lab_counts_phonon_as_installed_afterwards(self):
        self.run_main("--yes")
        # the run made .venv-phonon's Python; pip's stand-in left no dist-info, so add the one the lab looks for
        (self.base / ".venv-phonon" / "Lib" / "site-packages" / "ptts-0.5.0.dist-info").mkdir(parents=True)
        # the stand-in files sit at the catalog's revisions, so the lab's file check agrees that Phonon is there
        report = engine_checks.installation(ENTRY, self.base)
        self.assertEqual((report["installed"], report["missing"]), (True, []))

    def test_the_package_is_left_alone_when_it_is_already_installed(self):
        self.make_environment({"numpy": "1.26.4", "ptts": "0.5.0"})
        status, out, _err = self.run_main("--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["check"])
        self.assertIn("all 2 are already installed in .venv-phonon, ptts 0.5.0 among them", " ".join(out.split()))

    def test_a_different_version_of_the_runtime_stops_everything_before_any_change(self):
        self.make_environment({"pip": "26.2.2", "ptts": "0.4.0"})
        status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("This computer cannot take the plan yet:", err)
        self.assertIn("ptts 0.4.0 (the list pins 0.5.0)", " ".join(err.split()))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.base / "hf-cache").exists())
        self.assertEqual(self.server.requests, [])

    def test_the_model_files_alone_can_be_added_to_an_environment_that_has_its_own_runtime(self):
        self.make_environment({"ptts": "0.4.0"})
        status, out, _err = self.run_main("--yes", "--checkpoint-only")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["check"])
        self.assertIn("left alone (--checkpoint-only)", out)
        self.assertTrue((self.lab.snapshot(0) / "model.q8.gguf").is_file())

    def test_a_computer_that_fails_a_check_is_told_why_and_nothing_is_changed(self):
        with mock.patch.object(ip, "interpreter_problems", lambda root: ["Run it with this folder's own Python."]), \
                mock.patch.object(base, "space_problems", lambda root, need: ["About 300 MB of free disk space is needed."]), \
                mock.patch.object(ip, "long_path_problems", lambda root, manifest: ["The path is too long."]):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("  - Run it with this folder's own Python.\n  - The path is too long.\n  - About 300 MB of free disk space is needed.", err)
        self.untouched()

    def test_a_pip_failure_stops_before_the_model_is_fetched(self):
        def failing(root, **kwargs):
            raise ip.InstallError("pip stopped with exit code 1.")

        with mock.patch.object(ip, "install_packages", failing):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("error: pip stopped with exit code 1.", err)
        self.assertFalse((self.base / "hf-cache").exists())

    def test_pip_that_finishes_without_installing_the_runtime_is_caught(self):
        with mock.patch.object(ip, "install_packages", lambda root, **kwargs: None):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("pip finished but 2 packages are still missing, for example numpy", err)

    def test_a_failed_speech_check_leaves_the_files_and_writes_no_receipt(self):
        def silent(root, **kwargs):
            raise ip.InstallError("The package and model files are in place, but Phonon did not load or speak:\nboom")

        with mock.patch.object(ip, "run_speech_check", silent):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("did not load or speak", err)
        self.assertTrue((self.lab.snapshot(0) / "model.q8.gguf").is_file())
        self.assertFalse((self.base / "out" / "phonon" / "installation.json").exists())

    def test_a_damaged_file_is_fetched_again_and_a_second_run_downloads_nothing_new(self):
        self.run_main("--yes")
        asked, self.calls[:] = len(self.server.requests), []
        status, _out, _err = self.run_main("--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["check"])
        self.assertEqual(len(self.server.requests), asked)
        damaged = self.lab.snapshot(1) / "voices" / "bbb.safetensors"
        damaged.write_bytes(b"damaged")
        self.server.requests.clear()
        self.run_main("--yes")
        self.assertEqual(damaged.read_bytes(), FakeLab.CONTENTS[1]["voices/bbb.safetensors"])
        self.assertEqual(self.server.requests, [f"/gradium/phonon-voices/resolve/{self.lab.manifest['repos'][1]['revision']}/voices/bbb.safetensors"])

    def test_a_file_the_system_will_not_let_it_write_ends_with_an_error_line_not_a_traceback(self):
        with mock.patch.object(ip, "write_receipt", side_effect=PermissionError(13, "Access is denied", "installation.json")):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("error: [Errno 13] Access is denied", err)
        self.assertNotIn("Traceback", err)

    def test_control_c_stops_politely(self):
        def interrupted(root, **kwargs):
            raise KeyboardInterrupt

        with mock.patch.object(ip, "install_packages", interrupted):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 130)
        self.assertIn("Run the installer again to continue", err)

    def test_a_file_list_entry_that_leaves_the_model_folder_is_refused_before_anything_is_fetched(self):
        repo = {**self.lab.manifest["repos"][0], "files": [{"path": "../escape.txt", "size": 4, "sha256": HASH}]}
        with self.assertRaisesRegex(ip.InstallError, "outside the model folder"):
            base.download_checkpoint(self.base, repo, repo["files"], say=lambda text: None)
        self.assertEqual(self.server.requests, [])


class ManifestTests(unittest.TestCase):
    def test_a_missing_or_incomplete_file_list_is_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.json"
            with self.assertRaisesRegex(ip.InstallError, "missing or unreadable"):
                ip.load_manifest(path)
            for broken in ('{"repos": []}', '{"repos": [{"repo": "a/b"}]}', '{"repos": "x"}', "not json", "{}"):
                path.write_text(broken, "utf-8")
                with self.assertRaisesRegex(ip.InstallError, "missing or unreadable"):
                    ip.load_manifest(path)

    def test_the_shipped_file_list_loads(self):
        self.assertEqual(ip.load_manifest(ROOT / ip.MANIFEST), REAL_MANIFEST)


if __name__ == "__main__":
    unittest.main()
