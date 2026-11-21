"""Guided operations a given switch model rejected, so they become CLI-only.

Keyed by the switch's identity (profile + model + firmware): the same model and
firmware rejects the same syntax. Persisted under instance/ so a rejected
operation is not retried after a restart (a retry could leave partial changes).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


def default_path() -> Path:
    settings = Path(os.getenv("QM_SETTINGS_FILE", Path(__file__).with_name("local-settings.ps1"))).expanduser()
    return Path(os.getenv("QM_CAPABILITIES_FILE", settings.parent / "instance" / "switch-capabilities.json")).expanduser()


class SwitchCapabilities:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_path()
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _store(self, data: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".caps-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def unsupported(self, identity: str) -> dict[str, dict]:
        """{action: {"command": rejected command, "model": ..., "at": ...}} for this switch identity."""
        with self._lock:
            entry = self._load().get(identity, {})
            return {k: v for k, v in entry.items() if isinstance(v, dict)} if isinstance(entry, dict) else {}

    def mark_unsupported(self, identity: str, action: str, command: str, model: str) -> None:
        with self._lock:
            data = self._load()
            entry = data.setdefault(identity, {})
            entry[action] = {"command": command, "model": model,
                             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._store(data)

    def reset(self, identity: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(identity, None) is not None:
                self._store(data)
