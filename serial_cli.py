"""One-at-a-time interactive CLI bridge for the local serial console."""
from __future__ import annotations

import re
import secrets
import threading
import time

from serial_readonly import SerialSettings, open_connection
from switch_text import decode_switch_text


class SerialCLIError(RuntimeError):
    """Safe, user-facing terminal session error."""


PROMPT = re.compile(rb"(?m)(?:<[^<>\r\n]+>|\[[^\]\r\n]+\])\s*$")
INPUT_PROMPT = re.compile(rb"(?i)(?:username|user name|login|password|press enter|continue|confirm)[^\r\n:]*:\s*$|\[\s*y\s*/\s*n\s*\]\s*$|\(y/n\)\s*$")
MORE_MARKERS = (b"---- More ----", b"More: ")
MAX_OUTPUT_BYTES = 256_000
MAX_COMMAND_CHARS = 512
IDLE_SECONDS = 20 * 60


class SerialCLISession:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._connection = None
        self._token: str | None = None
        self._last_activity = 0.0
        self._settings: SerialSettings | None = None
        self._idle_timer: threading.Timer | None = None
        self._close_request_lock = threading.Lock()
        self._close_request_token = None
        self._owner_id = None
        self._close_request_owner = None

    @property
    def connected(self) -> bool:
        return self.status()["connected"]

    def status(self, token=None):
        # The cache/status endpoint must not wait for a slow serial response.
        # A busy operation conservatively reserves the console until it finishes.
        if not self._lock.acquire(blocking=False):
            return {"connected": True, "busy": True, "token_valid": None}
        try:
            self._close_if_requested()
            self._expire_if_idle()
            connected = self._connection is not None
            valid = (connected and isinstance(token, str) and token.isascii()
                     and secrets.compare_digest(token, self._token or ""))
            return {"connected": connected, "busy": False, "token_valid": bool(valid)}
        finally:
            self._lock.release()

    def request_close(self, token):
        """Queue owner-only closure without waiting for an in-flight command."""
        if (not isinstance(token, str) or not token.isascii() or not self._token
                or not secrets.compare_digest(token, self._token)):
            return
        with self._close_request_lock:
            self._close_request_token = token
        if self._lock.acquire(blocking=False):
            try:
                self._close_if_requested()
            finally:
                self._lock.release()

    def request_close_owner(self, owner_id):
        """Revoke a web owner even before opening has produced a CLI token."""
        if not isinstance(owner_id, str) or not owner_id:
            return
        with self._close_request_lock:
            if self._owner_id != owner_id:
                return
            self._close_request_owner = owner_id
        if self._lock.acquire(blocking=False):
            try:
                self._close_if_requested()
            finally:
                self._lock.release()

    def _close_if_requested(self):
        # Caller holds the serial lock. A delayed request must never close a
        # newer console which has acquired a different token in the meantime.
        with self._close_request_lock:
            token = self._close_request_token
            owner = self._close_request_owner
            self._close_request_token = None
            self._close_request_owner = None
        if ((token and self._token and secrets.compare_digest(token, self._token))
                or (owner is not None and owner == self._owner_id)):
            self._close_unlocked()

    def _schedule_idle_close(self) -> None:
        if self._idle_timer:
            self._idle_timer.cancel()
        self._idle_timer = threading.Timer(IDLE_SECONDS, self._idle_close)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _idle_close(self) -> None:
        with self._lock:
            self._expire_if_idle()

    def _expire_if_idle(self) -> None:
        if self._connection and time.monotonic() - self._last_activity > IDLE_SECONDS:
            self._close_unlocked()

    def _read_response(self, timeout: int) -> tuple[bytes, bool]:
        started = time.monotonic()
        data = bytearray()
        handled_more = 0
        last_data = started
        while time.monotonic() - started < timeout:
            block = self._connection.read(4096)
            if not block:
                if data and (PROMPT.search(bytes(data).rstrip()) or INPUT_PROMPT.search(bytes(data).rstrip())) and time.monotonic() - last_data >= 0.20:
                    return bytes(data), True
                time.sleep(0.01)
                continue
            last_data = time.monotonic()
            data.extend(block)
            if len(data) > MAX_OUTPUT_BYTES:
                self._close_unlocked()
                raise SerialCLIError("A resposta ultrapassou 256 KB. Encerrei a sessão para liberar o console.")
            raw = bytes(data)
            marker_count = max((raw.count(marker) for marker in MORE_MARKERS), default=0)
            if marker_count > handled_more:
                self._connection.write(b" ")
                handled_more = marker_count
        return bytes(data), False

    @staticmethod
    def _decode(raw: bytes) -> str:
        return decode_switch_text(raw).replace("\r", "").replace("\x00", "")

    def open(self, settings: SerialSettings, *, owner_id=None) -> dict[str, object]:
        with self._lock:
            self._expire_if_idle()
            if self._connection:
                raise SerialCLIError("Já existe uma sessão CLI aberta. Encerre-a antes de abrir outra.")
            try:
                with self._close_request_lock:
                    self._owner_id = owner_id
                self._connection = open_connection(settings)
                self._settings = settings
                self._connection.reset_input_buffer()
                # Blank line only wakes the console prompt; no configuration command is sent.
                self._connection.write(b"\r")
                raw, complete = self._read_response(settings.timeout_seconds)
                self._token = secrets.token_urlsafe(32)
                self._last_activity = time.monotonic()
                self._close_if_requested()
                if self._connection is None:
                    raise SerialCLIError("A sessão web foi encerrada durante a abertura; o console foi liberado.")
                self._schedule_idle_close()
                return {"token": self._token, "output": self._decode(raw), "complete": complete, "port": settings.port, "baud_rate": settings.baud_rate}
            except SerialCLIError:
                self._close_unlocked()
                raise
            except Exception as exc:
                self._close_unlocked()
                raise SerialCLIError(f"Não foi possível abrir {settings.port}. Feche Tera Term/MobaXterm e confira o cabo e a velocidade serial.") from exc

    def send(self, token: object, command: object) -> dict[str, object]:
        with self._lock:
            self._expire_if_idle()
            if not self._connection or not isinstance(token, str) or not token.isascii() or not secrets.compare_digest(token, self._token or ""):
                raise SerialCLIError("A sessão expirou ou foi encerrada. Conecte novamente.")
            if not isinstance(command, str):
                raise SerialCLIError("Informe um único comando de texto.")
            command = command.strip()
            if len(command) > MAX_COMMAND_CHARS:
                raise SerialCLIError("Digite um comando de até 512 caracteres.")
            if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in command):
                raise SerialCLIError("Use somente caracteres ASCII visíveis e envie um comando por vez. Quebras de linha e controles não são aceitos.")
            try:
                self._connection.write(command.encode("ascii") + b"\r")
                raw, complete = self._read_response(self._settings.timeout_seconds)
                self._last_activity = time.monotonic()
                self._schedule_idle_close()
                return {"output": self._decode(raw), "complete": complete, "timed_out": not complete}
            except SerialCLIError:
                raise
            except Exception as exc:
                self._close_unlocked()
                raise SerialCLIError("A comunicação serial falhou. A sessão foi encerrada para liberar a porta COM.") from exc
            finally:
                self._close_if_requested()

    def close(self, token: object | None = None) -> None:
        with self._lock:
            if self._connection and (not isinstance(token, str) or not token.isascii() or not secrets.compare_digest(token, self._token or "")):
                raise SerialCLIError("Token de sessão inválido.")
            self._close_unlocked()

    def shutdown(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        connection, self._connection = self._connection, None
        self._token = None
        with self._close_request_lock:
            self._owner_id = None
        self._settings = None
        self._last_activity = 0.0
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None
        if connection:
            try:
                # Leave the console in the user view: a physical switch keeps the
                # current view after the port closes, and guided plans start there.
                connection.write(b"return\r")
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass


cli_session = SerialCLISession()
