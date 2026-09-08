$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$stores = @(
    @{ Scope = 'CurrentUser'; Path = 'Cert:\CurrentUser\My' },
    @{ Scope = 'LocalMachine'; Path = 'Cert:\LocalMachine\My' }
)

$result = @()
foreach ($store in $stores) {
    if (Test-Path $store.Path) {
        $certs = Get-ChildItem $store.Path | Where-Object {
            $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date)
        }
        foreach ($cert in $certs) {
            $result += [PSCustomObject]@{
                subject = $cert.Subject
                thumbprint = ($cert.Thumbprint -replace '\s', '').ToUpperInvariant()
                not_after = $cert.NotAfter.ToString('yyyy-MM-ddTHH:mm:ssK')
                has_private_key = [bool]$cert.HasPrivateKey
                store_scope = $store.Scope
                issuer = $cert.Issuer
                serial_number = $cert.SerialNumber
            }
        }
    }
}

@($result) | ConvertTo-Json -Compress -Depth 4
