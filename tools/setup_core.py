"""Create a minimal broker environment without altering an existing model environment."""
import os
import pathlib
import subprocess
import sys
import venv

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Left inside .venv while setup.bat is still installing the core packages, so the next run finishes a run that was interrupted.
# A .venv without it is an environment that works, or one someone else made: setup.bat checks it and never changes it.
UNFINISHED = ".voice-lab-setup-unfinished"


def onedrive_folder(root, environ=os.environ):
    """The OneDrive folder that holds `root`, or None. OneDrive would upload the gigabytes of packages and models the Lab
    installs, and can hold the Lab's databases open while it syncs them."""
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        folder = environ.get(name)
        if folder and pathlib.Path(root).resolve().is_relative_to(pathlib.Path(folder).resolve()):
            return folder
    return None


def install(command):
    if subprocess.run(command, cwd=ROOT).returncode:
        raise SystemExit("pip stopped before the core packages were installed; its message is above. Check the internet "
                         "connection, then run setup.bat again: it continues where it stopped.")


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Use Python 3.12: py -3.12 tools/setup_core.py")
    folder = onedrive_folder(ROOT)
    if folder:
        print(f"This folder is inside OneDrive ({folder}). OneDrive would upload the gigabytes of packages and models the Lab "
              "installs here, and can lock its databases while it syncs them. Move the folder somewhere OneDrive does not "
              "sync, such as C:\\voice-lab, and run setup.bat there.")
        if sys.stdin.isatty():
            input("Press Enter to set up here anyway, or close this window to stop. ")
    environment = ROOT / ".venv"
    python = environment / "Scripts/python.exe"
    unfinished = environment / UNFINISHED
    if python.is_file() and not unfinished.is_file():
        if subprocess.run([str(python), "-c", "import numpy, scipy, soundfile, pyloudnorm; import serve"], cwd=ROOT).returncode:
            raise SystemExit("The .venv folder cannot load the Lab's core packages (the error is above), and setup.bat never "
                             "changes an environment that is already there. If an earlier setup was interrupted and nothing "
                             "else has been installed into it, delete the .venv folder and run setup.bat again.")
        print("Existing .venv verified and preserved. No packages changed.")
    else:
        environment.mkdir(exist_ok=True)
        unfinished.write_text("setup.bat has not finished installing the core packages here. Run it again to finish.\n",
                              encoding="utf-8")
        venv.EnvBuilder(with_pip=True).create(environment)
        install([str(python), "-m", "pip", "install", "pip==26.2.2"])
        install([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements/core.txt")])
        unfinished.unlink()
    print("Core ready. Run setup-kokoro.bat to add Kokoro, the small speech model that runs on the CPU, setup-phonon.bat "
          "to add Phonon, a smaller English one with 37 preset voices, or setup-chatterbox.bat to add Chatterbox Turbo, which "
          "clones a voice from a recording. Other models: docs/SETUP.md.")


if __name__ == "__main__":
    main()
