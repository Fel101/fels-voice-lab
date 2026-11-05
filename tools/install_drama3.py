"""Install Fish Audio Drama 3 (35B MoE, Q4_K) for the CPU: the pinned speech.cpp runtime and the pinned GGUF model, then a
speech check.

    .venv\\Scripts\\python.exe tools\\install_drama3.py
    .venv\\Scripts\\python.exe tools\\install_drama3.py --dry-run
    .venv\\Scripts\\python.exe tools\\install_drama3.py --yes
    .venv\\Scripts\\python.exe tools\\install_drama3.py --runtime-only

It prints what it will do and asks before it changes anything (--yes accepts the plan, --dry-run only shows it). Run it with
the lab's own Python, which setup.bat creates; setup-drama3.bat does that for you. Drama 3 is under the Fish Audio Community
License: the plan says so first, and installing it means accepting it. Nothing is installed with pip: the Lab runs Drama 3
through speech.cpp, a native program, from the lab's own .venv. The installer fetches speech.cpp 0.8.10 for Windows x64 (MIT)
from its GitHub release and unpacks it into out\\drama3\\runtime, then the 20.8 GB Q4_K conversion of fishaudio/drama-3 with its
licence, notice and model card from Hugging Face into out\\drama3\\models. Every file is checked against a SHA-256 written here.
The model comes in 32 MB pieces, six at a time, and an interrupted download continues from the pieces already done. It then
writes out\\drama3\\installation.json, which the Lab reads to find both, and loads the model on the CPU to speak one sentence.
Drama 3 needs 26 GB of free RAM to load, so the installer stops on a computer with less RAM than that in all. --runtime-only
installs the runtime alone. Running it again installs only what is missing.
"""

import argparse
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    from tools import install_kokoro as base
except ImportError:  # run as a script, which puts tools\ first on the path and not the lab folder
    import install_kokoro as base

InstallError = base.InstallError
ROOT = Path(__file__).resolve().parents[1]
INSTALL = "out/drama3"
UPSTREAM = "fishaudio/drama-3"
UPSTREAM_REVISION = "e0a9218aa48888270f06ef1ed9ea67622c0e17d2"
REPO = "sakasegawa/drama-3-GGUF"
REVISION = "9a01354d04ab6eb521bc9cdfc56ab6824c474af2"
HOST = "https://huggingface.co"
MODEL = "Drama-35B-A3B-v3-Q4_K.gguf"
MODEL_SIZE = 20787264352
MODEL_SHA256 = "2a57b14c9b208a2ff833ab7305b920ca8f154ad807004a52d96a86a90a55523e"
# The licence, notice and model card that come with the model, and its own description.
ASSETS = (
    {"path": "LICENSE", "size": 19685, "sha256": "733a32442087acbda12ecafe8e71eb8ee5890c89b50ce7ac4ea3777faa3c823e"},
    {"path": "NOTICE", "size": 1459, "sha256": "0a69b4b75ccd472ac9a1d81c1407dcee73971edbae43ed3495be64967ddf63d5"},
    {"path": "README.md", "size": 10884, "sha256": "3ecf041ae2959e7b6feeea04c04e42a722d66f20a18076a9ba33a1da9c27314d"},
    {"path": MODEL + ".json", "size": 2413, "sha256": "b26907ededde102e6c99901a0a17331fcf567516228b519fbb2beb075cc0fa75"},
)
RUNTIME_VERSION = "0.8.10"
RUNTIME_URL = "https://github.com/nyosegawa/speech.cpp/releases/download/v0.8.10/speech-0.8.10-windows-x64-vulkan.zip"
RUNTIME_ARCHIVE = "speech-0.8.10-windows-x64-vulkan.zip"
RUNTIME_SIZE = 28886479
RUNTIME_SHA256 = "2904dd54de7f18cf438338c28216ebffc205d29c6a2de3e645af40dd136b92f2"
LICENSE_LINK = f"https://huggingface.co/{UPSTREAM}/blob/{UPSTREAM_REVISION}/LICENSE"
BLOCK = 32 * 1024 * 1024
WORKERS = 6
RAM_GB = 26  # ram_needs.MIN_FREE_RAM_GB["drama-3"]
SPACE_MARGIN_MB = 1000
CHECK_TIMEOUT = 3600

