"""Install Chatterbox Turbo 350M on the CPU: its own .venv-chatterbox with pinned packages, the pinned checkpoint into hf-cache,
then a speech check that clones a voice.

    .venv\\Scripts\\python.exe tools\\install_chatterbox.py
    .venv\\Scripts\\python.exe tools\\install_chatterbox.py --dry-run
    .venv\\Scripts\\python.exe tools\\install_chatterbox.py --yes
    .venv\\Scripts\\python.exe tools\\install_chatterbox.py --checkpoint-only

It prints what it will do and asks before it changes anything (--yes accepts the plan, --dry-run only shows it). Run it with
the lab's own Python, which setup.bat creates; setup-chatterbox.bat does that for you. Chatterbox gets an environment of its own,
.venv-chatterbox, made from the same Python 3.12 as .venv: its PyTorch 2.6 and the PyTorch 2.12 that Kokoro's installer puts in
.venv cannot share one. pip installs requirements/chatterbox-cpu.txt there with every file's SHA-256 checked, and the script
refuses to replace a package that is already installed at another version, so an environment with its own PyTorch keeps it.
The checkpoint files in catalog/chatterbox_turbo_snapshot.json come from Hugging Face at the pinned revision, resume after an
interruption, and are kept only when their SHA-256 matches. Running it again installs only what is missing. --checkpoint-only
fetches the model files and leaves the packages alone, for a .venv-chatterbox you made yourself (with a GPU build of PyTorch,
say). The download, hash and pip helpers are the ones install_kokoro.py uses, so a fix there reaches this installer too.
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
MANIFEST = "catalog/chatterbox_turbo_snapshot.json"
ENVIRONMENT = ".venv-chatterbox"
PYTHON = ENVIRONMENT + "/Scripts/python.exe"
OUTPUT = "out/chatterbox"
REFERENCE_WAV = "install-reference.wav"
CHECK_WAV = "install-check.wav"
RECEIPT = "installation.json"
# Measured on a clean install of the lock: 400 MB of wheels, 1984 MB installed, and 130 characters for the longest path below
# site-packages. Windows stops at 259 characters unless long paths are switched on.
PACKAGES_DOWNLOAD_MB = 400
PACKAGES_DISK_MB = 2000
LONGEST_PACKAGE_PATH = 135
SPACE_MARGIN_MB = 500
CHECK_TIMEOUT = 1800

VERSIONS = """
import importlib.metadata, json
print(json.dumps({dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions() if dist.metadata["Name"]}))
"""

SPEECH_CHECK = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
import soundfile
import torch
import engine_checks
from common import REFERENCE_TEXT
from engines import ChatterboxTurbo

started = time.perf_counter()
engine = ChatterboxTurbo("cpu")
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
                  "audio_seconds": wav.size / rate, "torch": torch.__version__}))
"""


def environment_python(root):
    return Path(root) / PYTHON


def environment_versions(root, *, runner=subprocess.run):
    """The packages installed in .venv-chatterbox by canonical name, read by its own Python; {} when it has none yet."""
    python = environment_python(root)
    if not python.is_file():
        return {}
    done = runner([str(python), "-I", "-c", VERSIONS], capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=120)
    found = base.last_json_line(done.stdout or "")
    if done.returncode or not isinstance(found, dict):
        raise InstallError(f"{ENVIRONMENT} is here but its Python does not run:\n"
                           + "\n".join(((done.stdout or "") + (done.stderr or "")).strip().splitlines()[-8:])
                           + f"\nDelete the {ENVIRONMENT} folder and run the installer again: it makes a new one.")
    return {base.canonical(name): version for name, version in found.items()}


