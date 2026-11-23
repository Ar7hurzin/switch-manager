"""OpenID Connect single sign-on for the local Switch Manager.

The browser authenticates at the identity provider (IdP). This server never
receives, stores or hashes a user password: it only verifies a signed ID token
obtained with the Authorization Code flow + PKCE, and accepts it only when the
IdP asserts a second factor and the e-mail is on the local allowlist.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import jwt

log = logging.getLogger("quidway.oidc")


class OIDCError(RuntimeError):
    """Login refused. The message is safe to show in the browser."""


class OIDCNotAuthorized(OIDCError):
    """Fully verified identity (signature, nonce, e-mail, 2FA) that is not on the allowlist."""

    def __init__(self, identity: dict):
        super().__init__("Esta conta não está autorizada neste Switch Manager.")
        self.identity = identity


# Only asymmetric signatures: a symmetric/"none" token could be forged with
# the client secret or without any key at all.
ASYMMETRIC_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                         "ES256", "ES384", "ES512", "EdDSA")
# RFC 8176 values that prove more than a single password factor.
DEFAULT_MFA_AMR = "mfa,otp,totp,hwk,fido,webauthn"
SCOPES = "openid email profile"
STATE_TTL_SECONDS = 10 * 60
HTTP_TIMEOUT_SECONDS = 10
DISCOVERY_TTL_SECONDS = 60 * 60
JWKS_REFRESH_COOLDOWN_SECONDS = 60
CLOCK_LEEWAY_SECONDS = 60
MAX_RESPONSE_BYTES = 1_000_000
MICROSOFT_HOST = "login.microsoftonline.com"
GOOGLE_ISSUER = "https://accounts.google.com"


def microsoft_issuer(issuer: str) -> bool:
    try:
        return (urlsplit(issuer).hostname or "").lower() == MICROSOFT_HOST
    except ValueError:
        return False


def _csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def _flag(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "sim", "s"}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _url_problem(url: str, label: str, *, allow_loopback_http: bool) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"{label} não é uma URL válida."
    if not parts.netloc or parts.fragment or parts.username or parts.password:
        return f"{label} precisa ser uma URL absoluta, sem fragmento nem credenciais."
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and allow_loopback_http and _loopback(parts.hostname):
        return None
    return f"{label} precisa usar https (http só é aceito em 127.0.0.1/localhost)."


@dataclass(frozen=True)
class OIDCSettings:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    allowed_emails: tuple[str, ...]
    require_mfa: bool = True
    mfa_amr: tuple[str, ...] = _csv(DEFAULT_MFA_AMR)
    accepted_acr: tuple[str, ...] = ()
    acr_values: str = ""
    provider_logout: bool = False
    # Claim compared with the allowlist, and whether the IdP must assert it is verified.
    # Microsoft Entra ID work accounts carry no email_verified claim: they are
    # identified by preferred_username (UPN) inside a single, fixed tenant.
    user_claim: str = "email"
    require_email_verified: bool = True
    # The single administrator: always allowed, and the only one who manages the
    # runtime access list. Defaults to the first e-mail in QM_OIDC_ALLOWED_EMAILS.
    admin_email: str = ""

    @property
    def admin(self) -> str:
        return (self.admin_email or next(iter(self.allowed_emails), "")).lower()

    @classmethod
    def from_environment(cls) -> "OIDCSettings":
        port = os.getenv("QM_WEB_PORT", "8787").strip() or "8787"
        issuer = os.getenv("QM_OIDC_ISSUER", "").strip()
        microsoft = microsoft_issuer(issuer)
        return cls(
            issuer=issuer,
            user_claim=os.getenv("QM_OIDC_USER_CLAIM", "").strip() or ("preferred_username" if microsoft else "email"),
            require_email_verified=_flag(os.getenv("QM_OIDC_REQUIRE_EMAIL_VERIFIED"), not microsoft),
            client_id=os.getenv("QM_OIDC_CLIENT_ID", "").strip(),
            client_secret=os.getenv("QM_OIDC_CLIENT_SECRET", "").strip(),
            redirect_uri=(os.getenv("QM_OIDC_REDIRECT_URI", "").strip()
                          or f"http://127.0.0.1:{port}/auth/callback"),
            allowed_emails=tuple(email.lower() for email in _csv(os.getenv("QM_OIDC_ALLOWED_EMAILS", ""))),
            require_mfa=_flag(os.getenv("QM_OIDC_REQUIRE_MFA"), True),
            mfa_amr=tuple(v.lower() for v in _csv(os.getenv("QM_OIDC_MFA_AMR", "") or DEFAULT_MFA_AMR)),
            accepted_acr=_csv(os.getenv("QM_OIDC_ACCEPTED_ACR", "")),
            acr_values=os.getenv("QM_OIDC_ACR_VALUES", "").strip(),
            provider_logout=_flag(os.getenv("QM_OIDC_PROVIDER_LOGOUT"), False),
            admin_email=os.getenv("QM_OIDC_ADMIN_EMAIL", "").strip().lower(),
        )

    def problems(self) -> list[str]:
        """Offline validation: never touches the network."""
        found = []
        if not self.issuer:
            found.append("Defina QM_OIDC_ISSUER.")
        else:
            problem = _url_problem(self.issuer, "QM_OIDC_ISSUER", allow_loopback_http=True)
            if problem or urlsplit(self.issuer).query:
                found.append(problem or "QM_OIDC_ISSUER não pode ter parâmetros de consulta.")
            elif microsoft_issuer(self.issuer):
                segments = [s for s in urlsplit(self.issuer).path.split("/") if s]
                # The discovery document always names the tenant by its GUID, so a
                # domain here would never match; shared tenants admit any organization.
                if (len(segments) != 2 or segments[1] != "v2.0"
                        or not re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", segments[0])):
                    found.append("Para Microsoft, use o issuer do seu tenant com o ID (GUID): "
                                 "https://login.microsoftonline.com/<ID-do-tenant>/v2.0. "
                                 "Não use common/organizations/consumers nem o domínio da empresa.")
        if self.user_claim not in {"email", "preferred_username", "upn"}:
            found.append("QM_OIDC_USER_CLAIM deve ser email, preferred_username ou upn.")
        if not self.client_id:
            found.append("Defina QM_OIDC_CLIENT_ID.")
        problem = _url_problem(self.redirect_uri, "QM_OIDC_REDIRECT_URI", allow_loopback_http=True)
        if problem:
            found.append(problem)
        elif urlsplit(self.redirect_uri).path != "/auth/callback":
            found.append("QM_OIDC_REDIRECT_URI precisa terminar em /auth/callback.")
        if not self.admin:
            found.append("Defina QM_OIDC_ADMIN_EMAIL com o e-mail do administrador.")
        elif not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", self.admin):
            found.append("QM_OIDC_ADMIN_EMAIL deve ser um e-mail completo.")
        if any(not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", email) for email in self.allowed_emails):
            found.append("QM_OIDC_ALLOWED_EMAILS deve conter e-mails completos separados por vírgula "
                         "(domínios inteiros são liberados pela tela de administração).")
        if self.require_mfa and not self.mfa_amr and not self.accepted_acr:
            found.append("Com QM_OIDC_REQUIRE_MFA=true, informe QM_OIDC_MFA_AMR ou QM_OIDC_ACCEPTED_ACR.")
        for value in (self.issuer, self.client_id, self.client_secret, self.redirect_uri, self.acr_values):
            if any(ord(ch) < 0x20 for ch in value):
                found.append("As variáveis QM_OIDC_* não podem conter caracteres de controle.")
                break
        return found

    def configured(self) -> bool:
        return not self.problems()

    def version(self) -> str:
        """Changes whenever who may sign in changes, revoking existing sessions."""
        material = "\0".join((self.issuer, self.client_id, self.redirect_uri, self.admin,
                              ",".join(sorted(self.allowed_emails)), str(self.require_mfa),
                              self.user_claim, str(self.require_email_verified),
                              ",".join(sorted(self.mfa_amr)), ",".join(sorted(self.accepted_acr))))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def email_allowed(self, email: object) -> bool:
        """Static authorization from configuration: the administrator plus QM_OIDC_ALLOWED_EMAILS."""
        return isinstance(email, str) and (email.lower() == self.admin or email.lower() in self.allowed_emails)

    def is_admin(self, email: object) -> bool:
        return isinstance(email, str) and bool(self.admin) and email.lower() == self.admin


class _SecureRedirects(HTTPRedirectHandler):
    """Follow provider redirects only to https (or http on loopback, for a local test IdP)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _url_problem(newurl, "redirect", allow_loopback_http=True) is not None:
            raise HTTPError(newurl, code, "Redirecionamento inseguro recusado.", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_SecureRedirects())


