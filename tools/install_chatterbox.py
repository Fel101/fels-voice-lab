"""Install Chatterbox Turbo 350M: its own .venv-chatterbox with pinned packages, the pinned checkpoint into hf-cache, then a
speech check that clones a voice.

    .venv\\Scripts\\python.exe tools\\install_chatterbox.py
    .venv\\Scripts\\python.exe tools\\install_chatterbox.py --dry-run
    .venv\\Scripts\\python.exe tools\\install_chatterbox.py --yes
    .venv\\Scripts\\python.exe tools\\install_chatterbox.py --nvidia
    .venv\\Scripts\\python.exe tools\\install_chatterbox.py --checkpoint-only
    .venv\\Scripts\\python.exe tools\\install_chatterbox.py --original

It prints what it will do and asks before it changes anything (--yes accepts the plan, --dry-run only shows it). Run it with
the lab's own Python, which setup.bat creates; setup-chatterbox.bat does that for you. Chatterbox gets an environment of its own,
.venv-chatterbox, made from the same Python 3.12 as .venv, as every model does: its PyTorch 2.6 and the PyTorch 2.12 of Kokoro's
.venv-kokoro could not share one. pip installs requirements/chatterbox-cpu.txt there with every file's SHA-256 checked, and the
script refuses to replace a package that is already installed at another version, so an environment with its own PyTorch keeps
it. --nvidia takes the CUDA 13.0 builds of PyTorch 2.12 and torchaudio 2.11 in requirements/chatterbox-nvidia.txt instead of
the CPU ones, and in a .venv-chatterbox that holds the CPU build it replaces those two and nothing else; the Lab has not been
tried on an NVIDIA card yet. The checkpoint files in catalog/chatterbox_turbo_snapshot.json come from Hugging
Face at the pinned revision, resume after an interruption, and are kept only when their SHA-256 matches. Running it again
installs only what is missing. --checkpoint-only fetches the model files and leaves the packages alone, for a .venv-chatterbox
you made yourself. --original installs Chatterbox 500M and Chatterbox Voice Conversion instead of Turbo: the same environment
and packages, and the ResembleAI/chatterbox files in catalog/chatterbox_snapshot.json, which both of them read. The
environment, download, hash and pip helpers are the ones install_kokoro.py uses, so a fix there reaches this installer too.
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
LOCK = "requirements/chatterbox-cpu.txt"
NVIDIA_LOCK = "requirements/chatterbox-nvidia.txt"
MANIFEST = "catalog/chatterbox_turbo_snapshot.json"
ORIGINAL_MANIFEST = "catalog/chatterbox_snapshot.json"
ENVIRONMENT = ".venv-chatterbox"
PYTHON = ENVIRONMENT + "/Scripts/python.exe"
OUTPUT = "out/chatterbox"
REFERENCE_WAV = "install-reference.wav"
CHECK_WAV = "install-check.wav"
NVIDIA_REQUIREMENTS = "requirements-nvidia.txt"
RECEIPT = "installation.json"
ORIGINAL_WAV = "install-original.wav"
CONVERTED_WAV = "install-vc.wav"
ORIGINAL_RECEIPT = "installation-original.json"
# Measured on a clean install of the lock: 400 MB of wheels, 1984 MB installed, and 130 characters for the longest path below
# site-packages. Windows stops at 259 characters unless long paths are switched on. The CUDA builds of PyTorch and torchaudio
# are 1.93 GB of wheels, about 1.7 GB more than the CPU ones, and take about 4 GB more once installed (an estimate: they have
# not been installed on an NVIDIA computer yet).
PACKAGES_DOWNLOAD_MB = 400
PACKAGES_DISK_MB = 2000
NVIDIA_DOWNLOAD_MB = 1930
NVIDIA_EXTRA_DOWNLOAD_MB = 1730
NVIDIA_EXTRA_DISK_MB = 4000
LONGEST_PACKAGE_PATH = 135
SPACE_MARGIN_MB = 500
# The NVIDIA build is Kokoro's PyTorch: CUDA 13.0 has code for compute capability 7.5 and up (GeForce GTX 16 and RTX 20 series
# and newer, RTX 50 included) and runs with NVIDIA driver 580 or later. PyTorch 2.6, which chatterbox-tts asks for, has no build
# for RTX 50 series cards, so the NVIDIA build is PyTorch 2.12, with which Chatterbox was checked to speak and clone.
NVIDIA_BUILD = "Chatterbox's NVIDIA build (PyTorch 2.12 with CUDA 13.0)"
NVIDIA_CARDS = base.NVIDIA_CARDS
NVIDIA_LOWEST_CAPABILITY = base.NVIDIA_LOWEST_CAPABILITY
NVIDIA_DRIVER = base.NVIDIA_DRIVER
CUDA_NEEDS = base.CUDA_NEEDS
CHECK_TIMEOUT = 1800

SPEECH_CHECK = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
import soundfile
import torch
import engine_checks
from common import REFERENCE_TEXT
from engines import ChatterboxTurbo

started = time.perf_counter()
engine = ChatterboxTurbo(sys.argv[4])
loaded = time.perf_counter() - started
reference, rate, built_in = engine.speak(REFERENCE_TEXT)
soundfile.write(sys.argv[2], reference, rate)
started = time.perf_counter()
engine.prepare_reference(sys.argv[2])
prepared = time.perf_counter() - started
request = engine_checks.smoke_request("turbo")
wav, rate, info = engine.speak(request["text"], ref_wav=sys.argv[2])
soundfile.write(sys.argv[3], wav, rate)
problem = engine_checks.audible(reference, rate) or engine_checks.audible(wav, rate)
print(json.dumps({"problem": problem, "load_seconds": loaded, "reference_seconds": reference.size / rate,
                  "reference_total_seconds": built_in["total_s"], "prepare_seconds": prepared, "total_seconds": info["total_s"],
                  "audio_seconds": wav.size / rate, "torch": torch.__version__, "device": sys.argv[4]}))
"""

