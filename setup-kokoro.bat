@echo off
rem Installs Kokoro, the small speech model that runs on the CPU. Pass --dry-run to see the plan without changing anything.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\install_kokoro.py %*
pause
