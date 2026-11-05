"""Install Kokoro 82M: its own .venv-kokoro with pinned packages, the pinned checkpoint into hf-cache, then a speech check.

    .venv\\Scripts\\python.exe tools\\install_kokoro.py
    .venv\\Scripts\\python.exe tools\\install_kokoro.py --dry-run
    .venv\\Scripts\\python.exe tools\\install_kokoro.py --yes --voices all
    .venv\\Scripts\\python.exe tools\\install_kokoro.py --nvidia
    .venv\\Scripts\\python.exe tools\\install_kokoro.py --checkpoint-only

It prints what it will do and asks before it changes anything (--yes accepts the plan, --dry-run only shows it). Run it with
the lab's own Python, which setup.bat creates; setup-kokoro.bat does that for you. Kokoro gets an environment of its own,
.venv-kokoro, made from the same Python 3.12 as .venv, so nothing another model installs can clash with its packages. pip
installs requirements/kokoro-cpu.txt there with every file's SHA-256 checked, and the script refuses to replace a package that
is already installed at another version, so an environment that has its own PyTorch keeps it. --nvidia takes the CUDA build of
PyTorch in requirements/kokoro-nvidia.txt instead of the CPU one; the Lab has not been tried on an NVIDIA card yet. The
checkpoint files in catalog/kokoro_snapshot.json come from Hugging Face at the pinned revision, resume after an interruption,
and are kept only when their SHA-256 matches. Running it again installs only what is missing. --checkpoint-only fetches the
model files and leaves the packages alone, for a .venv-kokoro you made yourself. --voices takes supported (the default: every
voice that works with the packages it installs), english, all, or voice IDs such as bm_george.

The helpers for an engine's own environment, the downloads, the hashes and pip live here and install_phonon.py and
install_chatterbox.py use them, so a fix here reaches every installer.
"""

import argparse
import hashlib
import http.client
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    from hardware import capability_text, driver_text, nvidia_cards, nvidia_driver_present
except ImportError:  # run as a script, which puts tools\ first on the path and not the lab folder
    sys.path.append(str(ROOT))
    from hardware import capability_text, driver_text, nvidia_cards, nvidia_driver_present

LOCK = "requirements/kokoro-cpu.txt"
NVIDIA_LOCK = "requirements/kokoro-nvidia.txt"
MANIFEST = "catalog/kokoro_snapshot.json"
ENVIRONMENT = ".venv-kokoro"
SNAPSHOT = "hf-cache/hub/models--{repo}/snapshots/{revision}"
OUTPUT = "out/kokoro"
CHECK_WAV = "install-check.wav"
NVIDIA_REQUIREMENTS = "requirements-nvidia.txt"
RECEIPT = "installation.json"
HOST = "https://huggingface.co"
CHUNK = 1024 * 1024
# Voice IDs start with their language code. These two are American and British English.
ENGLISH = ("a", "b")
# Japanese and Mandarin Chinese need Misaki language packages that this script does not install.
EXTRA_PACKAGES = {"j": "Japanese", "z": "Mandarin Chinese"}
# Measured on a clean install of the lock into a new .venv-kokoro: 275 MB of wheels, 1.3 GB installed, and 126 characters for
# the longest path below site-packages. Windows stops at 259 characters unless long paths are switched on. The CUDA build of
# PyTorch is a 1.9 GB wheel where the CPU one is 123 MB, and takes about 4 GB more once installed (an estimate: it has not been
# installed on an NVIDIA computer yet).
PACKAGES_DOWNLOAD_MB = 275
PACKAGES_DISK_MB = 1330
NVIDIA_EXTRA_DOWNLOAD_MB = 1800
NVIDIA_EXTRA_DISK_MB = 4000
LONGEST_PACKAGE_PATH = 130
SPACE_MARGIN_MB = 500
MAX_PATH = 260
# The CUDA 13.0 build of PyTorch has code for cards of compute capability 7.5 and newer (GeForce GTX 16 and RTX 20 series and
# later, RTX 50 included), and CUDA 13.0 runs with NVIDIA driver 580 or later.
NVIDIA_BUILD = "Kokoro's NVIDIA build (PyTorch with CUDA 13.0)"
NVIDIA_CARDS = "GeForce GTX 16 and RTX 20 series cards and newer"
NVIDIA_LOWEST_CAPABILITY = (7, 5)
NVIDIA_DRIVER = (580, 0)
CUDA_NEEDS = "CUDA 13.0 needs an NVIDIA driver of version 580 or later and a GTX 16 or RTX 20 series card or newer"

