#!/usr/bin/env python3
"""Start Switch Manager on Windows, Linux or in a container."""
import json
import os
import re
import signal
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
SETTINGS = Path(os.environ.get('QM_SETTINGS_FILE', ROOT / 'local-settings.ps1')).expanduser()
AUTH = {'QM_OIDC_ISSUER', 'QM_OIDC_CLIENT_ID', 'QM_SECRET_KEY'}
OIDC = AUTH | {'QM_OIDC_ADMIN_EMAIL', 'QM_OIDC_ALLOWED_EMAILS', 'QM_OIDC_CLIENT_SECRET', 'QM_OIDC_REDIRECT_URI', 'QM_OIDC_REQUIRE_MFA', 'QM_OIDC_MFA_AMR', 'QM_OIDC_ACCEPTED_ACR', 'QM_OIDC_ACR_VALUES', 'QM_OIDC_PROVIDER_LOGOUT', 'QM_OIDC_USER_CLAIM', 'QM_OIDC_REQUIRE_EMAIL_VERIFIED'}
# Health 'service' names; the old one identifies instances started before the rename.
SERVICE_NAMES = {'Switch Manager', 'Quidway Manager'}
ALLOWED = OIDC | {'QM_MODE', 'QM_SERIAL_PORT', 'QM_SERIAL_BAUD', 'QM_WEB_PORT', 'QM_ENABLE_CONFIG_WRITES', 'QM_ENABLE_CLI', 'QM_HTTPS_ONLY', 'QM_CONNECT_TIMEOUT', 'QM_SWITCH_HOST', 'QM_SWITCH_PORT', 'QM_SWITCH_USER', 'QM_KNOWN_HOSTS', 'QM_SSH_KEY_FILE', 'QM_VIRTUAL_STATE', 'QM_ACCESS_FILE'}

def load_settings():
    values = {}
    if SETTINGS.exists():
        if os.name == 'posix':
            SETTINGS.chmod(0o600)
        for line in SETTINGS.read_text(encoding='utf-8-sig').splitlines():
            m = re.fullmatch(r"\s*\$env:(QM_[A-Z_]+)\s*=\s*'((?:[^']|'')*)'\s*", line)
            if m and m[1] in ALLOWED:
                values[m[1]] = m[2].replace("''", "'")
    return values

def main():
    os.chdir(ROOT)
    os.umask(0o077)
    if len(sys.argv) > 1:
        if sys.argv[1] == 'configure':
            from configure_login import main as configure
            return configure()
        if sys.argv[1] in ('capture', 'validate'):
            for key, value in load_settings().items():
                os.environ.setdefault(key, value)
            port = os.environ.get('QM_WEB_PORT', '8787')
            try:
                # The serial port is exclusive: never capture while the panel polls the switch.
                with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1) as resp:
                    if json.load(resp).get('service') in SERVICE_NAMES:
                        print('O Switch Manager está rodando e usa a porta serial. Encerre-o (Ctrl+C) antes de continuar.', file=sys.stderr)
                        return 2
            except (URLError, OSError, ValueError):
                pass
            if sys.argv[1] == 'validate':
                from model_validation import main as validate
                return validate()
            from device_capture import main as capture
            return capture()
        print('Uso: python start.py [configure|capture|validate]', file=sys.stderr)
        return 2
    for key, value in load_settings().items():
        os.environ.setdefault(key, value)
    os.environ.setdefault('QM_MODE', 'demo')
    os.environ.setdefault('QM_ENABLE_CONFIG_WRITES', 'false')
    os.environ.setdefault('QM_ENABLE_CLI', 'false')
    os.environ.setdefault('QM_WEB_PORT', '8787')
    if not all(os.getenv(k) for k in AUTH):
        if os.getenv('QM_CONTAINER') == '1' or not sys.stdin.isatty():
            print('Configure o login primeiro: python start.py configure (Docker: docker compose run --rm app configure).', file=sys.stderr)
            return 2
        from configure_login import main as configure
        if configure() != 0:
            return 2
        os.environ.update({k: v for k, v in load_settings().items() if k in OIDC})
    if os.environ['QM_MODE'] not in {'demo','serial','real','virtual'}:
        print('QM_MODE deve ser demo, virtual, serial ou real.', file=sys.stderr); return 2
    try:
        port = int(os.environ['QM_WEB_PORT'])
        if not 1 <= port <= 65535: raise ValueError
    except ValueError:
        print('QM_WEB_PORT inválida.', file=sys.stderr); return 2
    url = f'http://127.0.0.1:{port}'
    try:
        with urlopen(url+'/api/health', timeout=1) as resp:
            info = json.load(resp)
        if info.get('service') in SERVICE_NAMES and info.get('auth') == 'oidc' and info.get('auth_configured'):
            print('Switch Manager já está em execução: '+url, flush=True); return 0
        if info.get('service') in SERVICE_NAMES:
            # An instance from before the SSO login must never be silently reused.
            print('Um Switch Manager de versão anterior (login por senha) está rodando na porta '+str(port)+'. '
                  'Encerre-o (Ctrl+C no terminal dele, ou: pkill -f "python start.py") e rode este comando novamente.', file=sys.stderr); return 2
        print('A porta '+str(port)+' já está ocupada por outro serviço. Encerre-o ou defina outra QM_WEB_PORT.', file=sys.stderr); return 2
    except (URLError, OSError, ValueError, AttributeError):
        pass
    from app import app, _auth_configured, oidc_settings, start_live_monitor
    if not _auth_configured():
        problems = ' '.join(oidc_settings.problems()) or 'Defina QM_SECRET_KEY.'
        print('Configuração SSO inválida: '+problems+' Execute python start.py configure.', file=sys.stderr); return 2
    from waitress import serve
    host = '0.0.0.0' if os.getenv('QM_CONTAINER') == '1' else '127.0.0.1'
    print('Switch Manager: '+url+' | modo: '+os.environ['QM_MODE'], flush=True)
    start_live_monitor()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        serve(app, host=host, port=port, threads=8)
    except OSError as exc:
        print('Não foi possível abrir a porta '+str(port)+': '+str(exc), file=sys.stderr); return 2
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
