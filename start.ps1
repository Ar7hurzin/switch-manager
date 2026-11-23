$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv/Scripts/python.exe')) {
    & py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Instale Python 3.11+ e tente novamente.' }
}
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
& $python -c "import flask, jwt, paramiko, waitress, serial"
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar dependências.' }
}
& $python start.py
exit $LASTEXITCODE