SPEECH_CHECK = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
import soundfile
import torch
import engine_checks
import engines
from kokoro_support import discover_kokoro_voices

started = time.perf_counter()
engine = engines.Kokoro(sys.argv[3])
loaded = time.perf_counter() - started
request = engine_checks.smoke_request("kokoro")
wav, rate, info = engine.speak(request["text"], voice=request["voice"])
soundfile.write(sys.argv[2], wav, rate)
# The default reference that the cloning models' checks clone, spoken by the model already loaded.
if "af_heart" in discover_kokoro_voices():
    engines.ensure_reference(engine)
print(json.dumps({"voice": request["voice"], "problem": engine_checks.audible(wav, rate), "load_seconds": loaded,
                  "total_seconds": info["total_s"], "audio_seconds": wav.size / rate, "torch": torch.__version__,
                  "device": sys.argv[3]}))
"""

VERSIONS = """
import importlib.metadata, json
print(json.dumps({dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions() if dist.metadata["Name"]}))
"""


class InstallError(Exception):
    """A problem the person running the installer can read and act on."""


def say_now(text=""):
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def size_text(size):
    return f"{size / 1e6:.0f} MB" if size >= 1e6 else f"{size / 1e3:.0f} KB"


def load_manifest(path):
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        if not all(key in manifest for key in ("repo", "revision", "license", "files")):
            raise ValueError("it lacks repo, revision, license or files")
    except (OSError, ValueError, TypeError) as exc:
        raise InstallError(f"The model file list {Path(path).name} is missing or unreadable ({exc}). "
                           "Restore it from the repository.") from exc
    return manifest


_LOCK_LINE = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:==(?P<version>\S+)|@\s*(?P<url>\S+))"
                        r"\s+--hash=sha256:(?P<sha256>[0-9a-f]{64})")


def read_lock(path):
    """The pinned packages of requirements/kokoro-cpu.txt, each a dict with name, key, version and sha256."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"The package list {Path(path).name} is missing ({exc}). Restore it from the repository.") from exc
    packages = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_LINE.fullmatch(line)
        if match is None:
            raise InstallError(f"{Path(path).name} line {number} is not a pinned package with a SHA-256: {line[:80]}")
        version = match["version"] or urllib.parse.unquote(match["url"].rsplit("/", 1)[-1]).split("-")[1]
        packages.append({"name": match["name"], "key": canonical(match["name"]), "version": version,
                         "sha256": match["sha256"]})
    if not packages:
        raise InstallError(f"{Path(path).name} lists no packages.")
    return packages


def choose_voices(manifest, wanted):
    """The voice files to install: supported, english, all, or voice IDs, in ID order."""
    voices = {Path(item["path"]).stem: item for item in manifest["files"] if item["path"].startswith("voices/")}
    chosen = set()
    for item in wanted:
        if item == "supported":
            chosen.update(name for name in voices if name[0] not in EXTRA_PACKAGES)
        elif item == "english":
            chosen.update(name for name in voices if name[0] in ENGLISH)
        elif item == "all":
            chosen.update(voices)
        elif item in voices:
            chosen.add(item)
        else:
            raise InstallError(f"There is no voice named {item!r} at this revision. Use supported, english, all, or voice IDs "
                               f"such as {', '.join(sorted(voices)[:3])}.")
    return [voices[name] for name in sorted(chosen)]


def checkpoint_files(manifest, voices):
    return [item for item in manifest["files"] if not item["path"].startswith("voices/")] + list(voices)


def snapshot_dir(root, manifest):
    return Path(root) / SNAPSHOT.format(repo=manifest["repo"].replace("/", "--"), revision=manifest["revision"])


