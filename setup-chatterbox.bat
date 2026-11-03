@echo off
rem Installs Chatterbox Turbo, a model that clones a voice from a recording and runs on the CPU, into its own .venv-chatterbox. Pass --dry-run to see the plan without changing anything.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\install_chatterbox.py %*
pause
