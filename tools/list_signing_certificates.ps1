$ErrorActionPreference = "Stop"
Write-Host "Сертификаты пользователя с закрытым ключом и правом подписи:" -ForegroundColor Cyan
Get-ChildItem Cert:\CurrentUser\My |
  Where-Object { $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) } |
  Sort-Object NotAfter -Descending |
  Select-Object Subject, Thumbprint, NotBefore, NotAfter, HasPrivateKey |
  Format-Table -AutoSize

Write-Host ""
Write-Host "Скопируйте Thumbprint нужного сертификата в CZ_CERT_THUMBPRINT внутри .env." -ForegroundColor Yellow
Write-Host "PIN токена и закрытый ключ в FBE не вносятся." -ForegroundColor Yellow
Read-Host "Нажмите Enter для выхода"