def pending_files(root, manifest, files):
    """The checkpoint files that are not in place at their expected size."""
    folder = snapshot_dir(root, manifest)
    return [item for item in files
            if not (folder / item["path"]).is_file() or (folder / item["path"]).stat().st_size != item["size"]]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(8 * CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def installed_versions():
    """The packages installed in the Python running this script, by canonical name."""
    importlib.invalidate_caches()
    versions = {}
    for dist in importlib.metadata.distributions():
        if dist.metadata["Name"]:
            versions[canonical(dist.metadata["Name"])] = dist.version
    return versions


def env_python(root, environment):
    """The interpreter of an engine's own environment, such as .venv-kokoro."""
    return Path(root) / environment / "Scripts" / "python.exe"


def env_versions(root, environment, *, runner=subprocess.run):
    """The packages installed in an engine's own environment by canonical name, read by its own Python; {} when it has none
    yet."""
    python = env_python(root, environment)
    if not python.is_file():
        return {}
    done = runner([str(python), "-I", "-c", VERSIONS], capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=120)
    found = last_json_line(done.stdout or "")
    if done.returncode or not isinstance(found, dict):
        raise InstallError(f"{environment} is here but its Python does not run:\n"
                           + "\n".join(((done.stdout or "") + (done.stderr or "")).strip().splitlines()[-8:])
                           + f"\nDelete the {environment} folder and run the installer again: it makes a new one.")
    return {canonical(name): version for name, version in found.items()}


def make_env(root, environment, *, runner=subprocess.run):
    """Make an engine's environment from the Python that .venv was made from; venv puts pip in it from Python's own copy."""
    target = Path(root) / environment
    python = getattr(sys, "_base_executable", sys.executable)
    # .venv's launcher leaves these behind, and the base Python would then take itself for .venv's.
    env = {key: value for key, value in os.environ.items() if key.upper() not in {"__PYVENV_LAUNCHER__", "VIRTUAL_ENV"}}
    code = runner([python, "-m", "venv", str(target)], cwd=root, env={**env, "PYTHONUTF8": "1"}).returncode
    if code or not env_python(root, environment).is_file():
        raise InstallError(f"Python could not make {environment} (exit code {code}). Read its message above, delete the "
                           f"{environment} folder if it is there, and run the installer again.")


def pip_install(root, environment, requirements, *, runner=subprocess.run):
    """pip install a pinned list into an engine's environment, every file checked against its SHA-256."""
    command = [str(env_python(root, environment)), "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
               "--only-binary=:all:", "--no-deps", "--require-hashes", "-r", str(requirements)]
    code = runner(command, cwd=root, env={**os.environ, "PYTHONUTF8": "1"}).returncode
    if code:
        raise InstallError(f"pip stopped with exit code {code}. Read its message above, fix the cause, and run the installer "
                           "again: it continues where it stopped.")


def own_env_problems(root, environment, script, *, system=None, machine=None, version=None, prefix=None, bits=None,
                     base_python=None):
    """Why this Python cannot make or fill an engine's own environment, as sentences (an empty list when it can): it must be
    64-bit Windows Python 3.12, it must be the lab's .venv, and the Python that .venv was made from must still be there."""
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
                        f".venv\\Scripts\\python.exe {script} (setup.bat creates that environment).")
    elif not base_python.is_file():
        problems.append(f"The Python 3.12 that .venv was made from is gone ({base_python}), and {environment} is made from it. "
                        "Install Python 3.12 again from python.org, then run setup.bat and this installer.")
    return problems


def env_package_problems(environment, label, different, *, has_pip=True):
    """Why pip should not be run in an engine's environment: it has no pip, or packages are installed there at other versions."""
    problems = []
    if not has_pip:
        problems.append(f"{environment} has no pip. Delete the {environment} folder and run the installer again: it makes a new "
                        "one with pip.")
    if different:
        shown = ", ".join(f"{item['name']} {have} (the list pins {item['version']})" for item, have in different[:5])
        more = f" and {len(different) - 5} more" if len(different) > 5 else ""
        problems.append(f"{environment} already has other versions of packages the {label} list pins: {shown}{more}. "
                        f"The installer does not replace installed packages. If {label} already runs there (with your own "
                        f"PyTorch, say), add the model files with --checkpoint-only; otherwise delete the {environment} folder "
                        "and run the installer again.")
    return problems


def env_checkpoint_only_problems(root, environment, label, package, installed):
    """--checkpoint-only keeps the packages it finds, so there must be an environment with the engine's package in it."""
    if not env_python(root, environment).is_file():
        return [f"--checkpoint-only adds the model files to a {environment} that already runs {label}, and there is no "
                f"{environment} here. Run the installer without it to make one."]
    if canonical(package) not in installed:
        return [f"--checkpoint-only keeps the packages in {environment} as they are, and {package} is not among them. "
                "Install it there yourself, or delete the folder and run the installer without --checkpoint-only."]
    return []


def env_long_path_problems(root, environment, longest_package_path, extra_lengths=(), *, long_paths=None):
    """Windows stops at 259 characters unless long paths are on, and PyTorch, spaCy and the model caches have deep folders."""
    site = len(str((Path(root) / environment / "Lib" / "site-packages").resolve())) + 1 + longest_package_path
    longest = max([site, *extra_lengths])
    if longest < MAX_PATH or (long_paths_enabled() if long_paths is None else long_paths):
        return []
    return [f"This folder's path is too long for Windows: the packages need paths of up to {longest} characters and Windows "
            f"stops at {MAX_PATH - 1}. Move the lab to a shorter path such as C:\\voice-lab, or switch on long paths in "
            "Windows."]