# Loads Drama 3 on the CPU through the Lab's own worker, which finds the model and runtime through the receipt, and speaks the
# Models page's check sentence in a voice made from a description, as its Test does.
SPEECH_CHECK = """
import json, sys, time
from pathlib import Path
root, wav_path = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(root))
import soundfile
import engine_checks
from common import require_ram
from drama3_worker import Drama3Worker
from ram_needs import MIN_FREE_RAM_GB

require_ram(MIN_FREE_RAM_GB["drama-3"], "Drama 3")
worker = Drama3Worker("cpu")
started = time.perf_counter()
loaded = worker.load()
load_seconds = time.perf_counter() - started
request = {key: value for key, value in engine_checks.smoke_request("drama-3", loaded, root=root).items() if key != "engine"}
wav, rate, info = worker.generate(request)
worker.unload()
soundfile.write(wav_path, wav, rate)
print(json.dumps({"problem": engine_checks.audible(wav, rate), "load_seconds": load_seconds, "total_seconds": info["total_s"],
                  "audio_seconds": wav.size / rate, "device": "cpu"}))
"""


def say_now(text=""):
    base.say_now(text)


def interpreter_problems(root, *, system=None, machine=None, bits=None, prefix=None):
    """Why this Python is not the one to run the installer with: speech.cpp's build is for 64-bit Windows, and the Lab runs
    Drama 3 from its own .venv."""
    system, machine = system or sys.platform, (machine or platform.machine()).lower()
    bits, prefix = bits or (64 if sys.maxsize > 2**32 else 32), prefix or sys.prefix
    problems = []
    if system != "win32" or machine not in {"amd64", "x86_64"} or bits != 64:
        problems.append("This installer is for 64-bit Windows: the pinned speech.cpp runtime is a Windows x64 build.")
    if Path(prefix).resolve() != (Path(root) / ".venv").resolve():
        problems.append(
            "Run it with this folder's own Python, which Drama 3 runs from: .venv\\Scripts\\python.exe "
            "tools\\install_drama3.py (setup.bat creates that environment)."
        )
    return problems


def total_ram_gb():
    """This computer's physical memory in GB (Windows)."""

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullTotalPhys / 2**30


def ram_problems(total_gb):
    """Drama 3 loads only with 26 GB of RAM free, so a computer with less in all can never run it."""
    if total_gb < RAM_GB:
        return [
            f"This computer has {total_gb:.0f} GB of RAM, and Drama 3 needs {RAM_GB} GB of it free to load, so it cannot "
            "run here."
        ]
    return []


def folder(root):
    return Path(root) / INSTALL


def model_path(root):
    return folder(root) / "models" / MODEL


def runtime_exe(root):
    found = sorted((folder(root) / "runtime").rglob("speech.exe")) if (folder(root) / "runtime").is_dir() else []
    return found[0] if found else None


def model_done(root):
    """Whether the model is in place: its size, and a receipt naming it with the pinned SHA-256 (the full hash of 20.8 GB is
    read again only when the receipt is missing)."""
    path = model_path(root)
    if not path.is_file() or path.stat().st_size != MODEL_SIZE:
        return False
    receipt = read_receipt(root)
    if receipt and receipt.get("model_sha256") == MODEL_SHA256 and Path(receipt.get("model_path", "")) == path.resolve():
        return True
    return base.sha256_file(path) == MODEL_SHA256


