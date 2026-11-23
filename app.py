"""Switch Manager: local monitoring and serial-console management for legacy Huawei VRP 3 switches."""
from __future__ import annotations

import os
import atexit
import secrets
import threading
import time
from io import BytesIO
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template_string, request, send_file, session, url_for
from flask.sessions import SecureCookieSessionInterface
from waitress import serve

from serial_readonly import SerialSettings, SerialReadOnlyError, collect_serial_diagnostics
from serial_config import CommandRejected, execute_plan, export_current_configuration, prepare_plan
from switch_capabilities import SwitchCapabilities
from serial_cli import SerialCLIError, cli_session
from ssh_readonly import SwitchSettings, SSHReadOnlyError, collect_diagnostics
from switch_config import ConfigurationError, build_plan, clear_previews, consume_preview, issue_preview
from virtual_switch import VERSION as VIRTUAL_VERSION, snapshot as virtual_snapshot
import virtual_switch
from switch_profiles import Inventory, identify
from console_auth import console_credentials, validate_credentials
from physical_dashboard import PhysicalDashboard
from live_monitor import LiveMonitor
from lab_capture import record_lab_reading
from session_registry import SessionRegistry
from oidc_auth import GOOGLE_ISSUER, OIDCClient, OIDCError, OIDCNotAuthorized, OIDCSettings
from access_list import AccessList, AccessListError
from model_validation import GUIDED_ACTIONS, ValidatedModels

physical_dashboard = PhysicalDashboard()
# The validated S3900 identity, used by the virtual switch and as the safe default
# before a physical switch has been identified.
DEFAULT_DEVICE = identify(VIRTUAL_VERSION)


def _on_switch_changed(device):
    # A preview built for the previous switch must never be applied to this one.
    clear_previews()


physical_dashboard.listeners.append(_on_switch_changed)
# Operations each switch model rejected: they become CLI-only for that model.
switch_capabilities = SwitchCapabilities()
UNVALIDATED_WARNING = ("Modelo ainda não validado neste programa: os comandos seguem a sintaxe do manual VRP 3. "
                       "Confira a prévia. Se o switch recusar um comando, os anteriores já terão sido aplicados "
                       "e esta operação passará a ser feita pela CLI neste modelo.")


def _capability_key(device) -> str:
    # Simulated switches never mark real models of the same firmware as CLI-only.
    return ("virtual:" if mode() == "virtual" else "") + device.identity_key


def _unsupported_actions(device) -> dict:
    return switch_capabilities.unsupported(_capability_key(device)) if device is not None else {}


# Operations that passed `python start.py validate` on this switch model and firmware.
validated_models = ValidatedModels()


def _validated_actions(device) -> set[str]:
    if device is None:
        return set()
    if device.profile.validated_writes:
        return set(GUIDED_ACTIONS)
    return validated_models.actions(_capability_key(device))


def _device_public(device) -> dict | None:
    if device is None:
        return None
    public = device.public()
    validated = _validated_actions(device)
    public["validated_actions"] = [action for action in GUIDED_ACTIONS if action in validated]
    public["guided_actions"] = list(GUIDED_ACTIONS)
    public["validated_writes"] = validated >= set(GUIDED_ACTIONS)
    return public