NO_NVIDIA_DRIVER = ("--nvidia asks for the CUDA build of PyTorch, and this computer has no NVIDIA driver (System32\\nvcuda.dll "
                    "is missing). Install the driver from nvidia.com first, or leave out --nvidia for the CPU build.")


def nvidia_card_problems(cards, *, build, cards_text, lowest=None, newest_major=None, driver=None):
    """Why an NVIDIA build of PyTorch would not run on these cards, as sentences that name the card: a card of a generation the
    build has no code for (older than `lowest`, or of a newer generation than `newest_major`), or a driver older than its CUDA
    needs. An empty list when it would run, and when nvidia-smi could not say."""
    problems = []
    for card in cards:
        capability = card.get("capability")
        if capability and ((lowest and capability < lowest) or (newest_major is not None and capability[0] > newest_major)):
            problems.append(f"{build} has no code for the {card['name']} (compute capability {capability_text(capability)}); "
                            f"it runs on {cards_text}.")
    drivers = [card["driver"] for card in cards if card.get("driver")]
    if driver and drivers and min(drivers) < driver:
        problems.append(f"{build} needs NVIDIA driver {driver[0]} or later, and this computer has {driver_text(min(drivers))}.")
    return problems


def card_problems(cards):
    """Why Kokoro's NVIDIA build would not run on these NVIDIA cards. tools/first_setup.py asks this too, and gives a model whose
    NVIDIA build cannot run here its CPU build instead."""
    return nvidia_card_problems(cards, build=NVIDIA_BUILD, cards_text=NVIDIA_CARDS, lowest=NVIDIA_LOWEST_CAPABILITY,
                                driver=NVIDIA_DRIVER)


def nvidia_advice(problem):
    """A problem card_problems found, with what to do about it for someone who typed --nvidia."""
    if "needs NVIDIA driver" in problem:
        return (problem + " Update the driver from nvidia.com or the NVIDIA app and run the installer again, or leave out "
                "--nvidia for the CPU build.")
    return problem + " Leave out --nvidia for the CPU build."


def nvidia_problems(*, present=None, cards=None, check=card_problems):
    """Why --nvidia cannot work on this computer, checked before anything is downloaded: there is no NVIDIA driver, or the card
    or the driver is one the build cannot run on (nvidia-smi says which)."""
    if not (present if present is not None else nvidia_driver_present()):
        return [NO_NVIDIA_DRIVER]
    return [nvidia_advice(problem) for problem in check(nvidia_cards() if cards is None else cards)]


def swap_packages(packages, replacements):
    """A pinned list with some packages swapped for other builds of them, such as torch for its CUDA build."""
    swapped = {item["key"]: item for item in replacements}
    unknown = sorted(set(swapped) - {item["key"] for item in packages})
    if unknown:
        raise InstallError(f"The GPU package list swaps {', '.join(unknown)}, which the CPU list does not pin.")
    return [swapped.get(item["key"], item) for item in packages]


def lock_lines(path):
    """The package lines of a pinned list by canonical name, exactly as written."""
    lines = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines[canonical(_LOCK_LINE.match(line)["name"])] = line
    return lines


def write_swapped_lock(root, lock, replacement_lock, destination):
    """Write the CPU list with the GPU list's lines in place of the packages it swaps, for pip to install."""
    lines = lock_lines(Path(root) / lock)
    lines.update(lock_lines(Path(root) / replacement_lock))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(f"# {lock} with the builds in {replacement_lock}, written by the installer.\n"
                           + "\n".join(lines.values()) + "\n", encoding="utf-8")
    return destination


def build_switch(different, cpu_packages, replacements):
    """The (package, installed version) pairs that --nvidia replaces in an environment that holds the CPU build: the packages
    the GPU list swaps, installed at the CPU list's version. Any other package at another version still stops the installer,
    so an environment someone filled with their own packages is never changed."""
    swapped = {item["key"] for item in replacements}
    cpu = {item["key"]: item["version"] for item in cpu_packages}
    return [(item, have) for item, have in different if item["key"] in swapped and have == cpu.get(item["key"])]


def switch_text(switching, build):
    """The plan's sentence for an environment whose CPU build of PyTorch is replaced by a GPU build."""
    names = ", ".join(f"{item['name']} {have} becomes {item['version']}" for item, have in switching)
    return f"its CPU build of PyTorch is replaced by {build} ({names})"