def read_receipt(root):
    try:
        return json.loads((folder(root) / "installation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pending_assets(root):
    models = folder(root) / "models"
    return [
        item
        for item in ASSETS
        if not (models / item["path"]).is_file()
        or (models / item["path"]).stat().st_size != item["size"]
        or base.sha256_file(models / item["path"]) != item["sha256"]
    ]


def licence_text():
    return (
        f"Licence: Drama 3 is under the Fish Audio Community License 1.0 ({LICENSE_LINK}): free for research and "
        "non-commercial use; commercial use is allowed only while your annual revenue is under USD 500,000, and a hosted "
        'service needs a separate licence from Fish Audio. Anything you publish that uses it must say "Built with Fish '
        'Audio". Installing it means you accept it, so read it first. speech.cpp is MIT.'
    )


def plan_text(root, *, runtime_needed, model_needed, assets_needed, runtime_only):
    def step(number, text):
        return textwrap.fill(
            f"{number}. {text}", width=110, break_on_hyphens=False, initial_indent="  ", subsequent_indent="     "
        )

    first = (
        f"speech.cpp {RUNTIME_VERSION} for Windows x64, the native program that runs Drama 3, from its GitHub release "
        f"(github.com/nyosegawa/speech.cpp): a {RUNTIME_SIZE / 1e6:.0f} MB archive, checked against its SHA-256 and "
        "unpacked into out\\drama3\\runtime. " + ("To download." if runtime_needed else "Already in place.")
    )
    steps = [step(1, first)]
    if not runtime_only:
        second = (
            f"The Q4_K conversion of {UPSTREAM} (revision {UPSTREAM_REVISION[:7]}) from huggingface.co/{REPO}, revision "
            f"{REVISION[:7]}: the {MODEL_SIZE / 1e9:.1f} GB model with its licence, notice and model card, into "
            "out\\drama3\\models. It comes in 32 MB pieces, six at a time, and an interrupted download continues from the "
            "pieces already done; every file is checked against a SHA-256. "
            + (
                "To download."
                if model_needed
                else f"The model is already in place{'; its small files are to download' if assets_needed else ''}."
            )
        )
        third = (
            f"A speech check on the CPU: loads the model, which reads all {MODEL_SIZE / 1e9:.1f} GB and needs {RAM_GB} GB of "
            "free RAM, and speaks one sentence in a voice made from a short description, which you can play afterwards. "
            "It can take a few minutes."
        )
        steps += [step(2, second), step(3, third)]
    intro = textwrap.fill(
        "This adds Fish Audio Drama 3, a 35B mixture-of-experts model (about 3B active) that designs voices "
        "and clones them from a recording and its transcript, in a 4-bit (Q4_K) conversion that runs on the "
        "CPU. Everything is installed into out\\drama3 and nothing system-wide; nothing is installed with pip.",
        width=110,
        break_on_hyphens=False,
    )
    title = "Fel's Voice Lab: Fish Audio Drama 3 (35B MoE, Q4_K) on the CPU" + (" (runtime only)" if runtime_only else "")
    return (
        "\n\n".join([title, textwrap.fill(licence_text(), width=110, break_on_hyphens=False), intro, "\n".join(steps)]) + "\n"
    )


def install_runtime(root, *, say=say_now):
    """Fetch the speech.cpp archive (kept beside the runtime, so a second run need not fetch it) and unpack it, member by member,
    into out/drama3/runtime; returns speech.exe."""
    archive = folder(root) / RUNTIME_ARCHIVE
    if not base.fetch(RUNTIME_URL, archive, RUNTIME_SIZE, RUNTIME_SHA256, say=say):
        say("      already in place")
    runtime = folder(root) / "runtime"
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if runtime.resolve() not in (runtime / member.filename).resolve().parents:
                raise InstallError(f"The speech.cpp archive names a path outside the runtime folder: {member.filename}")
        for member in bundle.infolist():
            destination = runtime / member.filename
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            with bundle.open(member) as stream:
                expected = hashlib.file_digest(stream, "sha256").hexdigest()
            if not destination.is_file() or base.sha256_file(destination) != expected:
                bundle.extract(member, runtime)
    executable = runtime_exe(root)
    if executable is None:
        raise InstallError("The speech.cpp archive holds no speech.exe.")
    return executable


def fetch_assets(root, *, say=say_now):
    """The model's licence, notice, card and description, each checked against its SHA-256."""
    models = folder(root) / "models"
    for position, item in enumerate(ASSETS, 1):
        say(f"  [{position}/{len(ASSETS)}] {item['path']} ({base.size_text(item['size'])})")
        url = f"{HOST}/{REPO}/resolve/{REVISION}/{item['path']}"
        if not base.fetch(url, models / item["path"], item["size"], item["sha256"], say=say):
            say("      already in place")


def download_model(root, *, say=say_now, pause=time.sleep):
    """Fetch the model in pieces of BLOCK bytes, WORKERS at a time, into a .partial file of the full size. The pieces done are
    kept in .ranges.json, so an interrupted download continues from them. Only a file whose SHA-256 matches replaces the
    model."""
    block, workers, size, sha256 = BLOCK, WORKERS, MODEL_SIZE, MODEL_SHA256
    models = folder(root) / "models"
    models.mkdir(parents=True, exist_ok=True)
    target = model_path(root)
    partial, state = models / (MODEL + ".partial"), models / (MODEL + ".ranges.json")
    try:
        completed = set(json.loads(state.read_text(encoding="utf-8"))) if state.exists() and partial.exists() else set()
    except (OSError, ValueError):
        completed = set()
    if not partial.exists() or partial.stat().st_size != size:
        completed = set()
        with partial.open("wb") as stream:
            stream.truncate(size)
    url = f"{HOST}/{REPO}/resolve/{REVISION}/{MODEL}?download=true"
    pieces = (size + block - 1) // block
    lock = threading.Lock()
    shown = [len(completed) * 10 // pieces]

    def piece(index):
        start, end = index * block, min(size, (index + 1) * block) - 1
        last = None
        for attempt in range(5):
            try:
                request = urllib.request.Request(
                    f"{url}&range_start={start}",
                    headers={"Range": f"bytes={start}-{end}", "User-Agent": "fels-voice-lab-installer"},
                )
                with urllib.request.urlopen(request, timeout=120) as source:
                    if source.status != 206 or source.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                        raise OSError("the model host did not send the piece asked for")
                    data = source.read(end - start + 2)
                if len(data) != end - start + 1:
                    raise OSError("a piece of the model came back short")
                with lock:
                    with partial.open("r+b") as stream:
                        stream.seek(start)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    completed.add(index)
                    temporary = state.with_suffix(".tmp")
                    temporary.write_text(json.dumps(sorted(completed)), encoding="utf-8")
                    temporary.replace(state)
                    if len(completed) * 10 // pieces > shown[0]:
                        shown[0] = len(completed) * 10 // pieces
                        say(f"      {shown[0] * 10}% ({min(size, len(completed) * block) / 1e9:.1f} GB of {size / 1e9:.1f} GB)")
                return
            except (OSError, urllib.error.URLError) as exc:
                last = exc
                pause(attempt + 1)
        raise InstallError(
            f"A piece of the model would not download ({last}). Run the installer again: it continues from the "
            "pieces already done."
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(piece, [index for index in range(pieces) if index not in completed]))
    say("      checking the whole file's SHA-256 (a minute or two)")
    if base.sha256_file(partial) != sha256:
        partial.unlink()
        state.unlink(missing_ok=True)
        raise InstallError(
            f"{MODEL} does not match the published SHA-256, so it was deleted. Run the installer again; if this "
            "repeats, the file at the pinned revision has changed."
        )
    base.replace_file(partial, target)
    state.unlink(missing_ok=True)


def write_receipt(root, executable, check=None):
    """out/drama3/installation.json: the Lab finds the model and the runtime through it (drama3_runtime.assets)."""
    models = folder(root) / "models"
    receipt = {
        "schema": 1,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upstream_model": UPSTREAM,
        "upstream_revision": UPSTREAM_REVISION,
        "repo": REPO,
        "revision": REVISION,
        "model_path": str(model_path(root).resolve()),
        "model_sha256": MODEL_SHA256,
        "runtime": "speech.cpp",
        "runtime_version": RUNTIME_VERSION,
        "runtime_path": str(Path(executable).resolve()),
        "runtime_sha256": base.sha256_file(executable),
        "runtime_archive_sha256": RUNTIME_SHA256,
        "backend": "cpu",
        "quantization": "Q4_K",
        "attribution": "Built with Fish Audio",
        "license": "Fish Audio Community License 1.0",
        "license_path": str((models / "LICENSE").resolve()),
        "notice_path": str((models / "NOTICE").resolve()),
    }
    if check:
        receipt["speech_check"] = {key: check.get(key) for key in ("load_seconds", "total_seconds", "audio_seconds")}
    path = folder(root) / "installation.json"
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path


def run_speech_check(root, *, runner=subprocess.run):
    """In the lab's .venv, which Drama 3 runs from: load the model on the CPU and speak the Models page's check sentence."""
    wav = folder(root) / "install-check.wav"
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONUTF8": "1"}
    try:
        done = runner(
            [str(Path(root) / ".venv" / "Scripts" / "python.exe"), "-c", SPEECH_CHECK, str(root), str(wav)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(
            f"The speech check took more than {CHECK_TIMEOUT // 60} minutes, so it was stopped. The files are "
            "in place: press Test on its card in Models & features to try again."
        ) from exc
    result = base.last_json_line(done.stdout or "")
    if done.returncode or not isinstance(result, dict):
        output = (done.stdout or "") + (done.stderr or "")
        hint = (
            "\nClose other programs to free memory, then press Test on its card in Models & features."
            if "RAM free" in output
            else ""
        )
        raise InstallError(
            "The runtime and model are in place, but Drama 3 did not load or speak:\n"
            + "\n".join(output.strip().splitlines()[-12:])
            + hint
        )
    if result["problem"]:
        raise InstallError(f"Drama 3 loaded but the check clip was not speech: {result['problem']}.")
    return {**result, "wav": wav}


def run(args, *, say=say_now, ask=input, interactive=None, ram_gb=None):
    root = ROOT
    executable = runtime_exe(root)
    runtime_needed = executable is None or not (folder(root) / RUNTIME_ARCHIVE).is_file()
    model_needed = not args.runtime_only and not model_done(root)
    assets = [] if args.runtime_only else pending_assets(root)
    need = (RUNTIME_SIZE * 4 if runtime_needed else 0) + (MODEL_SIZE if model_needed else 0) + SPACE_MARGIN_MB * 10**6
    problems = interpreter_problems(root)
    if not args.runtime_only:
        problems += ram_problems(total_ram_gb() if ram_gb is None else ram_gb)
    problems += base.space_problems(root, need)
    say(
        plan_text(
            root,
            runtime_needed=runtime_needed,
            model_needed=model_needed,
            assets_needed=bool(assets),
            runtime_only=args.runtime_only,
        )
    )
    if problems:
        raise InstallError(
            "This computer cannot take the plan yet:\n"
            + "\n".join(
                textwrap.fill(problem, width=110, break_on_hyphens=False, initial_indent="  - ", subsequent_indent="    ")
                for problem in problems
            )
        )
    if args.dry_run:
        say("Checks passed. This was a dry run, so nothing was changed.")
        return 0
    if not base.confirmed(args, ask=ask, interactive=interactive):
        say("Nothing was changed.")
        return 1
    started = time.monotonic()
    folder(root).mkdir(parents=True, exist_ok=True)
    say(f"Fetching and unpacking speech.cpp {RUNTIME_VERSION}:")
    executable = install_runtime(root, say=say)
    if args.runtime_only:
        say(f"speech.cpp is in place: {Path(executable).relative_to(root)}")
        return 0
    say("Fetching the model's licence, notice and card:")
    fetch_assets(root, say=say)
    if model_needed:
        say(f"Fetching the Drama 3 model ({MODEL_SIZE / 1e9:.1f} GB):")
        download_model(root, say=say)
    else:
        say(f"The Drama 3 model is already in place ({MODEL_SIZE / 1e9:.1f} GB).")
    receipt = write_receipt(root, executable)  # the Lab, and the speech check, find the model and the runtime through it
    say("Checking that Drama 3 speaks, on the CPU (it can take a few minutes):")
    check = run_speech_check(root)
    write_receipt(root, executable, check)
    say("")
    say(
        f"Drama 3 is installed. It loaded in {check['load_seconds']:.0f} s and spoke {check['audio_seconds']:.1f} s in a voice "
        f"made from a short description in {check['total_seconds']:.0f} s, on the CPU. The whole install took "
        f"{time.monotonic() - started:.0f} s."
    )
    say(f"  Listen to it: {Path(check['wav']).relative_to(root)}")
    say(f"  Receipt: {receipt.relative_to(root)}")
    say(
        "  Next: start the studio (the Fel's Voice Lab shortcut, or serve.bat) and choose Fish Audio Drama 3 in Model studio. "
        "Built with Fish Audio."
    )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="accept the plan, and the model's licence, without asking")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run the checks; change nothing")
    parser.add_argument("--runtime-only", action="store_true", help="install the speech.cpp runtime alone")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (InstallError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Run the installer again to continue; the pieces already downloaded are kept.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