def interpreter_problems(root, *, system=None, machine=None, version=None, prefix=None, bits=None, base_python=None):
    """Why this Python is not the one to run the installer with, as sentences (an empty list when it is)."""
    system, machine = system or sys.platform, (machine or platform.machine()).lower()
    version, prefix = version or sys.version_info, prefix or sys.prefix
    bits = bits or (64 if sys.maxsize > 2**32 else 32)
    base_python = Path(base_python or getattr(sys, "_base_executable", sys.executable))
    problems = []
    if system != "win32" or machine not in {"amd64", "x86_64"} or bits != 64:
        problems.append("This installer is for 64-bit Windows with 64-bit Python: the pinned packages are Windows x64 builds.")
    if tuple(version[:2]) != (3, 12):
        problems.append(f"This is Python {version[0]}.{version[1]}; the pinned packages are built for Python 3.12.")
    if Path(prefix).resolve() != (Path(root) / ".venv").resolve():
        problems.append("Run it with this folder's own Python, so that nothing else is changed: "
                        ".venv\\Scripts\\python.exe tools\\install_chatterbox.py (setup.bat creates that environment).")
    elif not base_python.is_file():
        problems.append(f"The Python 3.12 that .venv was made from is gone ({base_python}), and {ENVIRONMENT} is made from it. "
                        "Install Python 3.12 again from python.org, then run setup.bat and this installer.")
    return problems


def package_problems(different, *, has_pip=True):
    """Why pip should not be run in .venv-chatterbox: it has no pip, or packages are installed there at other versions."""
    problems = []
    if not has_pip:
        problems.append(f"{ENVIRONMENT} has no pip. Delete the {ENVIRONMENT} folder and run the installer again: it makes a new "
                        "one with pip.")
    if different:
        shown = ", ".join(f"{item['name']} {have} (the list pins {item['version']})" for item, have in different[:5])
        more = f" and {len(different) - 5} more" if len(different) > 5 else ""
        problems.append(f"{ENVIRONMENT} already has other versions of packages the Chatterbox list pins: {shown}{more}. "
                        "The installer does not replace installed packages. If Chatterbox already runs there (with your own "
                        f"PyTorch, say), add the model files with --checkpoint-only; otherwise delete the {ENVIRONMENT} folder "
                        "and run the installer again.")
    return problems


def checkpoint_only_problems(root, installed):
    """--checkpoint-only keeps the packages it finds, so there must be an environment that has Chatterbox in it."""
    if not environment_python(root).is_file():
        return [f"--checkpoint-only adds the model files to a {ENVIRONMENT} that already runs Chatterbox, and there is no "
                f"{ENVIRONMENT} here. Run the installer without it to make one."]
    if "chatterbox-tts" not in installed:
        return [f"--checkpoint-only keeps the packages in {ENVIRONMENT} as they are, and chatterbox-tts is not among them. "
                "Install it there yourself, or delete the folder and run the installer without --checkpoint-only."]
    return []


def long_path_problems(root, manifest, *, long_paths=None):
    """Windows stops at 259 characters unless long paths are on; torch and transformers have deep folders."""
    site = len(str((Path(root) / ENVIRONMENT / "Lib" / "site-packages").resolve())) + 1 + LONGEST_PACKAGE_PATH
    snapshot = max(len(str(base.snapshot_dir(root, manifest).resolve())) + 1 + len(item["path"]) for item in manifest["files"])
    longest = max(site, snapshot)
    if longest < base.MAX_PATH or (base.long_paths_enabled() if long_paths is None else long_paths):
        return []
    return [f"This folder's path is too long for Windows: the packages need paths of up to {longest} characters and Windows "
            f"stops at {base.MAX_PATH - 1}. Move the lab to a shorter path such as C:\\voice-lab, or switch on long paths in "
            "Windows."]


def size_gb(size):
    return f"{size / 1e9:.1f} GB"