def package_state(packages, installed):
    """The packages that are not installed, and (package, installed version) pairs for those at another version."""
    missing = [item for item in packages if item["key"] not in installed]
    different = [(item, installed[item["key"]]) for item in packages
                 if item["key"] in installed and installed[item["key"]] != item["version"]]
    return missing, different


def long_paths_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except (ImportError, OSError):
        return False


def environment_python(root):
    return env_python(root, ENVIRONMENT)


def environment_versions(root, *, runner=subprocess.run):
    """The packages installed in .venv-kokoro by canonical name; {} when it has none yet."""
    return env_versions(root, ENVIRONMENT, runner=runner)


def interpreter_problems(root, **facts):
    """Why this Python is not the one to run the installer with, as sentences (an empty list when it is)."""
    return own_env_problems(root, ENVIRONMENT, "tools\\install_kokoro.py", **facts)


def package_problems(different, *, has_pip=True):
    """Why pip should not be run in .venv-kokoro: it has no pip, or packages are installed there at other versions."""
    return env_package_problems(ENVIRONMENT, "Kokoro", different, has_pip=has_pip)


def checkpoint_only_problems(root, installed):
    return env_checkpoint_only_problems(root, ENVIRONMENT, "Kokoro", "kokoro", installed)


def long_path_problems(root, *, long_paths=None):
    """Windows stops at 259 characters unless long paths are on; torch and spaCy have deep folders."""
    return env_long_path_problems(root, ENVIRONMENT, LONGEST_PACKAGE_PATH, long_paths=long_paths)


def space_problems(root, need, *, free=None):
    free = shutil.disk_usage(root).free if free is None else free
    if free < need:
        return [f"About {size_text(need)} of free disk space is needed here and {size_text(free)} is free."]
    return []


def mb_text(megabytes):
    return f"{megabytes} MB" if megabytes < 1000 else f"{megabytes / 1000:.1f} GB"


def packages_download_mb(nvidia):
    return PACKAGES_DOWNLOAD_MB + (NVIDIA_EXTRA_DOWNLOAD_MB if nvidia else 0)


def packages_disk_mb(nvidia):
    return PACKAGES_DISK_MB + (NVIDIA_EXTRA_DISK_MB if nvidia else 0)


def plan_text(manifest, packages, todo, files, pending, *, checkpoint_only, environment_exists=False, nvidia=False,
              switching=()):
    """What the installer is about to do, written for the person who ran it."""
    voices = [item for item in files if item["path"].startswith("voices/")]
    checkpoint = max(files, key=lambda item: item["size"])
    languages = sorted({EXTRA_PACKAGES[Path(item["path"]).stem[0]] for item in voices
                        if Path(item["path"]).stem[0] in EXTRA_PACKAGES})

    def step(number, text):
        return textwrap.fill(f"{number}. {text}", width=110, break_on_hyphens=False, initial_indent="  ", subsequent_indent="     ")

    build = ("the CUDA 13.0 build of PyTorch for NVIDIA graphics cards (not yet tried on one)" if nvidia
             else "PyTorch for the CPU")
    lists = f"{LOCK} and {NVIDIA_LOCK}" if nvidia else LOCK
    if checkpoint_only:
        first = f"Python packages: the ones in {ENVIRONMENT} are left alone (--checkpoint-only)."
    elif not todo:
        first = f"Python packages: all {len(packages)} are already installed in {ENVIRONMENT}."
    elif switching and todo == len(switching):
        first = (f"Python packages: Kokoro is already installed in {ENVIRONMENT}, and {switch_text(switching, build)}. "
                 f"About {mb_text(NVIDIA_EXTRA_DOWNLOAD_MB)} to download and {mb_text(NVIDIA_EXTRA_DISK_MB)} more on disk. pip "
                 f"checks every file against a SHA-256 written in {lists}.")
    else:
        made = (f"{ENVIRONMENT} is already here." if environment_exists else
                f"First a Python environment of its own, {ENVIRONMENT}, made from the same Python 3.12 as .venv, so that "
                "nothing another model installs can clash with Kokoro's packages.")
        first = (f"{made} Python packages: {todo} of {len(packages)} to install there, among them {build}, Transformers "
                 f"and spaCy. About {mb_text(packages_download_mb(nvidia))} to download and "
                 f"{mb_text(packages_disk_mb(nvidia))} on disk. pip checks every file against a SHA-256 written in "
                 f"{lists}.")
    english = bool(voices) and all(Path(item["path"]).stem[0] in ENGLISH for item in voices)
    second = (f"Kokoro 82M, revision {manifest['revision'][:7]}, from huggingface.co/{manifest['repo']}: the "
              f"{size_text(checkpoint['size'])} checkpoint, its model card and {len(voices)} {'English ' if english else ''}"
              f"voice{'' if len(voices) == 1 else 's'}. "
              + (f"{size_text(sum(item['size'] for item in pending))} to download" if pending else "Everything is already in place")
              + ". Every file is checked against a SHA-256 written in catalog/kokoro_snapshot.json.")
    third = ("A speech check: loads the model without using the network and speaks one sentence, which you can play afterwards"
             + (", on the graphics card." if nvidia else "."))
    notes = []
    if languages:
        notes.append(f"The {' and '.join(languages)} voices also need Misaki language packages that this installer does not add.")
    if nvidia:
        notes.append("The CUDA 13.0 build needs a GeForce GTX 16 or RTX 20 series card or newer and an NVIDIA driver of version 580 or "
                     "later. It carries NVIDIA's CUDA libraries inside it, under NVIDIA's own licence terms. Nobody has tried the Lab "
                     "on an NVIDIA card yet: if it works, or does not, a hardware report on GitHub helps the next person.")
    notes.append(f"Licences: the Kokoro weights are {manifest['license']}. The Python packages keep their own licences, nearly all "
                 "MIT, BSD or Apache. The exceptions are phonemizer-fork and the espeak-ng library inside espeakng-loader "
                 "(GPL-3.0), num2words (LGPL), and certifi and tqdm (MPL-2.0). They are installed on this computer and are not "
                 "part of this repository.")
    intro = textwrap.fill("This adds a small speech model that runs on an ordinary computer, so the studio can speak. Everything is "
                          f"installed into this folder ({ENVIRONMENT}, hf-cache and out\\kokoro) and nothing system-wide; pip keeps "
                          "its usual download cache in your user profile.", width=110, break_on_hyphens=False)
    title = "Fel's Voice Lab: Kokoro 82M on an NVIDIA graphics card" if nvidia else "Fel's Voice Lab: Kokoro 82M on the CPU"
    return "\n\n".join([title, intro, "\n".join([step(1, first), step(2, second), step(3, third)]),
                        *(textwrap.fill(note, width=110, break_on_hyphens=False) for note in notes)]) + "\n"


