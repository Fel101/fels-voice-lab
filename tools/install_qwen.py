"""Install one of the four Qwen3-TTS models for an NVIDIA or AMD graphics card: the .venv-fqwen they share with pinned
packages, the model's pinned checkpoint into hf-cache, then a speech check on the card.

    .venv\\Scripts\\python.exe tools\\install_qwen.py --model 0.6b-base
    .venv\\Scripts\\python.exe tools\\install_qwen.py --model 1.7b-voice-design --dry-run
    .venv\\Scripts\\python.exe tools\\install_qwen.py --model 1.7b-custom-voice --yes
    .venv\\Scripts\\python.exe tools\\install_qwen.py --model 1.7b-base --checkpoint-only

--model chooses the model: 0.6b-base and 1.7b-base clone a voice from a recording, 1.7b-voice-design makes a voice from a
written description, and 1.7b-custom-voice speaks in nine preset voices that take direction (the Models page's catalog ids,
such as qwen3-tts-0.6b-base, work too). It prints what it will do and asks before it changes anything (--yes accepts the plan,
--dry-run only shows it). Run it with the lab's own Python, which setup.bat creates; setup-qwen.bat does that for you. The four
models share .venv-fqwen, an environment of their own made from the same Python 3.12 as .venv, and pip installs
requirements/qwen-nvidia.txt there with every file's SHA-256 checked; the script refuses to replace a package that is already
installed at another version. The Lab runs Qwen3-TTS only on a graphics card, because its fast decoder (faster-qwen3-tts)
records CUDA graphs, so there is no CPU build. On a computer with an NVIDIA driver the list's CUDA 13.0 build of PyTorch 2.12
goes in, and the installer stops for a card or driver that build cannot run on; nobody has tried it on an NVIDIA card yet. On a
computer whose graphics card is an AMD Radeon, AMD's ROCm 7.14 build of PyTorch 2.12 takes its place, with the code for the
card's chip (tools/amd_build.py and requirements/amd-rocm.txt say how); it was checked on an AMD Radeon AI PRO R9700. --nvidia
is accepted and changes nothing. The model's files, listed in catalog/qwen3_tts_<model>_snapshot.json, come from Hugging Face at
the pinned revision, resume after an interruption, and are kept only when their SHA-256 matches. Running it again installs only
what is missing. --checkpoint-only fetches the model files and leaves the packages alone, for a .venv-fqwen you made yourself
with another GPU build of PyTorch: the speech check then runs on that card. The environment, download, hash and pip helpers are
the ones install_kokoro.py uses, so a fix there reaches this installer too.
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
    import install_kokoro as base

    import amd_build as amd

InstallError = base.InstallError
ROOT = Path(__file__).resolve().parents[1]
LOCK = "requirements/qwen-nvidia.txt"
ENVIRONMENT = ".venv-fqwen"
PYTHON = ENVIRONMENT + "/Scripts/python.exe"
OUTPUT = "out/qwen"
REFERENCE = "reference/af_heart.wav"
# The four models: catalog id -> the name --model takes, the snapshot list, the name the Lab shows, what it does, and how its
# speech check speaks.
MODELS = {
    "qwen3-tts-0.6b-base": {
        "short": "0.6b-base",
        "manifest": "catalog/qwen3_tts_0.6b_base_snapshot.json",
        "name": "Qwen3-TTS 0.6B Base",
        "does": "clones a voice from a short recording, and is the smallest and fastest of the four",
        "check": "in a voice cloned from reference\\af_heart.wav",
        "clones": True,
    },
    "qwen3-tts-1.7b-base": {
        "short": "1.7b-base",
        "manifest": "catalog/qwen3_tts_1.7b_base_snapshot.json",
        "name": "Qwen3-TTS 1.7B Base",
        "does": "clones a voice from a short recording",
        "check": "in a voice cloned from reference\\af_heart.wav",
        "clones": True,
    },
    "qwen3-tts-1.7b-voice-design": {
        "short": "1.7b-voice-design",
        "manifest": "catalog/qwen3_tts_1.7b_voice_design_snapshot.json",
        "name": "Qwen3-TTS 1.7B VoiceDesign",
        "does": "makes a new voice from a written description",
        "check": "in a voice made from a short description",
        "clones": False,
    },
    "qwen3-tts-1.7b-custom-voice": {
        "short": "1.7b-custom-voice",
        "manifest": "catalog/qwen3_tts_1.7b_custom_voice_snapshot.json",
        "name": "Qwen3-TTS 1.7B CustomVoice",
        "does": "speaks in nine preset voices that take direction",
        "check": "in its first preset voice",
        "clones": False,
    },
}
SHORT_NAMES = {spec["short"]: engine for engine, spec in MODELS.items()}
# Measured: 2082 MB of wheels, 1926 MB of them PyTorch's CUDA build. Installed (with the CPU build of PyTorch in its place), the
# other packages take 0.7 GB and the longest path below site-packages is 126 characters; the CUDA build of PyTorch takes about
# 4.5 GB (an estimate: it has not been installed on an NVIDIA computer yet), and its folders get a margin. Windows stops at 259
# characters unless long paths are switched on.
PACKAGES_DOWNLOAD_MB = 2100
PACKAGES_DISK_MB = 5300
# The packages other than PyTorch, to which AMD's build adds its own (amd_build.sizes_mb).
OTHER_DOWNLOAD_MB = 160
OTHER_DISK_MB = 800
LONGEST_PACKAGE_PATH = 140
SPACE_MARGIN_MB = 500
# The same CUDA 13.0 build of PyTorch 2.12 as Kokoro's and Chatterbox's NVIDIA builds: code for compute capability 7.5 and up
# (GeForce GTX 16 and RTX 20 series and newer, RTX 50 included), with NVIDIA driver 580 or later.
NVIDIA_BUILD = "Qwen3-TTS's NVIDIA build (PyTorch 2.12 with CUDA 13.0)"
NVIDIA_CARDS = base.NVIDIA_CARDS
NVIDIA_LOWEST_CAPABILITY = base.NVIDIA_LOWEST_CAPABILITY
NVIDIA_DRIVER = base.NVIDIA_DRIVER
CUDA_NEEDS = base.CUDA_NEEDS
NO_NVIDIA_DRIVER = (
    "The Lab runs Qwen3-TTS only on a graphics card, an NVIDIA card or an AMD Radeon, and this computer has neither an "
    "NVIDIA driver (System32\\nvcuda.dll is missing) nor an AMD graphics card that Windows lists. Install the card's driver "
    "first: from nvidia.com, or AMD Software: Adrenalin Edition from amd.com. On a computer with another graphics card, make "
    ".venv-fqwen yourself with a GPU build of PyTorch for it and faster-qwen3-tts 0.4.0, then run this with --checkpoint-only."
)
CHECK_TIMEOUT = 1800

# Loads the model on the graphics card through the Lab's own Qwen worker, so the check takes the path the studio takes, warms it
# up with a short line, and then speaks the Models page's check sentence the way its Test does.
SPEECH_CHECK = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import soundfile
import torch
import engine_checks
from qwen_worker import QwenWorker

root, engine, wav_path = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
worker = QwenWorker(engine, "cuda")
started = time.perf_counter()
loaded = worker.load()
request = {key: value for key, value in engine_checks.smoke_request(engine, loaded, root=root).items() if key != "engine"}
# The first sentence also records the decoder's CUDA graphs, as the worker's own warm-up does when the studio loads the model.
worker.generate({**request, "text": "Hello there."})
load_seconds = time.perf_counter() - started
wav, rate, info = worker.generate(request)
soundfile.write(wav_path, wav, rate)
print(json.dumps({"problem": engine_checks.audible(wav, rate), "load_seconds": load_seconds, "total_seconds": info["total_s"],
                  "audio_seconds": wav.size / rate, "torch": torch.__version__, "device": "cuda",
                  "device_name": loaded.get("device_name"),
                  "peak_gpu_gb": torch.cuda.max_memory_reserved() / 2**30 if torch.cuda.is_available() else None}))
"""