atexit.register(cli_session.shutdown)

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.config.update(
    JSON_SORT_KEYS=False,
    MAX_CONTENT_LENGTH=32 * 1024,
    SECRET_KEY=os.getenv("QM_SECRET_KEY") or secrets.token_urlsafe(48),
    SESSION_COOKIE_NAME="qm_virtual_session" if os.getenv("QM_MODE") == "virtual" else "session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("QM_HTTPS_ONLY", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)


def _https_request() -> bool:
    """The browser reached the panel over HTTPS (directly or through Tailscale Serve).

    The app only listens on loopback, so these signals come from the local proxy;
    a local process faking them only makes its own cookie stricter.
    """
    host = request.host.rsplit(":", 1)[0].lower().rstrip(".")
    return (request.is_secure or host.endswith(".ts.net")
            or request.headers.get("X-Forwarded-Proto", "").lower() == "https")


class _SessionInterface(SecureCookieSessionInterface):
    # Secure whenever the page was served over HTTPS, so a session issued via
    # Tailscale Serve is never sent to a plain-HTTP listener on the same name.
    # Plain local HTTP (127.0.0.1) keeps working without QM_HTTPS_ONLY.
    def get_cookie_secure(self, app):
        return super().get_cookie_secure(app) or _https_request()


app.session_interface = _SessionInterface()
# Sign-in is delegated to an OpenID Connect provider: no password or password
# hash exists anywhere in this application.
oidc_settings = OIDCSettings.from_environment()
# Runtime list of e-mails/domains the administrator has authorized (no passwords).
access_list = AccessList()


def _authorized(email, org_domain) -> bool:
    """Checked at login AND on every request, so a removal takes effect immediately."""
    if not isinstance(email, str):
        return False
    return oidc_settings.email_allowed(email) or access_list.allows(email, domain_verified_by=org_domain)


oidc_client = OIDCClient(oidc_settings, authorize=lambda email, org_domain: _authorized(email, org_domain))
ACTIVE_SESSION_TTL_SECONDS = 5 * 60
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
_session_registry = SessionRegistry()
_active_sessions: dict[str, tuple[str, float]] = {}
_active_sessions_lock = threading.Lock()
_serial_request_lock = threading.Lock()
_login_attempts = {}
_login_lock = threading.Lock()

LOGIN_ATTEMPTS_PER_MINUTE = 8
MAX_LOGIN_RATE_KEYS = 1024


def _login_rate_key():
    """Per browser, not per address: behind Tailscale Serve, Docker or any local
    proxy every client arrives as 127.0.0.1, so an address-only bucket would let
    one anonymous client lock everybody out of SSO."""
    browser = session.get("qm_login_rl")
    if not isinstance(browser, str):
        browser = secrets.token_urlsafe(16)
        session["qm_login_rl"] = browser
    return (request.remote_addr or "local", browser)


def _login_rate_allowed():
    now = time.monotonic()
    key = _login_rate_key()
    with _login_lock:
        for k, (_, until) in list(_login_attempts.items()):
            if until <= now: _login_attempts.pop(k, None)
        # Bounded memory without denying newcomers: evict the oldest buckets.
        while key not in _login_attempts and len(_login_attempts) >= MAX_LOGIN_RATE_KEYS:
            _login_attempts.pop(next(iter(_login_attempts)))
        count, until = _login_attempts.get(key, (0, now + 60))
        if count >= LOGIN_ATTEMPTS_PER_MINUTE:
            return False
        _login_attempts[key] = (count + 1, until)
        return True

def _serial_exclusive(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        if mode() not in {"serial", "virtual"}: return fn(*args, **kwargs)
        if not _serial_request_lock.acquire(blocking=False):
            return jsonify({"ok": False, "error": "Outra operação serial está em andamento. Aguarde e tente novamente."}), 409
        try: return fn(*args, **kwargs)
        finally: _serial_request_lock.release()
    return run


LOGIN_PAGE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="{{ csrf_token }}"><title>Entrar · Switch Manager</title><link rel="stylesheet" href="/static/styles.css?v=20261121-nav-1"></head><body class="login-body"><main class="login-shell"><section class="login-hero"><div><div class="brand-mark">S</div><h2>Gerencie sua infraestrutura de rede</h2><p>Switch Manager · switches Huawei VRP 3<br>Monitoramento, diagnóstico e configuração em um único console.</p></div><small>CONSOLE DE GERENCIAMENTO PRIVADO</small></section><section class="login-card"><p class="card-overline">ACESSO PRIVADO · SSO</p><h1>Entrar</h1><p class="login-note">O login é feito no provedor da sua organização ({{ provider }}) com autenticação de dois fatores. Nenhuma senha é digitada ou guardada neste aplicativo.</p>{% if error %}<p class="login-error" role="alert">{{ error }}</p>{% endif %}<form method="post" action="/login"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button id="sso-login" class="button button-primary" type="submit" autofocus>Entrar com SSO</button></form><small>Somente contas autorizadas entram. A sessão termina depois de 8 horas.</small></section></main></body></html>"""
SETUP_REQUIRED_PAGE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Configurar acesso · Switch Manager</title><link rel="stylesheet" href="/static/styles.css?v=20261121-nav-1"></head><body class="login-body"><main class="card setup-card"><h1>Configure o login SSO antes de iniciar</h1><p>O Switch Manager está bloqueado até que o provedor OpenID Connect (emissor, client ID e e-mails autorizados) seja configurado. Execute o comando correspondente ao seu ambiente e reinicie o servidor.</p><p>Linux: <code>.venv/bin/python start.py configure</code></p><p>Windows: <code>.\\configure-login.ps1</code></p><p>Docker: <code>docker compose run --rm app configure</code></p></main></body></html>"""


def _render_login(error: str = "", status: int = 200):
    token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
    provider = urlsplit(oidc_settings.issuer).hostname or "OpenID Connect"
    return render_template_string(LOGIN_PAGE, csrf_token=token, error=error, provider=provider), status


def _render_manager(page: str = "index.html"):
    template = (ROOT / "static" / page).read_text(encoding="utf-8")
    token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
    user = _signed_in_user()
    is_admin = oidc_settings.is_admin(user)
    pending = len(access_list.pending()) if is_admin else 0
    return render_template_string(template, csrf_token=token, is_admin=is_admin, user=user or "", pending=pending)


def _auth_configured() -> bool:
    return oidc_settings.configured() and bool(os.getenv("QM_SECRET_KEY"))


def _safe_token_equal(supplied: object, expected: object) -> bool:
    return (isinstance(supplied, str) and isinstance(expected, str) and bool(expected)
            and supplied.isascii() and expected.isascii() and secrets.compare_digest(supplied, expected))


def _credential_version() -> str:
    # Changing the provider, client or allowlist revokes every existing session.
    return oidc_settings.version()


def _signed_in_user():
    """The verified SSO e-mail of this browser session, or None."""
    user = session.get("qm_user")
    return user if _authorized(user, session.get("qm_org_domain")) else None


def _invalidate_browser_session() -> None:
    session_id = session.get("qm_session_id")
    _session_registry.revoke(session_id)
    cli_session.request_close_owner(session_id)
    token = session.get("qm_cli_token")
    if token:
        cli_session.request_close(token)
    if isinstance(session_id, str):
        with _active_sessions_lock:
            _active_sessions.pop(session_id, None)
    session.clear()


def _touch_active_session() -> None:
    session_id = session.get("qm_session_id")
    username = _signed_in_user()
    if not isinstance(session_id, str) or username is None:
        return
    now = time.time()
    with _active_sessions_lock:
        expired = [key for key, (_, last_seen) in _active_sessions.items() if now - last_seen > ACTIVE_SESSION_TTL_SECONDS]
        for key in expired:
            _active_sessions.pop(key, None)
        _active_sessions[session_id] = (username, now)


@app.before_request
def require_login_and_csrf():
    if request.endpoint == "health" or request.endpoint == "static":
        return None
    if not _auth_configured():
        return SETUP_REQUIRED_PAGE, 503

    if session.get("qm_user") is not None:
        logged_in_at = session.get("qm_login_time")
        now = time.time()
        user = _signed_in_user()
        valid = (user is not None
                 and isinstance(logged_in_at, (int, float))
                 and 0 <= now - logged_in_at < SESSION_MAX_AGE_SECONDS
                 and _session_registry.valid(session.get("qm_session_id"), user,
                                             _credential_version()))
        if not valid:
            _invalidate_browser_session()

    if request.path == "/auth/callback":
        # Protected by the single-use OIDC state bound to this browser session.
        return None

    if request.path == "/login":
        if request.method == "POST":
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not _safe_token_equal(supplied, expected):
                return "Token de formulário inválido. Atualize a página e tente novamente.", 400
        return None

    if _signed_in_user() is None:
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Sessão encerrada. Entre novamente."}), 401
        return redirect(url_for("login"))

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not _safe_token_equal(supplied, expected):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Token de segurança inválido. Atualize a página e tente novamente."}), 400
            return "Token de segurança inválido. Atualize a página e tente novamente.", 400
    _touch_active_session()
    return None


@app.after_request
def prevent_caching(response):
    # Avoid retaining switch diagnostics or device metadata in shared browser caches.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    # No inline scripts/styles and no third-party origins: an injected tag cannot run.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
    return response

DEMO_DATA: dict[str, Any] = {
    "device": {"name": "S3900 · demonstração", "model": "Quidway S3900 (exemplo)", "version": "Não coletada", "uptime": "Não coletado"},
    "summary": {"ports_up": "18 / 24", "vlans": "4", "alerts": "2", "last_backup": "Não realizado"},
    "ports": [
        {"number": n, "state": "up" if n not in {3, 6, 10, 13, 17, 20} else "down", "label": "Ativa" if n not in {3, 6, 10, 13, 17, 20} else "Sem link"}
        for n in range(1, 25)
    ],
    "vlans": [
        {"id": 1, "name": "default", "members": "23–24", "note": "Exemplo"},
        {"id": 10, "name": "ADMIN", "members": "23 (tagged), 1–4", "note": "Exemplo"},
        {"id": 20, "name": "USUARIOS", "members": "23 (tagged), 5–16", "note": "Exemplo"},
        {"id": 99, "name": "GESTAO", "members": "24 (tagged), 17–18", "note": "Exemplo"},
    ],
    "events": [
        {"time": "09:42", "title": "Backup ilustrativo", "kind": "info"},
        {"time": "09:18", "title": "Porta 07 mudou", "kind": "warning"},
        {"time": "08:55", "title": "Sessão SSH iniciada", "kind": "info"},
        {"time": "08:10", "title": "Alerta de link", "kind": "warning"},
    ],
}


def mode() -> str:
    return os.getenv("QM_MODE", "demo").strip().lower()


def switch_settings() -> SwitchSettings:
    return SwitchSettings.from_environment()


def serial_settings() -> SerialSettings:
    if mode() == "virtual":
        return SerialSettings("S3900-Virtual", 9600, 3, virtual=True)
    return SerialSettings.from_environment()


def collect_for_lab():
    reading = physical_dashboard.collect(serial_settings())
    record_lab_reading(reading)
    return reading


physical_monitor = LiveMonitor(
    collect=collect_for_lab,
    cli_busy=lambda: cli_session.connected,
    serial_lock=_serial_request_lock,
    interval=4,
    retry_interval=12,
)
atexit.register(physical_monitor.stop)


def start_live_monitor():
    if mode() == "serial":
        physical_monitor.start()


def current_device():
    """The switch the panel is working with, or None while not identified."""
    if mode() == "virtual":
        # Identified exactly like a physical switch: from its version text and ports.
        device = identify(virtual_switch.version())
        return device if device.inventory else device.with_inventory(Inventory(tuple(virtual_switch.port_names())))
    if mode() == "serial":
        return physical_dashboard.device
    return None


def config_writes_enabled() -> bool:
    device = current_device()
    if mode() == "virtual": return device.profile.guided_config
    if device is not None and not device.profile.guided_config:
        return False  # incompatible system: CLI only
    return os.getenv("QM_ENABLE_CONFIG_WRITES", "false").strip().lower() in {"1", "true", "yes", "on"}


@app.get("/")
def index():
    return _render_manager()


@app.get("/monitoramento")
def monitoring_page():
    return _render_manager()


@app.get("/sistema")
def system_page():
    return _render_manager()


@app.get("/configuracao")
def configuration_page():
    return _render_manager()


@app.get("/cli")
def cli_page():
    return _render_manager()


@app.route("/login", methods=["GET", "POST"])
def login():
    if _signed_in_user() is not None:
        return redirect(url_for("index"))
    if request.method == "POST":
        # The CSRF check in require_login_and_csrf prevents forced (login-CSRF) sign-ins.
        if not _login_rate_allowed():
            response = app.make_response(_render_login("Muitas tentativas. Aguarde um minuto e tente novamente.", 429))
            response.headers["Retry-After"] = "60"
            return response
        try:
            authorization_url, pending = oidc_client.begin()
        except OIDCError as exc:
            return _render_login(str(exc), 502)
        session["qm_oidc_pending"] = pending
        return redirect(authorization_url)
    return _render_login()


ACCESS_REQUEST_MESSAGES = {
    "pending": "Esta conta ainda não tem acesso. Seu pedido foi enviado ao administrador; entre novamente depois da aprovação.",
    "already_pending": "Seu pedido de acesso já está com o administrador. Entre novamente depois da aprovação.",
    "rejected": "O pedido de acesso desta conta foi recusado pelo administrador. Tente novamente mais tarde.",
    "full": "Há muitos pedidos de acesso aguardando o administrador. Tente novamente mais tarde.",
}


def _record_access_request(identity: dict) -> str:
    """Only a fully verified SSO identity reaches here; recording never grants access."""
    try:
        outcome = access_list.request(identity.get("email"), identity.get("name"))
    except (AccessListError, OSError):
        return "Esta conta não está autorizada neste Switch Manager."
    return ACCESS_REQUEST_MESSAGES[outcome]


@app.get("/auth/callback")
def auth_callback():
    # Single use: a replayed or second callback finds no pending attempt.
    pending = session.pop("qm_oidc_pending", None)
    if _signed_in_user() is not None:
        return redirect(url_for("index"))
    try:
        identity = oidc_client.complete(pending, request.args)
    except OIDCNotAuthorized as exc:
        return _render_login(_record_access_request(exc.identity), 403)
    except OIDCError as exc:
        return _render_login(str(exc), 401)
    try:
        session_id = _session_registry.issue(identity["email"], _credential_version(), SESSION_MAX_AGE_SECONDS,
                                             org_domain=identity.get("org_domain"))
    except ValueError as exc:
        return _render_login(str(exc), 503)
    if not _authorized(identity["email"], identity.get("org_domain")):
        # Access was removed while this login was in flight, after the IdP check
        # but before the session existed: the removal could not revoke it.
        _session_registry.revoke(session_id)
        return _render_login("Seu acesso foi removido pelo administrador.", 403)
    with _login_lock:
        _login_attempts.pop((request.remote_addr or "local", session.get("qm_login_rl")), None)
    _invalidate_browser_session()
    session.permanent = True
    session["qm_user"] = identity["email"]
    session["qm_org_domain"] = identity.get("org_domain")
    session["qm_session_id"] = session_id
    session["qm_login_time"] = time.time()
    session["csrf_token"] = secrets.token_urlsafe(32)
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    _invalidate_browser_session()
    return redirect(oidc_client.logout_url() or url_for("login"))


def _cli_request_allowed() -> bool:
    if not request.is_json:
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc == request.host


@app.get("/api/cli/status")
def cli_status():
    return jsonify({"mode": mode(), "available": mode() in {"serial", "virtual"} and cli_enabled(), **cli_session.status(request.headers.get("X-CLI-Token")), "message": "CLI virtual habilitada; sem hardware." if mode() == "virtual" else "CLI habilitada." if cli_enabled() else "CLI bloqueada. Defina QM_ENABLE_CLI=true somente em ambiente autorizado."})


def cli_enabled():
    if mode() == "virtual": return True
    return os.getenv("QM_ENABLE_CLI", "false").lower() == "true"


@app.post("/api/cli/open")
@_serial_exclusive
def cli_open():
    if not cli_enabled():
        return jsonify({"ok": False, "error": "CLI bloqueada. Defina QM_ENABLE_CLI=true somente após validar o ambiente."}), 403
    if not _cli_request_allowed():
        return jsonify({"ok": False, "error": "Requisição bloqueada: use a interface local do Switch Manager."}), 403
    if mode() not in {"serial", "virtual"}:
        return jsonify({"ok": False, "error": "O terminal CLI integrado exige QM_MODE=serial e o cabo console conectado a este computador."}), 409
    try:
        owner = session.get("qm_session_id")
        result = cli_session.open(serial_settings(), owner_id=owner)
        if not _session_registry.valid(owner, session.get("qm_user"), _credential_version()):
            cli_session.request_close(result["token"])
            return jsonify({"ok": False, "error": "Sessão encerrada durante a abertura. Entre novamente."}), 401
        session["qm_cli_token"] = result["token"]
        return jsonify({"ok": True, **result})
    except (SerialCLIError, SerialReadOnlyError) as exc:
        status_code = 503 if _session_registry.valid(session.get("qm_session_id"), session.get("qm_user"), _credential_version()) else 401
        return jsonify({"ok": False, "error": str(exc)}), status_code


@app.post("/api/cli/send")
@_serial_exclusive
def cli_send():
    if not cli_enabled():
        return jsonify({"ok": False, "error": "CLI bloqueada."}), 403
    if not _cli_request_allowed():
        return jsonify({"ok": False, "error": "Requisição bloqueada: origem inválida."}), 403
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict): return jsonify({"ok": False, "error": "JSON inválido."}), 400
        result = cli_session.send(payload.get("token"), payload.get("command"))
        return jsonify({"ok": True, **result})
    except SerialCLIError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/cli/close")
@_serial_exclusive
def cli_close():
    if not _cli_request_allowed():
        return jsonify({"ok": False, "error": "Requisição bloqueada: origem inválida."}), 403
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict): return jsonify({"ok": False, "error": "JSON inválido."}), 400
        cli_session.close(payload.get("token"))
        session.pop("qm_cli_token", None)
        if mode() == "serial": physical_dashboard.invalidate()
        return jsonify({"ok": True, "message": "Sessão virtual encerrada." if mode() == "virtual" else "Sessão serial encerrada; porta COM liberada."})
    except SerialCLIError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/status")