def download_url(manifest, item):
    return f"{HOST}/{manifest['repo']}/resolve/{manifest['revision']}/{urllib.parse.quote(item['path'])}"


def replace_file(source, destination):
    """os.replace, tried again for a few seconds because a virus scanner may still hold the new file."""
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(1)


NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, ssl.SSLError)


def fetch(url, destination, size, sha256, *, say=say_now, attempts=4, pause=time.sleep):
    """Download one file: resumed after an interruption, and kept only when its size and SHA-256 match.

    Returns False when the file was already in place."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    if destination.is_file() and destination.stat().st_size == size and sha256_file(destination) == sha256:
        return False
    last, shown = "no attempt was made", 0
    for attempt in range(attempts):
        have = partial.stat().st_size if partial.is_file() else 0
        if have > size:
            partial.unlink()
            have = 0
        try:
            if have < size:
                request = urllib.request.Request(url, headers={"User-Agent": "fels-voice-lab-installer"})
                if have:
                    request.add_header("Range", f"bytes={have}-")
                with urllib.request.urlopen(request, timeout=60) as response:
                    if have and (response.status != 206 or not (response.headers.get("Content-Range") or "").startswith(f"bytes {have}-")):
                        have = 0
                    shown = max(shown, have * 10 // size)
                    with partial.open("ab" if have else "wb") as target:
                        while chunk := response.read(CHUNK):
                            target.write(chunk)
                            have += len(chunk)
                            if size >= 5e6 and have * 10 // size > shown:
                                shown = have * 10 // size
                                say(f"      {shown * 10}% ({size_text(have)} of {size_text(size)})")
                            if have > size:
                                last = "the server sent more data than the file holds"
                                break
        except urllib.error.HTTPError as exc:
            if exc.code not in (408, 429) and exc.code < 500:
                raise InstallError(f"{url} answered {exc.code} {exc.reason}. The pinned revision may have been removed.") from exc
            last = f"HTTP {exc.code}"
        except NETWORK_ERRORS as exc:
            last = f"{type(exc).__name__}: {getattr(exc, 'reason', None) or exc}"
        except OSError as exc:
            raise InstallError(f"Cannot write {partial}: {exc}") from exc
        if partial.is_file() and partial.stat().st_size == size:
            break
        if attempt + 1 < attempts:
            pause(attempt + 1)
    else:
        raise InstallError(f"The download of {destination.name} stopped ({last}). Run the installer again to resume it.")
    if sha256_file(partial) != sha256:
        partial.unlink()
        raise InstallError(f"{destination.name} does not match the published SHA-256, so it was deleted. Run the installer "
                           "again; if this repeats, the file at the pinned revision has changed.")
    replace_file(partial, destination)
    return True


def download_checkpoint(root, manifest, files, *, say=say_now, pause=time.sleep):
    folder = snapshot_dir(root, manifest)
    for item in files:
        if folder.resolve() not in (folder / item["path"]).resolve().parents:
            raise InstallError(f"The model file list names a path outside the model folder: {item['path']}")
    for position, item in enumerate(files, 1):
        say(f"  [{position}/{len(files)}] {item['path']} ({size_text(item['size'])})")
        if not fetch(download_url(manifest, item), folder / item["path"], item["size"], item["sha256"], say=say, pause=pause):
            say("      already in place")
    refs = folder.parent.parent / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(manifest["revision"], encoding="utf-8")


def make_environment(root, *, runner=subprocess.run):
    make_env(root, ENVIRONMENT, runner=runner)


def pinned_packages(root, *, nvidia=False):
    """The packages to install: requirements/kokoro-cpu.txt, with the CUDA build of torch in place of the CPU one for --nvidia."""
    packages = read_lock(Path(root) / LOCK)
    return swap_packages(packages, read_lock(Path(root) / NVIDIA_LOCK)) if nvidia else packages


def install_packages(root, *, nvidia=False, runner=subprocess.run):
    """Install the pinned packages into .venv-kokoro. For --nvidia, pip gets the CPU list with the CUDA torch swapped in,
    written beside the receipt."""
    requirements = Path(root) / LOCK
    if nvidia:
        requirements = write_swapped_lock(root, LOCK, NVIDIA_LOCK, Path(root) / OUTPUT / NVIDIA_REQUIREMENTS)
    pip_install(root, ENVIRONMENT, requirements, runner=runner)


def last_json_line(text):
    for line in reversed(text.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def check_failure_hint(output, *, nvidia=False, cuda_needs=CUDA_NEEDS):
    """A sentence for the most likely cause of a speech check that failed, from what it printed. cuda_needs says which cards and
    which driver the model's NVIDIA build runs on."""
    if "DLL" in output or "WinError 126" in output:
        return ("\nA missing DLL usually means the Microsoft Visual C++ Redistributable is not installed. Install the x64 "
                "version from https://aka.ms/vs/17/release/vc_redist.x64.exe and run the installer again.")
    if nvidia and ("CUDA" in output or "cuda" in output):
        return (f"\nPyTorch could not use the graphics card. {cuda_needs}: update the driver from nvidia.com and run the "
                "installer again. To use the CPU instead, delete the folder of the model's environment and run the installer "
                "without --nvidia.")
    return ""


