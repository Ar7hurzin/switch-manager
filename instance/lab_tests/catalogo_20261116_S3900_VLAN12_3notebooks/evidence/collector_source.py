"""Opt-in local laboratory capture of sanitized physical dashboard readings.

No switch credentials, login/session cookies or raw CLI responses are stored.
Capture is activated by an expiring marker inside instance/lab_tests/.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "instance" / "lab_tests"
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def record_lab_reading(result: dict, root: Path = ROOT) -> bool:
    active = root / "active.json"
    try:
        if not active.is_file():
            return False
        config = json.loads(active.read_text(encoding="utf-8"))
        identifier = config.get("session_id", "")
        if not isinstance(identifier, str) or not _SESSION.fullmatch(identifier):
            return False
        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(config["expires_utc"].replace("Z", "+00:00"))
        if expires.tzinfo is None or now >= expires:
            return False
        snap = result.get("snapshot")
        if not isinstance(snap, dict):
            return False

        vlans = []
        for vlan in snap.get("vlans") or []:
            if vlan.get("id") not in {1, 12}:
                continue
            vlans.append({
                "id": vlan.get("id"),
                "description": str(vlan.get("name") or "")[:80],
                "ports_tagged": vlan.get("tagged_numbers") or [],
                "ports_untagged": vlan.get("untagged_numbers") or [],
                "ports_confirmed_separately": vlan.get("membership_from_ports") or [],
                "membership_partial": bool(vlan.get("membership_partial")),
                "observed_at": vlan.get("collected_at"),
                "verified_individually": bool(vlan.get("verified_individually")),
            })

        ports = []
        for port in snap.get("ports") or []:
            if port.get("number") not in (1, 2, 3):
                continue
            ports.append({
                "number": port.get("number"),
                "state": port.get("state"),
                "description": str(port.get("description") or "")[:80],
                "mode": port.get("mode"),
                "pvid": port.get("pvid"),
                "rx_bps": port.get("rx_bps"),
                "tx_bps": port.get("tx_bps"),
                "observed_at": port.get("collected_at"),
                "link_observed_at": port.get("link_at"),
            })

        mac_table = snap.get("mac_table") or {}
        mac_records = []
        for entry in mac_table.get("entries") or []:
            if entry.get("port") in (1, 2, 3):
                mac_records.append({
                    "port": entry.get("port"),
                    "vlan": entry.get("vlan"),
                    "mac": entry.get("mac")
                })

        item = {
            "recorded_at": now.isoformat(),
            "snapshot_at": snap.get("collected_at"),
            "stale": bool(result.get("stale")),
            "read_error": str(result.get("message") or "")[:200],
            "switch_model": snap.get("model"),
            "switch_version": snap.get("version"),
            "vlans": vlans,
            "ports": ports,
            "mac_table_observed_at": mac_table.get("collected_at"),
            "mac_table_count": mac_table.get("count"),
            "mac_records": mac_records[:32],
        }
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = root / f"{identifier}.jsonl"
        if target.exists() and target.stat().st_size > 4 * 1024 * 1024:
            return False
        fd = os.open(str(target), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Capture failure must never interrupt real-time monitoring.
        return False
