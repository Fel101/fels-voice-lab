"""Install VoxCPM2 for an NVIDIA or AMD graphics card: .venv-voxcpm with pinned packages, VoxCPM's source files at the pinned
commit, the pinned checkpoint, then a speech check on the card.

    .venv\\Scripts\\python.exe tools\\install_voxcpm.py
    .venv\\Scripts\\python.exe tools\\install_voxcpm.py --dry-run
    .venv\\Scripts\\python.exe tools\\install_voxcpm.py --yes
    .venv\\Scripts\\python.exe tools\\install_voxcpm.py --checkpoint-only

It prints what it will do and asks before it changes anything (--yes accepts the plan, --dry-run only shows it). Run it with
the lab's own Python, which setup.bat creates; setup-voxcpm.bat does that for you. VoxCPM2 gets an environment of its own,
.venv-voxcpm, made from the same Python 3.12 as .venv, and pip installs requirements/voxcpm-nvidia.txt there with every file's
SHA-256 checked; the script refuses to replace a package that is already installed at another version. VoxCPM itself is not
installed as a package: the source files its inference imports come from GitHub at the commit the Lab was checked with
(catalog/voxcpm2_source.json) into out\\voxcpm2\\source, where the Lab's worker reads them, so its long list of packages for a
demo app, a denoiser, fine-tuning and text normalization is never needed. The Lab runs VoxCPM2 only on a graphics card with at
least 7 GB of memory, so there is no CPU build. On a computer with an NVIDIA driver the list's CUDA 13.0 build of PyTorch 2.12
goes in, and the installer stops for a card or driver that build cannot run on; nobody has tried it on an NVIDIA card yet. On a
computer whose graphics card is an AMD Radeon, AMD's ROCm 7.14 build of PyTorch 2.12 takes its place, with the code for the
card's chip (tools/amd_build.py and requirements/amd-rocm.txt say how); it was checked on an AMD Radeon AI PRO R9700. --gpu and
--nvidia are taken and change nothing. The checkpoint's files, listed in catalog/voxcpm2_snapshot.json, come from Hugging Face
at the pinned revision into the top of hf-cache, resume after an interruption, and are kept only when their SHA-256 matches.
Running it again installs only what is missing. --checkpoint-only fetches the source and model files and leaves the packages
alone, for a .venv-voxcpm you made yourself with another GPU build of PyTorch (or without PyTorch, borrowing the packages of a
.venv-fqwen, as the Lab first ran it): the speech check then runs on that card. The environment, download, hash and pip helpers
are the ones install_kokoro.py uses, so a fix there reaches this installer too.
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
    from tools import amd_build as amd
    from tools import install_kokoro as base
except ImportError:  # run as a script, which puts tools\ first on the path and not the lab folder
    # The lab folder goes on the path instead, so that this script and tools/amd_build.py share one install_kokoro and
    # its InstallError.
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from tools import amd_build as amd
    from tools import install_kokoro as base

InstallError = base.InstallError
ROOT = Path(__file__).resolve().parents[1]
LOCK = "requirements/voxcpm-nvidia.txt"
MANIFEST = "catalog/voxcpm2_snapshot.json"
SOURCE_MANIFEST = "catalog/voxcpm2_source.json"
ENVIRONMENT = ".venv-voxcpm"
PYTHON = ENVIRONMENT + "/Scripts/python.exe"
SOURCE = "out/voxcpm2/source"
OUTPUT = "out/voxcpm2"
CHECK_WAV = "install-check.wav"
RECEIPT = "installation.json"
RAW_HOST = "https://raw.githubusercontent.com"
# Measured: 2069 MB of wheels, 1926 MB of them PyTorch's CUDA build. The CUDA build of PyTorch takes about 4.5 GB installed (an
# estimate: it has not been installed on an NVIDIA computer yet) and the other packages about 0.6 GB.
PACKAGES_DOWNLOAD_MB = 2100
PACKAGES_DISK_MB = 5100
# The packages other than PyTorch, to which AMD's build adds its own (amd_build.sizes_mb).
OTHER_DOWNLOAD_MB = 150
OTHER_DISK_MB = 700
LONGEST_PACKAGE_PATH = 140
SPACE_MARGIN_MB = 500
GPU_MEMORY_GB = 7  # voxcpm_worker.MIN_GPU_GIB: it peaked at 5.6 GB, and an 8 GB card reports a little under 8
# The same CUDA 13.0 build of PyTorch 2.12 as the other NVIDIA builds: code for compute capability 7.5 and up (GeForce GTX 16 and
# RTX 20 series and newer, RTX 50 included), with NVIDIA driver 580 or later.
NVIDIA_BUILD = "VoxCPM2's NVIDIA build (PyTorch 2.12 with CUDA 13.0)"
NVIDIA_CARDS = base.NVIDIA_CARDS
NVIDIA_LOWEST_CAPABILITY = base.NVIDIA_LOWEST_CAPABILITY
NVIDIA_DRIVER = base.NVIDIA_DRIVER
CUDA_NEEDS = base.CUDA_NEEDS
NO_NVIDIA_DRIVER = (
    "The Lab runs VoxCPM2 only on a graphics card, an NVIDIA card or an AMD Radeon, and this computer has neither an NVIDIA "
    "driver (System32\\nvcuda.dll is missing) nor an AMD graphics card that Windows lists. Install the card's driver first: "
    "from nvidia.com, or AMD Software: Adrenalin Edition from amd.com. On a computer with another graphics card, make "
    ".venv-voxcpm yourself with a GPU build of PyTorch for it, then run this with --checkpoint-only."
)
CHECK_TIMEOUT = 1800

# Loads VoxCPM2 on the graphics card through the Lab's own worker, so the check takes the path the studio takes, and speaks the
# Models page's check sentence in a voice made from a description, as its Test does. A .venv-voxcpm without PyTorch borrows
# .venv-fqwen's packages here as engine_worker.py does.
SPEECH_CHECK = """
import importlib.util, json, sys, time
from pathlib import Path
root, wav_path = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(root))
if importlib.util.find_spec("torch") is None:
    sys.path.append(str(root / ".venv-fqwen" / "Lib" / "site-packages"))
