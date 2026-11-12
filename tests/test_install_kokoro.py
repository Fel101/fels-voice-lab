"""tools/install_kokoro.py: the pinned Kokoro CPU install. Nothing here reaches the real network, pip or the real Kokoro."""
import argparse
import contextlib
import hashlib
import http.server
import io
import json
import os
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
from tests.test_amd_build import DEVICES, R9700, RADEON_780M, RADEON_VII  # noqa: E402
from tools import amd_build as amd  # noqa: E402
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
    """A lab folder holding a three-package lock, its GPU swap, the shipped AMD list and a small stand-in for the model files, at
    the real revision."""
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
        (self.root / amd.LOCK).write_bytes((ROOT / amd.LOCK).read_bytes())
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

    def test_amds_build_swaps_only_torch_and_adds_its_runtime_and_the_code_for_the_cards_chip(self):
        cpu = ik.read_lock(ROOT / ik.LOCK)
        packages = ik.pinned_packages(ROOT, card=R9700)
        self.assertEqual([item["key"] for item in packages[:len(cpu)]], [item["key"] for item in cpu])
        torch = next(item for item in packages if item["key"] == "torch")
        self.assertEqual(torch["version"], "2.12.0+rocm7.14.0")
        self.assertEqual([item for item in packages[:len(cpu)] if item["key"] != "torch"], [item for item in cpu if item["key"] != "torch"])
        self.assertEqual(sorted(item["key"] for item in packages[len(cpu):]),
                         ["amd-torch-device-gfx12-0", "amd-torch-device-gfx1201", "rocm", "rocm-bootstrap", "rocm-sdk-core",
                          "rocm-sdk-device-gfx1201", "rocm-sdk-libraries"])
        self.assertNotIn("torchaudio", {item["key"] for item in packages}, "Kokoro needs no torchaudio")

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
        self.assertEqual(first_setup.MODELS["kokoro"], {"engine": "kokoro", "script": "tools/install_kokoro.py", "gpu": True})
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
        for todo, only, exists, nvidia, card in ((len(packages), False, False, False, None), (3, False, True, False, None),
                                                 (0, False, True, False, None), (0, True, True, False, None),
                                                 (len(packages), False, False, True, None), (len(packages), False, False, False, R9700)):
            text = ik.plan_text(REAL_MANIFEST, packages, todo, files, files, checkpoint_only=only, environment_exists=exists,
                                nvidia=nvidia, card=card, cards=[RADEON_780M, R9700])
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
        self.assertEqual(ik.nvidia_problems(present=True, cards=[]), [])
        problem = ik.nvidia_problems(present=False)[0]
        self.assertIn("no NVIDIA driver", problem)
        self.assertIn("leave out --nvidia", problem)
        with mock.patch.dict("os.environ", {"SystemRoot": str(ROOT / "no-such-windows")}):
            self.assertFalse(ik.nvidia_driver_present())

    def test_the_cuda_13_build_runs_on_rtx_20_cards_and_newer_with_driver_580_or_later(self):
        def card(name, capability, driver=(581, 80)):
            return {"name": name, "capability": capability, "driver": driver}

        for fine in (card("NVIDIA GeForce RTX 5070 Laptop GPU", (12, 0)), card("NVIDIA GeForce RTX 2060", (7, 5)),
                     card("NVIDIA GeForce RTX 4090", None, None)):
            with self.subTest(card=fine["name"]):
                self.assertEqual(ik.nvidia_problems(present=True, cards=[fine]), [])
        old_card = ik.nvidia_problems(present=True, cards=[card("NVIDIA GeForce GTX 1080", (6, 1))])
        self.assertEqual(len(old_card), 1)
        self.assertIn("Kokoro's NVIDIA build (PyTorch with CUDA 13.0) has no code for the NVIDIA GeForce GTX 1080 (compute "
                      "capability 6.1); it runs on GeForce GTX 16 and RTX 20 series cards and newer. Leave out --nvidia", old_card[0])
        old_driver = ik.nvidia_problems(present=True, cards=[card("NVIDIA GeForce RTX 5070 Laptop GPU", (12, 0), (572, 83))])
        self.assertEqual(len(old_driver), 1)
        self.assertIn("needs NVIDIA driver 580 or later, and this computer has 572.83. Update the driver", old_driver[0])
        # Without nvidia-smi the generation is unknown and only the driver Windows lists is checked.
        self.assertEqual(ik.card_problems([card("NVIDIA GeForce GTX 1080", None, (581, 80))]), [])

    def test_gpu_asks_for_the_build_of_this_computers_graphics_card_and_nvidia_for_nvidias_alone(self):
        def build(graphics=("amd", [R9700]), **options):
            args = argparse.Namespace(**{"gpu": False, "nvidia": False, **options})
            with mock.patch.object(ik, "graphics", lambda: graphics):
                return ik.requested_build(args)

        self.assertEqual(build(), (None, []))
        self.assertEqual(build(gpu=True), ("amd", [R9700]))
        self.assertEqual(build(gpu=True, graphics=("nvidia", [])), ("nvidia", []))
        self.assertEqual(build(nvidia=True), ("nvidia", []))
        self.assertEqual(build(gpu=True, nvidia=True), ("nvidia", []))
        with self.assertRaisesRegex(ik.InstallError, "neither an NVIDIA driver .* nor an AMD graphics card"):
            build(gpu=True, graphics=(None, []))
        self.assertIn("For an AMD Radeon card, use --gpu instead", ik.nvidia_problems(present=False)[0])

    def test_amds_build_runs_on_a_card_whose_chip_it_has_code_for(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)
            self.assertEqual(ik.amd_card_problems([RADEON_780M, R9700], root=lab.root, environ={}), [])
            [problem] = ik.amd_card_problems([RADEON_VII], root=lab.root, environ={})
        self.assertIn("AMD's ROCm 7.14 build of PyTorch 2.12 has no code for the AMD Radeon VII (gfx906)", problem)
        refusal = " ".join(ik.amd_refusal("Kokoro's GPU build", [problem]).split())
        self.assertTrue(refusal.startswith("This computer cannot take Kokoro's GPU build yet: - AMD's ROCm 7.14 build"), refusal)
        self.assertIn("- Leave out --gpu to install the CPU build, which runs on the processor.", refusal)

    def test_a_gpu_build_replaces_only_the_cpu_builds_pytorch_in_an_install_that_has_it(self):
        cpu = ik.read_lock(ROOT / ik.LOCK)
        installed = {item["key"]: item["version"] for item in cpu}
        for packages in (ik.pinned_packages(ROOT, card=R9700), ik.pinned_packages(ROOT, nvidia=True)):
            switching, conflicts, switch_only = ik.gpu_switch(packages, cpu, installed)
            self.assertEqual([(item["key"], have) for item, have in switching], [("torch", "2.12.0")])
            self.assertEqual((conflicts, switch_only), ([], True))
        # A package of someone's own still stops the installer, and an environment without Kokoro is not a switch.
        switching, conflicts, switch_only = ik.gpu_switch(ik.pinned_packages(ROOT, card=R9700), cpu, {**installed, "spacy": "3.0.0"})
        self.assertEqual(([item["key"] for item, _have in conflicts], switch_only), (["spacy"], False))
        switching, conflicts, switch_only = ik.gpu_switch(ik.pinned_packages(ROOT, card=R9700), cpu, {"torch": "2.12.0"})
        self.assertEqual((len(switching), conflicts, switch_only), (1, [], False))

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

    def test_the_nvidia_plan_for_a_cpu_install_replaces_pytorch_and_nothing_else(self):
        files = ik.checkpoint_files(REAL_MANIFEST, ik.choose_voices(REAL_MANIFEST, ["supported"]))
        packages = ik.pinned_packages(ROOT, nvidia=True)
        cpu = ik.read_lock(ROOT / ik.LOCK)
        installed = {item["key"]: item["version"] for item in cpu}
        _missing, different = ik.package_state(packages, installed)
        switching = ik.build_switch(different, cpu, ik.read_lock(ROOT / ik.NVIDIA_LOCK))
        self.assertEqual([(item["key"], have) for item, have in switching], [("torch", "2.12.0")])
        text = self.flat(ik.plan_text(REAL_MANIFEST, packages, 1, files, [], checkpoint_only=False, environment_exists=True,
                                      nvidia=True, switching=switching))
        self.assertIn("Kokoro is already installed in .venv-kokoro, and its CPU build of PyTorch is replaced by the CUDA 13.0 build "
                      "of PyTorch for NVIDIA graphics cards: torch 2.12.0 becomes 2.12.0+cu130. About "
                      f"{ik.mb_text(ik.NVIDIA_EXTRA_DOWNLOAD_MB)} to download", text)
        installed["spacy"] = "3.0.0"    # a package of someone's own: it still stops the installer
        _missing, different = ik.package_state(packages, installed)
        conflicts = [pair for pair in different if pair not in ik.build_switch(different, cpu, ik.read_lock(ROOT / ik.NVIDIA_LOCK))]
        self.assertEqual([item["key"] for item, _have in conflicts], ["spacy"])

    def test_the_nvidia_plan_names_the_cuda_build_its_size_and_that_it_is_untried(self):
        text = self.flat(self.plan(nvidia=True))
        for part in ("Kokoro 82M on an NVIDIA graphics card", "the CUDA 13.0 build of PyTorch for NVIDIA graphics cards",
                     f"About {ik.mb_text(ik.PACKAGES_DOWNLOAD_MB + ik.NVIDIA_EXTRA_DOWNLOAD_MB)} to download",
                     "requirements/kokoro-cpu.txt and requirements/kokoro-nvidia.txt", "speaks one sentence, which you can play "
                     "afterwards, on the graphics card.", "GTX 16 or RTX 20 series card or newer", "driver of version 580",
                     "hardware report on GitHub"):
            self.assertIn(part, text)

    def test_the_amd_plan_names_the_card_its_chip_amds_list_and_which_card_the_lab_uses(self):
        files = ik.checkpoint_files(REAL_MANIFEST, ik.choose_voices(REAL_MANIFEST, ["supported"]))
        packages = ik.pinned_packages(ROOT, card=R9700)
        text = self.flat(ik.plan_text(REAL_MANIFEST, packages, len(packages), files, files, checkpoint_only=False,
                                      card=R9700, cards=[RADEON_780M, R9700]))
        download, disk = ik.packages_mb(card=R9700)
        self.assertEqual((download, disk), amd.sizes_mb("gfx1201", ik.OTHER_DOWNLOAD_MB, ik.OTHER_DISK_MB))
        for part in ("Kokoro 82M on an AMD graphics card", f"{len(packages)} of {len(packages)} to install there",
                     "AMD's ROCm 7.14 build of PyTorch 2.12 with the code for the AMD Radeon AI PRO R9700 (gfx1201)",
                     f"About {ik.mb_text(download)} to download and {ik.mb_text(disk)} on disk",
                     "requirements/kokoro-cpu.txt and requirements/amd-rocm.txt",
                     "speaks one sentence, which you can play afterwards, on the AMD Radeon AI PRO R9700.",
                     "AMD Software: Adrenalin Edition", "under AMD's own licence terms", "hardware report on GitHub",
                     "This computer has 2 AMD graphics cards", 'runtime.local.json ("hip_visible_devices")'):
            self.assertIn(part, text)
        self.assertNotIn("NVIDIA", text)

    def test_the_amd_plan_for_a_cpu_install_replaces_pytorch_and_adds_amds_runtime(self):
        files = ik.checkpoint_files(REAL_MANIFEST, ik.choose_voices(REAL_MANIFEST, ["supported"]))
        cpu = ik.read_lock(ROOT / ik.LOCK)
        packages = ik.pinned_packages(ROOT, card=R9700)
        installed = {item["key"]: item["version"] for item in cpu}
        missing, different = ik.package_state(packages, installed)
        switching, _conflicts, switch_only = ik.gpu_switch(packages, cpu, installed)
        text = self.flat(ik.plan_text(REAL_MANIFEST, packages, len(missing) + len(different), files, [], checkpoint_only=False,
                                      environment_exists=True, switching=switching, switch_only=switch_only, card=R9700,
                                      cards=[R9700]))
        download, disk = ik.switch_mb(card=R9700)
        self.assertEqual(disk, amd.sizes_mb("gfx1201", 0, 0)[1] - (ik.PACKAGES_DISK_MB - ik.OTHER_DISK_MB))
        self.assertIn("Kokoro is already installed in .venv-kokoro, and its CPU build of PyTorch is replaced by AMD's ROCm 7.14 "
                      "build of PyTorch 2.12 with the code for the AMD Radeon AI PRO R9700 (gfx1201): torch 2.12.0 becomes "
                      f"2.12.0+rocm7.14.0. About {ik.mb_text(download)} to download and {ik.mb_text(disk)} more on disk.", text)
        self.assertNotIn("This computer has", text, "one card: nothing to say about which one the Lab uses")

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

    def test_for_an_amd_card_pip_gets_the_wheels_then_builds_amds_source_package(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)
            ik.install_packages(lab.root, card=R9700,
                                runner=lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0))
            wheels, source = (Path(command[command.index("-r") + 1]) for command in calls)
            self.assertEqual((wheels, source), (lab.root / "out" / "kokoro" / amd.WHEELS, lab.root / "out" / "kokoro" / amd.SOURCE))
            lines = [line for line in wheels.read_text("utf-8").splitlines() if not line.startswith("#")]
            source_lines = [line for line in source.read_text("utf-8").splitlines() if not line.startswith("#")]
        self.assertEqual(lines[0], f"alpha==1.0 --hash=sha256:{HASH}")
        self.assertTrue(lines[1].startswith("torch @ https://repo.amd.com/rocm/whl-multi-arch/"), lines[1])
        self.assertIn("rocm-sdk-device-gfx1201", " ".join(lines))
        self.assertNotIn("gfx1100", " ".join(lines), "only the code for the card's chip")
        self.assertEqual([line.split()[0] for line in source_lines], ["rocm"])
        self.assertIn("--only-binary=:all:", calls[0])
        self.assertIn("--no-build-isolation", calls[1])

    def test_a_pip_failure_says_to_fix_it_and_run_again(self):
        with self.assertRaisesRegex(ik.InstallError, "pip stopped with exit code 2.*run the installer again"):
            ik.install_packages(ROOT, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 2))


