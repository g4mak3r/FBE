param(
    [switch]$Silent
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "FBE.lnk"
$startScript = Join-Path $projectRoot "start_fbe.bat"
$iconPath = Join-Path $projectRoot "FBE.ico"
$buildPath = Join-Path $projectRoot "BUILD.json"
$buildVersion = "unknown"
$buildId = "unknown"
if (Test-Path $buildPath) {
    try {
        $build = Get-Content -Raw -Encoding UTF8 $buildPath | ConvertFrom-Json
        $buildVersion = [string]$build.version
        $buildId = [string]$build.build_id
    } catch {}
}

if (-not (Test-Path $startScript)) {
    throw "Start script not found: $startScript"
}
if (-not (Test-Path $iconPath)) {
    throw "FBE icon not found: $iconPath"
}

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $env:ComSpec
$shortcut.Arguments = '/c ""' + $startScript + '""'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.IconLocation = $iconPath + ",0"
$shortcut.Description = ("FBE {0} / {1}" -f $buildVersion, $buildId)
$shortcut.WindowStyle = 1
$shortcut.Save()

if (-not $Silent) {
    Write-Host "FBE desktop shortcut created:" -ForegroundColor Green
    Write-Host $shortcutPath
}
