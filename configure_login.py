"""Interactively configure SSO (OpenID Connect) login for Switch Manager.

No user password is requested or stored: people sign in at the identity
provider with two-factor authentication. This file only records which
provider and which e-mails are trusted, plus a random session-signing key.
"""
from __future__ import annotations

import getpass
import os
import re
import secrets
import tempfile
from pathlib import Path

from oidc_auth import DEFAULT_MFA_AMR, GOOGLE_ISSUER, OIDCSettings, _csv, microsoft_issuer

ROOT = Path(__file__).resolve().parent
SETTINGS = Path(os.environ.get("QM_SETTINGS_FILE", ROOT / "local-settings.ps1")).expanduser()
# Old password-based keys are removed so no password hash remains on disk.
MANAGED = re.compile(r"\s*\$env:QM_(AUTH_USERNAME|AUTH_PASSWORD_HASH|SECRET_KEY|OIDC_[A-Z_]+)\s*=", re.I)


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


GOOGLE_SECRET = re.compile(r"GOCSPX-[A-Za-z0-9_-]{28}")


def ask_secret(google: bool, attempts: int = 3) -> str | None:
    """Hidden input gives no feedback, so confirm what arrived and catch double pastes."""
    print("  O client secret fica INVISÍVEL ao colar: cole UMA vez (Ctrl+Shift+V no terminal) e aperte Enter.")
    for _ in range(attempts):
        secret = getpass.getpass("Client secret (Enter para cliente público com PKCE): ").strip()
        if not secret:
            if google:
                print("  O Google exige o client secret para aplicativos da Web. Cole a chave secreta do cliente.")
                continue
            return ""
        if google and not GOOGLE_SECRET.fullmatch(secret):
            pasted = secret.count("GOCSPX-")
            reason = (f"o texto contém {pasted} secrets emendados (colado mais de uma vez)" if pasted > 1 else
                      f"recebi {len(secret)} caracteres; o secret do Google tem 35 e começa com GOCSPX-")
            print(f"  Secret inválido: {reason}. Tente de novo.")
            continue
        print(f"  Recebido: {len(secret)} caracteres, terminando em …{secret[-4:]}. Confira com o Google Cloud.")
        return secret
    print("\nConfiguração não gravada: o client secret não foi recebido corretamente.")
    return None


def main() -> int:
    print("Configuração do login SSO (OpenID Connect) do Switch Manager")
    print("Nenhuma senha de usuário é pedida nem gravada: o login e o 2FA acontecem no provedor.\n")
    port = os.getenv("QM_WEB_PORT", "8787").strip() or "8787"
    issuer = ask("URL do emissor (issuer). Enter para contas Google", GOOGLE_ISSUER)
    microsoft = microsoft_issuer(issuer)
    google = issuer == GOOGLE_ISSUER
    if microsoft:
        print("  Microsoft Entra ID detectado: somente contas deste tenant entram, identificadas pelo login corporativo.")
    if google:
        print("  Google detectado: use o ID e a chave secreta do cliente OAuth do tipo 'Aplicativo da Web'.")
    client_id = ask("Client ID do aplicativo registrado no provedor"
                    + (" (ID do aplicativo/cliente)" if microsoft else ""))
    client_secret = ask_secret(google)
    if client_secret is None:
        return 2
    admin = ask("Seu e-mail de login (você será o único administrador)").lower()
    emails = ask("Outros e-mails autorizados, separados por vírgula (opcional: Enter e libere depois pela tela de administração)")
    redirect_uri = ask("Redirect URI cadastrada no provedor", f"http://127.0.0.1:{port}/auth/callback")
    if google:
        # Google ID tokens never state whether 2-Step Verification was used, so
        # the app cannot check it; the operator must vouch for every account.
        print("\n  O Google NÃO informa ao aplicativo se houve verificação em duas etapas (2FA).")
        print("  O 2FA precisa estar ativado em cada conta autorizada (myaccount.google.com → Segurança),")
        print("  ou ser obrigatório pela política do Google Workspace.")
        confirmed = ask("Confirmo que todas as contas autorizadas têm a verificação em duas etapas ativada (s/N)", "N")
        if confirmed.lower() not in {"s", "sim", "y", "yes"}:
            print("\nConfiguração não gravada: ative o 2FA nas contas e rode o configure novamente.")
            return 2
        require_mfa = False
    else:
        require_mfa = ask("Exigir comprovação de 2FA no token? (S/n)", "S").lower() not in {"n", "nao", "não", "no"}

    settings = OIDCSettings(
        issuer=issuer, client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri,
        allowed_emails=tuple(e.lower() for e in _csv(emails)), require_mfa=require_mfa, admin_email=admin,
        # Same defaults as OIDCSettings.from_environment(), derived from the issuer.
        user_claim="preferred_username" if microsoft else "email",
        require_email_verified=not microsoft,
    )
    problems = settings.problems()
    if problems:
        print("\nConfiguração não gravada:")
        for problem in problems:
            print(" - " + problem)
        return 2
    if not require_mfa:
        print("\nATENÇÃO: o app NÃO vai conferir o 2FA no token. Ao autorizar uma conta nova, confirme "
              "antes que ela tem a verificação em duas etapas ativada.")

    existing = SETTINGS.read_text(encoding="utf-8-sig") if SETTINGS.exists() else "# Configurações locais do Switch Manager\n"
    lines = [line for line in existing.splitlines() if not MANAGED.match(line)]
    lines = [line for line in lines
             if not re.match(r"\s*# (Login local do (?:Switch|Quidway) Manager|Login SSO \(OpenID Connect\)|Valores 'amr' aceitos)", line)]
    while lines and not lines[-1].strip():
        lines.pop()
    lines.extend((
        "",
        "# Login SSO (OpenID Connect). Nenhuma senha de usuário é armazenada.",
        f"$env:QM_OIDC_ISSUER = {ps_quote(settings.issuer)}",
        f"$env:QM_OIDC_CLIENT_ID = {ps_quote(settings.client_id)}",
        *( [f"$env:QM_OIDC_CLIENT_SECRET = {ps_quote(settings.client_secret)}"] if settings.client_secret else [] ),
        f"$env:QM_OIDC_REDIRECT_URI = {ps_quote(settings.redirect_uri)}",
        f"$env:QM_OIDC_ADMIN_EMAIL = {ps_quote(settings.admin)}",
        *( [f"$env:QM_OIDC_ALLOWED_EMAILS = {ps_quote(','.join(settings.allowed_emails))}"] if settings.allowed_emails else [] ),
        f"$env:QM_OIDC_REQUIRE_MFA = {ps_quote('true' if require_mfa else 'false')}",
        f"# Valores 'amr' aceitos como 2FA (padrão: {DEFAULT_MFA_AMR}). Keycloak/Authentik podem usar QM_OIDC_ACCEPTED_ACR.",
        f"$env:QM_SECRET_KEY = {ps_quote(secrets.token_urlsafe(48))}",
        "",
    ))
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".qm-login-", dir=SETTINGS.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        os.replace(temporary, SETTINGS)
        if os.name == "posix":
            SETTINGS.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"\nSSO configurado em {SETTINGS.name}. Administrador: {settings.admin}.")
    print("Para liberar outras pessoas ou um domínio inteiro, entre no painel e abra Administração.")
    print(f"Cadastre no provedor a redirect URI: {settings.redirect_uri}")
    print("Escopos usados: openid email profile. Reinicie o Switch Manager: python start.py (Windows: .\\start.ps1).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