def status():
    current_mode = mode()
    if current_mode == "virtual":
        return jsonify({"mode": "virtual", "label": "S3900 Virtual", "ready": True, "message": "SIMULAÇÃO: alterações persistem apenas no switch virtual. Nenhum equipamento físico é acessado."})
    if current_mode == "serial":
        try:
            cfg = serial_settings()
        except SerialReadOnlyError as exc:
            return jsonify({"mode": "serial", "label": "Console serial", "ready": False, "message": str(exc)})
        return jsonify({
            "mode": "serial", "label": "Console serial", "ready": bool(cfg.port),
            "message": f"Console serial em {cfg.port} a {cfg.baud_rate} baud. Diagnósticos são somente leitura; a CLI exige autorização explícita. Feche outros programas que usam a porta antes de conectar.",
        })
    if current_mode != "real":
        return jsonify({"mode": "demo", "label": "Demonstração", "ready": True, "message": "Dados inventados para demonstrar a interface. Nada foi lido do switch."})
    try:
        cfg = switch_settings()
    except SSHReadOnlyError as exc:
        return jsonify({"mode": "real", "label": "Leitura SSH", "ready": False, "message": str(exc)})
    known_hosts_ok = bool(cfg.known_hosts_file and Path(cfg.known_hosts_file).expanduser().is_file())
    key_ok = bool(cfg.key_filename and Path(cfg.key_filename).expanduser().is_file())
    has_one_credential = bool(cfg.password) != bool(cfg.key_filename)
    ready = bool(cfg.host and cfg.username and has_one_credential and known_hosts_ok and (not cfg.key_filename or key_ok))
    return jsonify({
        "mode": "real", "label": "Leitura SSH", "ready": ready,
        "message": "Pronto para consulta somente leitura." if ready else "Configuração incompleta: informe host e usuário, exatamente uma credencial, e um arquivo known_hosts existente nas variáveis QM_*.",
    })