def engine_id(model):
    """The catalog id for what --model was given: a short name such as 0.6b-base, or the id itself."""
    if model in MODELS:
        return model
    if model in SHORT_NAMES:
        return SHORT_NAMES[model]
    raise InstallError(f"There is no Qwen3-TTS model called {model!r}. Choose one of: {', '.join(SHORT_NAMES)}.")


def environment_python(root):
    return base.env_python(root, ENVIRONMENT)


def environment_versions(root, *, runner=subprocess.run):
    """The packages installed in .venv-fqwen by canonical name, read by its own Python; {} when it has none yet."""
    return base.env_versions(root, ENVIRONMENT, runner=runner)


def interpreter_problems(root, **facts):
    """Why this Python is not the one to run the installer with, as sentences (an empty list when it is)."""
    return base.own_env_problems(root, ENVIRONMENT, "tools\\install_qwen.py", **facts)


def package_problems(different, *, has_pip=True):
    """Why pip should not be run in .venv-fqwen: it has no pip, or packages are installed there at other versions."""
    return base.env_package_problems(ENVIRONMENT, "Qwen3-TTS", different, has_pip=has_pip)


def checkpoint_only_problems(root, installed):
    """--checkpoint-only keeps the packages it finds, so there must be an environment that has faster-qwen3-tts in it."""
    return base.env_checkpoint_only_problems(root, ENVIRONMENT, "Qwen3-TTS", "faster-qwen3-tts", installed)