# Chatterbox 500M speaks the Models page's check sentence in its built-in voice, then Voice Conversion says that clip again with
# the clip itself as the target voice, which is the path the conversion worker takes.
ORIGINAL_CHECK = """
import gc, json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import soundfile
import torch
import engine_checks
from chatterbox.vc import ChatterboxVC
from chatterbox_support import original_snapshot_dir
from common import to_numpy
from engines import ChatterboxOriginal

started = time.perf_counter()
engine = ChatterboxOriginal(sys.argv[4])
loaded = time.perf_counter() - started
wav, rate, info = engine.speak(engine_checks.smoke_request("orig")["text"])
soundfile.write(sys.argv[2], wav, rate)
problem = engine_checks.audible(wav, rate)
del engine
gc.collect()
started = time.perf_counter()
converter = ChatterboxVC.from_local(original_snapshot_dir(Path(sys.argv[1])), sys.argv[4])
converter_loaded = time.perf_counter() - started
started = time.perf_counter()
converted = to_numpy(converter.generate(audio=sys.argv[2], target_voice_path=sys.argv[2]))
converted_seconds = time.perf_counter() - started
soundfile.write(sys.argv[3], converted, converter.sr)
problem = problem or engine_checks.audible(converted, converter.sr)
print(json.dumps({"problem": problem, "load_seconds": loaded, "total_seconds": info["total_s"], "audio_seconds": wav.size / rate,
                  "vc_load_seconds": converter_loaded, "vc_seconds": converted_seconds,
                  "vc_audio_seconds": converted.size / converter.sr, "torch": torch.__version__, "device": sys.argv[4]}))
"""


def environment_python(root):
    return base.env_python(root, ENVIRONMENT)


def environment_versions(root, *, runner=subprocess.run):
    """The packages installed in .venv-chatterbox by canonical name, read by its own Python; {} when it has none yet."""
    return base.env_versions(root, ENVIRONMENT, runner=runner)


