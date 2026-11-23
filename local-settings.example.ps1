# Copie este arquivo para local-settings.ps1 para habilitar uma conexão real.
# O modo demonstração é usado quando QM_MODE não é definido.
# Não compartilhe local-settings.ps1 nem grave senhas neste arquivo.

# Opção de console local: cabo conectado ao computador e Tera Term/MobaXterm fechado.
# Configure QM_SERIAL_BAUD com a mesma velocidade usada na sessão de console.
# $env:QM_MODE = 'serial'
# $env:QM_SERIAL_PORT = 'COM9'
# $env:QM_SERIAL_BAUD = '115200'
# Gravações no switch começam bloqueadas; habilite só após revisar e validar o release.
# $env:QM_ENABLE_CONFIG_WRITES = 'false'

# Configure o login SSO uma vez executando .\configure-login.ps1 (ou python start.py configure).
# O assistente grava QM_OIDC_ISSUER, QM_OIDC_CLIENT_ID, QM_OIDC_ALLOWED_EMAILS e a chave de sessão.
# Nenhuma senha de usuário é armazenada: o login e o 2FA acontecem no provedor. Veja docs/login-sso.md.

# Opção de rede SSHv2: configure apenas se o firmware aceitar os algoritmos SSH atuais.
# $env:QM_MODE = 'real'
# $env:QM_SWITCH_HOST = 'IP_DE_GESTAO_DO_SWITCH'
# $env:QM_SWITCH_PORT = '22'
# $env:QM_SWITCH_USER = 'USUARIO_DE_LEITURA'
# $env:QM_KNOWN_HOSTS = 'C:\caminho\protegido\known_hosts'
# $env:QM_SSH_KEY_FILE = 'C:\caminho\protegido\chave_privada'
# Alternativa: defina QM_SSH_PASSWORD somente na sessão atual do PowerShell.

# O terminal CLI pode alterar o equipamento: bloqueado por padrão.
# $env:QM_ENABLE_CLI = 'false'
# Linux: ajuste QM_SERIAL_PORT para /dev/ttyUSB0 ou /dev/ttyACM0.
