"""Install Breeze TTS 2 for an NVIDIA graphics card: .venv-breeze with pinned packages and the pinned BF16 checkpoint, then a
speech check on the card.

    .venv\\Scripts\\python.exe tools\\install_breeze.py
    .venv\\Scripts\\python.exe tools\\install_breeze.py --dry-run
    .venv\\Scripts\\python.exe tools\\install_breeze.py --yes
    .venv\\Scripts\\python.exe tools\\install_breeze.py --checkpoint-only

It prints what it will do and asks before it changes anything (--yes accepts the plan, --dry-run only shows it). Run it with
the lab's own Python, which setup.bat creates; setup-breeze.bat does that for you. Breeze TTS 2's weights are under the
BreezeBlue Research and Non-Commercial License, which also covers what you make with them; the plan says so first, and
installing them means accepting it. Breeze's code is already in this folder (breeze/), so .venv-breeze, made from the same
Python 3.12 as .venv, gets only the packages it imports, from requirements/breeze-nvidia.txt with every file's SHA-256 checked;
the script refuses to replace a package that is already installed at another version. The Lab runs Breeze only on a graphics
card, with its BF16 weights, which peaked at 7.5 GB of graphics memory, so the list holds the CUDA 13.0 build of PyTorch 2.12
and there is no CPU build: the installer stops on a computer without an NVIDIA driver, or with a card or driver that build
cannot run on, or with less graphics memory than Breeze needs (--nvidia is accepted and changes nothing). The Lab has not been
tried on an NVIDIA card yet. The checkpoint's files, listed in catalog/breeze_tts2_snapshot.json, come from Hugging Face at the
pinned revision into breeze\\models, where Breeze's loader looks, resume after an interruption, and are kept only when their
SHA-256 matches. Running it again installs only what is missing. --checkpoint-only fetches the model files and leaves the
packages alone, for a .venv-breeze you made yourself with another GPU build of PyTorch: the speech check then runs on that
card. The environment, download, hash and pip helpers are the ones install_kokoro.py uses.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from tools import install_kokoro as base
except ImportError:  # run as a script, which puts tools\ first on the path and not the lab folder
    import install_kokoro as base

InstallError = base.InstallError
ROOT = Path(__file__).resolve().parents[1]
LOCK = "requirements/breeze-nvidia.txt"
MANIFEST = "catalog/breeze_tts2_snapshot.json"
ENVIRONMENT = ".venv-breeze"
PYTHON = ENVIRONMENT + "/Scripts/python.exe"
OUTPUT = "out/breeze"
CHECK_WAV = "install-check.wav"
RECEIPT = "installation.json"
WEIGHTS = "Breeze-TTS-2-bf16.safetensors"
# Measured: 1976 MB of wheels, 1926 MB of them PyTorch's CUDA build, which takes about 4.5 GB installed (an estimate: it has not
# been installed on an NVIDIA computer yet); the other packages take about 0.3 GB.
PACKAGES_DOWNLOAD_MB = 2000
PACKAGES_DISK_MB = 4800
LONGEST_PACKAGE_PATH = 140
SPACE_MARGIN_MB = 500
# The BF16 weights alone are 6.5 GiB, and Breeze peaked at 7.5 GiB, on an RTX 5090 (its model card) as on the AMD card the Lab
# checked this installer on, so an 8 GB card, which reports a little under 8 GiB and shares it with the screen, is too small.
GPU_MEMORY_GB = 10
NVIDIA_BUILD = "Breeze TTS 2's NVIDIA build (PyTorch 2.12 with CUDA 13.0)"
NVIDIA_CARDS = base.NVIDIA_CARDS
NVIDIA_LOWEST_CAPABILITY = base.NVIDIA_LOWEST_CAPABILITY
NVIDIA_DRIVER = base.NVIDIA_DRIVER
CUDA_NEEDS = base.CUDA_NEEDS
NO_NVIDIA_DRIVER = (
    "The Lab runs Breeze TTS 2 only on an NVIDIA graphics card, and this computer has no NVIDIA driver "
    "(System32\\nvcuda.dll is missing). Install the driver from nvidia.com first. On a computer with another "
    "graphics card, make .venv-breeze yourself with a GPU build of PyTorch for it, then run this with "
    "--checkpoint-only."
)
CHECK_TIMEOUT = 1800

# Loads Breeze on the graphics card through the Lab's own worker (its load already speaks a short line, so missing or broken
# weights show there), then speaks the Models page's check sentence in a voice made from a description, as its Test does.
SPEECH_CHECK = """
import json, sys, time
from pathlib import Path
root, wav_path = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(root))
import soundfile
import torch
import engine_checks
from breeze_worker import BreezeWorker
from common import to_numpy

