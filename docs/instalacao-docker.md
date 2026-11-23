# Switch Manager Light — instalação e operação

Este guia cobre o mesmo painel em Windows, Fedora/Linux e contêiner Linux. A interface é clara, e o endereço padrão é **http://127.0.0.1:8787/login**. Não é necessário colocar o switch na internet. O modo demonstração usa dados fictícios e não envia comandos ao equipamento.

## 1. Pré-requisitos e download

- Git e acesso à conta autorizada do GitHub. O repositório é privado; use HTTPS autenticado pelo GitHub CLI (`gh auth login`) ou SSH com chave de conta. Nunca coloque tokens em URLs.
- Python 3.11 ou superior para instalação nativa (o ambiente de desenvolvimento Fedora foi testado com 3.15; a imagem usa 3.12).
- Para Docker, Docker Desktop com contêineres Linux no Windows, ou Docker Engine com Compose V2 no Linux. Docker Desktop requer WSL 2/virtualização conforme documentação do fabricante. No Fedora, Podman pode ser usado para construir e executar a imagem, mas a disponibilidade e a compatibilidade do provedor Compose variam.

```bash
git clone https://github.com/Ar7hurzin/switch-manager.git
cd switch-manager
# Caso a versão Light ainda esteja em revisão:
git switch feature/s3900-virtual
```

O código já contém o painel, arquivos estáticos, scripts e documentação. O arquivo `local-settings.ps1` não vem do GitHub: ele contém a configuração SSO (issuer, client ID/secret, e-mails autorizados) e a chave de sessão e deve permanecer local. Não copie seu ambiente `.venv` de Windows para Linux.

## 2. Windows — execução nativa (PowerShell)

Instale Python pelo site oficial e habilite o launcher `py`. Abra a pasta no VS Code ou terminal:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe start.py configure
.\.venv\Scripts\python.exe start.py
```

Alternativamente, use `.\configure-login.ps1` e `.\start.ps1`. Se a política local impedir scripts PowerShell, prefira os comandos Python acima em vez de desabilitar a proteção do sistema. Informe o provedor SSO e os e-mails autorizados ([login SSO com 2FA](login-sso.md)); nenhuma senha de usuário é cadastrada. Abra http://127.0.0.1:8787/login. Pressione Ctrl+C para encerrar.

## 3. Fedora/Linux — execução nativa

Se faltar Git/Python: `sudo dnf install git python3 python3-pip`. Na pasta do projeto:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python start.py configure
.venv/bin/python start.py
```

O alias `.venv/bin/python start-linux.py` continua disponível. Abra http://127.0.0.1:8787/login. Não use `sudo` para executar a aplicação. No VS Code, selecione `.venv/bin/python` como interpretador.

## 4. Instalação do Docker

