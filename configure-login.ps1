$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path $python)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $launcher) { throw 'Instale Python 3.11 ou mais recente e tente novamente.' }
    & py -3 configure_login.py
} else {
    & $python configure_login.py
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "Configuração não concluída (código $LASTEXITCODE). Leia a mensagem do assistente acima: confira o issuer (https), o client ID e os e-mails autorizados." -ForegroundColor Red
    return
}
