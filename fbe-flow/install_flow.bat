@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
    python -m venv .venv
) else (
    py -3 -m venv .venv
)
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -c requirements-lock.txt .
if errorlevel 1 goto fail
echo Installed. Run start_flow.bat and open http://127.0.0.1:8765
pause
exit /b 0
:fail
echo Installation failed. Python 3.11 or newer is required.
pause
exit /b 1