def long_path_problems(root, manifest, *, long_paths=None):
    """Windows stops at 259 characters unless long paths are on; torch and transformers have deep folders."""
    snapshot = [len(str(base.snapshot_dir(root, manifest).resolve())) + 1 + len(item["path"]) for item in manifest["files"]]
    return base.env_long_path_problems(root, ENVIRONMENT, LONGEST_PACKAGE_PATH, snapshot, long_paths=long_paths)


def reference_problems(root, engine):
    """The Base models' speech check clones the Lab's default reference clip, which Kokoro's installer makes."""
    if MODELS[engine]["clones"] and not (Path(root) / REFERENCE).is_file():
        return [
            f"{MODELS[engine]['name']} clones a voice, and its speech check clones reference\\af_heart.wav, the short clip "
            "Kokoro's installer makes, which is not here. Install Kokoro first (Install on its card in Models & features, "
            "or setup-kokoro.bat), then install this model."
        ]
    return []


def pinned_packages(root, card=None):
    """The packages to install: requirements/qwen-nvidia.txt, or for an AMD card (hardware.amd_cards) that list with AMD's
    build of PyTorch and the code for the card's chip."""
    return amd.pinned_packages(root, LOCK, card["chip"]) if card else base.read_lock(Path(root) / LOCK)


def card_problems(cards):
    """Why Qwen3-TTS's NVIDIA build would not run on these NVIDIA cards: a card older than the GTX 16 series, or a driver older
    than 580. The Models page asks this too before it offers Install."""
    return base.nvidia_card_problems(
        cards, build=NVIDIA_BUILD, cards_text=NVIDIA_CARDS, lowest=NVIDIA_LOWEST_CAPABILITY, driver=NVIDIA_DRIVER
    )


def nvidia_problems(*, present=None, cards=None):
    """Why Qwen3-TTS cannot run on this computer's graphics card, checked before anything is downloaded: no NVIDIA driver, or a
    card or driver the CUDA 13.0 build cannot run on."""
    if not (present if present is not None else base.nvidia_driver_present()):
        return [NO_NVIDIA_DRIVER]
    problems = card_problems(base.nvidia_cards() if cards is None else cards)
    return [
        problem
        + (
            " Update the driver from nvidia.com or the NVIDIA app and run the installer again."
            if "needs NVIDIA driver" in problem
            else " Qwen3-TTS cannot run on this card in the Lab."
        )
        for problem in problems
    ]


def graphics():
    """Which build goes in: ("nvidia", []) with an NVIDIA driver, ("amd", cards) for AMD cards, (None, []) for neither."""
    return amd.graphics()


def amd_card_problems(cards, *, root=None, environ=None):
    """Why Qwen3-TTS could not run on this computer's AMD cards with AMD's build: no card with a chip the build has code for, or
    a setting that names a card that is not there. The Models page asks this too before it offers Install."""
    return amd.card_problems(cards, model="Qwen3-TTS", root=ROOT if root is None else root, environ=environ)


def gpu_problems(vendor, cards, root=None):
    """Why Qwen3-TTS cannot run on this computer's graphics card, checked before anything is downloaded."""
    if vendor == "nvidia":
        return nvidia_problems()
    if vendor == "amd":
        return amd_card_problems(cards, root=root)
    return [NO_NVIDIA_DRIVER]


def size_gb(size):
    return f"{size / 1e9:.1f} GB"


def packages_mb(card=None):
    """(download, disk) in MB for the packages: the NVIDIA build's, or with AMD's build for the card's chip."""
    if card:
        return amd.sizes_mb(card["chip"], OTHER_DOWNLOAD_MB, OTHER_DISK_MB)
    return PACKAGES_DOWNLOAD_MB, PACKAGES_DISK_MB