import soundfile
import engine_checks
from voxcpm_worker import VoxCPMWorker

worker = VoxCPMWorker("cuda")
started = time.perf_counter()
loaded = worker.load()
load_seconds = time.perf_counter() - started
request = {key: value for key, value in engine_checks.smoke_request("voxcpm2", loaded, root=root).items() if key != "engine"}
wav, rate, info = worker.generate(request)
soundfile.write(wav_path, wav, rate)
print(json.dumps({"problem": engine_checks.audible(wav, rate), "load_seconds": load_seconds, "total_seconds": info["total_s"],
                  "audio_seconds": wav.size / rate, "torch": loaded.get("torch"), "device": "cuda",
                  "device_name": loaded.get("device_name"), "peak_gpu_gb": info.get("peak_vram_bytes", 0) / 2**30}))
"""


def environment_python(root):
    return base.env_python(root, ENVIRONMENT)


def environment_versions(root, *, runner=subprocess.run):
    """The packages installed in .venv-voxcpm by canonical name, read by its own Python; {} when it has none yet."""
    return base.env_versions(root, ENVIRONMENT, runner=runner)


def interpreter_problems(root, **facts):
    """Why this Python is not the one to run the installer with, as sentences (an empty list when it is)."""
    return base.own_env_problems(root, ENVIRONMENT, "tools\\install_voxcpm.py", **facts)


def package_problems(different, *, has_pip=True):
    """Why pip should not be run in .venv-voxcpm: it has no pip, or packages are installed there at other versions."""
    return base.env_package_problems(ENVIRONMENT, "VoxCPM2", different, has_pip=has_pip)


def checkpoint_only_problems(root):
    """--checkpoint-only keeps the packages it finds, so there must be a .venv-voxcpm to run the speech check in."""
    if not environment_python(root).is_file():
        return [
            f"--checkpoint-only adds the source and model files to a {ENVIRONMENT} that already runs VoxCPM2, and there is "
            f"no {ENVIRONMENT} here. Run the installer without it to make one."
        ]
    return []


def long_path_problems(root, manifest, source, *, long_paths=None):
    """Windows stops at 259 characters unless long paths are on; torch and transformers have deep folders."""
    lengths = [len(str(base.snapshot_dir(root, manifest).resolve())) + 1 + len(item["path"]) for item in manifest["files"]]
    lengths += [len(str((Path(root) / SOURCE).resolve())) + 1 + len(item["path"]) for item in source["files"]]
    return base.env_long_path_problems(root, ENVIRONMENT, LONGEST_PACKAGE_PATH, lengths, long_paths=long_paths)


def pinned_packages(root, card=None):
    """The packages to install: requirements/voxcpm-nvidia.txt, or for an AMD card (hardware.amd_cards) that list with AMD's
    build of PyTorch and the code for the card's chip."""
    return amd.pinned_packages(root, LOCK, card["chip"]) if card else base.read_lock(Path(root) / LOCK)


