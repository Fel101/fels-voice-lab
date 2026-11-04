"""tools/install_kokoro.py: the pinned Kokoro CPU install. Nothing here reaches the real network, pip or the real Kokoro."""
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
import kokoro_support  # noqa: E402
import soundfile as sf  # noqa: E402
from tools import install_kokoro as ik  # noqa: E402

REAL_MANIFEST = json.loads((ROOT / ik.MANIFEST).read_text("utf-8"))
KOKORO_ENTRY = next(entry for entry in json.loads((ROOT / "catalog" / "engines.json").read_text("utf-8"))["engines"]
                    if entry["id"] == "kokoro")
HASH = "ab" * 32


def sha(data):
    return hashlib.sha256(data).hexdigest()


def noise(length, seed=0):
    """Bytes that do not compress or repeat, so a half-sent file is never mistaken for a whole one."""
    return hashlib.shake_256(seed.to_bytes(4, "little")).digest(length)


class FileServer:
    """A local web server for the downloads: serves bytes by path, honours Range, and fails on request."""

    def __init__(self):
        self.files, self.faults, self.requests = {}, {}, []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urllib.parse.unquote(self.path.split("?")[0])
                wanted = self.headers.get("Range")
                server.requests.append((path, wanted))
                queue = server.faults.get(path) or []
                fault = queue.pop(0) if queue else None
                body = server.files.get(path)
                if fault in {"404", "503"} or body is None:
                    self.send_error(503 if fault == "503" else 404)
                    return
                start = 0
                if wanted and fault != "ignore-range":
                    start = int(re.fullmatch(r"bytes=(\d+)-", wanted)[1])
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                else:
                    self.send_response(200)
                chunk = body[start:] + (b"surplus" if fault == "extra" else b"")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk[:len(chunk) // 2] if fault == "cut" else chunk)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.files, self.faults, self.requests = {}, {}, []

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
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-kokoro-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.lines = []
        self.pauses = []


class FakeLab:
    """A lab folder holding a three-package lock, its GPU swap and a small stand-in for the model files, at the real revision."""
    CONTENTS = {"config.json": b'{"n_token": 178}', "kokoro-v1_0.pth": noise(40_000, 1), "README.md": b"---\nlicense: apache-2.0\n---\n",
                "voices/af_heart.pt": noise(5_000, 2), "voices/bm_george.pt": noise(5_001, 3), "voices/ef_dora.pt": noise(5_002, 4),
                "voices/jf_alpha.pt": noise(5_003, 5), "voices/zf_xiaoxiao.pt": noise(5_004, 6)}
    LOCK = ("# a stand-in for requirements/kokoro-cpu.txt\n"
            f"alpha==1.0 --hash=sha256:{HASH}\ntorch==2.12.0 --hash=sha256:{HASH}\n"
            f"en_core_web_sm @ https://example.invalid/en_core_web_sm-3.8.0-py3-none-any.whl --hash=sha256:{HASH}\n")
    NVIDIA_LOCK = ("# a stand-in for requirements/kokoro-nvidia.txt\n"
                   f"torch @ https://example.invalid/whl/cu130/torch-2.12.0%2Bcu130-cp312-cp312-win_amd64.whl --hash=sha256:{'cd' * 32}\n")

    def __init__(self, folder):
        self.root = Path(folder)
        (self.root / "catalog").mkdir()
        (self.root / "requirements").mkdir()
        manifest = {"schema": 1, "repo": REAL_MANIFEST["repo"], "revision": REAL_MANIFEST["revision"], "license": "Apache-2.0",
                    "files": [{"path": path, "size": len(data), "sha256": sha(data)} for path, data in self.CONTENTS.items()]}
        (self.root / ik.MANIFEST).write_text(json.dumps(manifest), "utf-8")
        (self.root / ik.LOCK).write_text(self.LOCK, "utf-8")
        (self.root / ik.NVIDIA_LOCK).write_text(self.NVIDIA_LOCK, "utf-8")
        self.manifest = manifest

    def served(self):
        prefix = f"/{self.manifest['repo']}/resolve/{self.manifest['revision']}/"
        return {prefix + path: data for path, data in self.CONTENTS.items()}

    def snapshot(self):
        return ik.snapshot_dir(self.root, self.manifest)


class ShippedFilesTests(unittest.TestCase):
    def test_the_file_list_pins_the_revision_the_catalog_uses(self):
        self.assertEqual(REAL_MANIFEST["revision"], KOKORO_ENTRY["local_revision"])
        self.assertEqual(REAL_MANIFEST["repo"], KOKORO_ENTRY["checkpoint"])
        self.assertEqual(REAL_MANIFEST["license"], "Apache-2.0")

    def test_the_file_list_holds_what_the_adapter_loads(self):
        paths = [item["path"] for item in REAL_MANIFEST["files"]]
        self.assertEqual(len(paths), len(set(paths)))
        for needed in ("config.json", "kokoro-v1_0.pth", "README.md", "voices/af_heart.pt"):
            self.assertIn(needed, paths)
        for item in REAL_MANIFEST["files"]:
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$", item["path"])
            self.assertIsInstance(item["size"], int)
            self.assertGreater(item["size"], 0)
            self.assertNotIn("..", Path(item["path"]).parts)
            self.assertFalse(item["path"].startswith("/"))
        voices = [Path(path).stem for path in paths if path.startswith("voices/")]
        self.assertEqual(len(voices), 54)
        self.assertTrue(all(kokoro_support._VOICE_RE.fullmatch(name) for name in voices))
        self.assertTrue({name[0] for name in voices} <= set(kokoro_support.KOKORO_LANGUAGES))

    def test_the_voice_groups_agree_with_the_languages_the_lab_knows(self):
        self.assertEqual(set(ik.EXTRA_PACKAGES), set(kokoro_support._OPTIONAL_FRONTEND_MODULES))
        for code in ik.ENGLISH:
            self.assertIn("English", kokoro_support.KOKORO_LANGUAGES[code]["name"])
        for code, name in ik.EXTRA_PACKAGES.items():
            self.assertEqual(name, kokoro_support.KOKORO_LANGUAGES[code]["name"])

    def test_the_default_voices_are_the_ones_that_work_with_the_packages_it_installs(self):
        defaults = ik.build_parser().parse_args([]).voices
        names = [Path(item["path"]).stem for item in ik.choose_voices(REAL_MANIFEST, defaults)]
        self.assertEqual(defaults, ["supported"])
        self.assertEqual(len(names), 41)
        self.assertIn("af_heart", names, "the Models page checks Kokoro with this voice")
        self.assertFalse({name[0] for name in names} & set(ik.EXTRA_PACKAGES))
        self.assertEqual(sorted({name[0] for name in names}), ["a", "b", "e", "f", "h", "i", "p"])

    def test_the_package_list_pins_the_versions_the_catalog_names(self):
        packages = {item["key"]: item for item in ik.read_lock(ROOT / ik.LOCK)}
        self.assertEqual(packages["kokoro"]["version"], KOKORO_ENTRY["backend"]["package_version"])
        self.assertEqual(packages["transformers"]["version"], KOKORO_ENTRY["backend"]["transformers_version"])
        for name in ("torch", "misaki", "spacy", "en-core-web-sm", "espeakng-loader", "phonemizer-fork", "num2words"):
            self.assertIn(name, packages)
        self.assertNotIn("+", packages["torch"]["version"], "the CPU build has no GPU suffix such as +rocm or +cu")
        self.assertEqual(packages["en-core-web-sm"]["version"], "3.8.0")

    def test_every_line_of_the_package_list_pins_one_file_and_nothing_points_at_another_index(self):
        lines = [line.strip() for line in (ROOT / ik.LOCK).read_text("utf-8").splitlines()]
        lines = [line for line in lines if line and not line.startswith("#")]
        self.assertGreater(len(lines), 50)
        for line in lines:
            self.assertRegex(line, r"--hash=sha256:[0-9a-f]{64}$")
            self.assertFalse(line.startswith("-"), line)
        urls = [line for line in lines if "@" in line]
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/"))

    def test_the_package_list_repeats_the_core_requirements_at_the_same_versions(self):
        # The worker runs the lab's own modules in .venv-kokoro, so numpy and soundfile behave as they do in .venv.
        core = dict(re.split(r"==", line.strip(), maxsplit=1)
                    for line in (ROOT / "requirements" / "core.txt").read_text("utf-8").splitlines()
                    if line.strip() and not line.startswith("#"))
        packages = {item["key"]: item["version"] for item in ik.read_lock(ROOT / ik.LOCK)}
        self.assertIn("numpy", core)
        for name, version in core.items():
            self.assertEqual(packages.get(ik.canonical(name)), version, name)

    def test_the_catalog_runs_kokoro_in_its_own_environment(self):
        self.assertEqual(KOKORO_ENTRY["runtime"]["environment"], ik.ENVIRONMENT)
        self.assertEqual(KOKORO_ENTRY["runtime"]["python"], f"{ik.ENVIRONMENT}/Scripts/python.exe")
        self.assertEqual(ik.environment_python(ROOT), ROOT / KOKORO_ENTRY["runtime"]["python"])
        self.assertEqual(engine_checks.REQUIRED_PACKAGES["kokoro"], ("kokoro",))

    def test_the_nvidia_list_swaps_only_torch_for_a_cuda_build_of_the_same_version_from_pytorchs_index(self):
        cpu = ik.read_lock(ROOT / ik.LOCK)
        gpu = ik.read_lock(ROOT / ik.NVIDIA_LOCK)
        self.assertEqual([item["key"] for item in gpu], ["torch"])
        cpu_torch = next(item for item in cpu if item["key"] == "torch")
        self.assertEqual(gpu[0]["version"], cpu_torch["version"] + "+cu130")
        line = ik.lock_lines(ROOT / ik.NVIDIA_LOCK)["torch"]
        self.assertRegex(line, r"^torch @ https://download\.pytorch\.org/whl/cu130/torch-2\.12\.0%2Bcu130-cp312-cp312-win_amd64\.whl "
                               r"--hash=sha256:[0-9a-f]{64}$")
        swapped = ik.pinned_packages(ROOT, nvidia=True)
        self.assertEqual([item["key"] for item in swapped], [item["key"] for item in cpu])
        self.assertEqual([item for item in swapped if item["key"] != "torch"], [item for item in cpu if item["key"] != "torch"])

    def test_the_batch_file_runs_the_installer_with_the_labs_own_python(self):
        data = (ROOT / "setup-kokoro.bat").read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "batch files keep CRLF line ends")
        text = data.decode("utf-8")
        self.assertIn('".venv\\Scripts\\python.exe" tools\\install_kokoro.py %*', text)
        self.assertIn("Run setup.bat first.", text)

    def test_every_setup_window_waits_for_a_key_so_a_double_click_can_be_read(self):
        for name in ("setup.bat", "setup-desktop.bat", "setup-kokoro.bat"):
            lines = [line.strip() for line in (ROOT / name).read_text("utf-8").splitlines() if line.strip()]
            last_run = max(index for index, line in enumerate(lines) if "tools\\" in line)
            self.assertIn("pause", lines[last_run + 1:], name)

    def test_the_first_setup_offers_the_installer(self):
        from tools import first_setup
        self.assertEqual(first_setup.MODELS["kokoro"], {"engine": "kokoro", "script": "tools/install_kokoro.py", "nvidia": True})
        self.assertIn("setup-kokoro.bat", first_setup.menu_text(16, first_setup.model_sizes()))

    def test_the_guides_describe_the_installer_with_the_numbers_it_ships(self):
        setup = (ROOT / "docs" / "SETUP.md").read_text("utf-8")
        for needed in ("setup-kokoro.bat", "tools\\install_kokoro.py", "--dry-run", "--yes", "--checkpoint-only", "--voices",
                       "requirements/kokoro-cpu.txt", "catalog/kokoro_snapshot.json", "Visual C++ Redistributable"):
            self.assertIn(needed, setup)
        packages = len(ik.read_lock(ROOT / ik.LOCK))
        supported = len(ik.choose_voices(REAL_MANIFEST, ["supported"]))
        english = len(ik.choose_voices(REAL_MANIFEST, ["english"]))
        every = len(ik.choose_voices(REAL_MANIFEST, ["all"]))
        flat = " ".join(setup.split())
        for number in (f"{packages} pinned Python packages", f"{supported} voices", f"`english` ({english})", f"`all` ({every})"):
            self.assertIn(number, flat)
        for name in ("README.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md"):
            text = " ".join((ROOT / name).read_text("utf-8").split())
            self.assertTrue("setup-kokoro.bat" in text or "tools/install_kokoro.py" in text, name)
            if name != "README.md":
                self.assertIn(f"{packages} ", text)
        self.assertIn("setup-kokoro.bat", (ROOT / "README.md").read_text("utf-8"))

    def test_the_installer_prints_only_plain_ascii(self):
        self.assertTrue((ROOT / "tools" / "install_kokoro.py").read_bytes().isascii())
        packages = ik.read_lock(ROOT / ik.LOCK)
        files = ik.checkpoint_files(REAL_MANIFEST, ik.choose_voices(REAL_MANIFEST, ["all"]))
        for todo, only, exists, nvidia in ((len(packages), False, False, False), (3, False, True, False), (0, False, True, False),
                                           (0, True, True, False), (len(packages), False, False, True)):
            text = ik.plan_text(REAL_MANIFEST, packages, todo, files, files, checkpoint_only=only, environment_exists=exists,
                                nvidia=nvidia)
            self.assertTrue(text.isascii())
            self.assertLessEqual(max(len(line) for line in text.splitlines()), 110)