def plan_text(engine, manifest, packages, todo, pending, *, environment_exists, checkpoint_only, card=None, cards=()):
    """What the installer is about to do, written for the person who ran it. card is the AMD card it installs for (None for
    the NVIDIA build), among cards, every AMD card this computer has."""
    spec = MODELS[engine]
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
            else f"First a Python environment of its own, {ENVIRONMENT}, made from the same Python 3.12 as .venv, which the four "
            "Qwen3-TTS models share, so that nothing another model installs can clash with their packages."
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
            f"faster-qwen3-tts 0.4. About {base.mb_text(download)} to download and {base.mb_text(disk)} on disk. pip checks "
            f"every file against a SHA-256 written in {lists}."
        )
    fetch = f"{size_gb(sum(item['size'] for item in pending))} to download" if pending else "Everything is already in place"
    second = (
        f"{spec['name']}, revision {manifest['revision'][:7]}, from huggingface.co/{manifest['repo']}: the "
        f"{size_gb(files['model.safetensors']['size'])} model, the {size_gb(files['speech_tokenizer/model.safetensors']['size'])} "
        "speech tokenizer that turns its codes into sound, its text tokenizer and settings, and the model card. "
        f"{fetch}. Every file is checked against a SHA-256 written in {spec['manifest']}."
    )
    where = f"the {card['name']}" if card else "the graphics card"
    graphs = "HIP graphs (AMD's CUDA graphs)" if card else "CUDA graphs"
    third = (
        f"A speech check on {where}: loads the model without using the network, records its {graphs}, and "
        f"speaks one sentence {spec['check']}, which you can play afterwards. It takes a minute or two."
    )
    notes = []
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
        f"Licences: the Qwen3-TTS weights and qwen-tts-hf are {manifest['license']} (the Qwen team, Alibaba), and "
        "faster-qwen3-tts is MIT. The Python packages keep their own licences, nearly all MIT, BSD or Apache. The "
        "exceptions are soxr and the libsndfile library inside soundfile (LGPL-2.1), and certifi and tqdm (MPL-2.0). They "
        "are installed on this computer and are not part of this repository."
    )
    intro = textwrap.fill(
        f"This adds {spec['name']}, which {spec['does']}. The four Qwen3-TTS models share {ENVIRONMENT}, and "
        "the Lab runs them only on a graphics card. Everything is installed into this folder (.venv-fqwen, "
        "hf-cache and out\\qwen) and nothing system-wide; pip keeps its usual download cache in your user "
        "profile.",
        width=110,
        break_on_hyphens=False,
    )
    title = f"Fel's Voice Lab: {spec['name']} on " + (
        "your own graphics card build" if checkpoint_only else "an AMD graphics card" if card else "an NVIDIA graphics card"
    )
    return (
        "\n\n".join(
            [
                title,
                intro,
                "\n".join([step(1, first), step(2, second), step(3, third)]),
                *(textwrap.fill(note, width=110, break_on_hyphens=False) for note in notes),
            ]
        )
        + "\n"
    )


def make_environment(root, *, runner=subprocess.run):
    """Make .venv-fqwen from the Python that .venv was made from; venv puts pip in it from Python's own copy."""
    base.make_env(root, ENVIRONMENT, runner=runner)


def install_packages(root, *, card=None, runner=subprocess.run):
    """Install the pinned packages into .venv-fqwen; for an AMD card, with AMD's build of PyTorch and its chip's code (the
    lists pip reads are written into out/qwen)."""
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


