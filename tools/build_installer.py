"""Build Setup.exe (FelsVoiceLab-Setup-<version>.exe) from a commit, and its SHA256SUMS.

    .venv\\Scripts\\python.exe tools\\build_installer.py                  the commit checked out now
    .venv\\Scripts\\python.exe tools\\build_installer.py --ref v0.3.0     a release's tag (the release workflow does this)

It stages three things in out\\installer\\staging and hands them to Inno Setup (installer\\FelsVoiceLab.iss):

- app: the commit's files from `git archive`, which is how GitHub packs the release's source ZIP. The installed folder holds
  exactly the release's files, so update.bat treats it like an unpacked ZIP.
- python: the Lab's own Python, fetched and checked by tools\\get_python.ps1 from catalog\\python_runtime.json, as setup.bat
  fetches it on a computer with none.
- wheels: the packages of requirements\\core.txt and requirements\\desktop.txt and the pip setup_core moves to, built for
  this Python (64-bit CPython 3.12 on Windows) by `pip wheel`. Setup installs them with no internet, and deletes them.

It needs git, Windows PowerShell, Inno Setup 6.7 or later (ISCC.exe: --iscc, the ISCC environment variable, the PATH, or
where Inno Setup installs) and this Python to be 64-bit 3.12. The internet is needed for Python and the wheels.
"""

import argparse
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_version import REPOSITORY  # noqa: E402
from tools import setup_core  # noqa: E402

SCRIPT = ROOT / "installer" / "FelsVoiceLab.iss"
STAGING = ROOT / "out" / "installer" / "staging"
OUTPUT = ROOT / "out" / "installer"
REQUIREMENTS = ("requirements/core.txt", "requirements/desktop.txt")
# What setup leaves in .venv and .venv-desktop beyond the files Setup.exe copies, so the folder page asks for enough space:
# measured from an installed copy, rounded up.
ENVIRONMENT_BYTES = 300 * 1024 * 1024  # 242 MB on 2026-11-12: .venv 222, .venv-desktop 20
ISCC_PLACES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
)
VERSION_LINE = re.compile(r'^VERSION = "(?P<version>[^"]+)"$', re.MULTILINE)


class BuildError(Exception):
    """The build stopped. The message says why and what to do."""


def say(text=""):
    print(text, flush=True)


def git(*arguments, capture=True):
    done = subprocess.run(["git", "-C", str(ROOT), *arguments], capture_output=capture)
    if done.returncode:
        message = done.stderr.decode("utf-8", "replace").strip() if capture else ""
        raise BuildError(f"git {' '.join(arguments)} failed: {message}")
    return done.stdout


def version_at(ref):
    """VERSION in app_version.py at a commit."""
    text = git("show", f"{ref}:app_version.py").decode("utf-8")
    found = VERSION_LINE.search(text)
    if not found:
        raise BuildError(f"app_version.py at {ref} has no VERSION line.")
    return found["version"]


def stage_app(ref, target):
    """The commit's files, as `git archive` gives them (and GitHub's source ZIP holds)."""
    data = git("archive", "--format=zip", ref)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for item in archive.infolist():
            parts = Path(item.filename).parts
            if item.is_dir() or not parts or ".." in parts or Path(item.filename).is_absolute():
                continue
            destination = target.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(item))
    return sum(1 for path in target.rglob("*") if path.is_file())


def stage_python(target):
    done = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                           str(ROOT / "tools" / "get_python.ps1"), "-Destination", str(target)])  # fmt: skip
    if done.returncode or not (target / "python.exe").is_file():
        raise BuildError("tools\\get_python.ps1 did not fetch the Lab's own Python; its message is above.")


def pinned(requirements):
    """The `name==version` lines of the requirement files, plus the pip setup_core moves to, without repeats."""
    lines = []
    for name in requirements:
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and line not in lines:
                lines.append(line)
    if setup_core.PIP not in lines:
        lines.append(setup_core.PIP)
    return lines