class ConsoleTests(unittest.TestCase):
    def test_a_console_that_cannot_print_a_character_gets_a_question_mark_not_a_crash(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict", newline="\n")
        with contextlib.redirect_stdout(stream):
            ik.say_now("café is open")
            stream.flush()
        self.assertEqual(stream.buffer.getvalue().decode("ascii"), "caf? is open\n")


class LockTests(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lock.txt"
            path.write_bytes(text.encode("utf-8"))
            return ik.read_lock(path)

    def test_pinned_and_url_lines_are_read_with_windows_line_ends(self):
        text = (f"# header\r\n\r\nJinja2==3.1.6 --hash=sha256:{HASH}\r\n"
                f"en_core_web_sm @ https://host/x/en_core_web_sm-3.8.0-py3-none-any.whl --hash=sha256:{HASH}\r\n")
        self.assertEqual(self.read(text), [
            {"name": "Jinja2", "key": "jinja2", "version": "3.1.6", "sha256": HASH},
            {"name": "en_core_web_sm", "key": "en-core-web-sm", "version": "3.8.0", "sha256": HASH}])

    def test_a_line_that_is_not_pinned_with_a_hash_is_refused_by_number(self):
        for bad in ("requests>=2 --hash=sha256:" + HASH, "requests==2.0", "requests==2.0 --hash=sha256:abc",
                    f"requests==2.0 --hash=sha256:{HASH} --extra", f"--index-url https://x --hash=sha256:{HASH}"):
            with self.assertRaisesRegex(ik.InstallError, "line 2 is not a pinned package"):
                self.read(f"alpha==1.0 --hash=sha256:{HASH}\n{bad}\n")

    def test_an_empty_or_missing_list_is_an_error(self):
        with self.assertRaisesRegex(ik.InstallError, "lists no packages"):
            self.read("# only a comment\n")
        with self.assertRaisesRegex(ik.InstallError, "is missing"):
            ik.read_lock(ROOT / "requirements" / "does-not-exist.txt")

    def test_a_missing_or_incomplete_file_list_is_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "m.json"
            with self.assertRaisesRegex(ik.InstallError, "missing or unreadable"):
                ik.load_manifest(path)
            path.write_text('{"repo": "a/b"}', "utf-8")
            with self.assertRaisesRegex(ik.InstallError, "lacks repo, revision, license or files"):
                ik.load_manifest(path)
            path.write_text("not json", "utf-8")
            with self.assertRaisesRegex(ik.InstallError, "missing or unreadable"):
                ik.load_manifest(path)


class VoiceChoiceTests(unittest.TestCase):
    def names(self, *wanted):
        return [Path(item["path"]).stem for item in ik.choose_voices(REAL_MANIFEST, list(wanted))]

    def test_english_is_the_american_and_british_voices(self):
        names = self.names("english")
        self.assertTrue(all(name[0] in "ab" for name in names))
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), 28)

    def test_supported_leaves_out_only_the_voices_that_need_more_packages(self):
        names = self.names("supported")
        self.assertEqual(len(names), 41)
        self.assertEqual(set(self.names("all")) - set(names), {name for name in self.names("all") if name[0] in "jz"})
        self.assertEqual(len(set(self.names("all")) - set(names)), 13)

    def test_all_is_every_voice_and_names_can_be_added_once(self):
        self.assertEqual(len(self.names("all")), 54)
        self.assertEqual(self.names("ef_dora", "ef_dora", "bm_george"), ["bm_george", "ef_dora"])
        self.assertEqual(len(self.names("english", "ef_dora", "bm_george")), 29)

    def test_an_unknown_voice_is_refused_by_name(self):
        with self.assertRaisesRegex(ik.InstallError, "no voice named 'xx_nobody'"):
            self.names("english", "xx_nobody")

    def test_the_checkpoint_always_comes_with_its_config_and_card(self):
        files = ik.checkpoint_files(REAL_MANIFEST, ik.choose_voices(REAL_MANIFEST, ["bm_george"]))
        self.assertEqual([item["path"] for item in files], ["config.json", "kokoro-v1_0.pth", "README.md", "voices/bm_george.pt"])


