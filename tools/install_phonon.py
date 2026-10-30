"""Install Phonon 31M English on the CPU: the ptts runtime into .venv, the pinned model and voices into hf-cache, then a speech check.

    .venv\\Scripts\\python.exe tools\\install_phonon.py
    .venv\\Scripts\\python.exe tools\\install_phonon.py --dry-run
    .venv\\Scripts\\python.exe tools\\install_phonon.py --yes
    .venv\\Scripts\\python.exe tools\\install_phonon.py --checkpoint-only

It prints what it will do and asks before it changes anything (--yes accepts the plan, --dry-run only shows it). Run it with
the lab's own Python, which setup.bat creates; setup-phonon.bat does that for you. pip installs requirements/phonon.txt with the
wheel's SHA-256 checked, and the script refuses to replace a package that is already installed at another version. The files in
catalog/phonon_snapshot.json (the model, and the 37 English preset voices with their catalog) come from Hugging Face at the
pinned revisions, resume after an interruption, and are kept only when their SHA-256 matches. Running it again installs only what
is missing. --checkpoint-only fetches the model files and leaves the Python package alone, for an environment that already has
ptts. The download, hash and pip helpers are the ones install_kokoro.py uses, so a fix there reaches this installer too.
"""

import argparse
import hashlib
import importlib.util
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
LOCK = "requirements/phonon.txt"
MANIFEST = "catalog/phonon_snapshot.json"
OUTPUT = "out/phonon"
CHECK_WAV = "install-check.wav"
RECEIPT = "installation.json"
# Measured: the wheel is 6.8 MB and the installed package 17.9 MB.
PACKAGE_DOWNLOAD_MB = 7
PACKAGE_DISK_MB = 18
SPACE_MARGIN_MB = 200

SPEECH_CHECK = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
import soundfile
import engine_checks
from engines import Phonon

started = time.perf_counter()
engine = Phonon("cpu")
loaded = time.perf_counter() - started
request = engine_checks.smoke_request("phonon-31m-en")
wav, rate, info = engine.speak(request["text"], voice=request["voice"])
soundfile.write(sys.argv[2], wav, rate)
print(json.dumps({"voice": request["voice"], "problem": engine_checks.audible(wav, rate), "load_seconds": loaded,
                  "total_seconds": info["total_s"], "audio_seconds": wav.size / rate}))