worker = BreezeWorker("cuda")
started = time.perf_counter()
loaded = worker.load("eager")
load_seconds = time.perf_counter() - started
request = {key: value for key, value in engine_checks.smoke_request("breeze-tts-2", loaded, root=root).items() if key != "engine"}
wav, rate, info = worker.generate(request)
wav = to_numpy(wav)    # Breeze hands back a tensor; the Lab's worker runtime makes the same conversion
soundfile.write(wav_path, wav, rate)
print(json.dumps({"problem": engine_checks.audible(wav, rate), "load_seconds": load_seconds, "total_seconds": info["total_s"],
                  "audio_seconds": wav.size / rate, "torch": torch.__version__, "device": "cuda",
                  "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
                  "peak_gpu_gb": torch.cuda.max_memory_reserved() / 2**30 if torch.cuda.is_available() else None}))
"""


def environment_python(root):
    return base.env_python(root, ENVIRONMENT)


def environment_versions(root, *, runner=subprocess.run):
    """The packages installed in .venv-breeze by canonical name, read by its own Python; {} when it has none yet."""
    return base.env_versions(root, ENVIRONMENT, runner=runner)


def interpreter_problems(root, **facts):
    """Why this Python is not the one to run the installer with, as sentences (an empty list when it is)."""
    return base.own_env_problems(root, ENVIRONMENT, "tools\\install_breeze.py", **facts)


def package_problems(different, *, has_pip=True):
    """Why pip should not be run in .venv-breeze: it has no pip, or packages are installed there at other versions."""
    return base.env_package_problems(ENVIRONMENT, "Breeze TTS 2", different, has_pip=has_pip)


def checkpoint_only_problems(root, installed):
    """--checkpoint-only keeps the packages it finds, so there must be an environment that has Transformers in it."""
    return base.env_checkpoint_only_problems(root, ENVIRONMENT, "Breeze TTS 2", "transformers", installed)


def long_path_problems(root, manifest, *, long_paths=None):
    """Windows stops at 259 characters unless long paths are on; torch and transformers have deep folders."""
    lengths = [len(str(base.snapshot_dir(root, manifest).resolve())) + 1 + len(item["path"]) for item in manifest["files"]]
    return base.env_long_path_problems(root, ENVIRONMENT, LONGEST_PACKAGE_PATH, lengths, long_paths=long_paths)


def pinned_packages(root):
    """The packages to install: requirements/breeze-nvidia.txt."""
    return base.read_lock(Path(root) / LOCK)


def card_problems(cards):
    """Why Breeze's NVIDIA build would not run on these NVIDIA cards: a card older than the GTX 16 series, a driver older than
    580, or less than 10 GB of graphics memory. The Models page asks this too before it offers Install."""
    return base.nvidia_card_problems(
        cards,
        build=NVIDIA_BUILD,
        cards_text=NVIDIA_CARDS,
        lowest=NVIDIA_LOWEST_CAPABILITY,
        driver=NVIDIA_DRIVER,
        memory_gb=GPU_MEMORY_GB,
    )


def nvidia_problems(*, present=None, cards=None):
    """Why Breeze cannot run on this computer's graphics card, checked before anything is downloaded."""
    if not (present if present is not None else base.nvidia_driver_present()):
        return [NO_NVIDIA_DRIVER]
    problems = card_problems(base.nvidia_cards() if cards is None else cards)
    return [
        problem
        + (
            " Update the driver from nvidia.com or the NVIDIA app and run the installer again."
            if "needs NVIDIA driver" in problem
            else " Breeze TTS 2 cannot run on this card in the Lab."
        )
        for problem in problems
    ]


