"""Install Phonon 31M English on the CPU: its own .venv-phonon with the ptts runtime, the pinned model and voices into hf-cache,
then a speech check.

    .venv\\Scripts\\python.exe tools\\install_phonon.py
    .venv\\Scripts\\python.exe tools\\install_phonon.py --dry-run
    .venv\\Scripts\\python.exe tools\\install_phonon.py --yes
    .venv\\Scripts\\python.exe tools\\install_phonon.py --checkpoint-only

It prints what it will do and asks before it changes anything (--yes accepts the plan, --dry-run only shows it). Run it with
the lab's own Python, which setup.bat creates; setup-phonon.bat does that for you. Phonon gets an environment of its own,
.venv-phonon, made from the same Python 3.12 as .venv, so installing it changes no other model's packages. pip installs
requirements/phonon.txt there with every wheel's SHA-256 checked, and the script refuses to replace a package that is already
installed at another version. The files in catalog/phonon_snapshot.json (the model, and the 37 English preset voices with their
catalog) come from Hugging Face at the pinned revisions, resume after an interruption, and are kept only when their SHA-256
matches. Running it again installs only what is missing. --checkpoint-only fetches the model files and leaves the packages
alone, for a .venv-phonon you made yourself. The environment, download, hash and pip helpers are the ones install_kokoro.py
uses, so a fix there reaches this installer too.
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
LOCK = "requirements/phonon.txt"
MANIFEST = "catalog/phonon_snapshot.json"
ENVIRONMENT = ".venv-phonon"
RUNTIME = "ptts"
OUTPUT = "out/phonon"
CHECK_WAV = "install-check.wav"
RECEIPT = "installation.json"
# Measured on a clean install of the lock into a new .venv-phonon: 60 MB of wheels (the ptts wheel is 6.8 MB of it), 240 MB
# installed (pip and the venv included), and 95 characters for the longest path below site-packages.
PACKAGES_DOWNLOAD_MB = 60
PACKAGES_DISK_MB = 240
LONGEST_PACKAGE_PATH = 100
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
                  "total_seconds": info["total_s"], "audio_seconds": wav.size / rate, "ptts": engine.version}))
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


def runtime_package(packages):
    """The ptts line of the pinned list."""
    return next(item for item in packages if item["key"] == RUNTIME)


def environment_python(root):
    return base.env_python(root, ENVIRONMENT)


def environment_versions(root, *, runner=subprocess.run):
    """The packages installed in .venv-phonon by canonical name; {} when it has none yet."""
    return base.env_versions(root, ENVIRONMENT, runner=runner)


def interpreter_problems(root, **facts):
    """Why this Python is not the one to run the installer with, as sentences (an empty list when it is)."""
    return base.own_env_problems(root, ENVIRONMENT, "tools\\install_phonon.py", **facts)


def package_problems(different, *, has_pip=True):
    """Why pip should not be run in .venv-phonon: it has no pip, or packages are installed there at other versions."""
    return base.env_package_problems(ENVIRONMENT, "Phonon", different, has_pip=has_pip)


def checkpoint_only_problems(root, installed):
    return base.env_checkpoint_only_problems(root, ENVIRONMENT, "Phonon", RUNTIME, installed)


def long_path_problems(root, manifest, *, long_paths=None):
    """Windows stops at 259 characters unless long paths are on; the voice files sit in a deep cache folder."""
    files = [len(str(base.snapshot_dir(root, repo).resolve())) + 1 + len(item["path"])
             for repo in manifest["repos"] for item in repo["files"]]
    return base.env_long_path_problems(root, ENVIRONMENT, LONGEST_PACKAGE_PATH, files, long_paths=long_paths)


def plan_text(manifest, packages, todo, pending, *, checkpoint_only, environment_exists=False):
    """What the installer is about to do, written for the person who ran it."""
    model, voices = manifest["repos"][0], manifest["repos"][1]
    weights = max(model["files"], key=lambda item: item["size"])
    voice_files = [item for item in voices["files"] if item["path"].startswith("voices/")]
    runtime = runtime_package(packages)

    def step(number, text):
        return textwrap.fill(f"{number}. {text}", width=110, break_on_hyphens=False, initial_indent="  ", subsequent_indent="     ")

    if checkpoint_only:
        first = f"Python packages: the ones in {ENVIRONMENT} are left alone (--checkpoint-only)."
    elif not todo:
        first = f"Python packages: all {len(packages)} are already installed in {ENVIRONMENT}, {runtime['name']} {runtime['version']} among them."
    else:
        made = (f"{ENVIRONMENT} is already here." if environment_exists else
                f"First a Python environment of its own, {ENVIRONMENT}, made from the same Python 3.12 as .venv, so that "
                "installing Phonon changes no other model's packages.")
        first = (f"{made} Python packages: {todo} of {len(packages)} to install there: the runtime, {runtime['name']} "
                 f"{runtime['version']}, a 7 MB wheel that needs only NumPy, and the Lab's own NumPy, SciPy and SoundFile. About "
                 f"{PACKAGES_DOWNLOAD_MB} MB to download and {PACKAGES_DISK_MB} MB on disk; there is no PyTorch. pip checks every "
                 f"wheel against a SHA-256 written in {LOCK}.")
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
             "that uses one. The runtime is MIT OR Apache-2.0."]
    intro = textwrap.fill("This adds a very small speech model that runs on an ordinary computer, so the studio can speak in "
                          f"{len(voice_files)} preset voices. Everything is installed into this folder ({ENVIRONMENT}, hf-cache "
                          "and out\\phonon) and nothing system-wide; pip keeps its usual download cache in your user profile.",
                          width=110, break_on_hyphens=False)
    return "\n\n".join(["Fel's Voice Lab: Phonon 31M English on the CPU", intro, "\n".join([step(1, first), step(2, second), step(3, third)]),
                        *(textwrap.fill(note, width=110, break_on_hyphens=False) for note in notes)]) + "\n"


def make_environment(root, *, runner=subprocess.run):
    base.make_env(root, ENVIRONMENT, runner=runner)


def install_packages(root, *, runner=subprocess.run):
    base.pip_install(root, ENVIRONMENT, Path(root) / LOCK, runner=runner)


def run_speech_check(root, *, runner=subprocess.run):
    """In .venv-phonon, offline: load Phonon and have it speak the Models page's check sentence."""
    wav = Path(root) / OUTPUT / CHECK_WAV
    wav.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"}
    try:
        done = runner([str(environment_python(root)), "-c", SPEECH_CHECK, str(root), str(wav)], cwd=root, env=env,
                      capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise InstallError("Loading Phonon took more than 5 minutes, so the speech check was stopped.") from exc
    result = base.last_json_line(done.stdout or "")
    if done.returncode or not isinstance(result, dict):
        output = (done.stdout or "") + (done.stderr or "")
        raise InstallError("The packages and model files are in place, but Phonon did not load or speak:\n"
                           + "\n".join(output.strip().splitlines()[-12:]) + base.check_failure_hint(output))
    if result["problem"]:
        raise InstallError(f"Phonon loaded but the check clip was not speech: {result['problem']}.")
    return {**result, "wav": wav}


def write_receipt(root, manifest, packages, check):
    files = [f"{repo['repo']}/{item['path']}" for repo in manifest["repos"] for item in repo["files"]]
    receipt = {"schema": 1, "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "python": platform.python_version(), "environment": ENVIRONMENT, "ptts": check.get("ptts"),
               "packages_pinned": len(packages), "lock_sha256": hashlib.sha256((Path(root) / LOCK).read_bytes()).hexdigest(),
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
    exists = environment_python(root).is_file()
    installed = environment_versions(root) if exists else {}
    missing, different = base.package_state(packages, installed)
    pending = pending_files(root, manifest)
    todo = 0 if args.checkpoint_only else len(missing) + len(different)
    need = ((PACKAGES_DOWNLOAD_MB + PACKAGES_DISK_MB) * 10**6 if todo else 0) \
        + sum(item["size"] for _repo, item in pending) + SPACE_MARGIN_MB * 10**6
    problems = interpreter_problems(root)
    if args.checkpoint_only:
        problems += checkpoint_only_problems(root, installed)
    elif todo:
        problems += package_problems(different, has_pip=not exists or "pip" in installed)
    problems += long_path_problems(root, manifest)
    problems += base.space_problems(root, need)
    say(plan_text(manifest, packages, todo, pending, checkpoint_only=args.checkpoint_only, environment_exists=exists))
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
        install_packages(root)
        still, _ = base.package_state(packages, environment_versions(root))
        if still:
            raise InstallError(f"pip finished but {len(still)} packages are still missing, for example {still[0]['name']}.")
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