"""


def load_manifest(path):
    """The pinned repositories of catalog/phonon_snapshot.json, each with its revision, licence and files."""
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        repos = manifest["repos"]
        keys = ("repo", "revision", "license", "license_source", "files")
        if not isinstance(repos, list) or not repos or not all(isinstance(repo, dict) and all(key in repo for key in keys)
                                                                for repo in repos):
            raise ValueError("it lacks repo, revision, license, license_source or files")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise InstallError(f"The model file list {Path(path).name} is missing or unreadable ({exc}). "
                           "Restore it from the repository.") from exc
    return manifest


def pending_files(root, manifest):
    """The files that are not in place at their expected size, as (repository entry, file entry) pairs."""
    return [(repo, item) for repo in manifest["repos"] for item in base.pending_files(root, repo, repo["files"])]


def interpreter_problems(root, *, system=None, machine=None, version=None, prefix=None, bits=None):
    """Why this Python is not the one to install into, as sentences (an empty list when it is)."""
    system, machine = system or sys.platform, (machine or platform.machine()).lower()
    version, prefix = version or sys.version_info, prefix or sys.prefix
    bits = bits or (64 if sys.maxsize > 2**32 else 32)
    problems = []
    if system != "win32" or machine not in {"amd64", "x86_64"} or bits != 64:
        problems.append("This installer is for 64-bit Windows with 64-bit Python: the pinned runtime is a Windows x64 wheel.")
    if tuple(version[:2]) != (3, 12):
        problems.append(f"This is Python {version[0]}.{version[1]}; the lab runs on Python 3.12.")
    if Path(prefix).resolve() != (Path(root) / ".venv").resolve():
        problems.append("Run it with this folder's own Python, so that nothing else is changed: "
                        ".venv\\Scripts\\python.exe tools\\install_phonon.py (setup.bat creates that environment).")
    return problems


def package_problems(different, installed, *, has_pip=None):
    """Why pip should not be run here: it is missing, numpy (which ptts needs and the lab always has) is not there, or the
    runtime is installed at another version."""
    problems = []
    if not (importlib.util.find_spec("pip") is not None if has_pip is None else has_pip):
        problems.append("pip is not installed in this environment. setup.bat creates .venv with pip.")
    if "numpy" not in installed:
        problems.append("numpy is not installed here, and the runtime needs it. Run setup.bat first: it installs the lab's core packages.")
    if different:
        shown = ", ".join(f"{item['name']} {have} (the list pins {item['version']})" for item, have in different)
        problems.append(f"This environment already has another version of the runtime: {shown}. The installer does not replace "
                        "installed packages. If Phonon already runs here, add the model files with --checkpoint-only; otherwise "
                        "use a fresh copy of the lab.")
    return problems


def long_path_problems(root, manifest, *, long_paths=None):
    """Windows stops at 259 characters unless long paths are on; the voice files sit in a deep cache folder."""
    longest = max(len(str(base.snapshot_dir(root, repo).resolve())) + 1 + len(item["path"])
                  for repo in manifest["repos"] for item in repo["files"])
    if longest < base.MAX_PATH or (base.long_paths_enabled() if long_paths is None else long_paths):
        return []
    return [f"This folder's path is too long for Windows: the model files need paths of up to {longest} characters and "
            f"Windows stops at {base.MAX_PATH - 1}. Move the lab to a shorter path such as C:\\voice-lab, or switch on long paths "
            "in Windows."]


def plan_text(manifest, packages, todo, pending, *, checkpoint_only):
    """What the installer is about to do, written for the person who ran it."""
    model, voices = manifest["repos"][0], manifest["repos"][1]
    weights = max(model["files"], key=lambda item: item["size"])
    voice_files = [item for item in voices["files"] if item["path"].startswith("voices/")]

    def step(number, text):
        return textwrap.fill(f"{number}. {text}", width=110, initial_indent="  ", subsequent_indent="     ")

    if checkpoint_only:
        first = "Python package: left alone (--checkpoint-only)."
    elif not todo:
        first = f"Python package: {packages[0]['name']} {packages[0]['version']} is already installed."
    else:
        first = (f"Python package: the runtime, {packages[0]['name']} {packages[0]['version']}, a {PACKAGE_DOWNLOAD_MB} MB wheel that "
                 f"needs only numpy, which is installed. About {PACKAGE_DISK_MB} MB on disk. pip checks the wheel against a "
                 "SHA-256 written in requirements/phonon.txt.")
    second = (f"Phonon 31M English, revision {model['revision'][:7]}, from huggingface.co/{model['repo']}: the "
              f"{base.size_text(weights['size'])} model with its tokenizer, configuration and default voice, and "
              f"{len(voice_files)} English preset voices with their catalog, revision {voices['revision'][:7]}, from "
              f"huggingface.co/{voices['repo']} ({base.size_text(sum(item['size'] for item in voices['files']))}). "
              + (f"{base.size_text(sum(item['size'] for _repo, item in pending))} to download" if pending
                 else "Everything is already in place")
              + ". Every file is checked against a SHA-256 written in catalog/phonon_snapshot.json.")
    third = "A speech check: loads the model without using the network and speaks one sentence, which you can play afterwards."
    notes = [f"Licences: the model's weights, tokenizer and default voice are {model['license']} (its model card). The "
             f"{len(voice_files)} preset voices come from a separate repository that states no licence: it has no model card and no "
             "LICENSE file, so nothing says what audio made with them may be used for. Ask Gradium before you publish a recording "
             "that uses one. The runtime is MIT OR Apache-2.0.",
             "Installing the runtime adds a package to .venv, so the Models page will ask you to test the other models there again."]
    intro = textwrap.fill("This adds a very small speech model that runs on an ordinary computer, so the studio can speak in "
                          f"{len(voice_files)} preset voices. Everything is installed into this folder (.venv, hf-cache and out\\phonon) "
                          "and nothing system-wide; pip keeps its usual download cache in your user profile.", width=110)
    return "\n\n".join(["Fel's Voice Lab: Phonon 31M English on the CPU", intro, "\n".join([step(1, first), step(2, second), step(3, third)]),
                        *(textwrap.fill(note, width=110) for note in notes)]) + "\n"


def install_packages(root, *, runner=subprocess.run):
    command = [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "--only-binary=:all:",
               "--no-deps", "--require-hashes", "-r", str(Path(root) / LOCK)]
    code = runner(command, cwd=root, env={**os.environ, "PYTHONUTF8": "1"}).returncode
    if code:
        raise InstallError(f"pip stopped with exit code {code}. Read its message above, fix the cause, and run the installer "
                           "again: it continues where it stopped.")


def run_speech_check(root, *, runner=subprocess.run):
    """Load Phonon in a fresh process, offline, and have it speak the Models page's check sentence."""
    wav = Path(root) / OUTPUT / CHECK_WAV
    wav.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"}
    try:
        done = runner([sys.executable, "-c", SPEECH_CHECK, str(root), str(wav)], cwd=root, env=env, capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise InstallError("Loading Phonon took more than 5 minutes, so the speech check was stopped.") from exc
    result = base.last_json_line(done.stdout or "")
    if done.returncode or not isinstance(result, dict):
        output = (done.stdout or "") + (done.stderr or "")
        hint = ""
        if "DLL" in output or "WinError 126" in output:
            hint = ("\nA missing DLL usually means the Microsoft Visual C++ Redistributable is not installed. Install the x64 "
                    "version from https://aka.ms/vs/17/release/vc_redist.x64.exe and run the installer again.")
        raise InstallError("The package and model files are in place, but Phonon did not load or speak:\n"
                           + "\n".join(output.strip().splitlines()[-12:]) + hint)
    if result["problem"]:
        raise InstallError(f"Phonon loaded but the check clip was not speech: {result['problem']}.")
    return {**result, "wav": wav}


def write_receipt(root, manifest, packages, check):
    files = [f"{repo['repo']}/{item['path']}" for repo in manifest["repos"] for item in repo["files"]]
    receipt = {"schema": 1, "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "python": platform.python_version(), "ptts": base.installed_versions().get("ptts"),
               "lock_sha256": hashlib.sha256((Path(root) / LOCK).read_bytes()).hexdigest(),
               "repos": [{"repo": repo["repo"], "revision": repo["revision"], "license": repo["license"]}
                         for repo in manifest["repos"]],
               "files": files, "checkpoint_bytes": sum(item["size"] for repo in manifest["repos"] for item in repo["files"]),
               "speech_check": {key: check[key] for key in ("voice", "load_seconds", "total_seconds", "audio_seconds")}}
    path = Path(root) / OUTPUT / RECEIPT
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path


def run(args, *, say=base.say_now, ask=input, interactive=None):
    root = ROOT
    manifest = load_manifest(root / MANIFEST)
    packages = base.read_lock(root / LOCK)
    installed = base.installed_versions()
    missing, different = base.package_state(packages, installed)
    pending = pending_files(root, manifest)
    todo = 0 if args.checkpoint_only else len(missing) + len(different)
    need = ((PACKAGE_DOWNLOAD_MB + PACKAGE_DISK_MB) * 10**6 if todo else 0) \
        + sum(item["size"] for _repo, item in pending) + SPACE_MARGIN_MB * 10**6
    problems = interpreter_problems(root)
    if todo:
        problems += package_problems(different, installed)
    problems += long_path_problems(root, manifest)
    problems += base.space_problems(root, need)
    say(plan_text(manifest, packages, todo, pending, checkpoint_only=args.checkpoint_only))
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
        say("Installing the Python package (pip prints its own progress):")
        install_packages(root)
        still, _ = base.package_state(packages, base.installed_versions())
        if still:
            raise InstallError(f"pip finished but {still[0]['name']} is still missing.")
    say("Fetching the Phonon model and voice files:")
    for repo in manifest["repos"]:
        base.download_checkpoint(root, repo, repo["files"], say=say)
    say("Checking that Phonon speaks:")
    check = run_speech_check(root)
    receipt = write_receipt(root, manifest, packages, check)
    say("")
    say(f"Phonon is installed and spoke a test sentence ({check['audio_seconds']:.1f} s of audio in {check['total_seconds']:.1f} s "
        f"after a {check['load_seconds']:.1f} s load). The whole install took {time.monotonic() - started:.0f} s.")
    say(f"  Listen to it: {Path(check['wav']).relative_to(root)}")
    say(f"  Receipt: {receipt.relative_to(root)}")
    say("  Next: start the studio (the Fel's Voice Lab shortcut, or serve.bat), open Models & features and press Test.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="accept the plan without asking")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run the checks; change nothing")
    parser.add_argument("--checkpoint-only", action="store_true",
                        help="fetch the model files only and leave the Python package alone")
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
