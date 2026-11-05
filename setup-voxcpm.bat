@echo off
rem Installs VoxCPM2, a model that designs or clones a voice and runs on an NVIDIA graphics card with 7 GB or more, into its own .venv-voxcpm. Pass --dry-run to see the plan without changing anything.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\install_voxcpm.py %*
pause
