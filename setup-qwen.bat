@echo off
rem Installs one of the four Qwen3-TTS models for an NVIDIA graphics card into .venv-fqwen, which they share. Name the model: setup-qwen.bat --model 0.6b-base (or 1.7b-base, 1.7b-voice-design, 1.7b-custom-voice). Add --dry-run to see the plan without changing anything.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\install_qwen.py %*
pause