def size_gb(size):
    return f"{size / 1e9:.1f} GB"


def licence_text(manifest):
    """What the plan says first: the weights' licence, which also covers what is made with them."""
    return (
        f"Licence: Breeze TTS 2's weights, these converted files and what you make with them on your own computer are "
        f"under the {manifest['license']} ({manifest['license_link']}): research and non-commercial use only; commercial "
        "use needs written permission from RESONIA, INC. Installing them means you accept it, so read it first. Breeze's "
        "code (breeze\\ in this folder) is Apache-2.0."
    )


def plan_text(manifest, packages, todo, pending, *, environment_exists, checkpoint_only):
    """What the installer is about to do, written for the person who ran it."""
    files = {item["path"]: item for item in manifest["files"]}

    def step(number, text):
        return textwrap.fill(
            f"{number}. {text}", width=110, break_on_hyphens=False, initial_indent="  ", subsequent_indent="     "
        )

    if checkpoint_only:
        first = f"Python packages: the ones in {ENVIRONMENT} are left alone (--checkpoint-only)."
    elif not todo:
        first = f"Python packages: all {len(packages)} are already installed in {ENVIRONMENT}."
    else:
        made = (
            f"{ENVIRONMENT} is already here."
            if environment_exists
            else f"First a Python environment of its own, {ENVIRONMENT}, made from the same Python 3.12 as .venv, as every model "
            "has, so that nothing another model installs can clash with its packages."
        )
        first = (
            f"{made} Python packages: {todo} of {len(packages)} to install there, among them the CUDA 13.0 builds of PyTorch "
            "2.12 and torchaudio 2.11 for NVIDIA graphics cards (not yet tried on one) and Transformers 5.17. About "
            f"{base.mb_text(PACKAGES_DOWNLOAD_MB)} to download and {base.mb_text(PACKAGES_DISK_MB)} on disk. pip checks every "
            f"file against a SHA-256 written in {LOCK}."
        )
    fetch = f"{size_gb(sum(item['size'] for item in pending))} to download" if pending else "Everything is already in place"
    folder = manifest["folder"].replace("/", "\\")
    second = (
        f"Breeze TTS 2, revision {manifest['revision'][:7]}, from huggingface.co/{manifest['repo']} (a conversion of "
        f"BreezeBlue/Breeze-TTS-2 for ComfyUI): the {size_gb(files[WEIGHTS]['size'])} BF16 model, the "
        f"{size_gb(files['audio_tokenizer/model.safetensors']['size'])} audio tokenizer, the text tokenizer and settings, "
        f"and the model card, into {folder}. {fetch}. Every file is checked against a SHA-256 written in {MANIFEST}. The "
        "repository's INT8 builds are left out: they need ComfyUI's own kernels."
    )
    third = (
        "A speech check on the graphics card: loads the model without using the network (the load itself speaks a short "
        "line) and speaks one sentence in a voice made from a short description, which you can play afterwards. It takes "
        "a few minutes."
    )
    notes = [
        f"Breeze TTS 2 needs a graphics card with at least {GPU_MEMORY_GB} GB of memory: it peaked at 7.5 GB, so an 8 GB card "
        "is too small. It also wants 12 GB of free RAM before it loads."
    ]
    if not checkpoint_only:
        notes.append(
            "The CUDA 13.0 builds need a GeForce GTX 16 or RTX 20 series card or newer, RTX 50 included, and an NVIDIA "
            "driver of version 580 or later. They carry NVIDIA's CUDA libraries inside them, under NVIDIA's own licence "
            "terms. Nobody has tried the Lab on an NVIDIA card yet: if it works, or does not, a hardware report on GitHub "
            "helps the next person."
        )
    notes.append(
        "The Python packages keep their own licences, nearly all MIT, BSD or Apache; the exceptions are the libsndfile "
        "library inside soundfile (LGPL-2.1), and certifi and tqdm (MPL-2.0). They are installed on this computer and "
        "are not part of this repository."
    )
    intro = textwrap.fill(
        "This adds Breeze TTS 2, a 3B model that makes a voice from a written description, clones one from a "
        "recording and its transcript, and takes direction such as whispering. The Lab runs it only on a "
        "graphics card. Everything is installed into this folder (.venv-breeze, breeze\\models and out\\breeze) "
        "and nothing system-wide; pip keeps its usual download cache in your user profile.",
        width=110,
        break_on_hyphens=False,
    )
    title = "Fel's Voice Lab: Breeze TTS 2 on " + (
        "your own graphics card build" if checkpoint_only else "an NVIDIA graphics card"
    )
    return (
        "\n\n".join(
            [
                title,
                textwrap.fill(licence_text(manifest), width=110, break_on_hyphens=False),
                intro,
                "\n".join([step(1, first), step(2, second), step(3, third)]),
                *(textwrap.fill(note, width=110, break_on_hyphens=False) for note in notes),
            ]
        )
        + "\n"
    )