@app.get("/api/virtual/state")
def virtual_state():
    if mode() != "virtual":
        return jsonify({"error": "Disponível somente no modo virtual."}), 409
    try:
        return jsonify(virtual_snapshot())
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/api/demo")
def demo():
    if mode() in {"real", "serial", "virtual"}:
        return jsonify({"error": "Os dados de demonstração estão desativados no modo real."}), 409
    return jsonify({**DEMO_DATA, "mode": "demo", "collected_at": datetime.now(timezone.utc).isoformat()})


@app.post("/api/refresh")
@_serial_exclusive
def refresh():
    current_mode = mode()
    if current_mode == "virtual":
        try:
            outputs = [] if cli_session.connected else collect_serial_diagnostics(serial_settings())
            return jsonify({"mode": "virtual", "ok": True, "message": "Estado do S3900 Virtual atualizado. Nenhum switch físico consultado.", "collected_at": datetime.now(timezone.utc).isoformat(), "outputs": outputs, "snapshot": virtual_snapshot()})
        except (SerialReadOnlyError, OSError, ValueError) as exc:
            return jsonify({"mode": "virtual", "ok": False, "message": str(exc), "outputs": []}), 503
    if current_mode not in {"real", "serial"}:
        return jsonify({
            "mode": "demo", "ok": True,
            "message": "Demonstração atualizada. Os valores continuam sendo fictícios.",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "outputs": [],
        })
    try:
        if current_mode == "serial":
            if cli_session.connected:
                return jsonify({"mode": "serial", "ok": False, "message": "Encerre a sessão CLI antes de abrir uma nova consulta serial.", "collected_at": datetime.now(timezone.utc).isoformat(), "outputs": []}), 409
            cfg = serial_settings()
            payload = request.get_json(silent=True)
            automatic = isinstance(payload, dict) and payload.get("automatic") is True
            reading = physical_dashboard.collect(cfg, force=not automatic)
            return jsonify({
                "mode": "serial", "ok": True,
                "message": "Leitura física concluída. Portas e detalhes de VLAN são coletados em lotes.",
                **reading,
            })
        cfg = switch_settings()
        outputs = collect_diagnostics(cfg)
        return jsonify({
            "mode": "real", "ok": True,
            "message": "Consultas concluídas. Saídas brutas; parser e compatibilidade deste firmware ainda precisam ser validados.",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "outputs": outputs,
        })
    except SSHReadOnlyError as exc:
        return jsonify({
            "mode": "real", "ok": False,
            "message": str(exc),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "outputs": [],
        }), 503
    except SerialReadOnlyError as exc:
        return jsonify({
            "mode": "serial", "ok": False,
            "message": str(exc),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "outputs": [],
        }), 503


