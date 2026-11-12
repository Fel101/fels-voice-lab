"""Install only the desktop shell in its own environment, then create a shortcut.

setup-desktop.bat and first setup run it as it is. Setup.exe runs it with --wheels, from the packages it carries, and with
--no-shortcut, because the installer makes the Start menu and desktop shortcuts itself."""
import argparse
import pathlib
import subprocess
import sys
import venv

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main(argv=()):
    parser = argparse.ArgumentParser(description="Set up the desktop window's environment, .venv-desktop.")
    parser.add_argument("--wheels", type=pathlib.Path, help="install from the wheels in this folder, without the internet")
    parser.add_argument("--no-shortcut", action="store_true", help="make no desktop shortcut")
    args = parser.parse_args(argv)
    python = ROOT / ".venv-desktop/Scripts/python.exe"
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv-desktop")
    offline = ["--no-index", "--find-links", str(args.wheels)] if args.wheels else []
    subprocess.run([str(python), "-m", "pip", "install", *offline, "-r", str(ROOT / "requirements/desktop.txt")], check=True)
    if args.no_shortcut:
        print("Desktop window ready.")
        return
    subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "tools/create-shortcut.ps1")], check=True)
    print("Fel's Voice Lab desktop shortcut created. Closing its window stops its own backend.")


if __name__ == "__main__":
    main(sys.argv[1:])
