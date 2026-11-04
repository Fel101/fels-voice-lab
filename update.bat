@echo off
rem Updates the Lab to its newest release on GitHub. It shows what will change and asks first, and keeps your voices, scenes,
rem models and settings. --check only says whether there is a newer release, --dry-run shows the plan, and --undo puts back
rem the version the last update replaced. Close the Lab before you run it.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
rem An update can replace this file while it runs, and cmd.exe reads a batch file as it goes, so the rest is one block that
rem cmd.exe has read whole before Python starts. call reads the exit code when the line runs, not when the block was read.
(
  ".venv\Scripts\python.exe" tools\update.py %*
  call set result=%%errorlevel%%
  pause
  call exit /b %%result%%
)