def _http_json(url: str, *, form: dict | None = None, basic_auth: tuple[str, str] | None = None) -> dict:
    """Single network primitive (patched by tests). Errors never leak details to the browser."""
    headers = {"Accept": "application/json", "User-Agent": "SwitchManager-OIDC/1"}
    body = None
    if form is not None:
        body = urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if basic_auth:
        pair = quote_plus(basic_auth[0]) + ":" + quote_plus(basic_auth[1])
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("ascii")).decode("ascii")
    request = Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with _OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        # OAuth error codes (invalid_client, invalid_grant…) carry no secrets and explain the failure.
        try:
            code = str(json.loads(exc.read(4096)).get("error", ""))[:60]
        except (ValueError, AttributeError, OSError):
            code = ""
        log.warning("Provedor OIDC respondeu HTTP %s em %s %s", exc.code, urlsplit(url).path, code)
        raise OIDCError("O provedor de login recusou a solicitação. Tente entrar novamente.") from exc
    except (URLError, OSError, ValueError) as exc:
        log.warning("Falha de rede com o provedor OIDC: %s", exc)
        raise OIDCError("Não foi possível falar com o provedor de login. Confira a rede e tente novamente.") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise OIDCError("Resposta do provedor de login excedeu o limite seguro.")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise OIDCError("Resposta do provedor de login não reconhecida.") from exc
    if not isinstance(data, dict):
        raise OIDCError("Resposta do provedor de login não reconhecida.")
    return data