@app.get("/api/dashboard")
def dashboard_cache():
    if mode() != "serial":
        return jsonify({"error": "Cache disponível somente no modo serial físico."}), 409
    result = physical_dashboard.cached()
    result["device"] = _device_public(physical_dashboard.device)
    return jsonify({"mode": "serial", "ok": True, "cli_busy": cli_session.connected, "monitor": physical_monitor.status(), **result})


def _admin_only():
    if not oidc_settings.is_admin(_signed_in_user()):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Somente o administrador pode gerenciar o acesso."}), 403
        return "Somente o administrador pode acessar esta página.", 403
    return None


@app.get("/admin")
def admin_page():
    return _admin_only() or _render_manager("admin.html")


@app.get("/api/admin/access")
def admin_access_list():
    denied = _admin_only()
    if denied:
        return denied
    return jsonify({"ok": True, "admin": oidc_settings.admin,
                    "configured": list(oidc_settings.allowed_emails),
                    "entries": access_list.entries(),
                    "requests": access_list.pending(),
                    "google": oidc_settings.issuer == GOOGLE_ISSUER})


@app.post("/api/admin/access")
def admin_access_add():
    denied = _admin_only()
    if denied:
        return denied
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "JSON inválido."}), 400
    try:
        value = access_list.add(payload.get("value"), added_by=_signed_in_user())
    except AccessListError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError:
        return jsonify({"ok": False, "error": "Não foi possível gravar a lista de acesso."}), 503
    return jsonify({"ok": True, "value": value, "entries": access_list.entries()})