class EnvironmentTests(unittest.TestCase):
    PINS = [{"name": "Jinja2", "key": "jinja2", "version": "3.1.6"}, {"name": "torch", "key": "torch", "version": "2.12.0"},
            {"name": "kokoro", "key": "kokoro", "version": "0.9.4"}]

    def test_installed_versions_are_keyed_by_canonical_name(self):
        versions = ik.installed_versions()
        self.assertIn("numpy", versions)
        self.assertTrue(all(key == ik.canonical(key) for key in versions))

    def test_packages_are_missing_the_same_or_at_another_version(self):
        missing, different = ik.package_state(self.PINS, {"jinja2": "3.1.6", "torch": "2.12.0+rocm7.14.0"})
        self.assertEqual([item["name"] for item in missing], ["kokoro"])
        self.assertEqual([(item["name"], have) for item, have in different], [("torch", "2.12.0+rocm7.14.0")])
        self.assertEqual(ik.package_state(self.PINS, {"jinja2": "3.1.6", "torch": "2.12.0", "kokoro": "0.9.4"}), ([], []))

    def test_only_a_64_bit_windows_python_312_in_the_labs_own_environment_passes(self):
        ok = {"system": "win32", "machine": "AMD64", "version": (3, 12, 10), "prefix": str(ROOT / ".venv")}
        self.assertEqual(ik.interpreter_problems(ROOT, **ok), [])
        self.assertIn("64-bit Windows", ik.interpreter_problems(ROOT, **{**ok, "system": "linux"})[0])
        self.assertIn("64-bit Windows", ik.interpreter_problems(ROOT, **{**ok, "machine": "ARM64"})[0])
        self.assertIn("64-bit Python", ik.interpreter_problems(ROOT, **{**ok, "bits": 32})[0])
        self.assertEqual(ik.interpreter_problems(ROOT, **{**ok, "bits": 64}), [])
        self.assertIn("Python 3.13", ik.interpreter_problems(ROOT, **{**ok, "version": (3, 13, 0)})[0])
        elsewhere = ik.interpreter_problems(ROOT, **{**ok, "prefix": str(ROOT / "other-venv")})
        self.assertEqual(len(elsewhere), 1)
        self.assertIn(".venv\\Scripts\\python.exe tools\\install_kokoro.py", elsewhere[0])

    def test_other_versions_already_installed_are_named_and_the_way_out_is_given(self):
        different = [({"name": "torch", "version": "2.12.0"}, "2.12.0+rocm7.14.0")]
        problems = ik.package_problems(different, has_pip=True)
        self.assertEqual(len(problems), 1)
        self.assertIn("torch 2.12.0+rocm7.14.0 (the list pins 2.12.0)", problems[0])
        self.assertIn("--checkpoint-only", problems[0])
        many = ik.package_problems([({"name": f"p{n}", "version": "1"}, "2") for n in range(8)], has_pip=True)
        self.assertIn("and 3 more", many[0])

    def test_a_missing_pip_is_reported(self):
        self.assertEqual(ik.package_problems([], has_pip=True), [])
        self.assertIn(".venv-kokoro has no pip. Delete the .venv-kokoro folder", ik.package_problems([], has_pip=False)[0])

    def test_the_versions_in_the_engines_environment_are_read_by_its_own_python(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(ik.environment_versions(folder, runner=lambda *a, **k: self.fail("ran without an environment")), {})
            python = ik.environment_python(folder)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            calls = []

            def runner(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, 'noise\n{"Jinja2": "3.1.6", "torch": "2.12.0"}\n', "")

            self.assertEqual(ik.environment_versions(folder, runner=runner), {"jinja2": "3.1.6", "torch": "2.12.0"})
            self.assertEqual(calls[0][:3], [str(python), "-I", "-c"])
            broken = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "ImportError: no encodings")  # noqa: E731
            with self.assertRaisesRegex(ik.InstallError, r"(?s)\.venv-kokoro is here but its Python does not run.*no encodings"
                                                         r".*Delete the \.venv-kokoro folder"):
                ik.environment_versions(folder, runner=broken)

    def test_the_environment_is_made_from_the_python_that_made_dot_venv(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            def runner(command, **kwargs):
                calls.append((command, kwargs))
                python = ik.environment_python(folder)
                python.parent.mkdir(parents=True)
                python.write_bytes(b"")
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.dict("os.environ", {"__PYVENV_LAUNCHER__": "x", "VIRTUAL_ENV": "y"}):
                ik.make_environment(folder, runner=runner)
            command, kwargs = calls[0]
            self.assertEqual(command, [getattr(sys, "_base_executable", sys.executable), "-m", "venv",
                                       str(Path(folder) / ".venv-kokoro")])
            self.assertNotIn("VIRTUAL_ENV", kwargs["env"])
            self.assertNotIn("__PYVENV_LAUNCHER__", kwargs["env"])
            with self.assertRaisesRegex(ik.InstallError, "could not make .venv-other"):
                ik.make_env(folder, ".venv-other", runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 1))

    def test_the_model_files_alone_need_an_environment_that_already_has_kokoro(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIn("there is no .venv-kokoro here", ik.checkpoint_only_problems(folder, {})[0])
            python = ik.environment_python(folder)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            self.assertIn("kokoro is not among them", ik.checkpoint_only_problems(folder, {"torch": "2.12.0"})[0])
            self.assertEqual(ik.checkpoint_only_problems(folder, {"kokoro": "0.9.4"}), [])

    def test_the_nvidia_build_needs_an_nvidia_driver(self):
        self.assertEqual(ik.nvidia_problems(present=True), [])
        problem = ik.nvidia_problems(present=False)[0]
        self.assertIn("no NVIDIA driver", problem)
        self.assertIn("leave out --nvidia", problem)
        with mock.patch.dict("os.environ", {"SystemRoot": str(ROOT / "no-such-windows")}):
            self.assertFalse(ik.nvidia_driver_present())

    def test_a_gpu_list_may_only_swap_packages_the_cpu_list_pins(self):
        cpu = [{"name": "torch", "key": "torch", "version": "2.12.0", "sha256": HASH}]
        with self.assertRaisesRegex(ik.InstallError, "swaps torchaudio, which the CPU list does not pin"):
            ik.swap_packages(cpu, [{"name": "torchaudio", "key": "torchaudio", "version": "2.12.0+cu130", "sha256": HASH}])

    def test_a_folder_path_too_long_for_windows_is_refused_unless_long_paths_are_on(self):
        short = Path("C:/voice-lab")
        self.assertEqual(ik.long_path_problems(short, long_paths=False), [])
        deep = Path("C:/") / ("a-long-folder-name-" * 6) / "text-to-speech" / "fels-voice-lab"
        problems = ik.long_path_problems(deep, long_paths=False)
        self.assertEqual(len(problems), 1)
        self.assertIn("too long for Windows", problems[0])
        self.assertIn("C:\\voice-lab", problems[0])
        self.assertEqual(ik.long_path_problems(deep, long_paths=True), [])

    def test_too_little_free_space_is_reported_with_both_numbers(self):
        self.assertEqual(ik.space_problems(ROOT, 2_000_000_000, free=3_000_000_000), [])
        problem = ik.space_problems(ROOT, 2_000_000_000, free=1_000_000_000)[0]
        self.assertIn("2000 MB", problem)
        self.assertIn("1000 MB", problem)


class FetchTests(ServerCase):
    def fetch(self, name="f.bin", data=None, **options):
        data = self.data if data is None else data
        self.server.files["/" + name] = data
        return ik.fetch(f"{self.server.address}/{name}", self.base / name, len(data), sha(data), say=self.lines.append,
                        pause=self.pauses.append, **options)

    def setUp(self):
        super().setUp()
        self.data = noise(300_000, 9)

    def test_a_file_is_downloaded_checked_and_left_without_a_partial(self):
        self.assertTrue(self.fetch())
        self.assertEqual((self.base / "f.bin").read_bytes(), self.data)
        self.assertEqual(sorted(path.name for path in self.base.iterdir() if path.is_file()), ["f.bin"])

    def test_a_file_already_in_place_is_not_downloaded_again(self):
        (self.base / "f.bin").write_bytes(self.data)
        self.assertFalse(self.fetch())
        self.assertEqual(self.server.requests, [])

    def test_a_file_of_the_right_size_with_other_content_is_replaced(self):
        (self.base / "f.bin").write_bytes(b"x" * len(self.data))
        self.assertTrue(self.fetch())
        self.assertEqual((self.base / "f.bin").read_bytes(), self.data)

    def test_an_interrupted_download_resumes_where_it_stopped(self):
        half = len(self.data) // 2
        (self.base / "f.bin.partial").write_bytes(self.data[:half])
        self.fetch()
        self.assertEqual(self.server.requests, [("/f.bin", f"bytes={half}-")])
        self.assertEqual((self.base / "f.bin").read_bytes(), self.data)

    def test_a_server_that_ignores_the_range_gets_the_whole_file_instead(self):
        (self.base / "f.bin.partial").write_bytes(self.data[:1000])
        self.server.faults["/f.bin"] = ["ignore-range"]
        self.fetch()
        self.assertEqual((self.base / "f.bin").read_bytes(), self.data)
        self.assertEqual(len(self.server.requests), 1, "the full answer is used as it is, not appended to the partial file")

    def test_a_connection_that_drops_part_way_is_resumed(self):
        self.server.faults["/f.bin"] = ["cut"]
        self.fetch()
        self.assertEqual((self.base / "f.bin").read_bytes(), self.data)
        self.assertEqual(len(self.server.requests), 2)
        self.assertRegex(self.server.requests[1][1], r"^bytes=\d+-$")
        self.assertEqual(self.pauses, [1])

    def test_a_server_error_is_tried_again_and_a_missing_file_is_not(self):
        self.server.faults["/f.bin"] = ["503", "503"]
        self.fetch()
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(self.pauses, [1, 2])
        self.server.reset()
        with self.assertRaisesRegex(ik.InstallError, "answered 404.*pinned revision may have been removed"):
            ik.fetch(f"{self.server.address}/gone.bin", self.base / "gone.bin", 10, HASH, say=self.lines.append,
                     pause=self.pauses.append)
        self.assertEqual(len(self.server.requests), 1)

    def test_a_server_that_keeps_failing_ends_with_an_instruction_to_run_again(self):
        self.server.faults["/f.bin"] = ["503"] * 10
        with self.assertRaisesRegex(ik.InstallError, r"download of f\.bin stopped \(HTTP 503\)\. Run the installer again"):
            self.fetch()
        self.assertEqual(len(self.server.requests), 4)
        self.assertFalse((self.base / "f.bin").exists())

    def test_a_file_that_does_not_match_its_hash_is_deleted_not_kept(self):
        self.server.files["/f.bin"] = noise(len(self.data), 10)
        with self.assertRaisesRegex(ik.InstallError, "does not match the published SHA-256, so it was deleted"):
            ik.fetch(f"{self.server.address}/f.bin", self.base / "f.bin", len(self.data), sha(self.data), say=self.lines.append)
        self.assertEqual([path.name for path in self.base.iterdir()], [])

    def test_a_server_that_sends_more_than_the_file_holds_is_not_trusted(self):
        self.server.faults["/f.bin"] = ["extra"] * 4
        with self.assertRaisesRegex(ik.InstallError, "sent more data than the file holds"):
            self.fetch()
        self.assertFalse((self.base / "f.bin").exists())

    def test_a_large_file_reports_ten_steps_of_progress_at_most(self):
        self.fetch("big.bin", noise(5_200_000, 11))
        self.assertTrue(self.lines)
        self.assertLessEqual(len(self.lines), 10)
        self.assertRegex(self.lines[-1], r"^ +100% \(5 MB of 5 MB\)$")

    def test_a_resumed_download_does_not_repeat_the_progress_already_made(self):
        data = noise(40_000_000, 12)
        (self.base / "big.bin.partial").write_bytes(data[:14_000_000])
        self.fetch("big.bin", data)
        percents = [int(line.split("%")[0]) for line in self.lines]
        self.assertEqual(percents, [40, 50, 60, 70, 80, 90, 100], self.lines)

    def test_a_small_file_reports_nothing(self):
        self.fetch()
        self.assertEqual(self.lines, [])

    def test_a_disk_that_cannot_be_written_stops_at_once_with_the_path(self):
        self.server.files["/f.bin"] = self.data
        with mock.patch.object(Path, "open", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaisesRegex(ik.InstallError, r"Cannot write .*f\.bin\.partial"):
                ik.fetch(f"{self.server.address}/f.bin", self.base / "f.bin", len(self.data), sha(self.data))
        self.assertEqual(len(self.server.requests), 1, "a write error is not tried again")


class CheckpointDownloadTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.lab = FakeLab(self.base)
        self.server.files = self.lab.served()
        patch = mock.patch.object(ik, "HOST", self.server.address)
        patch.start()
        self.addCleanup(patch.stop)

    def download(self, voices=("english",)):
        files = ik.checkpoint_files(self.lab.manifest, ik.choose_voices(self.lab.manifest, list(voices)))
        ik.download_checkpoint(self.lab.root, self.lab.manifest, files, say=self.lines.append, pause=self.pauses.append)
        return files

    def test_the_urls_name_the_repository_the_revision_and_the_path(self):
        item = {"path": "voices/af_heart.pt"}
        self.assertEqual(ik.download_url(self.lab.manifest, item),
                         f"{self.server.address}/hexgrad/Kokoro-82M/resolve/{REAL_MANIFEST['revision']}/voices/af_heart.pt")

    def test_files_land_in_the_snapshot_the_lab_reads_and_the_revision_is_recorded(self):
        files = self.download()
        snapshot = self.lab.snapshot()
        self.assertEqual(sorted(str(path.relative_to(snapshot)).replace("\\", "/") for path in snapshot.rglob("*") if path.is_file()),
                         sorted(item["path"] for item in files))
        self.assertEqual((snapshot.parent.parent / "refs" / "main").read_text("utf-8"), REAL_MANIFEST["revision"])
        self.assertEqual((snapshot / "kokoro-v1_0.pth").read_bytes(), FakeLab.CONTENTS["kokoro-v1_0.pth"])

    def test_the_lab_finds_voices_and_counts_kokoro_as_installed_afterwards(self):
        self.download(["english", "ef_dora"])
        cache = self.lab.snapshot().parent.parent
        with mock.patch.object(kokoro_support, "KOKORO_REPO_CACHE", cache):
            self.assertEqual(kokoro_support.kokoro_snapshot_dir(), self.lab.snapshot())
            self.assertEqual(kokoro_support.discover_kokoro_voices(), ["af_heart", "bm_george", "ef_dora"])
        python = self.base / ".venv-kokoro" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"")
        report = engine_checks.installation(KOKORO_ENTRY, self.base)
        self.assertEqual((report["installed"], report["missing"]), (False, ["kokoro (Python package)"]))
        (self.base / ".venv-kokoro" / "Lib" / "site-packages" / "kokoro-0.9.4.dist-info").mkdir(parents=True)
        report = engine_checks.installation(KOKORO_ENTRY, self.base)
        self.assertEqual((report["installed"], report["missing"]), (True, []))

    def test_a_second_run_checks_the_files_and_does_not_download_them_again(self):
        self.download()
        asked = len(self.server.requests)
        self.lines.clear()
        self.download()
        self.assertEqual(len(self.server.requests), asked)
        self.assertEqual(sum(line.strip() == "already in place" for line in self.lines), 5)

    def test_a_damaged_file_in_the_snapshot_is_fetched_again(self):
        self.download()
        damaged = self.lab.snapshot() / "voices" / "af_heart.pt"
        damaged.write_bytes(b"damaged")
        self.server.requests.clear()
        self.download()
        self.assertEqual(damaged.read_bytes(), FakeLab.CONTENTS["voices/af_heart.pt"])
        self.assertEqual([path for path, _range in self.server.requests],
                         [f"/{REAL_MANIFEST['repo']}/resolve/{REAL_MANIFEST['revision']}/voices/af_heart.pt"])

    def test_a_file_list_entry_that_leaves_the_model_folder_is_refused_before_anything_is_fetched(self):
        for path in ("../escape.txt", "voices/../../escape.txt", "..\\escape.txt"):
            manifest = {**self.lab.manifest, "files": [{"path": path, "size": 4, "sha256": HASH}]}
            self.server.requests.clear()
            with self.assertRaisesRegex(ik.InstallError, "outside the model folder"):
                ik.download_checkpoint(self.lab.root, manifest, manifest["files"], say=self.lines.append)
            self.assertEqual(self.server.requests, [])
            self.assertFalse((self.base / "escape.txt").exists())

    def test_a_download_that_fails_leaves_no_complete_looking_file(self):
        self.server.files.pop(f"/{REAL_MANIFEST['repo']}/resolve/{REAL_MANIFEST['revision']}/kokoro-v1_0.pth")
        with self.assertRaises(ik.InstallError):
            self.download()
        self.assertFalse((self.lab.snapshot() / "kokoro-v1_0.pth").exists())
        self.assertEqual(ik.pending_files(self.lab.root, self.lab.manifest, ik.checkpoint_files(self.lab.manifest, [])),
                         [item for item in self.lab.manifest["files"] if item["path"] in {"kokoro-v1_0.pth", "README.md"}])


class PlanTests(unittest.TestCase):
    PACKAGES = len(ik.read_lock(ROOT / ik.LOCK))

    def plan(self, voices=("supported",), todo=None, pending=True, only=False, exists=False, nvidia=False):
        files = ik.checkpoint_files(REAL_MANIFEST, ik.choose_voices(REAL_MANIFEST, list(voices)))
        packages = ik.pinned_packages(ROOT, nvidia=nvidia)
        return ik.plan_text(REAL_MANIFEST, packages, self.PACKAGES if todo is None else todo, files, files if pending else [],
                            checkpoint_only=only, environment_exists=exists, nvidia=nvidia)

    @staticmethod
    def flat(text):
        return " ".join(text.split())

    def test_the_plan_says_what_is_installed_where_and_how_much_to_download(self):
        text = self.flat(self.plan())
        for part in ("Kokoro 82M on the CPU", "First a Python environment of its own, .venv-kokoro, made from the same Python 3.12",
                     f"{self.PACKAGES} of {self.PACKAGES} to install there", "PyTorch for the CPU",
                     f"About {ik.mb_text(ik.PACKAGES_DOWNLOAD_MB)} to download and {ik.mb_text(ik.PACKAGES_DISK_MB)} on disk",
                     "requirements/kokoro-cpu.txt", "revision f3ff357", "huggingface.co/hexgrad/Kokoro-82M", "327 MB checkpoint",
                     "41 voices", "Apache-2.0", "SHA-256", "(.venv-kokoro, hf-cache and out\\kokoro)", "nothing system-wide",
                     "pip keeps its usual download cache in your user profile", "speech check"):
            self.assertIn(part, text)
        self.assertNotIn("NVIDIA", text)
        self.assertIn("28 English voices", self.flat(self.plan(["english"])))
        self.assertIn("and 1 English voice.", self.flat(self.plan(["bm_george"])))
        self.assertIn("and 1 voice.", self.flat(self.plan(["ef_dora"])))

    def test_the_nvidia_plan_names_the_cuda_build_its_size_and_that_it_is_untried(self):
        text = self.flat(self.plan(nvidia=True))
        for part in ("Kokoro 82M on an NVIDIA graphics card", "the CUDA 13.0 build of PyTorch for NVIDIA graphics cards (not yet tried on one)",
                     f"About {ik.mb_text(ik.PACKAGES_DOWNLOAD_MB + ik.NVIDIA_EXTRA_DOWNLOAD_MB)} to download",
                     "requirements/kokoro-cpu.txt and requirements/kokoro-nvidia.txt", "speaks one sentence, which you can play "
                     "afterwards, on the graphics card.", "GTX 16 or RTX 20 series card or newer", "driver of version 580",
                     "hardware report on GitHub"):
            self.assertIn(part, text)

    def test_the_plan_names_the_packages_with_licences_other_than_mit_bsd_or_apache(self):
        text = self.flat(self.plan())
        for part in ("phonemizer-fork", "espeak-ng library inside espeakng-loader (GPL-3.0)", "num2words (LGPL)",
                     "certifi and tqdm (MPL-2.0)", "not part of this repository"):
            self.assertIn(part, text)

    def test_the_plan_reflects_what_is_already_done(self):
        self.assertIn(f"all {self.PACKAGES} are already installed in .venv-kokoro", self.flat(self.plan(todo=0)))
        self.assertIn(".venv-kokoro is already here. Python packages: 3 of", self.flat(self.plan(todo=3, exists=True)))
        self.assertIn("Everything is already in place", self.flat(self.plan(pending=False)))
        self.assertIn("the ones in .venv-kokoro are left alone (--checkpoint-only)", self.flat(self.plan(only=True)))

    def test_voices_that_need_more_packages_are_called_out_only_when_chosen(self):
        self.assertNotIn("Misaki", self.plan())
        text = self.flat(self.plan(["english", "jf_alpha", "zf_xiaoxiao"]))
        self.assertIn("The Japanese and Mandarin Chinese voices also need Misaki language packages", text)


class InstallPackagesTests(unittest.TestCase):
    def test_pip_gets_the_locked_file_with_hashes_required_and_nothing_resolved(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        ik.install_packages(ROOT, runner=runner)
        command, kwargs = calls[0]
        self.assertEqual(command[:4], [str(ROOT / ".venv-kokoro" / "Scripts" / "python.exe"), "-m", "pip", "install"])
        for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:", "--no-input", "--disable-pip-version-check"):
            self.assertIn(flag, command)
        self.assertEqual(Path(command[command.index("-r") + 1]), ROOT / "requirements" / "kokoro-cpu.txt")
        self.assertEqual(kwargs["cwd"], ROOT)
        self.assertNotIn("--index-url", " ".join(command))
        self.assertNotIn("--extra-index-url", " ".join(command))

    def test_for_nvidia_pip_gets_the_cpu_list_with_the_cuda_torch_in_its_place(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)
            ik.install_packages(lab.root, nvidia=True,
                                runner=lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0))
            written = Path(calls[0][calls[0].index("-r") + 1])
            self.assertEqual(written, lab.root / "out" / "kokoro" / "requirements-nvidia.txt")
            lines = [line for line in written.read_text("utf-8").splitlines() if not line.startswith("#")]
        self.assertEqual(lines, [f"alpha==1.0 --hash=sha256:{HASH}", FakeLab.NVIDIA_LOCK.splitlines()[1],
                                 f"en_core_web_sm @ https://example.invalid/en_core_web_sm-3.8.0-py3-none-any.whl --hash=sha256:{HASH}"])

    def test_a_pip_failure_says_to_fix_it_and_run_again(self):
        with self.assertRaisesRegex(ik.InstallError, "pip stopped with exit code 2.*run the installer again"):
            ik.install_packages(ROOT, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 2))


