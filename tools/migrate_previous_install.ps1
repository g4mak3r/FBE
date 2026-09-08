param([string]$CurrentRoot = "")

# FBE 0.85 deliberately does not auto-adopt legacy databases or credentials.
# The only supported automatic cross-install helper is local BarTender template
# staging. Templates remain outside public source and are claimed only after a
# verified WB seller is connected in the new installation.
$ErrorActionPreference = "Stop"

if (-not $CurrentRoot) {
    $CurrentRoot = Split-Path -Parent $PSScriptRoot
}
$CurrentRoot = ([string]$CurrentRoot).Trim().Trim('"').TrimEnd('\')
if (-not $CurrentRoot -or -not (Test-Path -LiteralPath $CurrentRoot)) {
    throw "Current FBE root is unavailable."
}
$CurrentRoot = (Resolve-Path -LiteralPath $CurrentRoot).Path

Write-Host "[FBE] Legacy database/credential migration remains disabled." -ForegroundColor DarkGray
Write-Host "[FBE] Looking only for private BarTender templates from the previous installation..." -ForegroundColor DarkGray

$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "FBE.lnk"
if (-not (Test-Path -LiteralPath $shortcutPath)) {
    Write-Host "[FBE] No previous FBE shortcut found. No local templates to stage." -ForegroundColor DarkGray
    exit 0
}

try {
    $wsh = New-Object -ComObject WScript.Shell
    $shortcut = $wsh.CreateShortcut($shortcutPath)
    $PreviousRoot = ([string]$shortcut.WorkingDirectory).Trim().Trim('"').TrimEnd('\')
} catch {
    Write-Host "[FBE] Could not inspect the previous FBE shortcut. Template staging skipped." -ForegroundColor Yellow
    exit 0
}

if (-not $PreviousRoot -or -not (Test-Path -LiteralPath $PreviousRoot)) {
    Write-Host "[FBE] Previous FBE folder is unavailable. Template staging skipped." -ForegroundColor DarkGray
    exit 0
}
$PreviousRoot = (Resolve-Path -LiteralPath $PreviousRoot).Path
if ($PreviousRoot.TrimEnd('\') -ieq $CurrentRoot.TrimEnd('\')) {
    Write-Host "[FBE] Reinstalling the same folder. Existing local templates are left untouched." -ForegroundColor DarkGray
    exit 0
}
if (-not (Test-Path -LiteralPath (Join-Path $PreviousRoot "app.py"))) {
    Write-Host "[FBE] Previous shortcut does not point to a recognizable FBE installation." -ForegroundColor Yellow
    exit 0
}

$templateNames = @("default.btw", "perfume.btw", "spa.btw", "set.btw", "samples.btw", "names.btw")
$sourceRoot = $null
$sourceKind = "legacy_root"
$expectedSid = ""

# A previous 0.85-style install may already have seller-scoped templates. If
# its local connection store identifies the active seller, prefer that exact
# workspace over the older global templates/ folder.
$previousConnection = Join-Path $PreviousRoot "data\local\connections.json"
if (Test-Path -LiteralPath $previousConnection) {
    try {
        $connection = Get-Content -Raw -Encoding UTF8 $previousConnection | ConvertFrom-Json
        $sid = [string]$connection.wb.profile.sid
        if ($sid) {
            $sha = [System.Security.Cryptography.SHA256]::Create()
            try {
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($sid.Trim())
                $hashBytes = $sha.ComputeHash($bytes)
                $hash = -join ($hashBytes | ForEach-Object { $_.ToString("x2") })
            } finally {
                $sha.Dispose()
            }
            $candidate = Join-Path $PreviousRoot ("data\workspaces\{0}\templates" -f $hash)
            if (Test-Path -LiteralPath $candidate) {
                $available = @($templateNames | Where-Object { Test-Path -LiteralPath (Join-Path $candidate $_) })
                if ($available.Count -gt 0) {
                    $sourceRoot = $candidate
                    $sourceKind = "seller_workspace"
                    $expectedSid = $sid.Trim()
                }
            }
        }
    } catch {
        # Never fail installation because an old local connection file is malformed.
        $sourceRoot = $null
        $expectedSid = ""
    }
}

if (-not $sourceRoot) {
    $candidate = Join-Path $PreviousRoot "templates"
    if (Test-Path -LiteralPath $candidate) {
        $available = @($templateNames | Where-Object { Test-Path -LiteralPath (Join-Path $candidate $_) })
        if ($available.Count -gt 0) {
            $sourceRoot = $candidate
            $sourceKind = "legacy_root"
        }
    }
}

if (-not $sourceRoot) {
    Write-Host "[FBE] No supported .btw templates found in the previous installation." -ForegroundColor DarkGray
    exit 0
}

$available = @($templateNames | Where-Object { Test-Path -LiteralPath (Join-Path $sourceRoot $_) })
Write-Host ("[FBE] Found {0} local BarTender template(s) in the previous installation." -f $available.Count) -ForegroundColor Cyan
Write-Host "[FBE] They are private runtime assets and will NOT become part of Git/source files." -ForegroundColor Cyan
$answer = Read-Host "[FBE] Preserve them for import into your verified WB profile? [Y/n]"
if ($answer -match '(?i)^n(?:o)?$') {
    Write-Host "[FBE] Template staging skipped. Previous installation was not changed." -ForegroundColor Yellow
    exit 0
}

$stageRoot = Join-Path $CurrentRoot "data\local\template_import"
$pendingRoot = Join-Path $stageRoot "pending"
New-Item -ItemType Directory -Force -Path $pendingRoot | Out-Null
$staged = @()
foreach ($name in $available) {
    $source = Join-Path $sourceRoot $name
    $target = Join-Path $pendingRoot $name
    if (Test-Path -LiteralPath $target) {
        Write-Host ("[FBE] Already staged; leaving untouched: {0}" -f $name) -ForegroundColor DarkGray
        $staged += $name
        continue
    }
    Copy-Item -LiteralPath $source -Destination $target
    $staged += $name
    Write-Host ("[FBE] Staged local template: {0}" -f $name) -ForegroundColor Green
}

$manifest = [ordered]@{
    version = 1
    source_kind = $sourceKind
    expected_sid = $expectedSid
    files = $staged
    staged_at = [DateTimeOffset]::UtcNow.ToString("o")
}
$manifestPath = Join-Path $stageRoot "manifest.json"
$json = $manifest | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText($manifestPath, $json + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "[FBE] Templates preserved locally." -ForegroundColor Green
if ($expectedSid) {
    Write-Host "[FBE] They are bound to the previously verified seller and can only be imported into the same SID." -ForegroundColor Green
} else {
    Write-Host "[FBE] After connecting WB, FBE will ask which verified seller profile should claim these legacy templates." -ForegroundColor Green
}
Write-Host "[FBE] No legacy database, token or credentials were copied." -ForegroundColor DarkGray
exit 0
