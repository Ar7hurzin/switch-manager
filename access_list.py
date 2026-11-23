"""Who may sign in, managed at runtime by the single administrator.

Stores only e-mail addresses, domains and pending access requests (e-mail and
display name as verified by the SSO provider) — never passwords. Authentication
itself always happens at the SSO provider; this list only authorizes the
identities the provider has verified.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_ENTRIES = 1000
MAX_PENDING_REQUESTS = 200
REJECTION_COOLDOWN_SECONDS = 24 * 60 * 60
EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
DOMAIN = re.compile(r"@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
# A domain entry for a public mail service would admit anyone on the internet.
PUBLIC_DOMAINS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
                  "yahoo.com", "yahoo.com.br", "icloud.com", "me.com", "proton.me", "protonmail.com",
                  "bol.com.br", "uol.com.br", "terra.com.br", "ig.com.br", "aol.com", "gmx.com", "zoho.com"}


class AccessListError(ValueError):
    """Message safe to show to the administrator."""


def normalize(value: object) -> str:
    """Validate an entry: a full e-mail (voce@ufu.br) or a whole domain (@ufu.br)."""
    if not isinstance(value, str):
        raise AccessListError("Informe um e-mail ou domínio.")
    value = value.strip().lower()
    if DOMAIN.fullmatch(value):
        if value[1:] in PUBLIC_DOMAINS:
            raise AccessListError(f"O domínio {value} é público: liberaria qualquer pessoa. Adicione os e-mails individualmente.")
        return value
    if EMAIL.fullmatch(value):
        return value
    raise AccessListError("Use um e-mail completo (voce@ufu.br) ou um domínio inteiro (@ufu.br).")


def normalize_email(value: object) -> str:
    value = normalize(value)
    if value.startswith("@"):
        raise AccessListError("Informe um e-mail completo.")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_path() -> Path:
    settings = Path(os.getenv("QM_SETTINGS_FILE", Path(__file__).with_name("local-settings.ps1"))).expanduser()
    return Path(os.getenv("QM_ACCESS_FILE", settings.parent / "instance" / "access-list.json")).expanduser()


class AccessList:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_path()
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._requests: list[dict] = []
        self._rejected: dict[str, float] = {}
        self._loaded_mtime: int | None = None

    # ----- storage -----------------------------------------------------------
    def _reload_if_changed(self) -> None:
        """Caller holds the lock. Fails closed: an unreadable file grants nobody."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._entries, self._requests, self._rejected, self._loaded_mtime = [], [], {}, None
            return
        if mtime == self._loaded_mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = [e for e in data.get("entries", []) if isinstance(e, dict) and isinstance(e.get("value"), str)]
            self._entries = [dict(e, value=normalize(e["value"])) for e in entries]
        except (OSError, ValueError, AttributeError):
            self._entries = []
            data = {}
        requests = data.get("requests", []) if isinstance(data, dict) else []
        self._requests = []
        for item in requests if isinstance(requests, list) else []:
            try:
                self._requests.append(dict(item, email=normalize_email(item["email"])))
            except (AccessListError, KeyError, TypeError):
                continue
        rejected = data.get("rejected", {}) if isinstance(data, dict) else {}
        self._rejected = {k: float(v) for k, v in rejected.items()
                          if isinstance(k, str) and isinstance(v, (int, float))} if isinstance(rejected, dict) else {}
        self._loaded_mtime = mtime

    def _store(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        cutoff = time.time() - REJECTION_COOLDOWN_SECONDS
        self._rejected = {k: v for k, v in self._rejected.items() if v > cutoff}
        fd, temporary = tempfile.mkstemp(prefix=".access-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"entries": self._entries, "requests": self._requests, "rejected": self._rejected},
                          handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._loaded_mtime = self.path.stat().st_mtime_ns

    # ----- allowlist ---------------------------------------------------------
    def entries(self) -> list[dict]:
        with self._lock:
            self._reload_if_changed()
            return [dict(e) for e in sorted(self._entries, key=lambda e: (not e["value"].startswith("@"), e["value"]))]

    def _add_unlocked(self, value: str, added_by: str) -> None:
        if any(e["value"] == value for e in self._entries):
            raise AccessListError(f"{value} já está na lista.")
        if len(self._entries) >= MAX_ENTRIES:
            raise AccessListError(f"Limite de {MAX_ENTRIES} entradas atingido. Use um domínio.")
        self._entries.append({"value": value, "added_by": added_by, "added_at": _now()})
        self._requests = [r for r in self._requests if r["email"] != value]
        self._rejected.pop(value, None)

    def add(self, value: object, added_by: str) -> str:
        value = normalize(value)
        with self._lock:
            self._reload_if_changed()
            self._add_unlocked(value, added_by)
            self._store()
        return value

    def remove(self, value: object) -> str:
        value = normalize(value)
        with self._lock:
            self._reload_if_changed()
            remaining = [e for e in self._entries if e["value"] != value]
            if len(remaining) == len(self._entries):
                raise AccessListError(f"{value} não está na lista.")
            self._entries = remaining
            self._store()
        return value

    def allows(self, email: str, *, domain_verified_by: str | None) -> bool:
        """domain_verified_by: the organization domain the IdP vouches for (Google 'hd'),
        or None when the provider's e-mail suffix itself is trustworthy."""
        email = email.lower()
        domain = "@" + email.rsplit("@", 1)[-1]
        with self._lock:
            self._reload_if_changed()
            for entry in self._entries:
                value = entry["value"]
                if value == email:
                    return True
                if value.startswith("@") and value == domain:
                    if domain_verified_by is None or domain_verified_by.lower() == value[1:]:
                        return True
        return False

    # ----- access requests ---------------------------------------------------
    def request(self, email: object, name: object = "") -> str:
        """Record a verified identity asking for access. Returns 'pending',
        'already_pending', 'rejected' (cooldown) or 'full'. Never grants access."""
        email = normalize_email(email)
        name = " ".join(str(name or "").split())[:120]
        with self._lock:
            self._reload_if_changed()
            rejected_at = self._rejected.get(email)
            if rejected_at and time.time() - rejected_at < REJECTION_COOLDOWN_SECONDS:
                return "rejected"
            if any(r["email"] == email for r in self._requests):
                return "already_pending"
            if len(self._requests) >= MAX_PENDING_REQUESTS:
                return "full"
            self._requests.append({"email": email, "name": name, "requested_at": _now()})
            self._store()
            return "pending"

    def pending(self) -> list[dict]:
        with self._lock:
            self._reload_if_changed()
            return [dict(r) for r in self._requests]

    def approve(self, email: object, approved_by: str) -> str:
        email = normalize_email(email)
        with self._lock:
            self._reload_if_changed()
            if not any(r["email"] == email for r in self._requests):
                raise AccessListError(f"Não há pedido pendente de {email}.")
            self._add_unlocked(email, approved_by)
            self._store()
        return email

    def reject(self, email: object) -> str:
        email = normalize_email(email)
        with self._lock:
            self._reload_if_changed()
            remaining = [r for r in self._requests if r["email"] != email]
            if len(remaining) == len(self._requests):
                raise AccessListError(f"Não há pedido pendente de {email}.")
            self._requests = remaining
            self._rejected[email] = time.time()
            self._store()
        return email