class SpeechCheckTests(unittest.TestCase):
    def check(self, stdout="", stderr="", code=0, error=None, device="cpu"):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if error:
                raise error
            return subprocess.CompletedProcess(command, code, stdout, stderr)

        with tempfile.TemporaryDirectory() as folder:
            return ik.run_speech_check(folder, device=device, runner=runner), calls, Path(folder)

    GOOD = ('{"voice": "af_heart", "problem": null, "load_seconds": 4.2, "total_seconds": 1.1, "audio_seconds": 2.6, '
            '"torch": "2.12.0+cpu", "device": "cpu"}')

    def test_the_clip_is_made_offline_by_the_new_environment_and_its_numbers_come_back(self):
        result, calls, folder = self.check(stdout="loguru noise\n" + self.GOOD + "\n", stderr="a warning\n")
        command, kwargs = calls[0]
        self.assertEqual(command[:2], [str(folder / ".venv-kokoro" / "Scripts" / "python.exe"), "-c"])
        self.assertIn("engine_checks.smoke_request", command[2])
        self.assertEqual(command[3:], [str(folder), str(folder / "out" / "kokoro" / "install-check.wav"), "cpu"])
        self.assertEqual((kwargs["env"]["HF_HUB_OFFLINE"], kwargs["env"]["TRANSFORMERS_OFFLINE"]), ("1", "1"))
        self.assertEqual((result["voice"], result["torch"]), ("af_heart", "2.12.0+cpu"))
        self.assertEqual(result["wav"], folder / "out" / "kokoro" / "install-check.wav")

    def test_on_the_graphics_card_the_check_asks_for_cuda_and_a_cuda_failure_points_at_the_driver(self):
        _result, calls, _folder = self.check(stdout=self.GOOD, device="cuda")
        self.assertEqual(calls[0][0][-1], "cuda")
        with self.assertRaisesRegex(ik.InstallError, r"(?s)CUDA driver version is insufficient.*version 580 or later.*without --nvidia"):
            self.check(stderr="RuntimeError: CUDA driver version is insufficient for CUDA runtime version", code=1, device="cuda")

    def test_a_crash_shows_the_end_of_the_output(self):
        with self.assertRaisesRegex(ik.InstallError, r"in place, but Kokoro did not load or speak:\n(.*\n)*last line"):
            self.check(stdout="first\n", stderr="\n".join(f"line {n}" for n in range(30)) + "\nlast line", code=1)

    def test_a_missing_dll_points_at_the_visual_c_runtime(self):
        with self.assertRaisesRegex(ik.InstallError, r"(?s)OSError: \[WinError 126\].*https://aka\.ms/vs/17/release/vc_redist\.x64\.exe"):
            self.check(stderr="OSError: [WinError 126] The specified module could not be found. fbgemm.dll", code=1)

    def test_a_clip_that_is_not_speech_is_reported(self):
        with self.assertRaisesRegex(ik.InstallError, "was not speech: generated silence"):
            self.check(stdout=self.GOOD.replace("null", '"generated silence"'))

    def test_no_result_line_is_a_failure_and_a_hang_is_stopped(self):
        with self.assertRaisesRegex(ik.InstallError, "did not load or speak"):
            self.check(stdout="no json here\n")
        with self.assertRaisesRegex(ik.InstallError, "more than 15 minutes"):
            self.check(error=subprocess.TimeoutExpired("python", 900))

    def test_the_last_json_line_is_found_among_log_lines(self):
        self.assertEqual(ik.last_json_line('{"a": 1}\nWARNING {oops\n{broken\n'), {"a": 1})
        self.assertIsNone(ik.last_json_line("nothing\nuseful\n"))

    def test_the_check_script_writes_the_clip_makes_the_default_reference_and_reports_both(self):
        with tempfile.TemporaryDirectory() as folder:
            lab, wav = Path(folder) / "lab", Path(folder) / "clip.wav"
            lab.mkdir()
            (lab / "engines.py").write_text(
                "import numpy as np\n"
                "class Kokoro:\n"
                "    def __init__(self, device):\n"
                "        assert device == 'cpu'\n"
                "    def speak(self, text, voice='af_heart'):\n"
                "        t = np.arange(24000) / 24000\n"
                "        return (0.1 * np.sin(2 * np.pi * 220 * t)).astype('float32'), 24000, {'total_s': 0.25}\n"
                "def ensure_reference(kokoro):\n"
                "    assert isinstance(kokoro, Kokoro), 'the model already loaded speaks the reference'\n"
                "    open(__file__ + '.reference', 'w').close()\n", "utf-8")
            (lab / "engine_checks.py").write_text(
                "def smoke_request(engine):\n    return {'engine': engine, 'text': 'check', 'voice': 'af_heart'}\n"
                "def audible(samples, rate):\n    return None\n", "utf-8")
            (lab / "kokoro_support.py").write_text("def discover_kokoro_voices():\n    return ['af_heart']\n", "utf-8")
            (lab / "torch.py").write_text("__version__ = '2.12.0+cpu'\n", "utf-8")
            done = subprocess.run([sys.executable, "-c", ik.SPEECH_CHECK, str(lab), str(wav), "cpu"], capture_output=True,
                                  text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            result = ik.last_json_line(done.stdout)
            self.assertEqual((result["voice"], result["problem"], result["total_seconds"]), ("af_heart", None, 0.25))
            self.assertEqual((result["torch"], result["device"]), ("2.12.0+cpu", "cpu"))
            self.assertAlmostEqual(result["audio_seconds"], 1.0)
            samples, rate = sf.read(str(wav))
            self.assertEqual((rate, len(samples)), (24000, 24000))
            self.assertTrue((lab / "engines.py.reference").is_file())

    def test_the_names_the_check_script_uses_exist_in_the_lab(self):
        import engines
        self.assertTrue(callable(engine_checks.smoke_request) and callable(engine_checks.audible))
        self.assertTrue(hasattr(engines, "Kokoro") and callable(engines.ensure_reference))
        self.assertTrue(callable(kokoro_support.discover_kokoro_voices))
        compile(ik.SPEECH_CHECK, "<speech check>", "exec")


class ReceiptTests(unittest.TestCase):
    def test_the_receipt_records_what_was_installed_and_how_it_spoke(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)
            (lab.root / "out" / "kokoro").mkdir(parents=True)
            packages = ik.read_lock(lab.root / ik.LOCK)
            files = ik.checkpoint_files(lab.manifest, ik.choose_voices(lab.manifest, ["bm_george"]))
            check = {"voice": "af_heart", "problem": None, "load_seconds": 4.2, "total_seconds": 1.1, "audio_seconds": 2.6, "wav": "x",
                     "torch": "2.12.0+cu130", "device": "cuda"}
            receipt = json.loads(ik.write_receipt(lab.root, lab.manifest, packages, files, check).read_text("utf-8"))
            self.assertEqual(receipt["lock_sha256"], sha((lab.root / ik.LOCK).read_bytes()))
        self.assertEqual((receipt["schema"], receipt["packages_pinned"], receipt["revision"], receipt["license"]),
                         (1, 3, REAL_MANIFEST["revision"], "Apache-2.0"))
        self.assertEqual((receipt["environment"], receipt["torch"], receipt["device"]), (".venv-kokoro", "2.12.0+cu130", "cuda"))
        self.assertEqual(receipt["files"], ["config.json", "kokoro-v1_0.pth", "README.md", "voices/bm_george.pt"])
        self.assertEqual(receipt["checkpoint_bytes"], sum(len(FakeLab.CONTENTS[path]) for path in receipt["files"]))
        self.assertEqual(receipt["speech_check"], {"voice": "af_heart", "load_seconds": 4.2, "total_seconds": 1.1, "audio_seconds": 2.6})
        self.assertRegex(receipt["installed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertNotIn(folder, json.dumps(receipt), "no folder names of the person's computer in the receipt")


class RunTests(ServerCase):
    """The whole install with venv, pip, the model host and Kokoro replaced by stand-ins."""
    LOCKED = {"alpha": "1.0", "torch": "2.12.0", "en-core-web-sm": "3.8.0"}

    def setUp(self):
        super().setUp()
        self.lab = FakeLab(self.base)
        self.server.files = self.lab.served()
        self.installed = {}
        self.calls = []
        self.pip_options = []
        self.check_devices = []
        wav = self.base / "out" / "kokoro" / "install-check.wav"
        self.check = {"voice": "af_heart", "problem": None, "load_seconds": 4.2, "total_seconds": 1.1, "audio_seconds": 2.6, "wav": wav}

        def venv(root, **kwargs):
            self.calls.append("venv")
            self.make_environment({"pip": "26.2.2"})

        def pip(root, **kwargs):
            self.calls.append("pip")
            self.pip_options.append(kwargs)
            self.installed.update(self.LOCKED)

        def speak(root, **kwargs):
            self.calls.append("check")
            self.check_devices.append(kwargs.get("device"))
            wav.parent.mkdir(parents=True, exist_ok=True)
            return self.check

        for target, value in (("ROOT", self.base), ("HOST", self.server.address)):
            patch = mock.patch.object(ik, target, value)
            patch.start()
            self.addCleanup(patch.stop)
        for name, replacement in (("environment_versions", lambda root, **kwargs: dict(self.installed)),
                                  ("make_environment", venv), ("install_packages", pip), ("run_speech_check", speak),
                                  ("interpreter_problems", lambda root: []), ("long_path_problems", lambda root: []),
                                  ("space_problems", lambda root, need: []), ("nvidia_problems", lambda: []),
                                  ("package_problems", lambda different, **kwargs: [f"{item['name']} is {have}"
                                                                                    for item, have in different])):
            patch = mock.patch.object(ik, name, replacement)
            patch.start()
            self.addCleanup(patch.stop)

    def make_environment(self, packages):
        python = self.base / ".venv-kokoro" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"")
        self.installed.update(packages)

    def args(self, **options):
        return argparse.Namespace(**{"yes": False, "dry_run": False, "voices": ["supported"], "checkpoint_only": False,
                                     "nvidia": False, **options})

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO()):
            status = ik.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def untouched(self):
        self.assertEqual(self.calls, [])
        self.assertFalse((self.base / "hf-cache").exists())
        self.assertFalse((self.base / "out").exists())
        self.assertFalse((self.base / ".venv-kokoro").exists())

    def test_a_dry_run_shows_the_plan_and_changes_nothing(self):
        status, out, err = self.run_main("--dry-run")
        self.assertEqual((status, err), (0, ""))
        self.assertIn("Kokoro 82M on the CPU", out)
        self.assertIn("This was a dry run, so nothing was changed.", out)
        self.untouched()
        self.assertEqual(self.server.requests, [])

    def test_without_a_terminal_and_without_yes_nothing_happens(self):
        status, out, err = self.run_main()
        self.assertEqual(status, 1)
        self.assertIn("not an interactive terminal", err)
        self.assertIn("Add --yes", err)
        self.untouched()

    def test_asked_in_a_terminal_only_yes_goes_ahead(self):
        lines = []
        for answer in ("", "no", "y please"):
            self.assertEqual(ik.run(self.args(), say=lines.append, ask=lambda prompt, answer=answer: answer, interactive=True), 1)
        self.assertIn("Nothing was changed.", lines)
        self.untouched()
        def closed(prompt):
            raise EOFError

        self.assertEqual(ik.run(self.args(), say=lines.append, ask=closed, interactive=True), 1)
        self.untouched()
        self.assertEqual(ik.run(self.args(), say=lines.append, ask=lambda prompt: " Yes ", interactive=True), 0)
        self.assertEqual(self.calls, ["venv", "pip", "check"])

    def test_the_whole_install_runs_in_order_and_ends_with_a_receipt(self):
        status, out, err = self.run_main("--yes")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["venv", "pip", "check"])
        self.assertEqual((self.pip_options, self.check_devices), ([{"nvidia": False}], ["cpu"]))
        self.assertIn("Making .venv-kokoro:", out)
        self.assertIn("Installing the Python packages into .venv-kokoro", out)
        self.assertIn("[1/6] config.json", out)
        self.assertIn("Kokoro is installed and spoke a test sentence (2.6 s of audio in 1.1 s after a 4 s load).", out)
        self.assertIn("Listen to it: out\\kokoro\\install-check.wav", out)
        self.assertIn("press Test.", out)
        receipt = json.loads((self.base / "out" / "kokoro" / "installation.json").read_text("utf-8"))
        self.assertEqual(receipt["revision"], REAL_MANIFEST["revision"])
        self.assertEqual(receipt["files"][-1], "voices/ef_dora.pt")
        self.assertEqual(len(receipt["files"]), 6)
        self.assertTrue((self.lab.snapshot() / "voices" / "af_heart.pt").is_file())

    def test_on_request_the_cuda_build_is_installed_and_checked_on_the_graphics_card(self):
        status, out, err = self.run_main("--yes", "--nvidia")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual((self.pip_options, self.check_devices), ([{"nvidia": True}], ["cuda"]))
        self.assertIn("Kokoro 82M on an NVIDIA graphics card", out)
        self.assertIn("spoke a test sentence on the graphics card", out)

    def test_the_cuda_build_is_refused_on_a_computer_without_an_nvidia_driver(self):
        with mock.patch.object(ik, "nvidia_problems", lambda: ["no NVIDIA driver here"]):
            status, _out, err = self.run_main("--yes", "--nvidia")
        self.assertEqual(status, 1)
        self.assertIn("no NVIDIA driver here", err)
        self.untouched()

    def test_packages_that_are_already_installed_are_left_alone(self):
        self.make_environment(self.LOCKED)
        status, out, _err = self.run_main("--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["check"])
        self.assertIn("all 3 are already installed in .venv-kokoro", " ".join(out.split()))

    def test_a_different_version_of_a_pinned_package_stops_everything_before_any_change(self):
        self.make_environment({"torch": "2.12.0+rocm7.14.0"})
        status, out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("This computer cannot take the plan yet:", err)
        self.assertIn("torch is 2.12.0+rocm7.14.0", err)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.base / "hf-cache").exists())
        self.assertEqual(self.server.requests, [])

    def test_the_checkpoint_alone_can_be_added_to_an_environment_that_has_its_own_packages(self):
        self.make_environment({"torch": "2.12.0+rocm7.14.0", "kokoro": "0.9.4"})
        status, out, _err = self.run_main("--yes", "--checkpoint-only")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["check"])
        self.assertIn("left alone (--checkpoint-only)", out)
        self.assertTrue((self.lab.snapshot() / "kokoro-v1_0.pth").is_file())

    def test_the_checkpoint_alone_is_refused_when_there_is_no_environment_to_add_it_to(self):
        status, _out, err = self.run_main("--yes", "--checkpoint-only")
        self.assertEqual(status, 1)
        self.assertIn("there is no .venv-kokoro here", " ".join(err.split()))
        self.untouched()

    def test_a_computer_that_fails_a_check_is_told_why_and_nothing_is_changed(self):
        with mock.patch.object(ik, "interpreter_problems", lambda root: ["Run it with this folder's own Python."]), \
                mock.patch.object(ik, "space_problems", lambda root, need: ["About 3 GB of free disk space is needed."]):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("  - Run it with this folder's own Python.\n  - About 3 GB of free disk space is needed.", err)
        self.untouched()

    def test_a_pip_failure_stops_before_the_model_is_fetched(self):
        def failing(root, **kwargs):
            raise ik.InstallError("pip stopped with exit code 1.")

        with mock.patch.object(ik, "install_packages", failing):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("error: pip stopped with exit code 1.", err)
        self.assertFalse((self.base / "hf-cache").exists())

    def test_pip_that_finishes_without_installing_everything_is_caught(self):
        with mock.patch.object(ik, "install_packages", lambda root, **kwargs: None):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("pip finished but 3 packages are still missing, for example alpha", err)

    def test_a_failed_speech_check_leaves_the_files_and_writes_no_receipt(self):
        def silent(root, **kwargs):
            raise ik.InstallError("The packages and model files are in place, but Kokoro did not load or speak:\nboom")

        with mock.patch.object(ik, "run_speech_check", silent):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("did not load or speak", err)
        self.assertTrue((self.lab.snapshot() / "kokoro-v1_0.pth").is_file())
        self.assertFalse((self.base / "out" / "kokoro" / "installation.json").exists())

    def test_a_second_run_downloads_and_installs_nothing_new(self):
        self.assertEqual(self.run_main("--yes")[0], 0)
        asked, self.calls[:] = len(self.server.requests), []
        status, _out, _err = self.run_main("--yes")
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, ["check"])
        self.assertEqual(len(self.server.requests), asked)

    def test_all_voices_are_fetched_on_request_and_the_extra_package_note_is_shown(self):
        status, out, _err = self.run_main("--yes", "--voices", "all")
        self.assertEqual(status, 0)
        self.assertIn("The Japanese and Mandarin Chinese voices also need Misaki language packages", out)
        self.assertEqual(len(list((self.lab.snapshot() / "voices").glob("*.pt"))), 5)

    def test_an_unknown_voice_is_refused_before_anything_happens(self):
        status, _out, err = self.run_main("--yes", "--voices", "xx_nobody")
        self.assertEqual(status, 1)
        self.assertIn("no voice named 'xx_nobody'", err)
        self.untouched()

    def test_a_file_the_system_will_not_let_it_write_ends_with_an_error_line_not_a_traceback(self):
        with mock.patch.object(ik, "write_receipt", side_effect=PermissionError(13, "Access is denied", "installation.json")):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 1)
        self.assertIn("error: [Errno 13] Access is denied", err)
        self.assertNotIn("Traceback", err)

    def test_control_c_stops_politely(self):
        def interrupted(root, **kwargs):
            raise KeyboardInterrupt

        with mock.patch.object(ik, "install_packages", interrupted):
            status, _out, err = self.run_main("--yes")
        self.assertEqual(status, 130)
        self.assertIn("Run the installer again to continue", err)


if __name__ == "__main__":
    unittest.main()
