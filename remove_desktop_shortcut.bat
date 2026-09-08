@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path ([Environment]::GetFolderPath('Desktop')) 'FBE.lnk'; if(Test-Path $p){Remove-Item $p -Force; Write-Host 'FBE shortcut removed.' -ForegroundColor Green}else{Write-Host 'FBE shortcut was not found.'}"
pause