def load_source(path):
    """catalog/voxcpm2_source.json: the VoxCPM files to fetch, from which repository and at which commit."""
    try:
        source = json.loads(Path(path).read_text(encoding="utf-8"))
        if not all(key in source for key in ("repo", "commit", "license", "files")):
            raise ValueError("it lacks repo, commit, license or files")
    except (OSError, ValueError, TypeError) as exc:
        raise InstallError(
            f"The source file list {Path(path).name} is missing or unreadable ({exc}). Restore it from the repository."
        ) from exc
    return source


def card_problems(cards):
    """Why VoxCPM2's NVIDIA build would not run on these NVIDIA cards: a card older than the GTX 16 series, a driver older than
    580, or less than 7 GB of graphics memory. The Models page asks this too before it offers Install."""
    return base.nvidia_card_problems(
        cards,
        build=NVIDIA_BUILD,
        cards_text=NVIDIA_CARDS,
        lowest=NVIDIA_LOWEST_CAPABILITY,
        driver=NVIDIA_DRIVER,
        memory_gb=GPU_MEMORY_GB,
    )


def nvidia_problems(*, present=None, cards=None):
    """Why VoxCPM2 cannot run on this computer's graphics card, checked before anything is downloaded: no NVIDIA driver, or a
    card or driver the CUDA 13.0 build cannot run on."""
    if not (present if present is not None else base.nvidia_driver_present()):
        return [NO_NVIDIA_DRIVER]
    problems = card_problems(base.nvidia_cards() if cards is None else cards)
    return [
        problem
        + (
            " Update the driver from nvidia.com or the NVIDIA app and run the installer again."
            if "needs NVIDIA driver" in problem
            else " VoxCPM2 cannot run on this card in the Lab."
        )
        for problem in problems
    ]


def graphics():
    """Which build goes in: ("nvidia", []) with an NVIDIA driver, ("amd", cards) for AMD cards, (None, []) for neither."""
    return amd.graphics()


def amd_card_problems(cards, *, root=None, environ=None):
    """Why VoxCPM2 could not run on this computer's AMD cards with AMD's build: no card with a chip the build has code for, a
    setting that names a card that is not there, or less than 7 GB of graphics memory. The Models page asks this too before it
    offers Install."""
    return amd.card_problems(
        cards, model="VoxCPM2", root=ROOT if root is None else root, memory_gb=GPU_MEMORY_GB, environ=environ
    )


def gpu_problems(vendor, cards, root=None):
    """Why VoxCPM2 cannot run on this computer's graphics card, checked before anything is downloaded."""
    if vendor == "nvidia":
        return nvidia_problems()
    if vendor == "amd":
        return amd_card_problems(cards, root=root)
    return [NO_NVIDIA_DRIVER]


def pending_source(root, source):
    """The source files that are not in place with their pinned content."""
    folder = Path(root) / SOURCE
    return [
        item
        for item in source["files"]
        if not (folder / item["path"]).is_file()
        or (folder / item["path"]).stat().st_size != item["size"]
        or base.sha256_file(folder / item["path"]) != item["sha256"]
    ]


