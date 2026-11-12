@echo off
rem First setup: the Lab's core, its desktop window, then the speech models you choose. Run it again to finish a setup that
rem was interrupted. Typed after it, --models recommended --yes installs Kokoro and Phonon without asking; --help lists the rest.
setlocal
cd /d "%~dp0"
rem Every environment the Lab makes is built from one Python: the Lab's own in python\ when it is there, else this computer's
rem Python 3.12, else the Lab's own, fetched now (tools\get_python.ps1). stdin is closed so that a py launcher that offers to
rem install a missing version cannot wait for an answer.
set "PYTHON="
if exist "python\python.exe" set "PYTHON=python\python.exe"
if not defined PYTHON (
  py -3.12 -c "import sys" <nul >nul 2>&1
  if not errorlevel 1 set "PYTHON=py -3.12"
)
if not defined PYTHON (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\get_python.ps1
  if errorlevel 1 goto no_python
  set "PYTHON=python\python.exe"
)
%PYTHON% -c "import sys; print('Python', sys.version.split()[0], 'at', sys.executable)" || goto no_python
%PYTHON% tools\setup_core.py
if errorlevel 1 goto done
".venv\Scripts\python.exe" tools\first_setup.py %*
:done
set result=%errorlevel%
pause
exit /b %result%

:no_python
echo.
echo Setup could not get Python 3.12 for the Lab; the reason is above. Check the internet connection and run setup.bat
echo again. Or install Python 3.12.10 yourself, the last 3.12 release with a Windows installer, and run setup.bat again:
echo   https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
pause
exit /b 1
