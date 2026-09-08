param(
    [Parameter(Mandatory = $true)][string]$Thumbprint,
    [Parameter(Mandatory = $true)][string]$InputFile,
    [Parameter(Mandatory = $false)][switch]$Detached
)

$ErrorActionPreference = 'Stop'

$normalized = ($Thumbprint -replace '\s', '').ToUpperInvariant()
if (-not (Test-Path -LiteralPath $InputFile)) {
    throw "Input file not found: $InputFile"
}

[byte[]]$contentBytes = [System.IO.File]::ReadAllBytes($InputFile)
$contentBase64 = [Convert]::ToBase64String($contentBytes)

# First choice: CryptoPro CAdESCOM. It supports GOST keys on hardware tokens.
$cadesError = $null
$store = $null
try {
    $store = New-Object -ComObject CAdESCOM.Store
    $certificate = $null

    # CAPICOM_LOCAL_MACHINE_STORE = 1
    # CAPICOM_CURRENT_USER_STORE = 2
    # CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED = 2
    foreach ($location in @(2, 1)) {
        try {
            $store.Open($location, 'My', 2)
            $certificates = $store.Certificates
            for ($index = 1; $index -le $certificates.Count; $index++) {
                $candidate = $certificates.Item($index)
                $candidateThumbprint = (($candidate.Thumbprint) -replace '\s', '').ToUpperInvariant()
                if ($candidateThumbprint -eq $normalized) {
                    $certificate = $candidate
                    break
                }
            }
            if ($certificate) {
                break
            }
            $store.Close()
        }
        catch {
            try { $store.Close() } catch {}
        }
    }

    if (-not $certificate) {
        throw "Certificate not found in CryptoPro CAdESCOM store: $normalized"
    }

    $signer = New-Object -ComObject CAdESCOM.CPSigner
    $signer.Certificate = $certificate
    $signer.CheckCertificate = $false

    $signedData = New-Object -ComObject CAdESCOM.CadesSignedData
    # CADESCOM_BASE64_TO_BINARY = 1
    $signedData.ContentEncoding = 1
    $signedData.Content = $contentBase64

    # CADESCOM_CADES_BES = 1
    # CAPICOM_ENCODE_BASE64 = 0
    $signature = $signedData.SignCades($signer, 1, [bool]$Detached, 0)
    $signature = ($signature -replace '\s', '')

    if ([string]::IsNullOrWhiteSpace($signature)) {
        throw 'CryptoPro CAdESCOM returned an empty signature.'
    }

    try { $store.Close() } catch {}
    [Console]::Out.Write($signature)
    exit 0
}
catch {
    $cadesError = $_.Exception.Message
    try {
        if ($store) { $store.Close() }
    }
    catch {}
}

# Compatibility fallback. CAdESCOM is preferred and should normally be used.
try {
    Add-Type -AssemblyName System.Security
    $cert = $null

    foreach ($path in @('Cert:\CurrentUser\My', 'Cert:\LocalMachine\My')) {
        if (Test-Path $path) {
            $candidate = Get-ChildItem $path | Where-Object {
                (($_.Thumbprint -replace '\s', '').ToUpperInvariant() -eq $normalized) -and $_.HasPrivateKey
            } | Select-Object -First 1

            if ($candidate) {
                $cert = $candidate
                break
            }
        }
    }

    if (-not $cert) {
        throw "Certificate with private key not found in Windows store: $normalized"
    }

    $contentInfo = New-Object System.Security.Cryptography.Pkcs.ContentInfo -ArgumentList (, $contentBytes)
    $signedCms = New-Object System.Security.Cryptography.Pkcs.SignedCms -ArgumentList $contentInfo, ([bool]$Detached)
    $cmsSigner = New-Object System.Security.Cryptography.Pkcs.CmsSigner -ArgumentList $cert
    $cmsSigner.IncludeOption = [System.Security.Cryptography.X509Certificates.X509IncludeOption]::EndCertOnly

    $publicKeyOid = $cert.PublicKey.Oid.Value
    switch ($publicKeyOid) {
        '1.2.643.7.1.1.1.1' { $cmsSigner.DigestAlgorithm = New-Object System.Security.Cryptography.Oid('1.2.643.7.1.1.2.2') }
        '1.2.643.7.1.1.1.2' { $cmsSigner.DigestAlgorithm = New-Object System.Security.Cryptography.Oid('1.2.643.7.1.1.2.3') }
        '1.2.643.2.2.19'    { $cmsSigner.DigestAlgorithm = New-Object System.Security.Cryptography.Oid('1.2.643.2.2.9') }
    }

    $signedCms.ComputeSignature($cmsSigner, $false)
    [Console]::Out.Write([Convert]::ToBase64String($signedCms.Encode()))
    exit 0
}
catch {
    $fallbackError = $_.Exception.Message
    throw "Signing failed. CryptoPro CAdESCOM: $cadesError; SignedCms fallback: $fallbackError"
}