def fetch_source(root, source, *, say=base.say_now, pause=time.sleep):
    """Fetch the VoxCPM source files from GitHub at the pinned commit, each kept only when its SHA-256 matches."""
    folder = Path(root) / SOURCE
    for item in source["files"]:
        if folder.resolve() not in (folder / item["path"]).resolve().parents:
            raise InstallError(f"The source file list names a path outside the source folder: {item['path']}")
    fetched = 0
    for item in pending_source(root, source):
        destination = folder / item["path"]
        if item["size"] == 0:  # an empty __init__.py: there is nothing to download
            if item["sha256"] != hashlib.sha256(b"").hexdigest():
                raise InstallError(f"The source file list gives {item['path']} no bytes but the SHA-256 of some.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"")
        else:
            url = f"{RAW_HOST}/{source['repo']}/{source['commit']}/{item['path']}"
            base.fetch(url, destination, item["size"], item["sha256"], say=say, pause=pause)
        fetched += 1
    say(f"  {fetched} of {len(source['files'])} source files fetched" if fetched else "  already in place")


def size_gb(size):
    return f"{size / 1e9:.1f} GB"


def packages_mb(card=None):
    """(download, disk) in MB for the packages: the NVIDIA build's, or with AMD's build for the card's chip."""
    if card:
        return amd.sizes_mb(card["chip"], OTHER_DOWNLOAD_MB, OTHER_DISK_MB)
    return PACKAGES_DOWNLOAD_MB, PACKAGES_DISK_MB


def plan_text(
    manifest, source, packages, todo, pending, source_pending, *, environment_exists, checkpoint_only, card=None, cards=()
):
    """What the installer is about to do, written for the person who ran it. card is the AMD card it installs for (None for
    the NVIDIA build), among cards, every AMD card this computer has."""
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
        build = (
            f"AMD's ROCm 7.14 builds of PyTorch 2.12 and torchaudio 2.11 with the code for the {amd.card_text(card)}"
            if card
            else "the CUDA 13.0 builds of PyTorch 2.12 and torchaudio 2.11 for NVIDIA graphics cards (not yet tried on one)"
        )
        download, disk = packages_mb(card)
        lists = f"{LOCK} and {amd.LOCK}" if card else LOCK
        first = (
            f"{made} Python packages: {todo} of {len(packages)} to install there, among them {build}, Transformers 5.15 and "
            f"librosa. About {base.mb_text(download)} to download and {base.mb_text(disk)} on disk. pip checks every file "
            f"against a SHA-256 written in {lists}."
        )
    source_size = sum(item["size"] for item in source["files"])
    second = (
        f"VoxCPM's source: the {len(source['files'])} files its inference imports ({source_size // 1000} KB), from "
        f"github.com/{source['repo']} at commit {source['commit'][:7]}, into {SOURCE.replace('/', chr(92))}. "
        + (f"{len(source_pending)} to fetch" if source_pending else "Everything is already in place")
        + f". Every file is checked against a SHA-256 written in {SOURCE_MANIFEST}."
    )
    fetch = f"{size_gb(sum(item['size'] for item in pending))} to download" if pending else "Everything is already in place"
    third = (
        f"VoxCPM2, revision {manifest['revision'][:7]}, from huggingface.co/{manifest['repo']}: the "
        f"{size_gb(files['model.safetensors']['size'])} model, the {size_gb(files['audiovae.pth']['size'])} audio decoder, "
        f"the tokenizer and settings, and the model card. {fetch}. Every file is checked against a SHA-256 written in "
        f"{MANIFEST}."
    )
    fourth = (
        f"A speech check on {'the ' + card['name'] if card else 'the graphics card'}: loads the model without using the "
        "network and speaks one sentence in a voice made from a short description, which you can play afterwards. It takes "
        "a minute or two."
    )
    notes = [
        f"VoxCPM2 needs a graphics card with at least {GPU_MEMORY_GB} GB of memory: it used up to 5.6 GB when it was measured."
    ]
    if card:
        notes.append(amd.plan_note(card, cards))
    elif not checkpoint_only:
        notes.append(
            "The CUDA 13.0 builds need a GeForce GTX 16 or RTX 20 series card or newer, RTX 50 included, and an NVIDIA "
            "driver of version 580 or later. They carry NVIDIA's CUDA libraries inside them, under NVIDIA's own licence "
            "terms. Nobody has tried the Lab on an NVIDIA card yet: if it works, or does not, a hardware report on GitHub "
            "helps the next person."
        )
    notes.append(
        f"Licences: the VoxCPM2 weights and VoxCPM's code are {manifest['license']} (OpenBMB). The Python packages keep "
        "their own licences, nearly all MIT, BSD or Apache. The exceptions are soxr and the libsndfile library inside "
        "soundfile (LGPL-2.1), and certifi and tqdm (MPL-2.0). They are installed on this computer and are not part of "
        "this repository."
    )
    intro = textwrap.fill(
        "This adds VoxCPM2, a 2B model that makes a voice from a written description, clones one from a "
        "recording, and speaks at 48 kHz. The Lab runs it only on a graphics card. Everything is installed into "
        "this folder (.venv-voxcpm, hf-cache and out\\voxcpm2) and nothing system-wide; pip keeps its usual "
        "download cache in your user profile.",
        width=110,
        break_on_hyphens=False,
    )
    title = "Fel's Voice Lab: VoxCPM2 on " + (
        "your own graphics card build" if checkpoint_only else "an AMD graphics card" if card else "an NVIDIA graphics card"
    )
    return (
        "\n\n".join(
            [
                title,
                intro,
                "\n".join([step(1, first), step(2, second), step(3, third), step(4, fourth)]),
                *(textwrap.fill(note, width=110, break_on_hyphens=False) for note in notes),
            ]
        )
        + "\n"
    )


def make_environment(root, *, runner=subprocess.run):
    """Make .venv-voxcpm from the Python that .venv was made from; venv puts pip in it from Python's own copy."""
    base.make_env(root, ENVIRONMENT, runner=runner)


def install_packages(root, *, card=None, runner=subprocess.run):
    """Install the pinned packages into .venv-voxcpm; for an AMD card, with AMD's build of PyTorch and its chip's code (the
    lists pip reads are written into out/voxcpm2)."""
    if card:
        amd.install_packages(root, ENVIRONMENT, LOCK, card["chip"], Path(root) / OUTPUT, runner=runner)
    else:
        base.pip_install(root, ENVIRONMENT, Path(root) / LOCK, runner=runner)


def check_failure_hint(output, *, amd_card=False):
    """A sentence for the most likely cause of a speech check that failed on the graphics card, from what it printed."""
    hint = base.check_failure_hint(output)
    if hint:
        return hint
    if amd_card:
        return amd.failure_hint(output)
    if "out of memory" in output.lower():
        return (
            "\nThe graphics card ran out of memory. Close other programs that use it (games, or another model loaded in the "
            "Lab) and run the installer again."
        )
    if "CUDA" in output or "cuda" in output:
        return (
            f"\nPyTorch could not use the graphics card. {CUDA_NEEDS}: update the driver from nvidia.com and run the "
            "installer again."
        )
    return ""


def run_speech_check(root, *, number=None, runner=subprocess.run):
    """In .venv-voxcpm, offline: load VoxCPM2 on the graphics card through the Lab's worker and speak the Models page's check
    sentence in a designed voice. number is the AMD card's number to PyTorch (amd_build.settle_number), None for an NVIDIA
    card."""
    folder = Path(root) / OUTPUT
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / CHECK_WAV
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"}
    if number is not None:
        env[amd.VARIABLE] = str(number)
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
            "The packages, source and model files are in place, but VoxCPM2 did not load or speak:\n"
            + "\n".join(output.strip().splitlines()[-12:])
            + check_failure_hint(output, amd_card=number is not None)
        )
    if result["problem"]:
        raise InstallError(f"VoxCPM2 loaded but the check clip was not speech: {result['problem']}.")
    return {**result, "wav": wav}


