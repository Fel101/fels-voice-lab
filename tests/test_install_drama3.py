"""tools/install_drama3.py: Fish Audio Drama 3 for the CPU, its speech.cpp runtime and its 20.8 GB model, with the licence said
first. Nothing here reaches the real network, speech.cpp or the real model."""

import contextlib
import hashlib
import http.server
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drama3_runtime  # noqa: E402
import drama3_worker  # noqa: E402
import engine_checks  # noqa: E402
import engine_install  # noqa: E402
import ram_needs  # noqa: E402
from tools import install_drama3 as idr  # noqa: E402
from tools import install_kokoro as base  # noqa: E402

CATALOG = {entry["id"]: entry for entry in json.loads((ROOT / "catalog" / "engines.json").read_text("utf-8"))["engines"]}
ENTRY = CATALOG["drama-3"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RangeServer:
    """A local web server that serves bytes by path and honours a Range header, as the model host does."""

    def __init__(self):
        self.files, self.requests, self.fail = {}, [], set()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urllib.parse.unquote(self.path.split("?")[0])
                header = self.headers.get("Range")
                server.requests.append((path, header))
                body = server.files.get(path)
                if body is None or (path, header) in server.fail:
                    self.send_error(404 if body is None else 503)
                    return
                if header:
                    start, end = (int(value) for value in header.removeprefix("bytes=").split("-"))
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                    body = body[start : end + 1]
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ShippedPinsTests(unittest.TestCase):
    def test_the_pins_are_the_ones_the_runtime_checks_and_the_catalog_names(self):
        self.assertEqual(
            (idr.REPO, idr.REVISION, idr.MODEL_SHA256),
            (drama3_runtime.REPO, drama3_runtime.REVISION, drama3_runtime.MODEL_SHA256),
        )
        self.assertEqual(idr.UPSTREAM_REVISION, ENTRY["local_revision"])
        self.assertEqual((ENTRY["backend"]["derivative"], ENTRY["backend"]["derivative_revision"]), (idr.REPO, idr.REVISION))
        self.assertEqual(idr.RAM_GB, ram_needs.MIN_FREE_RAM_GB["drama-3"])
        self.assertEqual(ENTRY["license"]["name"], "Fish Audio Community License 1.0")
        for item in idr.ASSETS:
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")

    def test_the_launcher_and_the_models_page_run_the_installer_without_a_gpu_build(self):
        text = (ROOT / "setup-drama3.bat").read_text("utf-8")
        self.assertIn('".venv\\Scripts\\python.exe" tools\\install_drama3.py %*', text)
        self.assertIn(b"\r\n", (ROOT / "setup-drama3.bat").read_bytes())
        self.assertEqual(engine_install.INSTALLERS["drama-3"], "tools/install_drama3.py")
        self.assertNotIn("drama-3", engine_install.GPU_BUILDS)
        self.assertEqual(engine_checks.GUIDED_INSTALLERS["drama-3"], "setup-drama3.bat")


class ProblemAndPlanTests(unittest.TestCase):
    def test_a_computer_with_less_ram_than_drama_needs_free_is_refused(self):
        self.assertIn("This computer has 16 GB of RAM, and Drama 3 needs 26 GB of it free", idr.ram_problems(16.0)[0])
        self.assertEqual(idr.ram_problems(31.6), [])

    def test_it_runs_only_from_the_labs_own_python_on_64_bit_windows(self):
        root = Path(tempfile.gettempdir()) / "lab"
        good = {"system": "win32", "machine": "AMD64", "bits": 64, "prefix": root / ".venv"}
        self.assertEqual(idr.interpreter_problems(root, **good), [])
        self.assertIn("this folder's own Python", idr.interpreter_problems(root, **{**good, "prefix": root / "other"})[0])
        self.assertIn("64-bit Windows", idr.interpreter_problems(root, **{**good, "system": "linux"})[0])

    def test_the_plan_says_the_licence_first_then_what_it_installs(self):
        text = idr.plan_text(ROOT, runtime_needed=True, model_needed=True, assets_needed=True, runtime_only=False)
        title, licence = text.split("\n\n")[:2]
        self.assertEqual(title, "Fel's Voice Lab: Fish Audio Drama 3 (35B MoE, Q4_K) on the CPU")
        licence = " ".join(licence.split())
        self.assertTrue(licence.startswith("Licence: Drama 3 is under the Fish Audio Community License 1.0"))
        self.assertIn("commercial use is allowed only while your annual revenue is under USD 500,000", licence)
        self.assertIn('"Built with Fish Audio"', licence)
        flat = " ".join(text.split())
        self.assertIn("a 29 MB archive", flat)
        self.assertIn("the 20.8 GB model with its licence, notice and model card", flat)
        self.assertIn("needs 26 GB of free RAM", flat)
        text.encode("ascii")
        flat = " ".join(
            idr.plan_text(ROOT, runtime_needed=False, model_needed=False, assets_needed=False, runtime_only=False).split()
        )
        self.assertIn("Already in place.", flat)
        self.assertIn("The model is already in place.", flat)


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = RangeServer()
        cls.addClassCleanup(cls.server.close)

    def setUp(self):
        self.server.requests, self.server.fail = [], set()
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-drama-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.model = hashlib.shake_256(b"drama").digest(10_000)
        self.server.files = {f"/{idr.REPO}/resolve/{idr.REVISION}/{idr.MODEL}": self.model}
        for owner, name, value in (
            (idr, "HOST", self.server.address),
            (idr, "BLOCK", 1024),
            (idr, "WORKERS", 3),
            (idr, "MODEL_SIZE", len(self.model)),
            (idr, "MODEL_SHA256", sha(self.model)),
        ):
            patch = mock.patch.object(owner, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_model_comes_in_pieces_and_an_interrupted_download_continues_from_those_done(self):
        path = f"/{idr.REPO}/resolve/{idr.REVISION}/{idr.MODEL}"
        self.server.fail = {(path, "bytes=4096-5119")}
        lines = []
        with self.assertRaisesRegex(idr.InstallError, "continues from the pieces already done"):
            idr.download_model(self.root, say=lines.append, pause=lambda seconds: None)
        state = json.loads((self.root / "out/drama3/models" / (idr.MODEL + ".ranges.json")).read_text("utf-8"))
        # Ten pieces: the refused one is not done, and pieces not yet started when it failed are left for the next run.
        self.assertNotIn(4, state)
        self.assertTrue(0 < len(state) < 10)
        self.server.fail, self.server.requests = set(), []
        idr.download_model(self.root, say=lines.append, pause=lambda seconds: None)
        missing = sorted(set(range(10)) - set(state))
        asked = sorted(int(header.removeprefix("bytes=").split("-")[0]) // 1024 for _path, header in self.server.requests)
        self.assertEqual(asked, missing)  # only the pieces not done before
        self.assertEqual(idr.model_path(self.root).read_bytes(), self.model)
        self.assertFalse((self.root / "out/drama3/models" / (idr.MODEL + ".partial")).exists())
        self.assertFalse((self.root / "out/drama3/models" / (idr.MODEL + ".ranges.json")).exists())

    def test_a_model_that_does_not_match_its_hash_is_deleted(self):
        with mock.patch.object(idr, "MODEL_SHA256", "0" * 64), self.assertRaisesRegex(idr.InstallError, "does not match"):
            idr.download_model(self.root, say=lambda text: None, pause=lambda seconds: None)
        self.assertFalse(idr.model_path(self.root).exists())
        self.assertFalse((self.root / "out/drama3/models" / (idr.MODEL + ".partial")).exists())

    def test_the_runtime_is_unpacked_and_an_archive_with_a_path_outside_it_is_refused(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("speech.exe", b"MZ fake speech.cpp")
            bundle.writestr("speech.dll", b"MZ fake library")
        data = archive.getvalue()
        url = f"{self.server.address}/speech.zip"
        self.server.files["/speech.zip"] = data
        with (
            mock.patch.object(idr, "RUNTIME_URL", url),
            mock.patch.object(idr, "RUNTIME_SIZE", len(data)),
            mock.patch.object(idr, "RUNTIME_SHA256", sha(data)),
        ):
            executable = idr.install_runtime(self.root, say=lambda text: None)
            self.assertEqual(executable.read_bytes(), b"MZ fake speech.cpp")
            bad = io.BytesIO()
            with zipfile.ZipFile(bad, "w") as bundle:
                bundle.writestr("../escape.exe", b"no")
            self.server.files["/speech.zip"] = bad.getvalue()
            (self.root / "out/drama3" / idr.RUNTIME_ARCHIVE).unlink()
            with (
                mock.patch.object(idr, "RUNTIME_SIZE", len(bad.getvalue())),
                mock.patch.object(idr, "RUNTIME_SHA256", sha(bad.getvalue())),
                self.assertRaisesRegex(idr.InstallError, "outside the runtime folder"),
            ):
                idr.install_runtime(self.root, say=lambda text: None)


class RunTests(unittest.TestCase):
    """The whole install with the hosts, speech.cpp and the speech check replaced by stand-ins."""

    @classmethod
    def setUpClass(cls):
        cls.server = RangeServer()
        cls.addClassCleanup(cls.server.close)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-drama-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.model = hashlib.shake_256(b"drama model").digest(5000)
        assets = [{"path": item["path"], "data": f"stand-in {item['path']}\n".encode()} for item in idr.ASSETS]
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("speech.exe", b"MZ fake speech.cpp")
        self.archive = archive.getvalue()
        prefix = f"/{idr.REPO}/resolve/{idr.REVISION}/"
        self.server.requests, self.server.fail = [], set()
        self.server.files = {
            prefix + idr.MODEL: self.model,
            "/speech.zip": self.archive,
            **{prefix + item["path"]: item["data"] for item in assets},
        }
        self.checks = []

        def speak(root, **kwargs):
            receipt = json.loads((root / "out/drama3/installation.json").read_text("utf-8"))
            self.checks.append(receipt)  # the receipt is in place before the check, which finds the files through it
            return {
                "problem": None,
                "load_seconds": 28.0,
                "total_seconds": 9.0,
                "audio_seconds": 2.3,
                "device": "cpu",
                "wav": root / "out/drama3/install-check.wav",
            }

        patches = (
            (idr, "ROOT", self.root),
            (idr, "HOST", self.server.address),
            (idr, "BLOCK", 1024),
            (idr, "MODEL_SIZE", len(self.model)),
            (idr, "MODEL_SHA256", sha(self.model)),
            (
                idr,
                "ASSETS",
                tuple({"path": item["path"], "size": len(item["data"]), "sha256": sha(item["data"])} for item in assets),
            ),
            (idr, "RUNTIME_URL", f"{self.server.address}/speech.zip"),
            (idr, "RUNTIME_SIZE", len(self.archive)),
            (idr, "RUNTIME_SHA256", sha(self.archive)),
            (idr, "run_speech_check", speak),
            (idr, "interpreter_problems", lambda root: []),
            (base, "space_problems", lambda root, need: []),
        )
        for owner, name, value in patches:
            patch = mock.patch.object(owner, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, *argv, ram_gb=64):
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch.object(sys, "stdin", io.StringIO()),
            mock.patch.object(idr, "total_ram_gb", lambda: ram_gb),
        ):
            status = idr.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def test_a_dry_run_shows_the_licence_and_changes_nothing(self):
        status, out, err = self.run_main("--dry-run")
        self.assertEqual((status, err), (0, ""))
        self.assertIn("Fish Audio Community License 1.0", out)
        self.assertEqual(self.server.requests, [])
        self.assertFalse((self.root / "out").exists())

    def test_too_little_ram_stops_it_before_any_download(self):
        status, _out, err = self.run_main("--yes", ram_gb=16)
        self.assertEqual(status, 1)
        self.assertIn("needs 26 GB of it free to load", " ".join(err.split()))
        self.assertEqual(self.server.requests, [])

    def test_the_whole_install_writes_the_receipt_the_lab_reads_and_checks_speech(self):
        status, out, err = self.run_main("--yes")
        self.assertEqual((status, err), (0, ""))
        self.assertIn("Drama 3 is installed. It loaded in 28 s and spoke 2.3 s", " ".join(out.split()))
        receipt = json.loads((self.root / "out/drama3/installation.json").read_text("utf-8"))
        self.assertEqual(Path(receipt["model_path"]), (self.root / "out/drama3/models" / idr.MODEL).resolve())
        self.assertEqual(Path(receipt["runtime_path"]).read_bytes(), b"MZ fake speech.cpp")
        self.assertEqual(receipt["runtime_sha256"], sha(b"MZ fake speech.cpp"))
        self.assertEqual(receipt["speech_check"]["audio_seconds"], 2.3)
        self.assertNotIn("speech_check", self.checks[0])
        self.assertEqual(receipt["attribution"], "Built with Fish Audio")
        self.assertEqual(
            engine_checks.installation(ENTRY, self.root)["missing"],
            [] if (self.root / ENTRY["runtime"]["python"]).is_file() else [ENTRY["runtime"]["python"]],
        )
        self.assertEqual(engine_checks._drama_missing(self.root), [])

    def test_a_second_run_downloads_nothing_new(self):
        self.run_main("--yes")
        before = len(self.server.requests)
        status, out, _err = self.run_main("--yes")
        self.assertEqual((status, len(self.server.requests)), (0, before))
        self.assertIn("The Drama 3 model is already in place", out)

    def test_the_runtime_alone_can_be_installed(self):
        status, out, _err = self.run_main("--yes", "--runtime-only", ram_gb=8)  # no model, so its RAM does not matter
        self.assertEqual(status, 0)
        self.assertIn("speech.cpp is in place", out)
        self.assertFalse(idr.model_path(self.root).exists())
        self.assertEqual(self.checks, [])


class SpeechCheckTests(unittest.TestCase):
    def test_the_check_runs_in_the_labs_own_python_through_the_drama_worker(self):
        calls = []
        good = '{"problem": null, "load_seconds": 28.0, "total_seconds": 9.0, "audio_seconds": 2.3, "device": "cpu"}'

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, good + "\n", "")

        with tempfile.TemporaryDirectory() as folder:
            result = idr.run_speech_check(folder, runner=runner)
            command, _kwargs = calls[0]
            self.assertEqual(command[:2], [str(Path(folder) / ".venv" / "Scripts" / "python.exe"), "-c"])
            self.assertIn('Drama3Worker("cpu")', command[2])
            self.assertIn('require_ram(MIN_FREE_RAM_GB["drama-3"]', command[2])
            self.assertEqual(result["audio_seconds"], 2.3)

    def test_too_little_free_ram_at_the_check_says_to_free_some_and_test_from_the_card(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "MemoryError: Only 12.0 GB RAM free; not loading Drama 3")

        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(idr.InstallError, "press Test on its card"):
            idr.run_speech_check(folder, runner=runner)

    def test_the_names_the_check_script_uses_exist_in_the_lab(self):
        for name in ("load", "generate", "unload"):
            self.assertTrue(callable(getattr(drama3_worker.Drama3Worker, name)))
        request = engine_checks.smoke_request("drama-3", {}, root=ROOT)
        self.assertEqual(request["mode"], "design")
        compile(idr.SPEECH_CHECK, "<speech check>", "exec")


if __name__ == "__main__":
    unittest.main()