class OIDCClient:
    def __init__(self, settings: OIDCSettings, authorize=None):
        self.settings = settings
        # authorize(email, org_domain) -> bool decides who may enter; the default
        # uses only the static configuration (administrator + QM_OIDC_ALLOWED_EMAILS).
        self.authorize = authorize or (lambda email, org_domain: settings.email_allowed(email))
        self._lock = threading.Lock()
        self._discovery: dict | None = None
        self._discovery_at = 0.0
        self._jwks: dict | None = None
        self._jwks_at = 0.0

    # ----- provider metadata -------------------------------------------------
    def _endpoint_allowed(self, url: object) -> bool:
        issuer_is_loopback = _loopback(urlsplit(self.settings.issuer).hostname)
        return isinstance(url, str) and _url_problem(url, "endpoint", allow_loopback_http=issuer_is_loopback) is None

    def discovery(self) -> dict:
        with self._lock:
            if self._discovery and time.monotonic() - self._discovery_at < DISCOVERY_TTL_SECONDS:
                return self._discovery
        document = _http_json(self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if document.get("issuer") != self.settings.issuer:
            log.warning("Issuer do discovery (%r) difere de QM_OIDC_ISSUER", document.get("issuer"))
            raise OIDCError("O emissor informado pelo provedor não confere com QM_OIDC_ISSUER.")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not self._endpoint_allowed(document.get(key)):
                raise OIDCError("O provedor de login publicou endpoints inválidos ou sem https.")
        if not self._endpoint_allowed(document.get("end_session_endpoint")):
            document.pop("end_session_endpoint", None)
        with self._lock:
            self._discovery, self._discovery_at = document, time.monotonic()
        return document

    def _jwk_set(self, *, refresh: bool) -> dict:
        with self._lock:
            cached, fetched_at = self._jwks, self._jwks_at
        if cached is not None and (not refresh or time.monotonic() - fetched_at < JWKS_REFRESH_COOLDOWN_SECONDS):
            return cached
        document = _http_json(self.discovery()["jwks_uri"])
        if not isinstance(document.get("keys"), list):
            raise OIDCError("As chaves públicas do provedor de login não foram reconhecidas.")
        with self._lock:
            self._jwks, self._jwks_at = document, time.monotonic()
        return document

    def _signing_key(self, kid: object, algorithm: str):
        for refresh in (False, True):  # second pass handles provider key rotation
            candidates = []
            for entry in self._jwk_set(refresh=refresh)["keys"]:
                if not isinstance(entry, dict) or entry.get("use", "sig") != "sig":
                    continue
                if kid is not None and entry.get("kid") != kid:
                    continue
                try:
                    candidates.append(jwt.PyJWK(entry, algorithm=algorithm).key)
                except (jwt.PyJWKError, jwt.InvalidKeyError, TypeError, ValueError):
                    continue
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise OIDCError("Chave de assinatura ambígua no provedor de login.")
        raise OIDCError("A chave que assinou o login não foi encontrada no provedor.")

    # ----- authorization flow ------------------------------------------------
    def begin(self) -> tuple[str, dict]:
        """Return the IdP URL and the one-time values the session must keep."""
        endpoint = self.discovery()["authorization_endpoint"]
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        params = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "scope": SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
            "code_challenge_method": "S256",
        }
        if self.settings.acr_values:
            params["acr_values"] = self.settings.acr_values
        url = endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)
        return url, {"state": state, "nonce": nonce, "verifier": verifier, "created": time.time()}

    def complete(self, pending: object, args) -> dict:
        """Validate the callback and return the verified identity."""
        if not isinstance(pending, dict) or not all(isinstance(pending.get(k), str) for k in ("state", "nonce", "verifier")):
            raise OIDCError("Login expirado ou iniciado em outra aba. Clique em Entrar novamente.")
        state = args.get("state")
        if not (isinstance(state, str) and state.isascii()
                and hmac.compare_digest(state, pending["state"])):
            raise OIDCError("Login expirado ou iniciado em outra aba. Clique em Entrar novamente.")
        created = pending.get("created")
        if not isinstance(created, (int, float)) or not 0 <= time.time() - created <= STATE_TTL_SECONDS:
            raise OIDCError("O login demorou mais de 10 minutos. Clique em Entrar novamente.")
        if args.get("error"):
            log.info("Provedor OIDC negou o login: %s", str(args.get("error"))[:80])
            raise OIDCError("O provedor de login não autorizou o acesso.")
        code = args.get("code")
        if not isinstance(code, str) or not code or len(code) > 4096:
            raise OIDCError("Resposta do provedor de login incompleta.")

        document = self.discovery()
        form = {"grant_type": "authorization_code", "code": code,
                "redirect_uri": self.settings.redirect_uri, "code_verifier": pending["verifier"]}
        basic_auth = None
        if self.settings.client_secret:
            methods = document.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
            if "client_secret_basic" in methods or "client_secret_post" not in methods:
                basic_auth = (self.settings.client_id, self.settings.client_secret)
            else:
                form.update(client_id=self.settings.client_id, client_secret=self.settings.client_secret)
        else:
            form["client_id"] = self.settings.client_id
        tokens = _http_json(document["token_endpoint"], form=form, basic_auth=basic_auth)
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OIDCError("O provedor de login não devolveu um ID token.")
        return self.validate_id_token(id_token, pending["nonce"])

    def validate_id_token(self, token: str, nonce: str) -> dict:
        document = self.discovery()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OIDCError("Token de login inválido.") from exc
        supported = document.get("id_token_signing_alg_values_supported") or ["RS256"]
        algorithm = header.get("alg")
        if algorithm not in ASYMMETRIC_ALGORITHMS or algorithm not in supported:
            raise OIDCError("Token de login com assinatura não permitida.")
        key = self._signing_key(header.get("kid"), algorithm)
        try:
            claims = jwt.decode(
                token, key=key, algorithms=[algorithm],
                audience=self.settings.client_id, issuer=self.settings.issuer,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            log.info("ID token rejeitado: %s", type(exc).__name__)
            raise OIDCError("Token de login inválido ou expirado.") from exc

        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        azp = claims.get("azp")
        if (len(audiences) > 1 or azp is not None) and azp != self.settings.client_id:
            raise OIDCError("Token de login emitido para outro aplicativo.")
        supplied_nonce = claims.get("nonce")
        if not (isinstance(supplied_nonce, str) and supplied_nonce.isascii()
                and hmac.compare_digest(supplied_nonce, nonce)):
            raise OIDCError("Token de login não corresponde a esta tentativa.")

        email = claims.get(self.settings.user_claim)
        if not isinstance(email, str) or not email:
            raise OIDCError("O provedor não informou o e-mail/login desta conta.")
        if self.settings.require_email_verified and claims.get("email_verified") not in (True, "true"):
            raise OIDCError("O provedor não confirmou o e-mail desta conta.")
        # 2FA is checked before authorization so that only a fully verified
        # identity can ever be recorded as an access request.
        if not self.mfa_satisfied(claims):
            raise OIDCError("O provedor não confirmou a autenticação de dois fatores. "
                            "Ative o 2FA na sua conta e entre novamente.")
        org_domain = self.organization_domain(claims)
        identity = {"email": email.lower(), "subject": str(claims["sub"]),
                    "name": str(claims.get("name") or "")[:120], "org_domain": org_domain}
        if not self.authorize(identity["email"], org_domain):
            log.warning("Login SSO recusado para conta fora da allowlist")
            raise OIDCNotAuthorized(identity)
        return identity

    def organization_domain(self, claims: dict):
        """Which domain the provider vouches the account belongs to, for '@domain' entries.

        Google: only the Workspace 'hd' claim proves domain membership (a personal
        Google account can carry any verified external address), so '' when absent.
        Other providers: None, meaning the verified e-mail/UPN suffix is trusted
        (Microsoft is already restricted to a single tenant).
        """
        if self.settings.issuer == GOOGLE_ISSUER:
            hd = claims.get("hd")
            return hd.lower() if isinstance(hd, str) else ""
        return None

    def mfa_satisfied(self, claims: dict) -> bool:
        if not self.settings.require_mfa:
            return True
        amr = claims.get("amr")
        if isinstance(amr, str):
            amr = [amr]
        if isinstance(amr, list) and {str(v).lower() for v in amr} & set(self.settings.mfa_amr):
            return True
        acr = claims.get("acr")
        return isinstance(acr, str) and acr in self.settings.accepted_acr

    def logout_url(self) -> str | None:
        """RP-initiated logout, only from cached metadata so logout never waits on the network."""
        if not self.settings.provider_logout:
            return None
        with self._lock:
            endpoint = (self._discovery or {}).get("end_session_endpoint")
        if not endpoint:
            return None
        parts = urlsplit(self.settings.redirect_uri)
        params = {"client_id": self.settings.client_id,
                  "post_logout_redirect_uri": f"{parts.scheme}://{parts.netloc}/login"}
        return endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)
