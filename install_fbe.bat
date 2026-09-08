@echo off
setlocal
cd /d "%~dp0"

for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "$b=Get-Content -Raw -Encoding UTF8 '%~dp0BUILD.json'|ConvertFrom-Json; $b.version"`) do set "FBE_VERSION=%%V"
for /f "usebackq delims=" %%B in (`powershell -NoProfile -Command "$b=Get-Content -Raw -Encoding UTF8 '%~dp0BUILD.json'|ConvertFrom-Json; $b.build_id"`) do set "FBE_BUILD=%%B"
if not defined FBE_VERSION set "FBE_VERSION=unknown"
if not defined FBE_BUILD set "FBE_BUILD=unknown"
title Install FBE %FBE_VERSION%

echo [FBE] Installing version %FBE_VERSION%
echo [FBE] Build %FBE_BUILD%
echo.

where java >nul 2>nul
if errorlevel 1 (
  echo [FBE] WARNING: Java not found in PATH.
  echo [FBE] Native DataMatrix printing needs Java 8+ or java_exe in config.json.
  echo.
)

set "FBE_PYTHON_EXE="
where py >nul 2>nul
if not errorlevel 1 (
  for /f "usebackq delims=" %%P in (`py -3.11 -c "import sys; print(sys.executable)" 2^>nul`) do set "FBE_PYTHON_EXE=%%P"
)
if not defined FBE_PYTHON_EXE (
  where python >nul 2>nul
  if not errorlevel 1 (
    for /f "usebackq delims=" %%P in (`python -c "import sys; print(sys.executable if sys.version_info[:2] == (3,11) else '')" 2^>nul`) do set "FBE_PYTHON_EXE=%%P"
  )
)
if not defined FBE_PYTHON_EXE (
  echo [FBE] Python 3.11 not found.
  echo Install Python 3.11 x64 from python.org and enable "Add Python to PATH".
  echo Then run install_fbe.bat again.
  pause
  exit /b 1
)
set "FBE_PYTHON_EXE=%FBE_PYTHON_EXE%"
echo [FBE] Python: %FBE_PYTHON_EXE%

REM Stop any currently running FBE before migrating the live SQLite database or replacing the venv.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\prepare_fbe_start.ps1" -BuildFile "%~dp0BUILD.json" -Port 8000 -ForceRestart
if errorlevel 1 (
  echo.
  echo [FBE] Installation stopped: the current FBE process could not be safely stopped.
  pause
  exit /b 1
)

REM Preserve only private BarTender templates from a previous install. Legacy DB/credentials are never auto-adopted.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\migrate_previous_install.ps1"
if errorlevel 1 goto error

if exist ".venv\Scripts\python.exe" (
  echo [FBE] Removing the old machine-specific virtual environment...
  rmdir /s /q ".venv"
  if exist ".venv" goto error
)

echo [FBE] Creating virtual environment...
"%FBE_PYTHON_EXE%" -m venv .venv
if errorlevel 1 goto error

echo [FBE] Installing requirements...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto error

echo [FBE] Creating desktop shortcut for this exact build...
call "%~dp0create_desktop_shortcut.bat" /silent
if errorlevel 1 (
  echo [FBE] WARNING: Desktop shortcut was not created.
  echo [FBE] You can run create_desktop_shortcut.bat later.
)

echo.
echo [FBE] Installation complete: %FBE_VERSION% / %FBE_BUILD%
echo [FBE] Start with the FBE desktop shortcut or start_fbe.bat
pause
exit /b 0

:error
echo.
echo [FBE] Installation failed.
echo See the error above. Project files and user data were not intentionally deleted.
pause
exit /b 1