def plan_text(manifest, packages, todo, pending, *, environment_exists, checkpoint_only):
    """What the installer is about to do, written for the person who ran it."""
    files = {item["path"]: item for item in manifest["files"]}

    def step(number, text):
        return textwrap.fill(f"{number}. {text}", width=110, initial_indent="  ", subsequent_indent="     ")

    if checkpoint_only:
        first = f"Python packages: the ones in {ENVIRONMENT} are left alone (--checkpoint-only)."
    elif not todo:
        first = f"Python packages: all {len(packages)} are already installed in {ENVIRONMENT}."
    else:
        made = (f"{ENVIRONMENT} is already here." if environment_exists else
                f"First a Python environment of its own, {ENVIRONMENT}, made from the same Python 3.12 as .venv: Chatterbox's "
                "PyTorch 2.6 cannot share .venv with the PyTorch 2.12 that Kokoro's installer puts there.")
        first = (f"{made} Python packages: {todo} of {len(packages)} to install there, among them PyTorch 2.6 for the CPU, "
                 f"Transformers and librosa. About {PACKAGES_DOWNLOAD_MB} MB to download and "
                 f"{PACKAGES_DISK_MB / 1000:.1f} GB on disk. pip checks every file against a SHA-256 written in "
                 "requirements/chatterbox-cpu.txt.")
    second = (f"Chatterbox Turbo 350M, revision {manifest['revision'][:7]}, from huggingface.co/{manifest['repo']}: the "
              f"{size_gb(files['t3_turbo_v1.safetensors']['size'])} language model, the "
              f"{size_gb(files['s3gen_meanflow.safetensors']['size'])} speech decoder, the speaker encoder, the built-in voice, "
              "the tokenizer and the model card. "
              + (f"{size_gb(sum(item['size'] for item in pending))} to download" if pending else "Everything is already in place")
              + f". Every file is checked against a SHA-256 written in {MANIFEST}.")
    third = ("A speech check: loads the model without using the network, speaks a paragraph in its built-in voice, then clones "
             "that recording and speaks one sentence in the cloned voice, which you can play afterwards. On a CPU this takes a "
             "few minutes.")
    notes = [f"Licences: the Chatterbox code (chatterbox-tts) and the Turbo weights are {manifest['license']}. Every clip Chatterbox "
             "makes carries Resemble AI's Perth watermark, a mark you cannot hear that detection tools can find; the Lab does not "
             "remove it. The Python packages keep their own licences, nearly all MIT, BSD or Apache. The exceptions are soxr and "
             "the libsndfile library inside soundfile (LGPL-2.1), and certifi and tqdm (MPL-2.0). They are installed on this "
             "computer and are not part of this repository.",
             "Turbo's speaker encoder also gives the Lab its voice match: the score that says how close a take is to the "
             "recording it was cloned from."]
    intro = textwrap.fill("This adds Chatterbox Turbo, a model that clones a voice from a recording of more than five seconds and "
                          "runs on an ordinary computer. Everything is installed into this folder (.venv-chatterbox, hf-cache and "
                          "out\\chatterbox) and nothing system-wide; pip keeps its usual download cache in your user profile.",
                          width=110)
    return "\n\n".join(["Fel's Voice Lab: Chatterbox Turbo 350M on the CPU", intro,
                        "\n".join([step(1, first), step(2, second), step(3, third)]),
                        *(textwrap.fill(note, width=110) for note in notes)]) + "\n"


def make_environment(root, *, runner=subprocess.run):
    """Make .venv-chatterbox from the Python that .venv was made from; venv puts pip in it from Python's own copy."""
    target = Path(root) / ENVIRONMENT
    python = getattr(sys, "_base_executable", sys.executable)
    # .venv's launcher leaves these behind, and the base Python would then take itself for .venv's.
    env = {key: value for key, value in os.environ.items() if key.upper() not in {"__PYVENV_LAUNCHER__", "VIRTUAL_ENV"}}
    code = runner([python, "-m", "venv", str(target)], cwd=root, env={**env, "PYTHONUTF8": "1"}).returncode
    if code or not environment_python(root).is_file():
        raise InstallError(f"Python could not make {ENVIRONMENT} (exit code {code}). Read its message above, delete the "
                           f"{ENVIRONMENT} folder if it is there, and run the installer again.")


def install_packages(root, *, runner=subprocess.run):
    command = [str(environment_python(root)), "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
               "--only-binary=:all:", "--no-deps", "--require-hashes", "-r", str(Path(root) / LOCK)]
    code = runner(command, cwd=root, env={**os.environ, "PYTHONUTF8": "1"}).returncode
    if code:
        raise InstallError(f"pip stopped with exit code {code}. Read its message above, fix the cause, and run the installer "
                           "again: it continues where it stopped.")


