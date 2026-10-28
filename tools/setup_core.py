"""Create a minimal broker environment without altering an existing model environment."""
import pathlib
import subprocess
import sys
import venv

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Use Python 3.12: py -3.12 tools/setup_core.py")
    python = ROOT / ".venv/Scripts/python.exe"
    if python.is_file():
        subprocess.run([str(python), "-c", "import numpy, scipy, soundfile, pyloudnorm; import serve"],
                       cwd=ROOT, check=True)
        print("Existing .venv verified and preserved. No packages changed.")
    else:
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
        subprocess.run([str(python), "-m", "pip", "install", "pip==26.2.2"], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements/core.txt")], check=True)
    print("Core ready. Run setup-kokoro.bat to add Kokoro, the small speech model that runs on the CPU. "
          "Other models: docs/SETUP.md.")


if __name__ == "__main__":
    main()
