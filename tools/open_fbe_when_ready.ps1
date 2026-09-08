param(
    [string]$BuildFile = "",
    [int]$Port = 8000
)

$ErrorActionPreference = "SilentlyContinue"
if (-not $BuildFile) {
    $BuildFile = Join-Path (Split-Path -Parent $PSScriptRoot) "BUILD.json"
}
$expected = Get-Content -Raw -Encoding UTF8 $BuildFile | ConvertFrom-Json
$ExpectedVersion = [string]$expected.version
$ExpectedBuildId = [string]$expected.build_id
$uri = "http://127.0.0.1:$Port/api/fbe-info"
for ($i = 0; $i -lt 150; $i++) {
    Start-Sleep -Milliseconds 100
    try {
        $info = Invoke-RestMethod -Uri $uri -TimeoutSec 1
        if ($info.app -eq "FBE" -and [string]$info.version -eq $ExpectedVersion -and [string]$info.build_id -eq $ExpectedBuildId) {
            Start-Process ("http://127.0.0.1:{0}/?_fbe_build={1}" -f $Port, [uri]::EscapeDataString($ExpectedBuildId))
            exit 0
        }
    } catch {}
}
Write-Host ("[FBE] Exact build did not become ready: {0} / {1}" -f $ExpectedVersion, $ExpectedBuildId) -ForegroundColor Red
exit 1
