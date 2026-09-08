@echo off
setlocal
cd /d "%~dp0"

set "PS_ARGS="
if /i "%~1"=="/silent" set "PS_ARGS=-Silent"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_desktop_shortcut.ps1" %PS_ARGS%
if errorlevel 1 (
  echo.
  echo [FBE] Failed to create the desktop shortcut.
  if /i not "%~1"=="/silent" pause
  exit /b 1
)

if /i not "%~1"=="/silent" pause
exit /b 0
