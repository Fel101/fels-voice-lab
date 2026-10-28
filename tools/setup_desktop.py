"""Install only the desktop shell in its own environment, then create a shortcut."""
import pathlib
import subprocess
import venv

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    python = ROOT / ".venv-desktop/Scripts/python.exe"
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv-desktop")
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements/desktop.txt")], check=True)
    subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "tools/create-shortcut.ps1")], check=True)
    print("Fel's Voice Lab desktop shortcut created. Closing its window stops its own backend.")


if __name__ == "__main__":
    main()