def _admin_request_action(action):
    denied = _admin_only()
    if denied:
        return denied
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "JSON inválido."}), 400
    try:
        email = action(payload.get("email"))
    except AccessListError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError:
        return jsonify({"ok": False, "error": "Não foi possível gravar a lista de acesso."}), 503
    return jsonify({"ok": True, "email": email})


@app.post("/api/admin/requests/approve")
def admin_request_approve():
    return _admin_request_action(lambda email: access_list.approve(email, approved_by=_signed_in_user()))


@app.post("/api/admin/requests/reject")
def admin_request_reject():
    return _admin_request_action(access_list.reject)


@app.post("/api/admin/access/remove")
def admin_access_remove():
    denied = _admin_only()
    if denied:
        return denied
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "JSON inválido."}), 400
    try:
        value = access_list.remove(payload.get("value"))
    except AccessListError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError:
        return jsonify({"ok": False, "error": "Não foi possível gravar a lista de acesso."}), 503
    revoked = _revoke_unauthorized_sessions()
    return jsonify({"ok": True, "value": value, "entries": access_list.entries(), "revoked_sessions": revoked})


def _revoke_unauthorized_sessions() -> int:
    """End, server-side, every session whose identity is no longer authorized.

    Without this, a copied cookie that stayed idle during the removal would
    regain access if the same e-mail/domain were later re-added or approved.
    """
    revoked = _session_registry.revoke_where(lambda user, domain: not _authorized(user, domain))
    for session_id in revoked:
        cli_session.request_close_owner(session_id)
    if revoked:
        with _active_sessions_lock:
            for session_id in revoked:
                _active_sessions.pop(session_id, None)
    return len(revoked)


@app.get("/api/device")
def device_info():
    device = current_device()
    return jsonify({"mode": mode(), "device": _device_public(device),
                    "login_required": mode() == "serial" and physical_dashboard.login_required,
                    "console_credentials": console_credentials.available,
                    "console_credentials_armed": console_credentials.armed})


