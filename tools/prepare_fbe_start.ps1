param(
    [string]$BuildFile = "",
    [int]$Port = 8000,
    [switch]$ForceRestart
)

$ErrorActionPreference = "Stop"
if (-not $BuildFile) {
    $BuildFile = Join-Path (Split-Path -Parent $PSScriptRoot) "BUILD.json"
}
if (-not (Test-Path $BuildFile)) {
    Write-Host "[FBE] BUILD.json not found: $BuildFile" -ForegroundColor Red
    exit 44
}
$expected = Get-Content -Raw -Encoding UTF8 $BuildFile | ConvertFrom-Json
$ExpectedVersion = [string]$expected.version
$ExpectedBuildId = [string]$expected.build_id
if (-not $ExpectedVersion -or -not $ExpectedBuildId) {
    Write-Host "[FBE] BUILD.json is invalid." -ForegroundColor Red
    exit 44
}

function Get-FbeListener {
    try {
        return Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
    } catch {
        return $null
    }
}

$listener = Get-FbeListener
if (-not $listener) {
    exit 0
}

$listenerPid = [int]$listener.OwningProcess
$detectedVersion = ""
$detectedBuildId = ""
$isFbe = $false
try {
    $info = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/fbe-info" -f $Port) -TimeoutSec 2
    if ([string]$info.app -eq "FBE") {
        $detectedVersion = [string]$info.version
        $detectedBuildId = [string]$info.build_id
        $isFbe = $true
    }
} catch {
    # Older FBE builds did not expose build_id. Fall back to the HTML version marker.
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ("http://127.0.0.1:{0}/" -f $Port) -TimeoutSec 2
        if ($response.Content -match 'FBE\s+([0-9]+\.[0-9]+\.[0-9]+)') {
            $detectedVersion = $Matches[1]
            $detectedBuildId = "legacy-no-build-id"
            $isFbe = $true
        }
    } catch {}
}

if (-not $isFbe) {
    Write-Host ("[FBE] Port {0} is already occupied by PID {1}, but it is not identifiable as FBE." -f $Port, $listenerPid) -ForegroundColor Red
    Write-Host "[FBE] Nothing was terminated automatically. Close the program using this port and start FBE again." -ForegroundColor Yellow
    exit 42
}

if ($detectedVersion -eq $ExpectedVersion -and $detectedBuildId -eq $ExpectedBuildId -and -not $ForceRestart) {
    Write-Host ("[FBE] Exact build already running: {0} / {1}" -f $ExpectedVersion, $ExpectedBuildId) -ForegroundColor Green
    Start-Process ("http://127.0.0.1:{0}/?_fbe_build={1}" -f $Port, [uri]::EscapeDataString($ExpectedBuildId))
    exit 10
}

$proc = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $listenerPid) -ErrorAction SilentlyContinue
$commandLine = if ($proc) { [string]$proc.CommandLine } else { "" }
if ($commandLine -notmatch '(?i)uvicorn' -or $commandLine -notmatch '(?i)app:app') {
    Write-Host ("[FBE] Another FBE build is answering on port {0}, but PID {1} cannot be safely identified as our local uvicorn process." -f $Port, $listenerPid) -ForegroundColor Red
    Write-Host ("[FBE] Detected: version={0}, build={1}" -f $detectedVersion, $detectedBuildId) -ForegroundColor Yellow
    Write-Host "[FBE] Close the old FBE console manually and run this shortcut again." -ForegroundColor Yellow
    exit 42
}

Write-Host ("[FBE] Replacing running build {0}/{1} with {2}/{3}..." -f $detectedVersion, $detectedBuildId, $ExpectedVersion, $ExpectedBuildId) -ForegroundColor Yellow
Stop-Process -Id $listenerPid -Force -ErrorAction Stop

for ($i = 0; $i -lt 50; $i++) {
    Start-Sleep -Milliseconds 100
    if (-not (Get-FbeListener)) {
        exit 0
    }
}

Write-Host ("[FBE] Previous server did not release port {0}." -f $Port) -ForegroundColor Red
exit 43
