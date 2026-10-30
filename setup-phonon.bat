@echo off
rem Installs Phonon 31M English, a very small speech model with 37 preset voices that runs on the CPU. Pass --dry-run to see the plan without changing anything.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\install_phonon.py %*
pause
