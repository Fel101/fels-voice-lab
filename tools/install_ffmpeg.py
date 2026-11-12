"""Install FFmpeg into the Lab's own ffmpeg folder: the build catalog/ffmpeg_build.json names, checked against its SHA-256.

    .venv\\Scripts\\python.exe tools\\install_ffmpeg.py
    .venv\\Scripts\\python.exe tools\\install_ffmpeg.py --dry-run
    .venv\\Scripts\\python.exe tools\\install_ffmpeg.py --yes

The Lab writes MP3, AAC and Ogg with FFmpeg, checks export packages with its ffprobe, and reads MP3 and M4A recordings with
it. **Install FFmpeg** on System & backups and on the export panel runs this script, and so can first setup. The ZIP (Gyan
Doshi's essentials build, from the GitHub release that mirrors www.gyan.dev's versioned builds) downloads into out\\downloads,
resumes after an interruption, and is kept only when its SHA-256 matches. Only ffmpeg.exe, ffprobe.exe and the build's licence
and readme are unpacked, into ffmpeg.partial\\, which becomes ffmpeg\\ once its FFmpeg has started and listed the encoders the
Lab needs. ffmpeg_tools looks there first, then on PATH, then where winget puts FFmpeg, so an FFmpeg installed some other way
keeps working when this one is not there.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

try:
    from tools import install_kokoro as base
except ImportError:  # run as a script, which puts tools\ first on the path and not the lab folder
    import install_kokoro as base

InstallError = base.InstallError
ROOT = Path(__file__).resolve().parents[1]
CATALOG = "catalog/ffmpeg_build.json"
FOLDER = "ffmpeg"
DOWNLOADS = "out/downloads"
# What the export panel's formats need: MP3, AAC (M4A), Ogg Vorbis and FLAC.
NEEDED_ENCODERS = ("libmp3lame", "aac", "libvorbis", "flac")
SPACE_MARGIN = 100 * 10**6
CHECK_TIMEOUT = 60
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def load_build(root=ROOT):
    return json.loads((Path(root) / CATALOG).read_text(encoding="utf-8"))


def disk_bytes(build):
    return sum(item["size"] for item in build["files"])


def plan_text(build):
    """What the installer is about to do, written for the person who ran it."""
    host = build["url"].split("/")[2]
    text = (
        f"FFmpeg {build['version']}, {build['builder']} (licence {build['license']}), from {host}: "
        f"{base.size_text(build['bytes'])} to download and {base.size_text(disk_bytes(build))} on disk in the Lab's "
        f"{FOLDER} folder. Only ffmpeg.exe, ffprobe.exe and the build's licence and readme are kept. The download is checked "
        "against its SHA-256. The Lab then writes MP3, AAC and Ogg with this FFmpeg, and reads MP3 and M4A recordings with "
        "it, in place of any other FFmpeg on this computer."
    )
    return textwrap.fill(text, width=110, break_on_hyphens=False) + "\n"


def check_build(bin_folder, *, runner=subprocess.run):
    """Why the FFmpeg in bin_folder will not do, or None when it starts and lists every encoder the Lab needs."""
    ffmpeg, ffprobe = Path(bin_folder) / "ffmpeg.exe", Path(bin_folder) / "ffprobe.exe"
    try:
        listed = runner([str(ffmpeg), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=CHECK_TIMEOUT,
                        creationflags=NO_WINDOW)  # fmt: skip
        probe = runner([str(ffprobe), "-hide_banner", "-version"], capture_output=True, text=True, timeout=CHECK_TIMEOUT,
                       creationflags=NO_WINDOW)  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        return f"FFmpeg could not be started ({exc})."
    if listed.returncode or probe.returncode:
        return "FFmpeg or ffprobe started but stopped with an error."
    missing = [name for name in NEEDED_ENCODERS if f" {name} " not in listed.stdout]
    if missing:
        return f"This FFmpeg cannot write with {', '.join(missing)}."
    return None


def unpack(package, build, target):
    """Unpack the listed files of the build into target, under the same relative paths."""
    with zipfile.ZipFile(package) as archive:
        for item in build["files"]:
            name = f"{build['folder']}/{item['path']}"
            try:
                info = archive.getinfo(name)
            except KeyError:
                raise InstallError(f"The FFmpeg download holds no {item['path']}.") from None
            destination = target / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as copy:
                shutil.copyfileobj(source, copy, 1024 * 1024)


def run(args, *, root=ROOT, say=base.say_now, ask=input, interactive=None, fetch=base.fetch, check=check_build):
    root = Path(root)
    build = load_build(root)
    folder, partial = root / FOLDER, root / f"{FOLDER}.partial"
    if (folder / "bin" / "ffmpeg.exe").is_file():
        problem = check(folder / "bin")
        if problem is None:
            say(f"FFmpeg is already in the Lab's {FOLDER} folder, and it starts and writes every format. Nothing to do.")
            if args.dry_run:
                say("Checks passed. This was a dry run, so nothing was changed.")
            return 0
        raise InstallError(f"The FFmpeg in the Lab's {FOLDER} folder does not work: {problem} Delete that folder, then install "
                           "FFmpeg again.")  # fmt: skip
    if folder.exists():
        raise InstallError(f"There is a {FOLDER} folder here without ffmpeg.exe in it, and the installer does not change a "
                           "folder it did not make. Move or delete it, then install FFmpeg again.")  # fmt: skip
    package = root / DOWNLOADS / build["url"].rsplit("/", 1)[-1]
    have = package.stat().st_size if package.is_file() else 0
    problems = base.space_problems(root, build["bytes"] - have + disk_bytes(build) + SPACE_MARGIN)
    say(plan_text(build))
    if problems:
        raise InstallError("This computer cannot take the plan yet:\n" + "\n".join("  - " + problem for problem in problems))
    if args.dry_run:
        say("Checks passed. This was a dry run, so nothing was changed.")
        return 0
    if not base.confirmed(args, ask=ask, interactive=interactive):
        say("Nothing was changed.")
        return 1
    say(f"Downloading FFmpeg {build['version']} ({base.size_text(build['bytes'])}):")
    try:
        fetch(build["url"], package, build["bytes"], build["sha256"], say=say)
    except InstallError as exc:
        # The downloader says to run the installer again, which on the page is the button.
        stopped = str(exc).replace(" Run the installer again to resume it.", "")
        raise InstallError(
            f"{stopped} If this computer is offline, connect it first. What was downloaded is kept, and the next try "
            "continues from there."
        ) from None
    say("Unpacking ffmpeg.exe and ffprobe.exe:")
    if partial.exists():
        shutil.rmtree(partial)
    try:
        unpack(package, build, partial)
        problem = check(partial / "bin")
        if problem:
            raise InstallError(f"The downloaded FFmpeg does not work here: {problem}")
        os.replace(partial, folder)
    finally:
        if partial.exists():
            shutil.rmtree(partial, ignore_errors=True)
    package.unlink()
    say("")
    say(f"FFmpeg {build['version']} is installed in the Lab's {FOLDER} folder. Export can write every format now.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="accept the plan without asking")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and run the checks; change nothing")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (InstallError, OSError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Install FFmpeg again to continue; what was downloaded is kept.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