class SpeechCheckTests(unittest.TestCase):
    def check(self, stdout="", stderr="", code=0, error=None, device="cpu", **options):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if error:
                raise error
            return subprocess.CompletedProcess(command, code, stdout, stderr)

        with tempfile.TemporaryDirectory() as folder:
            return ik.run_speech_check(folder, device=device, runner=runner, **options), calls, Path(folder)

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
        with self.assertRaisesRegex(ik.InstallError, r"(?s)CUDA driver version is insufficient.*version 580 or later.*"
                                                     r"without --gpu or --nvidia"):
            self.check(stderr="RuntimeError: CUDA driver version is insufficient for CUDA runtime version", code=1, device="cuda")

    def test_on_an_amd_card_the_check_is_given_its_number_and_a_failure_points_at_amds_driver(self):
        with mock.patch.dict("os.environ", {"HIP_VISIBLE_DEVICES": "0"}):
            _result, calls, _folder = self.check(stdout=self.GOOD, device="cuda", amd_card=True, number=1)
            self.assertEqual((calls[0][0][-1], calls[0][1]["env"]["HIP_VISIBLE_DEVICES"]), ("cuda", "1"))
            _result, calls, _folder = self.check(stdout=self.GOOD, device="cuda", amd_card=True)
            self.assertEqual(calls[0][1]["env"]["HIP_VISIBLE_DEVICES"], "0", "no number: the setting stands")
        with self.assertRaises(ik.InstallError) as caught:
            self.check(stderr="RuntimeError: HIP error: invalid device function", code=1, device="cuda", amd_card=True)
        self.assertIn("Update the graphics driver (AMD Software: Adrenalin Edition", str(caught.exception))
        self.assertNotIn("nvidia.com", str(caught.exception))

    # What a model printed on the laptop whose NVIDIA card was switched off: the sox package's warning, then the traceback.
    SOX = ("SoX could not be found!\n\n    If you do not have SoX, proceed here:\n     - - - http://sox.sourceforge.net/\n\n"
           "    If you do (or think that you should) have SoX, double-check your\n    path variables.\n    \n")
    NO_CARD = ('Traceback (most recent call last):\n  File "<string>", line 13, in <module>\n'
               "RuntimeError: No CUDA GPUs are available\n")

    def test_the_error_comes_first_without_the_sox_warning_and_all_of_the_output_is_kept(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", self.SOX + self.NO_CARD)

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ik.InstallError) as caught:
                ik.run_speech_check(folder, device="cuda", runner=runner)
            log = Path(folder) / "out" / "kokoro" / "install-check.log"
            self.assertEqual(log.read_text(encoding="utf-8"), self.SOX + self.NO_CARD)
        text = str(caught.exception)
        self.assertTrue(text.startswith("The packages and model files are in place, but Kokoro did not load or speak:\n"
                                        "RuntimeError: No CUDA GPUs are available\n\nThe end of its output (all of it is in "
                                        "out\\kokoro\\install-check.log):\nTraceback"), text)
        self.assertNotIn("SoX", text)
        # The card, not the driver: the driver was new enough.
        for part in ("PyTorch found no NVIDIA card it could use", "switched off", "Code 45", "run the installer again"):
            self.assertIn(part, text)
        self.assertNotIn("update the driver", text)

    def test_each_nvidia_failure_gets_its_own_hint_and_an_unknown_one_none(self):
        def hint(output, **options):
            return ik.check_failure_hint(output, nvidia=True, **options)

        for output in ("RuntimeError: No CUDA GPUs are available", "ValueError: CUDA graphs require CUDA device",
                       "RuntimeError: CUDA was selected but torch.cuda.is_available() is False.",
                       "CUDA_ERROR_NO_DEVICE: no CUDA-capable device is detected"):
            self.assertIn("PyTorch found no NVIDIA card it could use", hint(output), output)
        self.assertIn("driver is too old", hint("RuntimeError: The NVIDIA driver on your system is too old (found 12020)."))
        no_code = "torch.AcceleratorError: CUDA error: no kernel image is available for execution on the device"
        self.assertIn("has no code for this graphics card: CUDA 13.0 needs", hint(no_code))
        self.assertNotIn("without --gpu or --nvidia", hint(no_code))
        self.assertIn("without --gpu or --nvidia", hint(no_code, cpu=True))
        self.assertIn("ran out of memory", hint("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB"))
        self.assertIn("Breeze needs", hint("CUDA out of memory", memory="Breeze needs about 10 GB."))
        self.assertIn("CPU build of PyTorch", hint("AssertionError: Torch not compiled with CUDA enabled"))
        self.assertEqual(hint("RuntimeError: CUDA error: an illegal memory access was encountered"), "")
        self.assertEqual(ik.check_failure_hint("RuntimeError: No CUDA GPUs are available"), "")  # the CPU build: no guess

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