def interpreter_problems(root, **facts):
    """Why this Python is not the one to run the installer with, as sentences (an empty list when it is)."""
    return base.own_env_problems(root, ENVIRONMENT, "tools\\install_chatterbox.py", **facts)


def package_problems(different, *, has_pip=True):
    """Why pip should not be run in .venv-chatterbox: it has no pip, or packages are installed there at other versions."""
    return base.env_package_problems(ENVIRONMENT, "Chatterbox", different, has_pip=has_pip)


def checkpoint_only_problems(root, installed):
    """--checkpoint-only keeps the packages it finds, so there must be an environment that has Chatterbox in it."""
    return base.env_checkpoint_only_problems(root, ENVIRONMENT, "Chatterbox", "chatterbox-tts", installed)


def long_path_problems(root, manifest, *, long_paths=None):
    """Windows stops at 259 characters unless long paths are on; torch and transformers have deep folders."""
    snapshot = [len(str(base.snapshot_dir(root, manifest).resolve())) + 1 + len(item["path"]) for item in manifest["files"]]
    return base.env_long_path_problems(root, ENVIRONMENT, LONGEST_PACKAGE_PATH, snapshot, long_paths=long_paths)


def pinned_packages(root, *, nvidia=False):
    """The packages to install: requirements/chatterbox-cpu.txt, with the CUDA builds of torch and torchaudio for --nvidia."""
    packages = base.read_lock(Path(root) / LOCK)
    return base.swap_packages(packages, base.read_lock(Path(root) / NVIDIA_LOCK)) if nvidia else packages


def card_problems(cards):
    """Why Chatterbox's NVIDIA build would not run on these NVIDIA cards: a card older than the GTX 16 series, or a driver older
    than 580. tools/first_setup.py and the Models page ask this too, and give Chatterbox its CPU build on such a computer."""
    return base.nvidia_card_problems(cards, build=NVIDIA_BUILD, cards_text=NVIDIA_CARDS, lowest=NVIDIA_LOWEST_CAPABILITY,
                                     driver=NVIDIA_DRIVER)


def nvidia_problems(*, present=None, cards=None):
    """Why --nvidia cannot work on this computer, checked before anything is downloaded: no NVIDIA driver, or a card or driver
    the CUDA 13.0 build cannot run on."""
    return base.nvidia_problems(present=present, cards=cards, check=card_problems)


def size_gb(size):
    return f"{size / 1e9:.1f} GB"


