param([ValidateSet('list','sign')][string]$Action, [string]$Thumbprint,
      [string]$InputFile, [switch]$Detached)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $store = New-Object -ComObject CAdESCOM.Store
    $store.Open(2, 'My', 2)
    try {
        if ($Action -eq 'list') {
            $result = @()
            for ($i = 1; $i -le $store.Certificates.Count; $i++) {
                $cert = $store.Certificates.Item($i)
                if ($cert.HasPrivateKey() -and $cert.ValidToDate -gt (Get-Date)) {
                    $result += @{thumbprint=$cert.Thumbprint; subject=$cert.SubjectName;
                                 expires=$cert.ValidToDate.ToString('o')}
                }
            }
            ConvertTo-Json -InputObject @($result) -Compress
        } else {
            $found = $store.Certificates.Find(0, $Thumbprint)
            if ($found.Count -ne 1) { throw 'Certificate unavailable' }
            $cert = $found.Item(1)
            if (-not $cert.HasPrivateKey()) { throw 'Private key unavailable' }
            if ($cert.ValidFromDate -gt (Get-Date) -or $cert.ValidToDate -lt (Get-Date)) {
                throw 'Certificate expired'
            }
            $signer = New-Object -ComObject CAdESCOM.CPSigner
            $signer.Certificate = $cert
            $signer.CheckCertificate = $true
            $signed = New-Object -ComObject CAdESCOM.CadesSignedData
            $signed.ContentEncoding = 1
            $signed.Content = [Convert]::ToBase64String([IO.File]::ReadAllBytes($InputFile))
            $signed.SignCades($signer, 1, [bool]$Detached, 0)
        }
    } finally { $store.Close() }
} catch {
    # Avoid dumping COM provider internals, subject data or signed contents.
    [Console]::Error.WriteLine('CryptoPro operation failed')
    exit 1
}
