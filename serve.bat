@echo off
rem Foreground fallback. Use the Fel's Voice Lab desktop shortcut for a window without a console.
setlocal
cd /d "%~dp0"
echo Open the local URL printed by the server below. Close this console to stop browser/API mode.
rem Update now can replace this file while the service runs, and cmd.exe reads a batch file as it goes, so the rest is one
rem block that cmd.exe has read whole before Python starts.
(
  ".venv\Scripts\python.exe" serve.py --port 8765 %*
  pause
)