@app.post("/api/console/credentials")
def console_login():
    if mode() != "serial":
        return jsonify({"ok": False, "error": "Disponível somente no modo serial."}), 409
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "JSON inválido."}), 400
    try:
        username, password = validate_credentials(payload.get("username", ""), payload.get("password"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    # Memory only: never written to disk, logs or responses.
    console_credentials.set(username, password)
    physical_dashboard.invalidate()
    return jsonify({"ok": True, "message": "Credenciais recebidas; o próximo ciclo de leitura fará o login no console."})


@app.post("/api/console/credentials/confirm")
def console_confirm():
    """Allow one more automatic login with the credentials already in memory.

    The operator confirms the console cable is still on the same switch; nothing
    on the serial line can prove that before the password is sent."""
    if mode() != "serial":
        return jsonify({"ok": False, "error": "Disponível somente no modo serial."}), 409
    if not console_credentials.confirm():
        return jsonify({"ok": False, "error": "Não há credenciais guardadas. Informe usuário e senha."}), 409
    physical_dashboard.invalidate()
    return jsonify({"ok": True, "message": "Confirmado; o próximo ciclo de leitura fará o login no console."})


@app.post("/api/console/credentials/forget")
def console_forget():
    console_credentials.clear()
    return jsonify({"ok": True, "message": "Credenciais do console descartadas da memória."})


@app.post("/api/config/capabilities/reset")
def configuration_capabilities_reset():
    denied = _admin_only()
    if denied:
        return denied
    device = current_device()
    if device is None:
        return jsonify({"ok": False, "error": "Nenhum switch identificado."}), 409
    switch_capabilities.reset(_capability_key(device))
    return jsonify({"ok": True, "message": f"Operações recusadas por {device.model} liberadas para nova tentativa."})


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "Switch Manager", "mode": mode(), "auth_required": True, "auth": "oidc", "auth_configured": _auth_configured()})


@app.get("/api/connections")
def connections():
    now = time.time()
    with _active_sessions_lock:
        expired = [key for key, (_, last_seen) in _active_sessions.items() if now - last_seen > ACTIVE_SESSION_TTL_SECONDS]
        for key in expired:
            _active_sessions.pop(key, None)
        sessions_by_user: dict[str, list[float]] = {}
        for username, last_seen in _active_sessions.values():
            sessions_by_user.setdefault(username, []).append(last_seen)
    users = [
        {
            "username": username,
            "sessions": len(timestamps),
            "last_activity": datetime.fromtimestamp(max(timestamps), timezone.utc).isoformat(),
        }
        for username, timestamps in sorted(sessions_by_user.items())
    ]
    return jsonify({
        "manager": {"active_sessions": sum(user["sessions"] for user in users), "active_users": len(users), "users": users},
        "switch": {
            "status": "not_collected",
            "devices": None,
            "message": "A leitura de MAC/endereço de porta ainda não foi validada neste firmware. Nenhuma contagem de dispositivos foi inferida.",
        },
        "active_window_seconds": ACTIVE_SESSION_TTL_SECONDS,
    })


@app.get("/api/config/status")
def configuration_status():
    serial_mode = mode() in {"serial", "virtual"}
    enabled = serial_mode and config_writes_enabled()
    device = current_device()
    incompatible = device is not None and not device.profile.guided_config
    validated = _validated_actions(device)
    adapted = device is not None and device.profile.guided_config and not validated >= set(GUIDED_ACTIONS)
    unsupported = _unsupported_actions(device)
    return jsonify({
        "available": serial_mode and not incompatible,
        "writes_enabled": enabled,
        "device": _device_public(device),
        "unsupported_actions": {action: info.get("command") for action, info in unsupported.items()},
        "message": (
            "Configuração virtual habilitada: nenhuma alteração física." if mode() == "virtual" else
            f"{device.model}: configuração guiada indisponível para este sistema. Use a CLI."
            if incompatible else
            f"{device.model}: configuração guiada validada neste switch em {len(validated)} de {len(GUIDED_ACTIONS)} "
            "operações; nas demais, confira cada prévia. Operações que o switch recusar passam para a CLI."
            if adapted and enabled and validated else
            f"{device.model}: configuração guiada adaptada (modelo ainda não validado). Confira cada prévia; "
            "operações que o switch recusar passam para a CLI."
            if adapted and enabled else
            "A configuração por console está habilitada com prévia por operação."
            if enabled else
            "Prévia e validação disponíveis. O envio de mudanças está bloqueado por QM_ENABLE_CONFIG_WRITES=false."
            if serial_mode else
            "A configuração só está disponível pelo console serial local neste perfil."
        ),
        "features": ["VLANs", "Portas access/trunk/hybrid", "Descrição e estado de portas", "Nome do equipamento", "IP da interface VLAN", "Rotas estáticas IPv4", "Backup de configuração", "Salvar configuração"],
    })