def write_receipt(root, manifest, source, packages, check, *, card=None, number=None):
    """The installation record in out/voxcpm2/installation.json."""
    receipt = {
        "schema": 1,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "environment": ENVIRONMENT,
        "torch": check.get("torch"),
        "device": check.get("device", "cuda"),
        "device_name": check.get("device_name"),
        **amd.receipt_fields(root, card, number),
        "packages_pinned": len(packages),
        "lock_sha256": hashlib.sha256((Path(root) / LOCK).read_bytes()).hexdigest(),
        "source": {
            "repo": source["repo"],
            "commit": source["commit"],
            "license": source["license"],
            "files": len(source["files"]),
        },
        "repo": manifest["repo"],
        "revision": manifest["revision"],
        "license": manifest["license"],
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
    source = load_source(root / SOURCE_MANIFEST)
    vendor, cards = (None, []) if args.checkpoint_only else graphics()
    card = amd.chosen_card(cards, root) if vendor == "amd" else None
    if vendor == "amd" and card is None:
        raise InstallError(amd.no_card_text("VoxCPM2", amd_card_problems(cards, root=root)))
    packages = pinned_packages(root, card)
    exists = environment_python(root).is_file()
    installed = environment_versions(root) if exists else {}
    missing, different = base.package_state(packages, installed)
    pending = base.pending_files(root, manifest, manifest["files"])
    source_pending = pending_source(root, source)
    todo = 0 if args.checkpoint_only else len(missing) + len(different)
    packages_need = sum(packages_mb(card)) * 10**6 if todo else 0
    need = packages_need + sum(item["size"] for item in pending) + SPACE_MARGIN_MB * 10**6
    problems = interpreter_problems(root)
    if args.checkpoint_only:
        problems += checkpoint_only_problems(root)
    else:
        problems += gpu_problems(vendor, cards, root)
        if todo:
            problems += package_problems(different, has_pip=not exists or "pip" in installed)
    problems += long_path_problems(root, manifest, source)
    problems += base.space_problems(root, need)
    say(
        plan_text(
            manifest,
            source,
            packages,
            todo,
            pending,
            source_pending,
            environment_exists=exists,
            checkpoint_only=args.checkpoint_only,
            card=card,
            cards=cards,
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
    if todo:
        if not exists:
            say(f"Making {ENVIRONMENT}:")
            make_environment(root)
        say(f"Installing the Python packages into {ENVIRONMENT} (pip prints its own progress):")
        install_packages(root, card=card)
        still_missing, still_different = base.package_state(packages, environment_versions(root))
        if still_missing:
            raise InstallError(
                f"pip finished but {len(still_missing)} packages are still missing, for example {still_missing[0]['name']}."
            )
        if still_different:
            item, have = still_different[0]
            raise InstallError(f"pip finished but {item['name']} is {have}, not {item['version']}.")
    number = None
    if card:
        number, note = amd.settle_number(root, amd.hip_devices(root, ENVIRONMENT), card)
        if note:
            say(note)
    say("Fetching VoxCPM's source files:")
    fetch_source(root, source, say=say)
    say("Fetching the VoxCPM2 model files:")
    base.download_checkpoint(root, manifest, manifest["files"], say=say)
    say("Checking that VoxCPM2 speaks on the graphics card (a minute or two):")
    check = run_speech_check(root, number=number)
    receipt = write_receipt(root, manifest, source, packages, check, card=card, number=number)
    memory = f", using {check['peak_gpu_gb']:.1f} GB of graphics memory at most" if check.get("peak_gpu_gb") else ""
    say("")
    say(
        f"VoxCPM2 is installed. It loaded in {check['load_seconds']:.0f} s on the {check.get('device_name') or 'graphics card'} "
        f"and spoke {check['audio_seconds']:.1f} s in a voice made from a short description in {check['total_seconds']:.1f} s"
        f"{memory}. The whole install took {time.monotonic() - started:.0f} s."
    )
    say(f"  Listen to it: {Path(check['wav']).relative_to(root)}")
    say(f"  Receipt: {receipt.relative_to(root)}")
    say("  Next: start the studio (the Fel's Voice Lab shortcut, or serve.bat) and choose VoxCPM2 in Model studio.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="accept the plan without asking")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run the checks; change nothing")
    parser.add_argument(
        "--gpu",
        "--nvidia",
        action="store_true",
        help="accepted for the Models page and the other installers; VoxCPM2 always gets the GPU build for this "
        "computer's graphics card",
    )
    parser.add_argument(
        "--checkpoint-only",
        action="store_true",
        help=f"fetch the source and model files only and leave the packages in {ENVIRONMENT} alone",
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
