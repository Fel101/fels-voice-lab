@echo off
rem Foreground fallback. Use the Fel's Voice Lab desktop shortcut for a window without a console.
setlocal
cd /d "%~dp0"
echo Open the local URL printed by the server below. Close this console to stop browser/API mode.
".venv\Scripts\python.exe" serve.py --port 8768 %*
pause