@app.post("/api/config/preview")
@_serial_exclusive
def configuration_preview():
    if mode() not in {"serial", "virtual"}:
        return jsonify({"error": "A configuração requer QM_MODE=serial e o cabo console local."}), 409
    if cli_session.connected:
        return jsonify({"ok": False, "error": "Encerre a sessão CLI antes de preparar uma configuração."}), 409
    try:
        payload = request.get_json(silent=True)
        device = current_device() or DEFAULT_DEVICE
        if not device.profile.monitoring:
            return jsonify({"ok": False, "error": "Switch não suportado pela configuração guiada. Use a CLI."}), 409
        if not device.profile.guided_config:
            return jsonify({"ok": False, "error": "Este switch não tem configuração guiada. Use a CLI."}), 409
        action = payload.get("action") if isinstance(payload, dict) else None
        rejected = _unsupported_actions(device).get(action) if isinstance(action, str) else None
        if rejected:
            return jsonify({"ok": False, "cli_only": True,
                            "error": f"Este switch ({device.model}) recusou esta operação antes "
                                     f"(comando \"{rejected.get('command')}\"). Faça-a pela CLI."}), 409
        plan = build_plan(payload, inventory=device.inventory or DEFAULT_DEVICE.inventory,
                          device_identity=device.identity_key)
        if plan.action in {"set_trunk_port", "set_hybrid_port"}:
            plan = prepare_plan(serial_settings(), plan)
        if mode() == "virtual":
            from serial_config import _verify_vlan_preconditions
            from serial_readonly import open_connection
            with open_connection(serial_settings()) as connection:
                _verify_vlan_preconditions(connection, serial_settings(), plan)
        token = issue_preview(plan, owner=session.get("qm_session_id"))
        return jsonify({
            "ok": True, "token": token, "action": plan.action,
            "summary": plan.summary, "commands": list(plan.commands),
            "warnings": ((["SIMULAÇÃO: somente o estado virtual será alterado."] if mode() == "virtual" else [])
                         + ([UNVALIDATED_WARNING] if plan.action not in _validated_actions(device) else [])
                         + list(plan.warnings)),
            "expires_in_seconds": 300,
        })
    except ConfigurationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/config/apply")
@_serial_exclusive
def configuration_apply():
    if mode() not in {"serial", "virtual"}:
        return jsonify({"ok": False, "error": "A configuração requer o modo serial local."}), 409
    if not config_writes_enabled():
        device = current_device()
        if device is not None and not device.profile.guided_config:
            return jsonify({"ok": False, "error": f"{device.model}: configuração guiada indisponível para este sistema. Use a CLI."}), 403
        return jsonify({"ok": False, "error": "Envio de mudanças está bloqueado. Habilite QM_ENABLE_CONFIG_WRITES somente após revisar a prévia e os comandos para o firmware instalado."}), 403
    if cli_session.connected:
        return jsonify({"ok": False, "error": "Encerre a sessão CLI antes de aplicar uma configuração pela área guiada."}), 409
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict): return jsonify({"ok": False, "error": "JSON inválido."}), 400
        plan = consume_preview(payload.get("token"), owner=session.get("qm_session_id"))
        if mode() == "serial": physical_dashboard.invalidate(plan)
        try:
            outputs = execute_plan(serial_settings(), plan)
        except CommandRejected as exc:
            device = current_device()
            if device is not None and exc.command in plan.commands and plan.device_identity == device.identity_key:
                # This model does not accept the operation's syntax: CLI from now on.
                switch_capabilities.mark_unsupported(_capability_key(device), plan.action, exc.command, device.model)
                return jsonify({"ok": False, "cli_only": True,
                                "error": str(exc) + f" Esta operação passou a ser feita pela CLI neste modelo ({device.model})."}), 400
            raise
        return jsonify({
            "ok": True, "summary": plan.summary,
            "message": "Operação aplicada ao switch virtual." if mode() == "virtual" else "Operação enviada. Revise os retornos do switch; as mudanças em execução ainda podem precisar ser salvas.",
            "outputs": outputs,
            **({"snapshot": virtual_snapshot()} if mode() == "virtual" else {}),
        })
    except (ConfigurationError, SerialReadOnlyError, OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/config/backup")
@_serial_exclusive
def configuration_backup():
    if mode() not in {"serial", "virtual"}:
        return jsonify({"ok": False, "error": "O backup requer o modo serial local."}), 409
    if cli_session.connected:
        return jsonify({"ok": False, "error": "Encerre a sessão CLI antes de exportar o backup serial."}), 409
    try:
        content, filename = export_current_configuration(serial_settings())
        return send_file(
            BytesIO(content), mimetype="text/plain; charset=utf-8",
            as_attachment=True, download_name=filename,
            max_age=0,
        )
    except (ConfigurationError, SerialReadOnlyError, OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503


if __name__ == "__main__":
    start_live_monitor()
    # Native is loopback-only. Container publishes host loopback via Compose.
    port = int(os.getenv("QM_WEB_PORT", "8787"))
    host = "0.0.0.0" if os.getenv("QM_CONTAINER") == "1" else "127.0.0.1"
    serve(app, host=host, port=port, threads=8)
