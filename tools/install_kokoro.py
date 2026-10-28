"""Install Kokoro 82M on the CPU: pinned packages into .venv, the pinned checkpoint into hf-cache, then a speech check.

    .venv\\Scripts\\python.exe tools\\install_kokoro.py
    .venv\\Scripts\\python.exe tools\\install_kokoro.py --dry-run
    .venv\\Scripts\\python.exe tools\\install_kokoro.py --yes --voices all
    .venv\\Scripts\\python.exe tools\\install_kokoro.py --checkpoint-only

It prints what it will do and asks before it changes anything (--yes accepts the plan, --dry-run only shows it). Run it with
the lab's own Python, which setup.bat creates; setup-kokoro.bat does that for you. pip installs requirements/kokoro-cpu.txt
with every file's SHA-256 checked, and the script refuses to replace a package that is already installed at another version,
so an environment that has its own PyTorch keeps it. The checkpoint files in catalog/kokoro_snapshot.json come from Hugging
Face at the pinned revision, resume after an interruption, and are kept only when their SHA-256 matches. Running it again
installs only what is missing. --checkpoint-only fetches the model files and leaves the Python packages alone, for an
environment that already runs Kokoro. --voices takes supported (the default: every voice that works with the packages it
installs), english, all, or voice IDs such as bm_george.
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
LOCK = "requirements/kokoro-cpu.txt"
MANIFEST = "catalog/kokoro_snapshot.json"
SNAPSHOT = "hf-cache/hub/models--{repo}/snapshots/{revision}"
OUTPUT = "out/kokoro"
CHECK_WAV = "install-check.wav"
RECEIPT = "installation.json"
HOST = "https://huggingface.co"
CHUNK = 1024 * 1024
# Voice IDs start with their language code. These two are American and British English.
ENGLISH = ("a", "b")
# Japanese and Mandarin Chinese need Misaki language packages that this script does not install.
EXTRA_PACKAGES = {"j": "Japanese", "z": "Mandarin Chinese"}
# Measured on a clean install of the lock: 222 MB of wheels, 1011 MB installed, and 126 characters for the longest path
# below site-packages. Windows stops at 259 characters unless long paths are switched on.
PACKAGES_DOWNLOAD_MB = 225
PACKAGES_DISK_MB = 1000
LONGEST_PACKAGE_PATH = 130
SPACE_MARGIN_MB = 500
MAX_PATH = 260

SPEECH_CHECK = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
import soundfile
import engine_checks
from engines import Kokoro

started = time.perf_counter()
engine = Kokoro("cpu")
loaded = time.perf_counter() - started
request = engine_checks.smoke_request("kokoro")
wav, rate, info = engine.speak(request["text"], voice=request["voice"])
soundfile.write(sys.argv[2], wav, rate)
print(json.dumps({"voice": request["voice"], "problem": engine_checks.audible(wav, rate), "load_seconds": loaded,
                  "total_seconds": info["total_s"], "audio_seconds": wav.size / rate}))
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
        version = match["version"] or match["url"].rsplit("/", 1)[-1].split("-")[1]
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
    importlib.invalidate_caches()
    versions = {}
    for dist in importlib.metadata.distributions():
        if dist.metadata["Name"]:
            versions[canonical(dist.metadata["Name"])] = dist.version
    return versions


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


def interpreter_problems(root, *, system=None, machine=None, version=None, prefix=None, bits=None):
    """Why this Python is not the one to install into, as sentences (an empty list when it is)."""
    system, machine = system or sys.platform, (machine or platform.machine()).lower()
    version, prefix = version or sys.version_info, prefix or sys.prefix
    bits = bits or (64 if sys.maxsize > 2**32 else 32)
    problems = []
    if system != "win32" or machine not in {"amd64", "x86_64"} or bits != 64:
        problems.append("This installer is for 64-bit Windows with 64-bit Python: the pinned packages are Windows x64 builds.")
    if tuple(version[:2]) != (3, 12):
        problems.append(f"This is Python {version[0]}.{version[1]}; the pinned packages are built for Python 3.12.")
    if Path(prefix).resolve() != (Path(root) / ".venv").resolve():
        problems.append("Run it with this folder's own Python, so that nothing else is changed: "
                        ".venv\\Scripts\\python.exe tools\\install_kokoro.py (setup.bat creates that environment).")
    return problems


def package_problems(different, *, has_pip=None):
    """Why pip should not be run here: it is missing, or packages are installed at other versions."""
    problems = []
    if not (importlib.util.find_spec("pip") is not None if has_pip is None else has_pip):
        problems.append("pip is not installed in this environment. setup.bat creates .venv with pip.")
    if different:
        shown = ", ".join(f"{item['name']} {have} (the list pins {item['version']})" for item, have in different[:5])
        more = f" and {len(different) - 5} more" if len(different) > 5 else ""
        problems.append(f"This environment already has other versions of packages the Kokoro list pins: {shown}{more}. "
                        "The installer does not replace installed packages. If Kokoro already runs here, add the model "
                        "files with --checkpoint-only; otherwise use a fresh copy of the lab.")
    return problems


def long_path_problems(root, *, long_paths=None):
    """Windows stops at 259 characters unless long paths are on; torch and spaCy have deep folders."""
    longest = len(str((Path(root) / ".venv" / "Lib" / "site-packages").resolve())) + 1 + LONGEST_PACKAGE_PATH
    if longest < MAX_PATH or (long_paths_enabled() if long_paths is None else long_paths):
        return []
    return [f"This folder's path is too long for Windows: the packages need paths of up to {longest} characters and "
            f"Windows stops at {MAX_PATH - 1}. Move the lab to a shorter path such as C:\\voice-lab, or switch on long paths "
            "in Windows."]


def space_problems(root, need, *, free=None):
    free = shutil.disk_usage(root).free if free is None else free
    if free < need:
        return [f"About {size_text(need)} of free disk space is needed here and {size_text(free)} is free."]
    return []


def plan_text(manifest, packages, todo, files, pending, *, checkpoint_only):
    """What the installer is about to do, written for the person who ran it."""
    voices = [item for item in files if item["path"].startswith("voices/")]
    checkpoint = max(files, key=lambda item: item["size"])
    languages = sorted({EXTRA_PACKAGES[Path(item["path"]).stem[0]] for item in voices
                        if Path(item["path"]).stem[0] in EXTRA_PACKAGES})

    def step(number, text):
        return textwrap.fill(f"{number}. {text}", width=110, initial_indent="  ", subsequent_indent="     ")

    if checkpoint_only:
        first = "Python packages: left alone (--checkpoint-only)."
    elif not todo:
        first = f"Python packages: all {len(packages)} are already installed."
    else:
        first = (f"Python packages: {todo} of {len(packages)} to install, among them PyTorch for the CPU, Transformers and spaCy. "
                 f"About {PACKAGES_DOWNLOAD_MB} MB to download and {PACKAGES_DISK_MB / 1000:.1f} GB on disk. pip checks every "
                 "file against a SHA-256 written in requirements/kokoro-cpu.txt.")
    english = bool(voices) and all(Path(item["path"]).stem[0] in ENGLISH for item in voices)
    second = (f"Kokoro 82M, revision {manifest['revision'][:7]}, from huggingface.co/{manifest['repo']}: the "
              f"{size_text(checkpoint['size'])} checkpoint, its model card and {len(voices)} {'English ' if english else ''}"
              f"voice{'' if len(voices) == 1 else 's'}. "
              + (f"{size_text(sum(item['size'] for item in pending))} to download" if pending else "Everything is already in place")
              + ". Every file is checked against a SHA-256 written in catalog/kokoro_snapshot.json.")
    third = "A speech check: loads the model without using the network and speaks one sentence, which you can play afterwards."
    notes = []
    if languages:
        notes.append(f"The {' and '.join(languages)} voices also need Misaki language packages that this installer does not add.")
    notes.append(f"Licences: the Kokoro weights are {manifest['license']}. The Python packages keep their own licences, nearly all "
                 "MIT, BSD or Apache. The exceptions are phonemizer-fork and the espeak-ng library inside espeakng-loader "
                 "(GPL-3.0), num2words (LGPL), and certifi and tqdm (MPL-2.0). They are installed on this computer and are not "
                 "part of this repository.")
    intro = textwrap.fill("This adds a small speech model that runs on an ordinary computer, so the studio can speak. Everything is "
                          "installed into this folder (.venv, hf-cache and out\\kokoro) and nothing system-wide; pip keeps its usual "
                          "download cache in your user profile.", width=110)
    return "\n\n".join(["Fel's Voice Lab: Kokoro 82M on the CPU", intro, "\n".join([step(1, first), step(2, second), step(3, third)]),
                        *(textwrap.fill(note, width=110) for note in notes)]) + "\n"


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


def install_packages(root, *, runner=subprocess.run):
    command = [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "--only-binary=:all:",
               "--no-deps", "--require-hashes", "-r", str(Path(root) / LOCK)]
    code = runner(command, cwd=root, env={**os.environ, "PYTHONUTF8": "1"}).returncode
    if code:
        raise InstallError(f"pip stopped with exit code {code}. Read its message above, fix the cause, and run the installer "
                           "again: it continues where it stopped.")


def last_json_line(text):
    for line in reversed(text.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def run_speech_check(root, *, runner=subprocess.run):
    """Load Kokoro in a fresh process, offline, and have it speak the Models page's check sentence."""
    wav = Path(root) / OUTPUT / CHECK_WAV
    wav.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"}
    try:
        done = runner([sys.executable, "-c", SPEECH_CHECK, str(root), str(wav)], cwd=root, env=env, capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired as exc:
        raise InstallError("Loading Kokoro took more than 15 minutes, so the speech check was stopped.") from exc
    result = last_json_line(done.stdout or "")
    if done.returncode or not isinstance(result, dict):
        output = (done.stdout or "") + (done.stderr or "")
        hint = ""
        if "DLL" in output or "WinError 126" in output:
            hint = ("\nA missing DLL usually means the Microsoft Visual C++ Redistributable is not installed. Install the x64 "
                    "version from https://aka.ms/vs/17/release/vc_redist.x64.exe and run the installer again.")
        raise InstallError("The packages and model files are in place, but Kokoro did not load or speak:\n"
                           + "\n".join(output.strip().splitlines()[-12:]) + hint)
    if result["problem"]:
        raise InstallError(f"Kokoro loaded but the check clip was not speech: {result['problem']}.")
    return {**result, "wav": wav}


def write_receipt(root, manifest, packages, files, check):
    receipt = {"schema": 1, "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "python": platform.python_version(), "torch": installed_versions().get("torch"),
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
    manifest = load_manifest(root / MANIFEST)
    packages = read_lock(root / LOCK)
    files = checkpoint_files(manifest, choose_voices(manifest, args.voices))
    missing, different = package_state(packages, installed_versions())
    pending = pending_files(root, manifest, files)
    todo = 0 if args.checkpoint_only else len(missing) + len(different)
    packages_need = (PACKAGES_DOWNLOAD_MB + PACKAGES_DISK_MB) * 10**6 if todo else 0
    need = packages_need + sum(item["size"] for item in pending) + SPACE_MARGIN_MB * 10**6
    problems = interpreter_problems(root)
    if todo:
        problems += package_problems(different) + long_path_problems(root)
    problems += space_problems(root, need)
    say(plan_text(manifest, packages, todo, files, pending, checkpoint_only=args.checkpoint_only))
    if problems:
        raise InstallError("This computer cannot take the plan yet:\n" + "\n".join(
            textwrap.fill(problem, width=110, initial_indent="  - ", subsequent_indent="    ") for problem in problems))
    if args.dry_run:
        say("Checks passed. This was a dry run, so nothing was changed.")
        return 0
    if not confirmed(args, ask=ask, interactive=interactive):
        say("Nothing was changed.")
        return 1
    started = time.monotonic()
    if todo:
        say("Installing the Python packages (pip prints its own progress):")
        install_packages(root)
        still, _ = package_state(packages, installed_versions())
        if still:
            raise InstallError(f"pip finished but {len(still)} packages are still missing, for example {still[0]['name']}.")
    say("Fetching the Kokoro model files:")
    download_checkpoint(root, manifest, files, say=say)
    say("Checking that Kokoro speaks (the first load is slow):")
    check = run_speech_check(root)
    receipt = write_receipt(root, manifest, packages, files, check)
    say("")
    say(f"Kokoro is installed and spoke a test sentence ({check['audio_seconds']:.1f} s of audio in {check['total_seconds']:.1f} s "
        f"after a {check['load_seconds']:.0f} s load). The whole install took {time.monotonic() - started:.0f} s.")
    say(f"  Listen to it: {Path(check['wav']).relative_to(root)}")
    say(f"  Receipt: {receipt.relative_to(root)}")
    say("  Next: start the studio (the Fel's Voice Lab shortcut, or serve.bat), open Models & features and press Check it speaks.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="accept the plan without asking")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run the checks; change nothing")
    parser.add_argument("--voices", nargs="+", default=["supported"], metavar="VOICE",
                        help="supported (the default: every voice that works with these packages), english, all, or voice IDs "
                             "such as bm_george ef_dora")
    parser.add_argument("--checkpoint-only", action="store_true",
                        help="fetch the model files only and leave the Python packages alone")
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
