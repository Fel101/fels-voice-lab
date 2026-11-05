@echo off
rem Installs Breeze TTS 2, a model under a research and non-commercial licence that designs, clones and directs voices on an NVIDIA graphics card with 10 GB or more, into its own .venv-breeze. Pass --dry-run to see the plan, and its licence, without changing anything.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\install_breeze.py %*
pause