def make_environment(root, *, runner=subprocess.run):
    """Make .venv-breeze from the Python that .venv was made from; venv puts pip in it from Python's own copy."""
    base.make_env(root, ENVIRONMENT, runner=runner)


def install_packages(root, *, runner=subprocess.run):
    """Install the pinned packages into .venv-breeze."""
    base.pip_install(root, ENVIRONMENT, Path(root) / LOCK, runner=runner)


def check_failure_hint(output):
    """A sentence for the most likely cause of a speech check that failed on the graphics card, from what it printed."""
    hint = base.check_failure_hint(output)
    if hint:
        return hint
    if "out of memory" in output.lower():
        return (
            f"\nThe graphics card ran out of memory: Breeze needs about {GPU_MEMORY_GB} GB of it. Close other programs that "
            "use it (games, or another model loaded in the Lab) and run the installer again."
        )
    if "CUDA" in output or "cuda" in output:
        return (
            f"\nPyTorch could not use the graphics card. {CUDA_NEEDS}: update the driver from nvidia.com and run the "
            "installer again."
        )
    return ""


def run_speech_check(root, *, runner=subprocess.run):
    """In .venv-breeze, offline: load Breeze on the graphics card through the Lab's worker and speak the Models page's check
    sentence in a designed voice."""
    folder = Path(root) / OUTPUT
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / CHECK_WAV
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"}
    try:
        done = runner(
            [str(environment_python(root)), "-c", SPEECH_CHECK, str(root), str(wav)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"The speech check took more than {CHECK_TIMEOUT // 60} minutes, so it was stopped.") from exc
    result = base.last_json_line(done.stdout or "")
    if done.returncode or not isinstance(result, dict):
        output = (done.stdout or "") + (done.stderr or "")
        raise InstallError(
            "The packages and model files are in place, but Breeze TTS 2 did not load or speak:\n"
            + "\n".join(output.strip().splitlines()[-12:])
            + check_failure_hint(output)
        )
    if result["problem"]:
        raise InstallError(f"Breeze TTS 2 loaded but the check clip was not speech: {result['problem']}.")
    return {**result, "wav": wav}


def write_receipt(root, manifest, packages, check):
    """The installation record in out/breeze/installation.json."""
    receipt = {
        "schema": 1,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "environment": ENVIRONMENT,
        "torch": check.get("torch"),
        "device": check.get("device", "cuda"),
        "device_name": check.get("device_name"),
        "packages_pinned": len(packages),
        "lock_sha256": hashlib.sha256((Path(root) / LOCK).read_bytes()).hexdigest(),
        "repo": manifest["repo"],
        "revision": manifest["revision"],
        "license": manifest["license"],
        "license_link": manifest["license_link"],
        "files": [item["path"] for item in manifest["files"]],
        "checkpoint_bytes": sum(item["size"] for item in manifest["files"]),
        "speech_check": {key: check.get(key) for key in ("load_seconds", "total_seconds", "audio_seconds", "peak_gpu_gb")},
    }
    path = Path(root) / OUTPUT / RECEIPT
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path


def run(args, *, say=base.say_now, ask=input, interactive=None):
    root = ROOT
    manifest = base.load_manifest(root / MANIFEST)
    packages = pinned_packages(root)
    exists = environment_python(root).is_file()
    installed = environment_versions(root) if exists else {}
    missing, different = base.package_state(packages, installed)
    pending = base.pending_files(root, manifest, manifest["files"])
    todo = 0 if args.checkpoint_only else len(missing) + len(different)
    packages_need = (PACKAGES_DOWNLOAD_MB + PACKAGES_DISK_MB) * 10**6 if todo else 0
    need = packages_need + sum(item["size"] for item in pending) + SPACE_MARGIN_MB * 10**6
    problems = interpreter_problems(root)
    if args.checkpoint_only:
        problems += checkpoint_only_problems(root, installed)
    else:
        problems += nvidia_problems()
        if todo:
            problems += package_problems(different, has_pip=not exists or "pip" in installed)
    problems += long_path_problems(root, manifest)
    problems += base.space_problems(root, need)
    say(plan_text(manifest, packages, todo, pending, environment_exists=exists, checkpoint_only=args.checkpoint_only))
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
    if todo:
        if not exists:
            say(f"Making {ENVIRONMENT}:")
            make_environment(root)
        say(f"Installing the Python packages into {ENVIRONMENT} (pip prints its own progress):")
        install_packages(root)
        still_missing, still_different = base.package_state(packages, environment_versions(root))
        if still_missing:
            raise InstallError(
                f"pip finished but {len(still_missing)} packages are still missing, for example {still_missing[0]['name']}."
            )
        if still_different:
            item, have = still_different[0]
            raise InstallError(f"pip finished but {item['name']} is {have}, not {item['version']}.")
    say("Fetching the Breeze TTS 2 model files:")
    base.download_checkpoint(root, manifest, manifest["files"], say=say)
    say("Checking that Breeze TTS 2 speaks on the graphics card (a few minutes):")
    check = run_speech_check(root)
    receipt = write_receipt(root, manifest, packages, check)
    memory = f", using {check['peak_gpu_gb']:.1f} GB of graphics memory at most" if check.get("peak_gpu_gb") else ""
    say("")
    say(
        f"Breeze TTS 2 is installed. It loaded in {check['load_seconds']:.0f} s on the "
        f"{check.get('device_name') or 'graphics card'} and spoke {check['audio_seconds']:.1f} s in a voice made from a short "
        f"description in {check['total_seconds']:.1f} s{memory}. The whole install took {time.monotonic() - started:.0f} s."
    )
    say(f"  Listen to it: {Path(check['wav']).relative_to(root)}")
    say(f"  Receipt: {receipt.relative_to(root)}")
    say(
        "  Next: start the studio (the Fel's Voice Lab shortcut, or serve.bat) and choose Breeze TTS 2 in Model studio. What "
        "you make with it is for research and non-commercial use."
    )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--yes", action="store_true", help="accept the plan, and the model's non-commercial licence, without asking"
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run the checks; change nothing")
    parser.add_argument(
        "--nvidia",
        action="store_true",
        help="accepted for the Models page and the other installers; Breeze always gets the NVIDIA build",
    )
    parser.add_argument(
        "--checkpoint-only",
        action="store_true",
        help=f"fetch the model files only and leave the packages in {ENVIRONMENT} alone",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (InstallError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Run the installer again to continue; files already downloaded are kept.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