def plan_text(manifest, packages, todo, pending, *, environment_exists, checkpoint_only, nvidia=False, switching=(),
              original=False):
    """What the installer is about to do, written for the person who ran it. original is the plan of --original."""
    files = {item["path"]: item for item in manifest["files"]}

    def step(number, text):
        return textwrap.fill(f"{number}. {text}", width=110, break_on_hyphens=False, initial_indent="  ", subsequent_indent="     ")

    if checkpoint_only:
        first = f"Python packages: the ones in {ENVIRONMENT} are left alone (--checkpoint-only)."
    elif not todo:
        first = f"Python packages: all {len(packages)} are already installed in {ENVIRONMENT}."
    elif switching and todo == len(switching):
        first = (f"Python packages: Chatterbox is already installed in {ENVIRONMENT}, and "
                 + base.switch_text(switching, "the CUDA 13.0 builds of PyTorch 2.12 and torchaudio 2.11 for NVIDIA graphics "
                                               "cards (not yet tried on one)")
                 + f". About {base.mb_text(NVIDIA_DOWNLOAD_MB)} to download and {base.mb_text(NVIDIA_EXTRA_DISK_MB)} more on "
                 f"disk. pip checks every file against a SHA-256 written in {LOCK} and {NVIDIA_LOCK}.")
    else:
        made = (f"{ENVIRONMENT} is already here." if environment_exists else
                f"First a Python environment of its own, {ENVIRONMENT}, made from the same Python 3.12 as .venv, as every model "
                "has, so that nothing another model installs can clash with Chatterbox's packages.")
        build = ("the CUDA 13.0 builds of PyTorch 2.12 and torchaudio 2.11 for NVIDIA graphics cards (not yet tried on one)"
                 if nvidia else "PyTorch 2.6 for the CPU")
        download = PACKAGES_DOWNLOAD_MB + (NVIDIA_EXTRA_DOWNLOAD_MB if nvidia else 0)
        disk = PACKAGES_DISK_MB + (NVIDIA_EXTRA_DISK_MB if nvidia else 0)
        first = (f"{made} Python packages: {todo} of {len(packages)} to install there, among them {build}, Transformers and "
                 f"librosa. About {base.mb_text(download)} to download and {base.mb_text(disk)} on disk. pip checks every file "
                 f"against a SHA-256 written in {LOCK + ' and ' + NVIDIA_LOCK if nvidia else LOCK}.")
    fetch = (f"{size_gb(sum(item['size'] for item in pending))} to download" if pending else "Everything is already in place")
    if original:
        second = (f"Chatterbox 500M and Voice Conversion, revision {manifest['revision'][:7]}, from huggingface.co/"
                  f"{manifest['repo']}: the {size_gb(files['t3_cfg.safetensors']['size'])} language model, the "
                  f"{size_gb(files['s3gen.safetensors']['size'])} speech decoder that Voice Conversion uses too, the speaker "
                  f"encoder, the built-in voice, the tokenizer and the model card. {fetch}. Every file is checked against a "
                  f"SHA-256 written in {ORIGINAL_MANIFEST}.")
        third = ("A speech check: loads Chatterbox 500M without using the network and speaks one sentence in its built-in voice, "
                 "then Voice Conversion says that clip again, which you can play afterwards. "
                 + ("It runs on the graphics card." if nvidia else "On a CPU this takes a few minutes."))
    else:
        second = (f"Chatterbox Turbo 350M, revision {manifest['revision'][:7]}, from huggingface.co/{manifest['repo']}: the "
                  f"{size_gb(files['t3_turbo_v1.safetensors']['size'])} language model, the "
                  f"{size_gb(files['s3gen_meanflow.safetensors']['size'])} speech decoder, the speaker encoder, the built-in "
                  f"voice, the tokenizer and the model card. {fetch}. Every file is checked against a SHA-256 written in "
                  f"{MANIFEST}.")
        third = ("A speech check: loads the model without using the network, speaks a paragraph in its built-in voice, then "
                 "clones that recording and speaks one sentence in the cloned voice, which you can play afterwards. "
                 + ("It runs on the graphics card." if nvidia else "On a CPU this takes a few minutes."))
    notes = []
    if nvidia:
        notes.append("The CUDA 13.0 builds need a GeForce GTX 16 or RTX 20 series card or newer, RTX 50 included, and an NVIDIA "
                     "driver of version 580 or later. Chatterbox asks for PyTorch 2.6, which has no build for RTX 50 cards; it was "
                     "checked to speak and clone a voice with PyTorch 2.12. The builds carry NVIDIA's CUDA libraries inside them, "
                     "under NVIDIA's own licence terms. Nobody has tried the Lab on an NVIDIA card yet: if it works, or does not, a "
                     "hardware report on GitHub helps the next person.")
    weights = "the Chatterbox 500M weights" if original else "the Turbo weights"
    notes.append(f"Licences: the Chatterbox code (chatterbox-tts) and {weights} are {manifest['license']}. Every clip Chatterbox "
                 "makes carries Resemble AI's Perth watermark, a mark you cannot hear that detection tools can find; the Lab does "
                 "not remove it. The Python packages keep their own licences, nearly all MIT, BSD or Apache. The exceptions are "
                 "soxr and the libsndfile library inside soundfile (LGPL-2.1), and certifi and tqdm (MPL-2.0). They are installed "
                 "on this computer and are not part of this repository.")
    if original:
        intro = ("This adds Chatterbox 500M, the larger Chatterbox model, with exaggeration and pace controls and a built-in voice, "
                 "and Chatterbox Voice Conversion, which says a recording again in the voice of another. They share "
                 ".venv-chatterbox with Chatterbox Turbo. ")
        name = "Chatterbox 500M and Voice Conversion"
    else:
        notes.append("Turbo's speaker encoder also gives the Lab its voice match: the score that says how close a take is to the "
                     "recording it was cloned from.")
        intro = ("This adds Chatterbox Turbo, a model that clones a voice from a recording of more than five seconds and runs on "
                 "an ordinary computer. ")
        name = "Chatterbox Turbo 350M"
    intro = textwrap.fill(intro + "Everything is installed into this folder (.venv-chatterbox, hf-cache and out\\chatterbox) and "
                          "nothing system-wide; pip keeps its usual download cache in your user profile.",
                          width=110, break_on_hyphens=False)
    title = f"Fel's Voice Lab: {name} on " + ("an NVIDIA graphics card" if nvidia else "the CPU")
    return "\n\n".join([title, intro, "\n".join([step(1, first), step(2, second), step(3, third)]),
                        *(textwrap.fill(note, width=110, break_on_hyphens=False) for note in notes)]) + "\n"


