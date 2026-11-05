@echo off
rem Installs Fish Audio Drama 3, a 35B model under the Fish Audio Community License that designs and clones voices on the CPU with 26 GB of free RAM, into out\drama3 (a 20.8 GB download). Pass --dry-run to see the plan, and its licence, without changing anything.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\install_drama3.py %*
pause