**Windows:** instale Docker Desktop pelo [site oficial](https://docs.docker.com/desktop/setup/install/windows-install/), habilite o backend WSL 2 e use o modo Linux containers. Abra Docker Desktop e aguarde o engine iniciar. Se necessário, instale WSL com `wsl --install` em PowerShell administrador e reinicie conforme solicitado.

**Fedora:** siga [Docker Engine no Fedora](https://docs.docker.com/engine/install/fedora/) para adicionar o repositório oficial, instalar Docker Engine/CLI e Compose Plugin e iniciar o serviço. A instalação requer administração; para operar sem sudo, prefira o modo rootless documentado ou uma configuração autorizada pelo administrador. O grupo `docker` equivale a conceder acesso de alto privilégio ao host. Verifique as versões:

```bash
docker version
docker compose version
```

Se já existir uma instância nativa na porta 8787, encerre-a com Ctrl+C no terminal onde roda. Ou selecione outra porta no host com `QM_HOST_PORT=8788` (Linux: `export QM_HOST_PORT=8788`; PowerShell: `$env:QM_HOST_PORT = '8788'`). O container sempre utiliza 8787 internamente.

## 5. Docker Compose — primeira execução

Execute na pasta que contém `Dockerfile` e `compose.yaml`. Os mesmos comandos abaixo funcionam no terminal Windows e Linux com Docker Compose V2:

```bash
docker compose build
docker compose run --rm app configure
docker compose up -d
docker compose ps
docker compose logs -f app
```

O segundo comando abre a configuração interativa do SSO ([login SSO com 2FA](login-sso.md)). Não coloque o client secret na linha de comando. O volume de dados (`sm-data` no `compose.yaml`, gravado no Docker como `quidway-manager-s3900_qm-data`, nome anterior à mudança para Switch Manager, mantido para não perder dados de instalações existentes; troque com `QM_DATA_VOLUME`) mantém `local-settings.ps1` com a configuração SSO e a chave de sessão fora da imagem e do Git. O container roda com usuário não-root. A porta publicada está limitada a `127.0.0.1` do host. Abra http://127.0.0.1:8787/login (ou a porta alternativa que escolheu). O indicador de saúde confirma que o login está configurado; não comprova que o switch físico está disponível.

Operação diária:

```bash
docker compose stop
docker compose start
docker compose logs --tail=100 app
docker compose down
```

`down` não apaga os dados nomeados. **Não execute `docker compose down -v`** se quiser conservar login e configuração. Para atualização do código, faça `git pull` na branch desejada e `docker compose up -d --build`. Para redefinir o login: `docker compose stop app`, `docker compose run --rm app configure`, `docker compose up -d`. A chave de sessão é renovada; sessões antigas deixam de funcionar.

## 6. Podman — alternativa para Fedora

O Dockerfile foi construído e executado no Fedora com Podman rootless. Para operação manual sem Compose:

```bash
podman build --format docker -t localhost/switch-manager:light .
podman volume create quidway-data  # nome anterior à mudança para Switch Manager; mantido para preservar os dados
podman run --rm -it -v quidway-data:/data localhost/switch-manager:light configure
podman run -d --name switch-manager -p 127.0.0.1:8787:8787 -v quidway-data:/data localhost/switch-manager:light
podman logs -f switch-manager
```

Se a porta já estiver em uso, altere somente o lado do host (ex. `127.0.0.1:8788:8787`). Para parar: `podman stop switch-manager`. Para iniciar: `podman start switch-manager`. Remover o container não remove o volume nomeado. Para Compose com Podman, instale e confira um provedor compatível; não assuma que todos os recursos Docker Compose são idênticos.

## 7. Conexão real: console serial

O switch observado é Quidway 3900-SI 28-Port, VRP 3.10(CN) Release 0002. A sintaxe exata de escrita ainda não está validada. Feche Tera Term/MobaXterm/minicom antes da conexão. O servidor precisa estar fisicamente conectado ao cabo console, com adaptador apropriado; não conecte adaptador serial TTL a uma porta RS-232 sem compatibilidade elétrica.

**Windows nativo:** identifique a COM no Gerenciador de Dispositivos. **Linux nativo:** `ls -l /dev/ttyUSB* /dev/ttyACM*` e `ls -l /dev/serial/by-id/` (se houver). Compare antes/depois de conectar. Use a porta real, baud 115200 (o S3900 e o S3000 foram ajustados para 115200 em 23/11/2026; o padrão de fábrica é 9600), configuração 8N1. Confirme permissões com `ls -l /dev/ttyUSB0` e `id`; peça acesso pelo grupo proprietário do dispositivo conforme política da máquina, nunca `chmod 777`.

No arquivo local existente `local-settings.ps1`, acrescente (sem apagar os dados do login):

```powershell
$env:QM_MODE = 'serial'
$env:QM_SERIAL_PORT = 'COM9'
$env:QM_SERIAL_BAUD = '115200'
$env:QM_ENABLE_CONFIG_WRITES = 'false'
$env:QM_ENABLE_CLI = 'false'
```

Em Linux troque `COM9` por `/dev/ttyUSB0` (ou o caminho real). O inicializador interpreta somente atribuições reconhecidas; não executa comandos PowerShell no Linux. Reinicie o servidor. No modo serial, o monitor consulta automaticamente identificação, VLANs, portas e MACs, em lotes. O botão Atualizar leitura fica oculto; cada observação tem seu horário e dados antigos são sinalizados. O painel não inventa métricas reais.

**Docker Linux:** para Docker Engine nativo, use o override, depois de verificar o GID numérico e o caminho:

```bash
export QM_SERIAL_DEVICE=/dev/ttyUSB0
export QM_SERIAL_GID=$(stat -c '%g' "$QM_SERIAL_DEVICE")
docker compose -f compose.yaml -f compose.serial.yaml up -d --build
```

O arquivo mapeia o dispositivo como `/dev/ttyUSB0` no container e adiciona o grupo necessário. Verifique permissões e políticas SELinux; rootless Podman pode exigir mapeamentos de grupos e regras específicas. Não use `--privileged` como solução genérica. **No Docker Desktop Windows, o contêiner Linux não tem acesso direto à COM do Windows.** Para serial no Windows, use a instalação nativa (recomendada). Passagem USB via WSL/usbip é dependente do adaptador e não validada neste projeto; Docker em uma VM Linux também depende do passthrough. Não confunda essa limitação com falha do painel.

**Operações de escrita:** a configuração guiada e a CLI ficam bloqueadas de início. `QM_ENABLE_CONFIG_WRITES=true` só deve ser usado após confirmar o firmware e cada operação em bancada. `QM_ENABLE_CLI=true` libera terminal direto, capaz de alterar configuração mesmo se a escrita guiada estiver bloqueada. Não habilite por padrão, não exponha a CLI publicamente e mantenha um backup verificado. A CLI e os diagnósticos usam exclusão serial para evitar disputa.

## 8. Conexão real: Ethernet/SSH

O S3900 observado anunciou SSH 1.5; o Manager aceita somente SSHv2 com chave do host verificada. Não reduza a segurança do computador nem use Telnet via internet. Confirme a compatibilidade SSHv2 do switch antes de habilitar. Se for incompatível, use serial nativo ou um computador/gateway de console autorizado. Defina IP de gestão, usuário autorizado de leitura, `known_hosts` validado presencialmente e uma chave SSH dedicada protegida (ou senha apenas no ambiente da sessão nativa).

**Nativo:** preencha `QM_MODE=real`, `QM_SWITCH_HOST`, `QM_SWITCH_PORT`, `QM_SWITCH_USER`, `QM_KNOWN_HOSTS` e `QM_SSH_KEY_FILE` no arquivo local, com caminhos corretos da plataforma. Não grave senha no Git; se usar senha, forneça `QM_SSH_PASSWORD` somente no ambiente do processo. Não forneça senha e chave simultaneamente.

**Docker Compose:** use uma chave de serviço dedicada, guardada em volume privado. Os exemplos Linux abaixo pressupõem que os arquivos já foram obtidos e verificados em canal confiável. Não copie chave pessoal ao repositório.

```bash
docker compose run --rm -T --entrypoint sh app -c 'umask 077; mkdir -p /data/ssh; cat > /data/ssh/known_hosts' < /caminho/known_hosts
docker compose run --rm -T --entrypoint sh app -c 'umask 077; mkdir -p /data/ssh; cat > /data/ssh/id_key' < /caminho/id_key
export QM_SWITCH_HOST=IP_DE_GESTAO
export QM_SWITCH_USER=USUARIO_DE_LEITURA
docker compose -f compose.yaml -f compose.ssh.yaml up -d
```

No PowerShell, substitua cada redirecionamento de entrada por `Get-Content -Raw C:\caminho\arquivo | docker compose run --rm -T --entrypoint sh app -c 'umask 077; mkdir -p /data/ssh; cat > /data/ssh/NOME_DO_ARQUIVO'` (arquivos SSH são texto; confira integridade). Use `$env:QM_SWITCH_HOST = 'IP_DE_GESTAO'` e `$env:QM_SWITCH_USER = 'USUARIO_DE_LEITURA'`. O contêiner deve ter rota de rede para o IP do equipamento; confirme firewall e conectividade. Configure somente leitura.

## 9. Acesso remoto privado

Instale Tailscale no **host** que executa o Manager e no dispositivo cliente. Autentique ambos na tailnet autorizada. Com o painel funcionando localmente, publique via `tailscale serve --bg 8787` (ou a porta escolhida no host). Use `tailscale serve status` para obter o URL HTTPS. Não use Funnel nem encaminhamento público de portas. O PC/servidor precisa continuar ligado. Mantenha o app preso ao loopback. Pelo endereço HTTPS da tailnet o cookie de sessão já sai com `Secure` automaticamente. Caso seu uso seja exclusivamente HTTPS, `QM_HTTPS_ONLY=true` força `Secure` também nos demais acessos; no HTTP local essa opção impede a sessão, por isso permanece desligada no padrão. O limite de login por origem pode ser compartilhado entre usuários atrás de um proxy.

## 10. Testes e limites desta versão

```bash
python -m unittest discover -s tests -v
node --check static/app.js
```

Use o Python do ambiente virtual. Os testes cobrem login, CSRF, expiração, bloqueio de escrita/CLI, rate limiting, APIs e páginas. Testes de contêiner verificam instalação, login e persistência. Não são pentest formal ou certificação NIST/OWASP. A autenticação é SSO com 2FA e allowlist de e-mails, ainda sem RBAC; a proteção e auditoria precisam ser ampliadas antes de um cenário multiusuário de produção. Windows requer validação no host e no Docker Desktop; switch real precisa ser testado separadamente. O modo demo e os testes de software não confirmam portas, VLANs, SNMP, SSH legado, comandos de alteração ou recuperação do VRP real.
