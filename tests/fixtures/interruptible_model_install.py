"""Run a real installer's orchestration with tiny files and controllable package/check subprocesses."""

import hashlib
import io
import subprocess
import sys
import time
import zipfile
from functools import partial
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from tools import install_kokoro as base

BLOCK = 1024 * 1024
MODEL = b"A" * BLOCK + b"B" * BLOCK
CHECK = {
    "load_seconds": 1.0,
    "total_seconds": 2.0,
    "audio_seconds": 1.5,
    "reference_seconds": 1.5,
    "vc_seconds": 1.5,
}
CHILD = """
import os, sys, time
from pathlib import Path
phase = sys.argv[1]
root = Path.cwd()
(root / (phase + '.pid')).write_text(str(os.getpid()), 'ascii')
if (root / 'hold').read_text() == phase:
    deadline = time.monotonic() + 40
    while not (root / 'continue').exists() and time.monotonic() < deadline:
        time.sleep(0.02)
"""


def write_launcher(path, family):
    repo = Path(__file__).resolve().parents[2]
    path.write_text(
        f"import sys\nsys.path.insert(0, {str(repo)!r})\n"
        f"from tests.fixtures.interruptible_model_install import run\nsys.exit(run({family!r}))\n",
        "utf-8",
    )


def item(path, data):
    return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def run(family):
    root = Path.cwd()
    module = import_module(f"tools.install_{family}")
    weights = item("model.bin", MODEL)
    manifest = {
        "repo": "fixture/model",
        "revision": "v1",
        "license": "fixture",
        "folder": "checkpoint",
        "files": [weights],
    }
    files = {"model.bin": MODEL}
    patches = {
        "ROOT": root,
        "interpreter_problems": lambda *_: [],
        "long_path_problems": lambda *_: [],
        "plan_text": lambda *_, **_kwargs: "Install fixture",
    }
    installed = root / "packages-completed"
    wanted = {"fixture": "1.0"}

    def run_package(command, **opts):
        if "venv" in command:
            executable = module.environment_python(root)
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.touch()
            return subprocess.CompletedProcess(command, 0)
        done = subprocess.run([sys.executable, "-c", CHILD, "pip"], **opts)
        if not done.returncode:
            with installed.open("ab") as record:
                record.write(b"done\n")
        return done

    def check(*_args, **_kwargs):
        subprocess.run([sys.executable, "-c", CHILD, "check"], check=True)
        wav = root / "check.wav"
        return {**CHECK, "wav": wav, "converted": wav, "reference": wav}

    def save_receipt(*_args, **_kwargs):
        receipt = root / "receipt"
        receipt.touch()
        return receipt

    def no_problems(*_args, **_kwargs):
        return []

    def packages(*_args, **_kwargs):
        return [{"name": name, "key": name, "version": version} for name, version in wanted.items()]

    if family == "drama3":
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(zipfile.ZipInfo("speech.exe"), b"stand-in runtime")
        runtime = item("speech.zip", archive.getvalue())
        files = {module.MODEL: MODEL, "speech.zip": archive.getvalue()}
        patches.update(
            RUNTIME_URL="http://fixture/speech.zip",
            RUNTIME_SIZE=runtime["size"],
            RUNTIME_SHA256=runtime["sha256"],
            MODEL_SIZE=len(MODEL),
            MODEL_SHA256=weights["sha256"],
            ASSETS=(),
            BLOCK=BLOCK,
            WORKERS=1,
            total_ram_gb=lambda: 64,
        )
        extract = zipfile.ZipFile.extract

        def unpack(archive, *args, **kwargs):
            subprocess.run([sys.executable, "-c", CHILD, "unpack"], check=True)
            return extract(archive, *args, **kwargs)

        patches["run_speech_check"] = check
    else:
        patches.update(
            make_environment=partial(module.make_environment, runner=run_package),
            install_packages=partial(module.install_packages, runner=run_package),
            environment_versions=lambda _: {"pip": "26.0", **(wanted if installed.exists() else {})},
            run_speech_check=check,
            write_receipt=save_receipt,
        )
        extract = unpack = zipfile.ZipFile.extract
        if family in {"kokoro", "chatterbox"}:
            patches["requested_build"] = lambda _: (None, [])
        if family == "kokoro":
            patches["choose_voices"] = lambda *_: []
        if family == "phonon":
            patches["load_manifest"] = lambda _: {"repos": [manifest]}
        if family == "chatterbox":
            patches.update(run_original_check=check, voice_match=lambda _: {})
        if family in {"qwen", "voxcpm", "breeze"}:
            patches.update(graphics=lambda: ("nvidia", []), gpu_problems=no_problems)
        if family == "qwen":
            patches["reference_problems"] = no_problems
        if family == "voxcpm":
            patches["load_source"] = lambda _: {"files": []}

    class Response(io.BytesIO):
        def __init__(self, data, start, end, ranged, download, partial):
            super().__init__(data[start : end + 1])
            self.status = 206 if ranged else 200
            self.headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
            self.download = download
            self.partial = partial

        def read(self, size=-1):
            is_drama_chunk = family == "drama3" and self.partial
            if self.download and (self.tell() or is_drama_chunk) and (root / "hold").read_text() == "download":
                if not (root / "continue").exists():
                    (root / "download-ready").touch()
                    deadline = time.monotonic() + 40
                    while not (root / "continue").exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
            return super().read(size)

    def urlopen(request, **_kwargs):
        filename = request.full_url.split("?")[0].rsplit("/", 1)[1]
        data = files[filename]
        byte_range = request.get_header("Range")
        with (root / "requests").open("a", encoding="ascii") as history:
            history.write(f"{filename} {byte_range}\n")
        start, end = (0, len(data) - 1)
        if byte_range:
            first, last = byte_range.removeprefix("bytes=").split("-")
            start, end = int(first), int(last) if last else end
        return Response(data, start, end, byte_range, filename != "speech.zip", start > 0)

    overrides = {
        "CHUNK": BLOCK,
        "load_manifest": lambda _: manifest,
        "read_lock": packages,
        "space_problems": no_problems,
        "torch_cuda_step": no_problems,
    }
    if module is base:
        overrides.update(patches)
        patches = {}
    with (
        patch.multiple(base, **overrides),
        patch.multiple(module, create=True, **patches) if patches else patch.object(base, "HOST", "http://fixture"),
        patch.object(base.urllib.request, "urlopen", urlopen),
        patch.object(zipfile.ZipFile, "extract", unpack),
    ):
        return module.main(sys.argv[1:])
