@echo off
setlocal
cd /d "%~dp0"
py -3.12 tools\setup_core.py
pause
