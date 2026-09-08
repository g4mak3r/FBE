@echo off
setlocal
cd /d "%~dp0"

for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "$b=Get-Content -Raw -Encoding UTF8 '%~dp0BUILD.json'|ConvertFrom-Json; $b.version"`) do set "FBE_VERSION=%%V"
for /f "usebackq delims=" %%B in (`powershell -NoProfile -Command "$b=Get-Content -Raw -Encoding UTF8 '%~dp0BUILD.json'|ConvertFrom-Json; $b.build_id"`) do set "FBE_BUILD=%%B"
if not defined FBE_VERSION set "FBE_VERSION=unknown"
if not defined FBE_BUILD set "FBE_BUILD=unknown"
title FBE %FBE_VERSION%

where java >nul 2>nul
if errorlevel 1 (
  echo [FBE] WARNING: Java not found in PATH.
  echo [FBE] Native DataMatrix printing needs Java 8+ or java_exe in config.json.
  echo.
)

if not exist ".venv\Scripts\python.exe" (
  echo [FBE] Virtual environment not found.
  echo Run install_fbe.bat first.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\prepare_fbe_start.ps1" -BuildFile "%~dp0BUILD.json" -Port 8000
set "PREP_RC=%ERRORLEVEL%"
if "%PREP_RC%"=="10" exit /b 0
if not "%PREP_RC%"=="0" (
  echo.
  echo [FBE] Startup cancelled to avoid opening the wrong build.
  pause
  exit /b %PREP_RC%
)

echo [FBE] Starting FBE %FBE_VERSION%
echo [FBE] Build: %FBE_BUILD%
echo [FBE] http://127.0.0.1:8000
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\open_fbe_when_ready.ps1" -BuildFile "%~dp0BUILD.json" -Port 8000

:run_fbe
.venv\Scripts\python.exe tools\check_workspace.py
if errorlevel 1 (
  echo [FBE] Active workspace integrity check failed. Original data preserved.
  pause
  exit /b 45
)
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
set "FBE_EXIT=%ERRORLEVEL%"
if "%FBE_EXIT%"=="75" (
  echo [FBE] Applying local profile/mode change...
  goto run_fbe
)

echo.
echo [FBE] Server stopped.
pause