def wheel_name(requirement):
    """The start of a wheel's file name for `name==version`: the name with - and . as _, then the version."""
    name, version = requirement.split("==")
    return f"{re.sub(r'[-_.]+', '_', name).lower()}-{version}-"


def stage_wheels(target, requirements=REQUIREMENTS, *, python=sys.executable, runner=subprocess.run):
    wanted = pinned(requirements)
    target.mkdir(parents=True, exist_ok=True)
    done = runner([str(python), "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(target), *wanted])
    if done.returncode:
        raise BuildError("pip could not make the wheels; its message is above.")
    names = [path.name.lower() for path in target.glob("*.whl")]
    missing = [item for item in wanted if not any(name.startswith(wheel_name(item)) for name in names)]
    if missing:
        raise BuildError(f"No wheel was made for {', '.join(missing)}.")
    return wanted


def find_iscc(given=None):
    for candidate in (given, os.environ.get("ISCC"), shutil.which("iscc"), *ISCC_PLACES):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise BuildError("Inno Setup was not found. Install Inno Setup 6.7 or later (winget install JRSoftware.InnoSetup), or "
                     "give its ISCC.exe with --iscc.")  # fmt: skip


def iscc_command(iscc, *, version, staging, output):
    return [str(iscc), "/Qp", f"/DAppVersion={version}", f"/DRepository={REPOSITORY}", f"/DStaging={staging}",
            f"/DOutput={output}", f"/DExtraBytes={ENVIRONMENT_BYTES}", str(SCRIPT)]  # fmt: skip


def checksums(files):
    lines = []
    for path in files:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        lines.append(f"{digest.hexdigest()}  {path.name}\n")
    return "".join(lines)


def build(ref, *, output, iscc=None, keep_staging=False):
    if sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        raise BuildError("Run this with 64-bit Python 3.12 (the Lab's .venv), so the wheels fit the Python Setup.exe carries.")
    iscc = find_iscc(iscc)
    version = version_at(ref)
    say(f"Building Setup.exe for Fel's Voice Lab {version} from {ref}, with {iscc}.")
    if STAGING.exists():
        shutil.rmtree(STAGING)
    say("The release's files (git archive) ...")
    count = stage_app(ref, STAGING / "app")
    say(f"  {count} files.")
    say("The Lab's own Python ...")
    stage_python(STAGING / "python")
    say("The wheels for the core and the desktop window ...")
    wheels = stage_wheels(STAGING / "wheels")
    say(f"  {len(wheels)} wheels.")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    installer = output / f"FelsVoiceLab-Setup-{version}.exe"
    if installer.exists():
        installer.unlink()
    say("Inno Setup ...")
    if (
        subprocess.run(iscc_command(iscc, version=version, staging=STAGING, output=output)).returncode
        or not installer.is_file()
    ):
        raise BuildError("Inno Setup did not build Setup.exe; its message is above.")
    (output / "SHA256SUMS").write_text(checksums([installer]), encoding="utf-8", newline="\n")
    if not keep_staging:
        shutil.rmtree(STAGING, ignore_errors=True)
    say(f"Built {installer} ({installer.stat().st_size / 1e6:.0f} MB) and {output / 'SHA256SUMS'}.")
    return installer


def build_parser():
    parser = argparse.ArgumentParser(description="Build Setup.exe for the Lab from a commit.")
    parser.add_argument("--ref", default="HEAD", help="the commit or tag to build from (default: HEAD)")
    parser.add_argument(
        "--output", type=Path, default=OUTPUT, help="where Setup.exe and SHA256SUMS go (default: out\\installer)"
    )
    parser.add_argument("--iscc", type=Path, help="Inno Setup's ISCC.exe, when it is not found")
    parser.add_argument("--keep-staging", action="store_true", help="leave out\\installer\\staging in place")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        build(args.ref, output=args.output, iscc=args.iscc, keep_staging=args.keep_staging)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