def run_speech_check(root, *, runner=subprocess.run):
    """In .venv-chatterbox, offline: load Turbo, speak a paragraph in its built-in voice, clone it, and speak the Models page's
    check sentence in the cloned voice."""
    folder = Path(root) / OUTPUT
    folder.mkdir(parents=True, exist_ok=True)
    reference, wav = folder / REFERENCE_WAV, folder / CHECK_WAV
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"}
    try:
        done = runner([str(environment_python(root)), "-c", SPEECH_CHECK, str(root), str(reference), str(wav)], cwd=root, env=env,
                      capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"The speech check took more than {CHECK_TIMEOUT // 60} minutes, so it was stopped.") from exc
    result = base.last_json_line(done.stdout or "")
    if done.returncode or not isinstance(result, dict):
        output = (done.stdout or "") + (done.stderr or "")
        hint = ""
        if "DLL" in output or "WinError 126" in output:
            hint = ("\nA missing DLL usually means the Microsoft Visual C++ Redistributable is not installed. Install the x64 "
                    "version from https://aka.ms/vs/17/release/vc_redist.x64.exe and run the installer again.")
        raise InstallError("The packages and model files are in place, but Chatterbox Turbo did not load or speak:\n"
                           + "\n".join(output.strip().splitlines()[-12:]) + hint)
    if result["problem"]:
        raise InstallError(f"Chatterbox Turbo loaded but a check clip was not speech: {result['problem']}.")
    return {**result, "reference": reference, "wav": wav}


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


def write_receipt(root, manifest, packages, check, match):
    receipt = {"schema": 1, "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "python": platform.python_version(), "environment": ENVIRONMENT, "torch": check.get("torch"),
               "packages_pinned": len(packages), "lock_sha256": hashlib.sha256((Path(root) / LOCK).read_bytes()).hexdigest(),
               "repo": manifest["repo"], "revision": manifest["revision"], "license": manifest["license"],
               "files": [item["path"] for item in manifest["files"]],
               "checkpoint_bytes": sum(item["size"] for item in manifest["files"]),
               "speech_check": {key: check[key] for key in ("load_seconds", "reference_seconds", "prepare_seconds",
                                                            "total_seconds", "audio_seconds")},
               "voice_match": match}
    path = Path(root) / OUTPUT / RECEIPT
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path


def run(args, *, say=base.say_now, ask=input, interactive=None):
    root = ROOT
    manifest = base.load_manifest(root / MANIFEST)
    packages = base.read_lock(root / LOCK)
    exists = environment_python(root).is_file()
    installed = environment_versions(root) if exists else {}
    missing, different = base.package_state(packages, installed)
    pending = base.pending_files(root, manifest, manifest["files"])
    todo = 0 if args.checkpoint_only else len(missing) + len(different)
    need = ((PACKAGES_DOWNLOAD_MB + PACKAGES_DISK_MB) * 10**6 if todo else 0) + sum(item["size"] for item in pending) \
        + SPACE_MARGIN_MB * 10**6
    problems = interpreter_problems(root)
    if args.checkpoint_only:
        problems += checkpoint_only_problems(root, installed)
    elif todo:
        problems += package_problems(different, has_pip=not exists or "pip" in installed)
    problems += long_path_problems(root, manifest)
    problems += base.space_problems(root, need)
    say(plan_text(manifest, packages, todo, pending, environment_exists=exists, checkpoint_only=args.checkpoint_only))
    if problems:
        raise InstallError("This computer cannot take the plan yet:\n" + "\n".join(
            textwrap.fill(problem, width=110, initial_indent="  - ", subsequent_indent="    ") for problem in problems))
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
        still, _ = base.package_state(packages, environment_versions(root))
        if still:
            raise InstallError(f"pip finished but {len(still)} packages are still missing, for example {still[0]['name']}.")
    say("Fetching the Chatterbox Turbo model files:")
    base.download_checkpoint(root, manifest, manifest["files"], say=say)
    say("Checking that Chatterbox Turbo speaks and clones a voice (on a CPU this takes a few minutes):")
    check = run_speech_check(root)
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
    parser.add_argument("--checkpoint-only", action="store_true",
                        help=f"fetch the model files only and leave the packages in {ENVIRONMENT} alone")
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
