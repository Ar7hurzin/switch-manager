$ErrorActionPreference = 'Stop'
$port = if ($env:QM_WEB_PORT) { $env:QM_WEB_PORT } else { '8787' }
$url = "http://127.0.0.1:$port"
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'

if (-not (Test-Path $tailscale)) {
    $command = Get-Command tailscale.exe -ErrorAction SilentlyContinue
    if ($command) { $tailscale = $command.Source }
    else { throw 'Não encontrei o Tailscale. Instale e conecte este computador à sua tailnet primeiro.' }
}

try {
    $null = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 3
} catch {
    throw "O Quidway Manager não está ativo em $url. Inicie-o com .\start.ps1 e tente novamente."
}

$existing = (& $tailscale serve status 2>&1 | Out-String).Trim()
if ($existing -and $existing -notmatch '^No serve config\s*$') {
    throw "Este computador já tem uma configuração Tailscale Serve. Não alterei nada.`n$existing"
}

Write-Host 'Configurando acesso HTTPS privado pela tailnet...' -ForegroundColor Cyan
$result = (& $tailscale serve --bg $url 2>&1 | Out-String).Trim()
Write-Host $result
if ($result -match 'Serve is not enabled on your tailnet') {
    throw 'A tailnet precisa habilitar o Tailscale Serve. Abra o link informado acima, aprove a ativação e execute este arquivo novamente.'
}
if ($LASTEXITCODE -ne 0) { throw 'O Tailscale não conseguiu configurar o acesso privado.' }

Write-Host 'Pelo endereço HTTPS da tailnet o cookie de sessão é emitido com Secure automaticamente.'
Write-Host "`nConfiguração atual do Tailscale Serve:" -ForegroundColor Cyan
& $tailscale serve status