def run_speech_check(root, *, device="cpu", runner=subprocess.run):
    """In .venv-kokoro, offline: load Kokoro on the device and have it speak the Models page's check sentence."""
    wav = Path(root) / OUTPUT / CHECK_WAV
    wav.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"}
    try:
        done = runner([str(environment_python(root)), "-c", SPEECH_CHECK, str(root), str(wav), device], cwd=root, env=env,
                      capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired as exc:
        raise InstallError("Loading Kokoro took more than 15 minutes, so the speech check was stopped.") from exc
    result = last_json_line(done.stdout or "")
    if done.returncode or not isinstance(result, dict):
        output = (done.stdout or "") + (done.stderr or "")
        raise InstallError("The packages and model files are in place, but Kokoro did not load or speak:\n"
                           + "\n".join(output.strip().splitlines()[-12:]) + check_failure_hint(output, nvidia=device == "cuda"))
    if result["problem"]:
        raise InstallError(f"Kokoro loaded but the check clip was not speech: {result['problem']}.")
    return {**result, "wav": wav}


def write_receipt(root, manifest, packages, files, check):
    receipt = {"schema": 1, "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "python": platform.python_version(), "environment": ENVIRONMENT, "torch": check.get("torch"),
               "device": check.get("device", "cpu"),
               "packages_pinned": len(packages), "lock_sha256": hashlib.sha256((Path(root) / LOCK).read_bytes()).hexdigest(),
               "repo": manifest["repo"], "revision": manifest["revision"], "license": manifest["license"],
               "files": [item["path"] for item in files], "checkpoint_bytes": sum(item["size"] for item in files),
               "speech_check": {key: check[key] for key in ("voice", "load_seconds", "total_seconds", "audio_seconds")}}
    path = Path(root) / OUTPUT / RECEIPT
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path


def confirmed(args, *, ask=input, interactive=None):
    if args.yes:
        return True
    if not (sys.stdin.isatty() if interactive is None else interactive):
        raise InstallError("This is not an interactive terminal, so there is nobody to ask. Nothing was changed. "
                           "Add --yes to accept the plan.")
    try:
        return ask("Type yes to install, or press Enter to stop: ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def run(args, *, say=say_now, ask=input, interactive=None):
    root = ROOT
    nvidia = getattr(args, "nvidia", False)
    manifest = load_manifest(root / MANIFEST)
    packages = pinned_packages(root, nvidia=nvidia)
    files = checkpoint_files(manifest, choose_voices(manifest, args.voices))
    exists = environment_python(root).is_file()
    installed = environment_versions(root) if exists else {}
    missing, different = package_state(packages, installed)
    # --nvidia on a Kokoro installed for the CPU replaces its PyTorch; any other difference still stops the installer.
    switching = build_switch(different, read_lock(root / LOCK), read_lock(root / NVIDIA_LOCK)) if nvidia else []
    conflicts = [pair for pair in different if pair not in switching]
    pending = pending_files(root, manifest, files)
    todo = 0 if args.checkpoint_only else len(missing) + len(different)
    packages_need = (packages_download_mb(nvidia) + packages_disk_mb(nvidia)) * 10**6 if todo else 0
    if switching and todo == len(switching):
        packages_need = (NVIDIA_EXTRA_DOWNLOAD_MB + NVIDIA_EXTRA_DISK_MB) * 10**6
    need = packages_need + sum(item["size"] for item in pending) + SPACE_MARGIN_MB * 10**6
    problems = interpreter_problems(root)
    if nvidia:
        problems += nvidia_problems()
    if args.checkpoint_only:
        problems += checkpoint_only_problems(root, installed)
    elif todo:
        problems += package_problems(conflicts, has_pip=not exists or "pip" in installed) + long_path_problems(root)
    problems += space_problems(root, need)
    say(plan_text(manifest, packages, todo, files, pending, checkpoint_only=args.checkpoint_only, environment_exists=exists,
                  nvidia=nvidia, switching=switching))
    if problems:
        raise InstallError("This computer cannot take the plan yet:\n" + "\n".join(
            textwrap.fill(problem, width=110, break_on_hyphens=False, initial_indent="  - ", subsequent_indent="    ") for problem in problems))
    if args.dry_run:
        say("Checks passed. This was a dry run, so nothing was changed.")
        return 0
    if not confirmed(args, ask=ask, interactive=interactive):
        say("Nothing was changed.")
        return 1
    started = time.monotonic()
    if todo:
        if not exists:
            say(f"Making {ENVIRONMENT}:")
            make_environment(root)
        say(f"Installing the Python packages into {ENVIRONMENT} (pip prints its own progress):")
        install_packages(root, nvidia=nvidia)
        still_missing, still_different = package_state(packages, environment_versions(root))
        if still_missing:
            raise InstallError(f"pip finished but {len(still_missing)} packages are still missing, for example "
                               f"{still_missing[0]['name']}.")
        if switching and still_different:
            item, have = still_different[0]
            raise InstallError(f"pip finished but {item['name']} is still {have}, not {item['version']}.")
    say("Fetching the Kokoro model files:")
    download_checkpoint(root, manifest, files, say=say)
    say("Checking that Kokoro speaks (the first load is slow):")
    check = run_speech_check(root, device="cuda" if nvidia else "cpu")
    receipt = write_receipt(root, manifest, packages, files, check)
    say("")
    where = " on the graphics card" if nvidia else ""
    say(f"Kokoro is installed and spoke a test sentence{where} ({check['audio_seconds']:.1f} s of audio in "
        f"{check['total_seconds']:.1f} s after a {check['load_seconds']:.0f} s load). The whole install took "
        f"{time.monotonic() - started:.0f} s.")
    say(f"  Listen to it: {Path(check['wav']).relative_to(root)}")
    say(f"  Receipt: {receipt.relative_to(root)}")
    say("  Next: start the studio (the Fel's Voice Lab shortcut, or serve.bat), open Models & features and press Test.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="accept the plan without asking")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run the checks; change nothing")
    parser.add_argument("--voices", nargs="+", default=["supported"], metavar="VOICE",
                        help="supported (the default: every voice that works with these packages), english, all, or voice IDs "
                             "such as bm_george ef_dora")
    parser.add_argument("--nvidia", action="store_true",
                        help="install the CUDA build of PyTorch for an NVIDIA graphics card instead of the CPU build (untested)")
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