def make_environment(root, *, runner=subprocess.run):
    """Make .venv-chatterbox from the Python that .venv was made from; venv puts pip in it from Python's own copy."""
    base.make_env(root, ENVIRONMENT, runner=runner)


def install_packages(root, *, nvidia=False, runner=subprocess.run):
    """Install the pinned packages into .venv-chatterbox. For --nvidia, pip gets the CPU list with the CUDA builds swapped in,
    written beside the receipt."""
    requirements = Path(root) / LOCK
    if nvidia:
        requirements = base.write_swapped_lock(root, LOCK, NVIDIA_LOCK, Path(root) / OUTPUT / NVIDIA_REQUIREMENTS)
    base.pip_install(root, ENVIRONMENT, requirements, runner=runner)


def run_speech_check(root, *, device="cpu", runner=subprocess.run):
    """In .venv-chatterbox, offline: load Turbo on the device, speak a paragraph in its built-in voice, clone it, and speak the
    Models page's check sentence in the cloned voice."""
    folder = Path(root) / OUTPUT
    folder.mkdir(parents=True, exist_ok=True)
    reference, wav = folder / REFERENCE_WAV, folder / CHECK_WAV
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"}
    try:
        done = runner([str(environment_python(root)), "-c", SPEECH_CHECK, str(root), str(reference), str(wav), device], cwd=root,
                      env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"The speech check took more than {CHECK_TIMEOUT // 60} minutes, so it was stopped.") from exc
    result = base.last_json_line(done.stdout or "")
    if done.returncode or not isinstance(result, dict):
        output = (done.stdout or "") + (done.stderr or "")
        raise InstallError("The packages and model files are in place, but Chatterbox Turbo did not load or speak:\n"
                           + "\n".join(output.strip().splitlines()[-12:])
                           + base.check_failure_hint(output, nvidia=device == "cuda", cuda_needs=CUDA_NEEDS))
    if result["problem"]:
        raise InstallError(f"Chatterbox Turbo loaded but a check clip was not speech: {result['problem']}.")
    return {**result, "reference": reference, "wav": wav}


def run_original_check(root, *, device="cpu", runner=subprocess.run):
    """In .venv-chatterbox, offline: load Chatterbox 500M on the device and speak the Models page's check sentence in its
    built-in voice, then load Voice Conversion and say that clip again with the clip as the target voice."""
    folder = Path(root) / OUTPUT
    folder.mkdir(parents=True, exist_ok=True)
    wav, converted = folder / ORIGINAL_WAV, folder / CONVERTED_WAV
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"}
    try:
        done = runner([str(environment_python(root)), "-c", ORIGINAL_CHECK, str(root), str(wav), str(converted), device],
                      cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"The speech check took more than {CHECK_TIMEOUT // 60} minutes, so it was stopped.") from exc
    result = base.last_json_line(done.stdout or "")
    if done.returncode or not isinstance(result, dict):
        output = (done.stdout or "") + (done.stderr or "")
        raise InstallError("The packages and model files are in place, but Chatterbox 500M or Voice Conversion did not load "
                           "or speak:\n" + "\n".join(output.strip().splitlines()[-12:])
                           + base.check_failure_hint(output, nvidia=device == "cuda", cuda_needs=CUDA_NEEDS))
    if result["problem"]:
        raise InstallError(f"Chatterbox 500M or Voice Conversion loaded but a check clip was not speech: {result['problem']}.")
    return {**result, "wav": wav, "converted": converted}


