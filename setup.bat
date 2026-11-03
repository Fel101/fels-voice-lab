@echo off
setlocal
cd /d "%~dp0"
py -3.12 -c "import sys; print('Python', sys.version.split()[0])" || goto no_python
py -3.12 tools\setup_core.py
set result=%errorlevel%
pause
exit /b %result%

:no_python
echo.
echo Fel's Voice Lab needs Python 3.12 (64-bit), and "py -3.12" could not start it. The newer Python that
echo python.org offers first does not work with the Lab yet. Install Python 3.12.10, the last 3.12 release
echo with a Windows installer, then run setup.bat again:
echo   https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
echo If your py command offers "py install", running "py install 3.12" may also work.
pause
exit /b 1
