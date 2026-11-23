"""Console login for switches that ask for a user and password on the serial port.

Credentials typed in the web panel live ONLY in this process's memory. They are
never written to disk, logs or API responses.

Before logging in, nothing on a serial line proves which device is on the other
end: banner and prompt are text the peer itself chooses. So stored credentials
are "armed" for ONE login right after the operator types or confirms them. Any
later login prompt (switch idle timeout, port reopened, another device plugged
into the cable) is NOT answered automatically: the panel asks the operator to
confirm first. Credentials are also discarded when a different switch is
identified, when the operator clicks "Esquecer credenciais", after a rejected
login, or when the program stops.
"""
from __future__ import annotations

import re
import threading

from serial_readonly import PROMPT, SerialReadOnlyError, _read_until_prompt

USERNAME_PROMPT = re.compile(rb"(?i)(?:username|user name|login)[ \t]*:[ \t]*$")
PASSWORD_PROMPT = re.compile(rb"(?i)password[ \t]*:[ \t]*$")
AUTH_FAILED = re.compile(rb"(?i)(?:authentication failed|login failed|password is wrong|wrong password|"
                         rb"invalid (?:user|password)|login incorrect|access denied)")


class ConsoleLoginRequired(SerialReadOnlyError):
    """The console asks for credentials that have not been provided (or confirmed) yet."""

    def __init__(self, stored=False):
        self.stored = stored
        if stored:
            super().__init__("O console pediu login de novo. Por segurança, as credenciais guardadas só são "
                             "reenviadas depois que você confirmar no painel que o switch é o mesmo.")
        else:
            super().__init__("O console do switch pede usuário e senha. Informe-os no painel para continuar.")


class ConsoleLoginFailed(SerialReadOnlyError):
    """The switch rejected the credentials; they were discarded."""

    def __init__(self):
        super().__init__("O switch recusou o usuário ou a senha do console. As credenciais foram descartadas; "
                         "informe-as novamente.")


class ConsoleCredentials:
    def __init__(self):
        self._lock = threading.Lock()
        self._value: tuple[str, str] | None = None
        self._bound: str | None = None
        self._armed = False

    def set(self, username: str, password: str) -> None:
        with self._lock:
            self._value = (username, password)
            self._bound = None
            self._armed = True

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._bound = None
            self._armed = False

    def confirm(self) -> bool:
        """The operator confirmed the cable is on the same switch: allow ONE more login."""
        with self._lock:
            self._armed = self._value is not None
            return self._armed

    def take_for_login(self) -> tuple[str, str] | None:
        """Credentials for one login attempt, or None if absent or not confirmed."""
        with self._lock:
            if self._value is None or not self._armed:
                return None
            self._armed = False
            return self._value

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._value is not None and self._armed

    def bind(self, fingerprint: str) -> None:
        """Tie credentials to the first switch identified after they were typed;
        discard them as soon as a different switch is identified. This runs after
        login, so it does not protect the login itself; the one-shot arming does."""
        with self._lock:
            if self._value is None:
                return
            if self._bound is None:
                self._bound = fingerprint
            elif self._bound != fingerprint:
                self._value = None
                self._bound = None

    def get(self) -> tuple[str, str] | None:
        with self._lock:
            return self._value

    @property
    def available(self) -> bool:
        return self.get() is not None

    def __repr__(self) -> str:  # never reveal the secret in tracebacks or logs
        return f"<ConsoleCredentials available={self.available}>"


console_credentials = ConsoleCredentials()


def _tail(raw: bytes) -> bytes:
    return raw.rstrip()


def needs_login(raw: bytes) -> bool:
    tail = _tail(raw)
    return bool(USERNAME_PROMPT.search(tail) or PASSWORD_PROMPT.search(tail)) and not PROMPT.search(tail)


def open_console(connection, timeout_seconds: int, credentials: ConsoleCredentials = console_credentials,
                 reader=None) -> bytes:
    """Wake the console and, if it asks, log in. Returns the output up to the VRP prompt.

    reader(connection, timeout, accept_login=True) defaults to serial_readonly's reader;
    callers pass their own so tests can substitute the serial transport.
    """
    read = reader or _read_until_prompt
    connection.reset_input_buffer()
    connection.write(b"\r")
    raw = read(connection, timeout_seconds, accept_login=True)
    sent_password = False
    value = None
    for _ in range(4):
        if not needs_login(raw):
            return raw
        if sent_password and (AUTH_FAILED.search(raw) or needs_login(raw)):
            credentials.clear()
            raise ConsoleLoginFailed()
        if value is None:
            value = credentials.take_for_login()
            if value is None:
                raise ConsoleLoginRequired(stored=credentials.available)
        username, password = value
        if USERNAME_PROMPT.search(_tail(raw)):
            connection.write(username.encode("utf-8") + b"\r")
        else:
            connection.write(password.encode("utf-8") + b"\r")
            sent_password = True
        raw = read(connection, timeout_seconds, accept_login=True)
    credentials.clear()
    raise ConsoleLoginFailed()


def validate_credentials(username: object, password: object) -> tuple[str, str]:
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("Informe usuário e senha do console.")
    username = username.strip()
    if not password or len(username) > 64 or len(password) > 128:
        raise ValueError("Informe a senha (e o usuário, se o switch pedir), com até 64/128 caracteres.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in username + password):
        raise ValueError("Usuário e senha não podem conter quebras de linha ou caracteres de controle.")
    return username, password
