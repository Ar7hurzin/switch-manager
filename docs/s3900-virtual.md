# S3900 Virtual — laboratório de desenvolvimento

Simulador lógico em Python, não uma VM do firmware original. Não abre COM, SSH ou sockets para o equipamento; usa o mesmo coletor serial, executor de planos e terminal CLI do S3900 físico. A interface consulta o estado virtual a cada 3 s. O modo real permanece bloqueado para escritas por padrão.

## Fedora
Na pasta do projeto:
```bash
git switch feature/s3900-virtual
QM_MODE=virtual QM_WEB_PORT=8788 .venv/bin/python start.py
```
Abra http://127.0.0.1:8788/login com seu login local. Se necessário, configure antes com `.venv/bin/python start.py configure`. O modo virtual usa um cookie próprio, para não conflitar com a porta 8787. Encerre com Ctrl+C no terminal que iniciou o servidor.

## Windows (PowerShell)
```powershell
git switch feature/s3900-virtual
$env:QM_MODE = 'virtual'
$env:QM_WEB_PORT = '8788'
.\.venv\Scripts\python.exe start.py
```
Para voltar ao modo normal, encerre e remova as variáveis com `Remove-Item Env:QM_MODE, Env:QM_WEB_PORT`.

## Docker Compose
Use um projeto separado e uma porta diferente. No Bash:
```bash
export QM_MODE=virtual QM_HOST_PORT=8788 QM_DATA_VOLUME=qm-virtual_qm-data
docker compose -p qm-virtual build
docker compose -p qm-virtual run --rm app configure
docker compose -p qm-virtual up -d
```
No PowerShell, substitua o export por `$env:QM_MODE='virtual'; $env:QM_HOST_PORT='8788'; $env:QM_DATA_VOLUME='qm-virtual_qm-data'`. O login do contêiner é independente. Não use `down -v` se quiser preservar o estado.

## Primeiro teste
1. Configuração > VLAN > Criar VLAN 100, nome LAB; gere a prévia e aplique.
2. Configuração > Porta > Access: porta 4, VLAN 100; aplique.
3. Switching: confira VLAN 100 e mapa de 28 portas. Passe o mouse na porta para ver modo, PVID e estado administrativo.
4. CLI: conecte e execute `display vlan`, `display interface brief`, `display current-configuration`. Encerre a CLI antes de aplicar outro plano.
5. Salve a configuração pela área guiada, baixe o backup e reinicie o processo para conferir a persistência.

O estado fica em `instance/s3900-virtual.json` (Docker: /data/instance/s3900-virtual.json). O arquivo não é versionado. Cada comando que altera o estado grava atomicamente a configuração em execução, inclusive antes de salvar. O comando save mantém uma cópia de startup e data de salvamento; a reinicialização do processo preserva o estado em execução, não simula desligamento físico. Para usar outro estado, defina QM_VIRTUAL_STATE como caminho exclusivo. Não execute dois processos virtuais sobre o mesmo arquivo de estado.

Comandos cobertos: display version/vlan/interface brief/current-configuration/saved-configuration/ip interface brief/ip routing-table; system-view, return, quit, VLAN, nome, interface, access/trunk/hybrid, descrição, shutdown, IP, rotas e save com confirmação. Outros comandos retornam erro. Não simula tráfego Ethernet, ASIC, contadores, SSH 1.5, firmware completo nem todas as regras de VRP. Testes físicos continuam necessários.
