"""Install the two pinned offline speech recognition checkpoints and their Python environment."""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asr_installation as installation  # noqa: E402
from tools import install_kokoro as base  # noqa: E402

IMPORTS = (
    "from importlib.metadata import version; import faster_whisper; "
    "assert version('faster-whisper') == '1.2.1'; "
    "assert version('ctranslate2') == '4.8.0'; assert version('av') == '17.1.0'"
)


def check_runtime(root):
    python = installation.runtime_python(root)
    if not python.is_file():
        return False
    try:
        result = subprocess.run([str(python), "-I", "-c", IMPORTS], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def run(args, *, root=ROOT, say=base.say_now, ask=input, fetch=base.fetch):
    models = installation.checkpoints(root)
    total = sum(item["size"] for spec in models.values() for item in spec["files"])
    say(
        "Install speech recognition for transcripts and speech-to-English translation. It downloads two "
        f"checked speech models ({base.size_text(total)} together) and a separate Python environment. "
        "Your audio stays on this computer."
    )
    required = 350 * 10**6
    for spec in models.values():
        folder = installation.model_directory(root, spec)
        required += sum(item["size"] for item in spec["files"] if not (folder / item["path"]).is_file())
    for problem in base.space_problems(root, required):
        raise base.InstallError(problem)
    if args.dry_run:
        say("Checks passed. This was a dry run, so nothing was changed.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty() or ask("Install speech recognition? [y/N] ").strip().casefold() != "y":
            say("Install canceled.")
            return 0

    python = installation.runtime_python(root)
    if not python.is_file():
        say("Preparing the speech recognition environment.")
        base.make_env(root, installation.ENVIRONMENT)
    if not check_runtime(root):
        say("Installing the speech recognition packages.")
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-input",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "-r",
                str(Path(root) / installation.REQUIREMENTS),
            ],
            cwd=root,
            check=True,
        )
        if not check_runtime(root):
            raise base.InstallError("Speech recognition could not start. Run Install again and show its output if it repeats.")

    for size, spec in models.items():
        say(f"Checking the {size.capitalize()} speech model.")
        folder = installation.model_directory(root, spec)
        for item in spec["files"]:
            url = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{item['path']}"
            fetch(url, folder / item["path"], item["size"], item["sha256"], say=say)
    say("Speech recognition is ready.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    try:
        return run(parser.parse_args())
    except (base.InstallError, OSError, subprocess.SubprocessError) as exc:
        print("Speech recognition could not be installed. " + str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