class NvidiaCardTests(unittest.TestCase):
    """A card NVIDIA's driver cannot start CUDA on, a card whose memory nobody could read, and PyTorch's own check after pip."""

    # The laptop's RTX 5070 as hardware.nvidia_cards gives it while the card is switched off.
    OFF = {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": None, "driver": (617, 14), "memory_mib": None,
           "unavailable": "CUDA_ERROR_NO_DEVICE: no CUDA-capable device is detected"}

    def test_a_card_the_driver_cannot_start_cuda_on_stops_the_install_before_any_download(self):
        with mock.patch.dict("os.environ"):
            os.environ.pop("CUDA_VISIBLE_DEVICES", None)
            [problem] = ik.nvidia_problems(present=True, cards=[self.OFF])
        self.assertTrue(problem.startswith(
            "This computer has an NVIDIA driver, but CUDA cannot use the NVIDIA GeForce RTX 5070 Laptop GPU: the driver says "
            '"CUDA_ERROR_NO_DEVICE: no CUDA-capable device is detected".'), problem)
        for part in ("switched off", "GIGABYTE Control Center", "Code 45", "Or leave out --nvidia for the CPU build."):
            self.assertIn(part, problem)
        self.assertNotIn("CUDA_VISIBLE_DEVICES", problem)
        self.assertNotIn("Update the driver", problem)

    def test_a_setting_that_hides_the_card_is_named_and_another_driver_error_is_not_blamed_on_a_switch(self):
        hidden = ik.unavailable_text(self.OFF, environ={"CUDA_VISIBLE_DEVICES": "-1"})
        self.assertIn('CUDA_VISIBLE_DEVICES is set to "-1" in this window', hidden)
        broken = ik.unavailable_text({**self.OFF, "unavailable": "CUDA_ERROR_OPERATING_SYSTEM: OS call failed"}, environ={})
        self.assertIn("Restart the computer", broken)
        self.assertNotIn("switched off", broken)

    def test_a_memory_nobody_could_read_is_said_rather_than_skipped(self):
        card = {"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": (12, 0), "driver": (617, 14), "memory_mib": None}
        [problem] = ik.nvidia_card_problems([card], build="Breeze's NVIDIA build", cards_text="newer cards", memory_gb=10)
        self.assertIn("could not read how much memory the NVIDIA GeForce RTX 5070 Laptop GPU has", problem)
        self.assertEqual(ik.nvidia_card_problems([card], build="Kokoro's NVIDIA build", cards_text="newer cards"), [])

    def test_a_build_has_code_for_its_own_generations_and_the_later_versions_of_each(self):
        arches = ["sm_75", "sm_80", "sm_86", "sm_90", "sm_100", "sm_120"]  # PyTorch 2.12.0+cu130, as the laptop listed it
        self.assertTrue(ik.has_code_for(arches, (12, 0)))
        self.assertTrue(ik.has_code_for(arches, (8, 9)))     # sm_86 runs on an RTX 40
        self.assertFalse(ik.has_code_for(arches, (6, 1)))    # a GTX 10
        self.assertFalse(ik.has_code_for([], (12, 0)))
        self.assertTrue(ik.has_code_for(["sm_80", "compute_90"], (12, 0)))    # PTX, which the driver compiles

    def test_pytorch_is_asked_to_start_cuda_and_its_own_error_stops_the_install(self):
        def runner(stdout="", stderr="", code=0):
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, code, stdout, stderr)

            return run, calls

        def answer(name, capability):
            return json.dumps({"torch": "2.12.0+cu130", "arches": ["sm_75", "sm_80", "sm_86", "sm_90", "sm_100", "sm_120"],
                               "cards": [{"name": name, "capability": capability}]})

        with tempfile.TemporaryDirectory() as folder:
            run, calls = runner(stdout=answer("NVIDIA GeForce RTX 5070 Laptop GPU", [12, 0]))
            found = ik.check_torch_cuda(folder, ".venv-kokoro", runner=run)
            self.assertEqual(found["cards"][0]["name"], "NVIDIA GeForce RTX 5070 Laptop GPU")
            self.assertEqual(calls[0][:3], [str(Path(folder) / ".venv-kokoro" / "Scripts" / "python.exe"), "-I", "-c"])
            self.assertIn("torch.cuda.init()", calls[0][3])
            run, _calls = runner(stderr=SpeechCheckTests.SOX + SpeechCheckTests.NO_CARD, code=1)
            with self.assertRaises(ik.InstallError) as caught:
                ik.check_torch_cuda(folder, ".venv-kokoro", runner=run)
            text = str(caught.exception)
            for part in ("could not use the NVIDIA card, so the model files were not fetched",
                         "RuntimeError: No CUDA GPUs are available", "switched off"):
                self.assertIn(part, text)
            run, _calls = runner(stdout=answer("NVIDIA GeForce GTX 1080", [6, 1]))
            no_code = (r"(?s)has no code for the NVIDIA GeForce GTX 1080 \(compute capability 6\.1\), so the model files "
                       r"were not fetched.*without --gpu")
            with self.assertRaisesRegex(ik.InstallError, no_code):
                ik.check_torch_cuda(folder, ".venv-kokoro", cpu=True, runner=run)


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
        self.assertNotIn("build", receipt)

    def test_an_amd_install_records_the_build_amds_list_and_the_card(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = FakeLab(folder)
            (lab.root / "out" / "kokoro").mkdir(parents=True)
            files = ik.checkpoint_files(lab.manifest, ik.choose_voices(lab.manifest, ["bm_george"]))
            check = {"voice": "af_heart", "problem": None, "load_seconds": 6.9, "total_seconds": 0.8, "audio_seconds": 5.3,
                     "torch": "2.12.0+rocm7.14.0", "device": "cuda"}
            receipt = json.loads(ik.write_receipt(lab.root, lab.manifest, ik.pinned_packages(lab.root, card=R9700), files, check,
                                                  card=R9700, number=1).read_text("utf-8"))
            self.assertEqual(receipt["amd_lock_sha256"], sha((lab.root / amd.LOCK).read_bytes()))
        self.assertEqual((receipt["build"], receipt["torch"], receipt["packages_pinned"]), ("rocm", "2.12.0+rocm7.14.0", 10))
        self.assertEqual(receipt["gpu"], {"name": "AMD Radeon AI PRO R9700", "chip": "gfx1201", "driver": "32.0.31041.1004",
                                          "hip_visible_devices": 1})


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
        self.check_options = []
        self.graphics = ("nvidia", [])
        self.nvidia = []
        wav = self.base / "out" / "kokoro" / "install-check.wav"
        self.check = {"voice": "af_heart", "problem": None, "load_seconds": 4.2, "total_seconds": 1.1, "audio_seconds": 2.6, "wav": wav}
        environment = mock.patch.dict("os.environ")
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(amd.VARIABLE, None)

        def venv(root, **kwargs):
            self.calls.append("venv")
            self.make_environment({"pip": "26.2.2"})

        def pip(root, *, nvidia=False, card=None, **kwargs):
            self.calls.append("pip")
            self.pip_options.append({"nvidia": nvidia, "card": card["chip"] if card else None})
            self.installed.update({item["key"]: item["version"] for item in ik.pinned_packages(root, nvidia=nvidia, card=card)})

        def speak(root, **kwargs):
            self.calls.append("check")
            self.check_devices.append(kwargs.get("device"))
            self.check_options.append({key: value for key, value in kwargs.items() if key != "device"})
            wav.parent.mkdir(parents=True, exist_ok=True)
            return self.check

        for target, value in (("ROOT", self.base), ("HOST", self.server.address)):
            patch = mock.patch.object(ik, target, value)
            patch.start()
            self.addCleanup(patch.stop)
        def torch_cuda(root, environment, **kwargs):
            self.calls.append("cuda")
            self.cuda_options.append(kwargs)
            return {"torch": "2.12.0+cu130", "arches": ["sm_75", "sm_120"],
                    "cards": [{"name": "NVIDIA GeForce RTX 5070 Laptop GPU", "capability": [12, 0]}]}

        self.cuda_options = []
        for name, replacement in (("environment_versions", lambda root, **kwargs: dict(self.installed)),
                                  ("make_environment", venv), ("install_packages", pip), ("run_speech_check", speak),
                                  ("check_torch_cuda", torch_cuda),
                                  ("interpreter_problems", lambda root: []), ("long_path_problems", lambda root: []),
                                  ("space_problems", lambda root, need: []), ("nvidia_problems", lambda: list(self.nvidia)),
                                  ("graphics", lambda: (self.graphics[0], list(self.graphics[1]))),
                                  ("package_problems", lambda different, **kwargs: [f"{item['name']} is {have}"
                                                                                    for item, have in different])):
            patch = mock.patch.object(ik, name, replacement)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(amd, "hip_devices", lambda root, environment: [dict(device) for device in DEVICES])
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
        self.assertEqual((self.pip_options, self.check_devices), ([{"nvidia": False, "card": None}], ["cpu"]))
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
        self.assertEqual((self.pip_options, self.check_devices), ([{"nvidia": True, "card": None}], ["cuda"]))
        # PyTorch is asked to start CUDA between pip and the model files, so a card it cannot use stops it before the download.
        self.assertEqual(self.calls, ["venv", "pip", "cuda", "check"])
        self.assertEqual(self.cuda_options, [{"cuda_needs": ik.CUDA_NEEDS, "cpu": True}])
        self.assertLess(out.index("PyTorch 2.12.0+cu130 sees the NVIDIA GeForce RTX 5070 Laptop GPU."), out.index("[1/6]"))
        self.assertIn("Kokoro 82M on an NVIDIA graphics card", out)
        self.assertIn("spoke a test sentence on the graphics card", out)

    def test_the_cuda_build_is_refused_on_a_computer_without_an_nvidia_driver(self):
        with mock.patch.object(ik, "nvidia_problems", lambda: ["no NVIDIA driver here"]):
            status, _out, err = self.run_main("--yes", "--nvidia")
        self.assertEqual(status, 1)
        self.assertIn("no NVIDIA driver here", err)
        self.untouched()

    def test_gpu_on_a_computer_with_an_nvidia_driver_is_the_cuda_build(self):
        status, out, err = self.run_main("--yes", "--gpu")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual((self.pip_options, self.check_devices), ([{"nvidia": True, "card": None}], ["cuda"]))
        self.assertIn("Kokoro 82M on an NVIDIA graphics card", out)

    def test_gpu_on_an_amd_card_installs_amds_build_and_checks_it_on_that_card(self):
        self.graphics = ("amd", [RADEON_780M, R9700])
        self.check = {**self.check, "torch": "2.12.0+rocm7.14.0", "device": "cuda"}
        status, out, err = self.run_main("--yes", "--gpu")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["venv", "pip", "check"])
        self.assertEqual((self.pip_options, self.check_devices), ([{"nvidia": False, "card": "gfx1201"}], ["cuda"]))
        self.assertEqual(self.check_options, [{"amd_card": True, "number": 1}])
        text = " ".join(out.split())
        self.assertIn("Kokoro 82M on an AMD graphics card", text)
        self.assertIn("with the code for the AMD Radeon AI PRO R9700 (gfx1201)", text)
        self.assertIn('This computer has 2 AMD graphics cards, and the Lab uses the AMD Radeon AI PRO R9700: runtime.local.json now '
                      'says "hip_visible_devices": "1".', text)
        self.assertIn("Kokoro is installed and spoke a test sentence on the AMD Radeon AI PRO R9700 (2.6 s of audio", text)
        self.assertEqual(json.loads((self.base / "runtime.local.json").read_text("utf-8")), {"hip_visible_devices": "1"})
        receipt = json.loads((self.base / "out" / "kokoro" / "installation.json").read_text("utf-8"))
        self.assertEqual((receipt["build"], receipt["gpu"]["hip_visible_devices"]), ("rocm", 1))
        # A second run finds everything in place and the card already named.
        self.calls.clear()
        status, out, _err = self.run_main("--yes", "--gpu")
        self.assertEqual((status, self.calls), (0, ["check"]))
        self.assertNotIn("now says", out)

    def test_gpu_on_a_cpu_install_replaces_its_pytorch_with_amds_build(self):
        self.make_environment({item["key"]: item["version"] for item in ik.read_lock(self.base / ik.LOCK)})
        self.graphics = ("amd", [R9700])
        with mock.patch.object(amd, "hip_devices", lambda root, environment: [{**DEVICES[1], "index": 0}]):
            status, out, err = self.run_main("--yes", "--gpu")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.calls, ["pip", "check"])
        self.assertIn("Kokoro is already installed in .venv-kokoro, and its CPU build of PyTorch is replaced by AMD's ROCm 7.14 "
                      "build of PyTorch 2.12 with the code for the AMD Radeon AI PRO R9700 (gfx1201): torch 2.12.0 becomes "
                      "2.12.0+rocm7.14.0.", " ".join(out.split()))
        self.assertEqual(self.installed["torch"], "2.12.0+rocm7.14.0")
        self.assertFalse((self.base / "runtime.local.json").exists(), "one card: nothing to note")

    def test_gpu_on_an_amd_card_the_build_has_no_code_for_is_refused_before_any_change(self):
        self.graphics = ("amd", [RADEON_VII])
        status, out, err = self.run_main("--yes", "--gpu")
        self.assertEqual((status, out), (1, ""))
        text = " ".join(err.split())
        self.assertIn("This computer cannot take Kokoro's GPU build yet: - AMD's ROCm 7.14 build of PyTorch 2.12 has no code for "
                      "the AMD Radeon VII (gfx906)", text)
        self.assertIn("Leave out --gpu to install the CPU build", text)
        self.untouched()

    def test_gpu_on_a_computer_with_neither_card_is_refused_before_any_change(self):
        self.graphics = (None, [])
        status, _out, err = self.run_main("--yes", "--gpu")
        self.assertEqual(status, 1)
        self.assertIn("neither an NVIDIA driver", " ".join(err.split()))
        self.untouched()

    def test_a_card_a_setting_names_that_pytorch_numbers_otherwise_stops_before_the_download(self):
        self.graphics = ("amd", [RADEON_780M, R9700])
        os.environ[amd.VARIABLE] = "1"
        swapped = [{**DEVICES[0], "index": 1}, {**DEVICES[1], "index": 0}]
        with mock.patch.object(amd, "hip_devices", lambda root, environment: swapped):
            status, _out, err = self.run_main("--yes", "--gpu")
        self.assertEqual(status, 1)
        self.assertIn("error: HIP_VISIBLE_DEVICES names card 1, the AMD Radeon 780M Graphics in PyTorch's numbering",
                      " ".join(err.split()))
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.calls, ["venv", "pip"])
        self.assertFalse((self.base / "hf-cache").exists())

    def test_run_as_a_script_it_runs_the_module_the_other_installers_and_amd_build_import(self):
        # Their InstallError is then the one main() catches, so an error from AMD's build ends in an error line.
        code = ("import runpy, sys\n"
                f"sys.path.insert(0, {str(ROOT / 'tools')!r})\n"
                "sys.argv = ['install_kokoro.py', '--help']\n"
                "try:\n"
                f"    runpy.run_path({str(ROOT / 'tools' / 'install_kokoro.py')!r}, run_name='__main__')\n"
                "except SystemExit:\n"
                "    pass\n"
                "print('ran', 'tools.install_kokoro' in sys.modules)\n")
        done = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--gpu", done.stdout)
        self.assertEqual(done.stdout.strip().splitlines()[-1], "ran True")

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