def voice_match(check):
    """Compare the cloned clip with its reference the way the studio's voice match does, in this Python (the lab's own .venv),
    which is where the service runs it. A failure here is reported, not fatal: the model itself is installed."""
    try:
        sys.path.insert(0, str(ROOT))
        import speaker_similarity

        score = speaker_similarity.similarity(check["reference"], check["wav"])
        return {"score": round(score, 4), "band": speaker_similarity.band(score)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def write_receipt(root, manifest, packages, check, match=None, *, original=False):
    """The installation record in out/chatterbox: installation.json for Turbo, installation-original.json for --original."""
    keys = (("load_seconds", "total_seconds", "audio_seconds", "vc_load_seconds", "vc_seconds", "vc_audio_seconds") if original
            else ("load_seconds", "reference_seconds", "prepare_seconds", "total_seconds", "audio_seconds"))
    receipt = {"schema": 1, "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "python": platform.python_version(), "environment": ENVIRONMENT, "torch": check.get("torch"),
               "device": check.get("device", "cpu"), "packages_pinned": len(packages),
               "lock_sha256": hashlib.sha256((Path(root) / LOCK).read_bytes()).hexdigest(),
               "repo": manifest["repo"], "revision": manifest["revision"], "license": manifest["license"],
               "files": [item["path"] for item in manifest["files"]],
               "checkpoint_bytes": sum(item["size"] for item in manifest["files"]),
               "speech_check": {key: check[key] for key in keys}}
    if not original:
        receipt["voice_match"] = match
    path = Path(root) / OUTPUT / (ORIGINAL_RECEIPT if original else RECEIPT)
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path


def run(args, *, say=base.say_now, ask=input, interactive=None):
    root = ROOT
    nvidia = getattr(args, "nvidia", False)
    original = getattr(args, "original", False)
    manifest = base.load_manifest(root / (ORIGINAL_MANIFEST if original else MANIFEST))
    packages = pinned_packages(root, nvidia=nvidia)
    exists = environment_python(root).is_file()
    installed = environment_versions(root) if exists else {}
    missing, different = base.package_state(packages, installed)
    # --nvidia on a Chatterbox installed for the CPU replaces its PyTorch and torchaudio; any other difference still stops it.
    switching = (base.build_switch(different, base.read_lock(root / LOCK), base.read_lock(root / NVIDIA_LOCK))
                 if nvidia else [])
    conflicts = [pair for pair in different if pair not in switching]
    pending = base.pending_files(root, manifest, manifest["files"])
    todo = 0 if args.checkpoint_only else len(missing) + len(different)
    extra = (NVIDIA_EXTRA_DOWNLOAD_MB + NVIDIA_EXTRA_DISK_MB) if nvidia else 0
    packages_need = (PACKAGES_DOWNLOAD_MB + PACKAGES_DISK_MB + extra) * 10**6 if todo else 0
    if switching and todo == len(switching):
        packages_need = (NVIDIA_DOWNLOAD_MB + NVIDIA_EXTRA_DISK_MB) * 10**6
    need = packages_need + sum(item["size"] for item in pending) + SPACE_MARGIN_MB * 10**6
    problems = interpreter_problems(root)
    if nvidia:
        problems += nvidia_problems()
    if args.checkpoint_only:
        problems += checkpoint_only_problems(root, installed)
    elif todo:
        problems += package_problems(conflicts, has_pip=not exists or "pip" in installed)
    problems += long_path_problems(root, manifest)
    problems += base.space_problems(root, need)
    say(plan_text(manifest, packages, todo, pending, environment_exists=exists, checkpoint_only=args.checkpoint_only,
                  nvidia=nvidia, switching=switching, original=original))
    if problems:
        raise InstallError("This computer cannot take the plan yet:\n" + "\n".join(
            textwrap.fill(problem, width=110, break_on_hyphens=False, initial_indent="  - ", subsequent_indent="    ") for problem in problems))
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
        install_packages(root, nvidia=nvidia)
        still_missing, still_different = base.package_state(packages, environment_versions(root))
        if still_missing:
            raise InstallError(f"pip finished but {len(still_missing)} packages are still missing, for example "
                               f"{still_missing[0]['name']}.")
        if switching and still_different:
            item, have = still_different[0]
            raise InstallError(f"pip finished but {item['name']} is still {have}, not {item['version']}.")
    device = "cuda" if nvidia else "cpu"
    if original:
        say("Fetching the Chatterbox 500M and Voice Conversion model files:")
        base.download_checkpoint(root, manifest, manifest["files"], say=say)
        say("Checking that Chatterbox 500M speaks and Voice Conversion converts"
            + (" on the graphics card:" if nvidia else " (on a CPU this takes a few minutes):"))
        check = run_original_check(root, device=device)
        receipt = write_receipt(root, manifest, packages, check, original=True)
        say("")
        say(f"Chatterbox 500M and Voice Conversion are installed. Chatterbox 500M loaded in {check['load_seconds']:.0f} s and "
            f"spoke {check['audio_seconds']:.1f} s in its built-in voice in {check['total_seconds']:.1f} s; Voice Conversion "
            f"said it again in {check['vc_seconds']:.1f} s. The whole install took {time.monotonic() - started:.0f} s.")
        say(f"  Listen to them: {Path(check['wav']).relative_to(root)} and {Path(check['converted']).relative_to(root)}")
        say(f"  Receipt: {receipt.relative_to(root)}")
        say("  Next: start the studio (the Fel's Voice Lab shortcut, or serve.bat): Chatterbox 500M is in Model studio, and "
            "Voice Conversion in Voice conversion.")
        return 0
    say("Fetching the Chatterbox Turbo model files:")
    base.download_checkpoint(root, manifest, manifest["files"], say=say)
    say("Checking that Chatterbox Turbo speaks and clones a voice"
        + (" on the graphics card:" if nvidia else " (on a CPU this takes a few minutes):"))
    check = run_speech_check(root, device=device)
    match = voice_match(check)
    receipt = write_receipt(root, manifest, packages, check, match)
    say("")
    say(f"Chatterbox Turbo is installed. It loaded in {check['load_seconds']:.0f} s, spoke {check['reference_seconds']:.1f} s in its "
        f"built-in voice, cloned that recording and spoke {check['audio_seconds']:.1f} s in the cloned voice in "
        f"{check['total_seconds']:.1f} s. The whole install took {time.monotonic() - started:.0f} s.")
    say(f"  Listen to them: {Path(check['reference']).relative_to(root)} and {Path(check['wav']).relative_to(root)}")
    if "error" in match:
        say(f"  The voice match could not compare them in .venv ({match['error']}); takes will show no voice match until "
            "that is fixed.")
    say(f"  Receipt: {receipt.relative_to(root)}")
    say("  Next: start the studio (the Fel's Voice Lab shortcut, or serve.bat), open Voice cloning and choose Chatterbox Turbo.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="accept the plan without asking")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run the checks; change nothing")
    parser.add_argument("--nvidia", action="store_true",
                        help="install the CUDA builds of PyTorch and torchaudio for an NVIDIA graphics card instead of the CPU "
                             "builds (untested)")
    parser.add_argument("--checkpoint-only", action="store_true",
                        help=f"fetch the model files only and leave the packages in {ENVIRONMENT} alone")
    parser.add_argument("--original", action="store_true",
                        help="install Chatterbox 500M and Chatterbox Voice Conversion (ResembleAI/chatterbox) instead of Turbo")
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