def run_speech_check(root, engine, *, number=None, runner=subprocess.run):
    """In .venv-fqwen, offline: load the model on the graphics card through the Lab's Qwen worker and speak the Models page's
    check sentence. number is the AMD card's number to PyTorch (amd_build.settle_number), None for an NVIDIA card."""
    folder = Path(root) / OUTPUT
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / f"install-{MODELS[engine]['short']}.wav"
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"}
    if number is not None:
        env[amd.VARIABLE] = str(number)
    try:
        done = runner(
            [str(environment_python(root)), "-c", SPEECH_CHECK, str(root), engine, str(wav)],
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
    name = MODELS[engine]["name"]
    if done.returncode or not isinstance(result, dict):
        output = (done.stdout or "") + (done.stderr or "")
        raise InstallError(
            f"The packages and model files are in place, but {name} did not load or speak:\n"
            + "\n".join(output.strip().splitlines()[-12:])
            + check_failure_hint(output, amd_card=number is not None)
        )
    if result["problem"]:
        raise InstallError(f"{name} loaded but the check clip was not speech: {result['problem']}.")
    return {**result, "wav": wav}


def write_receipt(root, engine, manifest, packages, check, *, card=None, number=None):
    """The installation record in out/qwen, one per model: installation-<model>.json."""
    receipt = {
        "schema": 1,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": engine,
        "python": platform.python_version(),
        "environment": ENVIRONMENT,
        "torch": check.get("torch"),
        "device": check.get("device", "cuda"),
        "device_name": check.get("device_name"),
        **amd.receipt_fields(root, card, number),
        "packages_pinned": len(packages),
        "lock_sha256": hashlib.sha256((Path(root) / LOCK).read_bytes()).hexdigest(),
        "repo": manifest["repo"],
        "revision": manifest["revision"],
        "license": manifest["license"],
        "files": [item["path"] for item in manifest["files"]],
        "checkpoint_bytes": sum(item["size"] for item in manifest["files"]),
        "speech_check": {key: check.get(key) for key in ("load_seconds", "total_seconds", "audio_seconds", "peak_gpu_gb")},
    }
    path = Path(root) / OUTPUT / f"installation-{MODELS[engine]['short']}.json"
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path


def run(args, *, say=base.say_now, ask=input, interactive=None):
    root = ROOT
    engine = engine_id(args.model)
    spec = MODELS[engine]
    manifest = base.load_manifest(root / spec["manifest"])
    vendor, cards = (None, []) if args.checkpoint_only else graphics()
    card = amd.chosen_card(cards, root) if vendor == "amd" else None
    if vendor == "amd" and card is None:
        raise InstallError(amd.no_card_text(spec["name"], amd_card_problems(cards, root=root)))
    packages = pinned_packages(root, card)
    exists = environment_python(root).is_file()
    installed = environment_versions(root) if exists else {}
    missing, different = base.package_state(packages, installed)
    pending = base.pending_files(root, manifest, manifest["files"])
    todo = 0 if args.checkpoint_only else len(missing) + len(different)
    packages_need = sum(packages_mb(card)) * 10**6 if todo else 0
    need = packages_need + sum(item["size"] for item in pending) + SPACE_MARGIN_MB * 10**6
    problems = interpreter_problems(root)
    if args.checkpoint_only:
        problems += checkpoint_only_problems(root, installed)
    else:
        problems += gpu_problems(vendor, cards, root)
        if todo:
            problems += package_problems(different, has_pip=not exists or "pip" in installed)
    problems += reference_problems(root, engine)
    problems += long_path_problems(root, manifest)
    problems += base.space_problems(root, need)
    say(
        plan_text(
            engine,
            manifest,
            packages,
            todo,
            pending,
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
    say(f"Fetching the {spec['name']} model files:")
    base.download_checkpoint(root, manifest, manifest["files"], say=say)
    say(f"Checking that {spec['name']} speaks on the graphics card (a minute or two):")
    check = run_speech_check(root, engine, number=number)
    receipt = write_receipt(root, engine, manifest, packages, check, card=card, number=number)
    memory = f", using {check['peak_gpu_gb']:.1f} GB of graphics memory at most" if check.get("peak_gpu_gb") else ""
    say("")
    say(
        f"{spec['name']} is installed. It loaded and warmed up in {check['load_seconds']:.0f} s on the "
        f"{check.get('device_name') or 'graphics card'} "
        f"and spoke {check['audio_seconds']:.1f} s {spec['check']} in {check['total_seconds']:.1f} s{memory}. The whole install "
        f"took {time.monotonic() - started:.0f} s."
    )
    say(f"  Listen to it: {Path(check['wav']).relative_to(root)}")
    say(f"  Receipt: {receipt.relative_to(root)}")
    say(f"  Next: start the studio (the Fel's Voice Lab shortcut, or serve.bat) and choose {spec['name']} in Model studio.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--model",
        required=True,
        choices=[*SHORT_NAMES, *MODELS],
        metavar="MODEL",
        help=f"the model to install: {', '.join(SHORT_NAMES)}",
    )
    parser.add_argument("--yes", action="store_true", help="accept the plan without asking")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run the checks; change nothing")
    parser.add_argument(
        "--nvidia",
        action="store_true",
        help="accepted for the Models page and the other installers; Qwen3-TTS always gets the build for this computer's "
        "graphics card",
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
