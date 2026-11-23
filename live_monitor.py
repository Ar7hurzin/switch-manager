"""Read-only background polling for the physical S3900 console.

The worker never opens the CLI when an interactive session is active.
The web dashboard only reads the in-memory snapshot, so browser refreshes
cannot multiply serial requests.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


class LiveMonitor:
    def __init__(self, collect, cli_busy, serial_lock, interval=5, retry_interval=15):
        self._collect = collect
        self._cli_busy = cli_busy
        self._serial_lock = serial_lock
        self.interval = interval
        self.retry_interval = retry_interval
        self._stop = threading.Event()
        self._thread = None
        self._state_lock = threading.Lock()
        self._state = "iniciando"
        self._message = "Aguardando primeira leitura do console."
        self._last_attempt = None
        self._last_success = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="s3900-live-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _update(self, state, message, *, attempted=False, succeeded=False):
        with self._state_lock:
            self._state = state
            self._message = message
            if attempted:
                self._last_attempt = _utcnow()
            if succeeded:
                self._last_success = _utcnow()

    def status(self):
        with self._state_lock:
            return {
                "state": self._state,
                "message": self._message,
                "last_attempt": self._last_attempt,
                "last_success": self._last_success,
                "interval_seconds": self.interval,
            }

    def _tick(self):
        if self._cli_busy():
            self._update("pausado", "CLI aberta. Monitoramento pausado para liberar o console.")
            return self.interval
        if not self._serial_lock.acquire(blocking=False):
            self._update("aguardando", "Console ocupado com outra operação. Aguardando.")
            return self.interval
        try:
            if self._cli_busy():
                self._update("pausado", "CLI aberta. Monitoramento pausado.")
                return self.interval
            self._update("consultando", "Lendo o switch pelo console serial.", attempted=True)
            result = self._collect()
            if result.get("stale"):
                self._update("desatualizado", result.get("message") or "Leitura parcial; mantendo últimas medições válidas.")
                # A possible cable swap is confirmed with `display version` on the next
                # poll: do not delay it by the error back-off.
                return self.interval if result.get("identity_recheck") else self.retry_interval
            message = ("Leitura concluída; há recursos pendentes ou antigos. Consulte os horários individuais."
                       if (result.get("snapshot") or {}).get("coverage_partial") else "Leitura automática concluída.")
            self._update("ativo", message, succeeded=True)
            return self.interval
        except Exception as exc:
            # No CLI commands/configuration are ever echoed in browser status.
            self._update("erro", "Falha na leitura do switch: " + str(exc)[:240])
            return self.retry_interval
        finally:
            self._serial_lock.release()

    def _run(self):
        while not self._stop.is_set():
            delay = self._tick()
            self._stop.wait(delay)
