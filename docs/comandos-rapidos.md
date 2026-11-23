# Switch Manager Light — comandos rápidos

## Fedora (seu notebook atual)

Abra a pasta do projeto no VS Code e o terminal **Bash** (Terminal > New Terminal). Execute:

```bash
cd ~/Projetos/switch-manager
.venv/bin/python start-linux.py
```

Abra **http://127.0.0.1:8787/login** no navegador e clique em **Entrar com SSO**. Mantenha o terminal aberto; encerre com Ctrl+C no terminal que iniciou o servidor. Se já estiver rodando, o inicializador informa isso; apenas abra o navegador. Não use o botão Run do `app.py`, pois ele não carrega automaticamente o arquivo de configuração local.

### Recuperar ou atualizar pelo GitHub

Se a pasta não existir, clone o repositório privado após autenticar-se no GitHub:

```bash
mkdir -p ~/Projetos
cd ~/Projetos
git clone https://github.com/Ar7hurzin/switch-manager.git
cd switch-manager
git switch feature/s3900-virtual
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python start.py configure
.venv/bin/python start.py
```

Se já possui o projeto, atualize na pasta existente, conferindo primeiro `git status` e sem descartar alterações locais:

```bash
git switch feature/s3900-virtual
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python start.py
```

Para trocar o provedor SSO ou os e-mails autorizados, encerre o servidor, execute `.venv/bin/python start.py configure` e inicie novamente. Não apague nem envie `local-settings.ps1` ao GitHub.

## Windows (PowerShell do VS Code)

Na pasta do repositório já baixado, execute:

```powershell
git switch feature/s3900-virtual
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe start.py configure
.\.venv\Scripts\python.exe start.py
```

Nas próximas execuções basta `.\.venv\Scripts\python.exe start.py`. Se o SSO já estiver configurado, não execute `configure` novamente. Use o mesmo endereço no navegador.

## Docker (terminal Bash ou PowerShell com Docker Compose V2)

Com Docker Engine/Desktop ligado e na pasta do projeto:

```bash
docker compose build
docker compose run --rm app configure
docker compose up -d
docker compose ps
```

Abra http://127.0.0.1:8787/login. Nas próximas execuções use `docker compose up -d`; para encerrar `docker compose down` (não adicione `-v`, que apagaria o volume de login). Para evitar conflito, pare primeiro a instância nativa. Se usar Podman no Fedora, consulte o [guia completo](instalacao-docker.md#6-podman--alternativa-para-fedora). O modo padrão é demo; não habilite serial/SSH ou escritas até validar o equipamento. A porta COM do Windows não passa diretamente para um contêiner Linux.

## Conferir a aplicação

No terminal da pasta: `.venv/bin/python -m unittest discover -s tests -v` no Linux (Windows: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`). A página `/api/health` confirma o servidor, modo e estado do login, não a conexão real com o switch. Acesse o [guia completo](instalacao-docker.md) para configurar acesso físico e remoto privado.
