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
_PORT_FIELDS = ("state", "description", "mode", "pvid", "rx_bps", "tx_bps")


def _field_freshness(source, fields, now, max_age):
    """Retain only observation metadata; never turn a cache time into a sample."""
    timestamps, stale = {}, {}
    supplied = source.get("field_observed_at") or {}
    flags = source.get("field_stale") or {}
    for field in fields:
        value = supplied.get(field)
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("Timestamp without timezone")
            timestamps[field] = value
            old = (now - stamp).total_seconds() > max_age
        except (AttributeError, TypeError, ValueError):
            timestamps[field], old = None, True
        stale[field] = bool(source.get("read_error")) or bool(flags.get(field)) or old
    return {"field_observed_at": timestamps, "field_stale": stale}



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

        max_age = snap.get("stale_after_seconds", 180)
        if not isinstance(max_age, (int, float)) or not 0 < max_age <= 3600:
            max_age = 180
        vlans = []
        for vlan in snap.get("vlans") or []:
            if vlan.get("id") not in {1, 12}:
                continue
            freshness = _field_freshness(vlan, ("membership", "name"), now, max_age)
            membership_stale = bool(vlan.get("membership_stale")) or bool(vlan.get("membership_partial")) or freshness["field_stale"]["membership"]
            vlans.append({
                "id": vlan.get("id"),
                "description": str(vlan.get("name") or "")[:80],
                "ports_tagged": vlan.get("effective_tagged_numbers", vlan.get("tagged_numbers")) or [],
                "ports_untagged": vlan.get("effective_untagged_numbers", vlan.get("untagged_numbers")) or [],
                "ports_confirmed_separately": vlan.get("membership_from_ports") or [],
                "membership_partial": bool(vlan.get("membership_partial")),
                "observed_at": vlan.get("collected_at"),
                "verified_individually": bool(vlan.get("verified_individually")),
                "stale": bool(vlan.get("stale")) or membership_stale or (vlan.get("name") is not None and freshness["field_stale"]["name"]),
                "membership_stale": membership_stale,
                **freshness,
            })

        ports = []
        for port in snap.get("ports") or []:
            if port.get("number") not in (1, 2, 3):
                continue
            freshness = _field_freshness(port, _PORT_FIELDS, now, max_age)
            known = [field for field in _PORT_FIELDS if port.get(field) is not None]
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
                "stale": bool(port.get("stale")) or not known or any(freshness["field_stale"][field] for field in known),
                **freshness,
            })

        mac_table = snap.get("mac_table") or {}
        mac_freshness = _field_freshness({
            "field_observed_at": {"table": mac_table.get("collected_at")},
            "field_stale": {"table": bool(mac_table.get("stale"))},
            "read_error": mac_table.get("read_error"),
        }, ("table",), now, max_age)
        mac_stale = mac_freshness["field_stale"]["table"]
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
            "collection_stale": bool(result.get("stale")),
            "coverage_partial": bool(snap.get("coverage_partial")) or not mac_table
                                or any(port["stale"] for port in ports) or any(vlan["stale"] for vlan in vlans),
            "stale": bool(result.get("stale")) or bool(snap.get("coverage_partial"))
                     or any(port["stale"] for port in ports) or any(vlan["stale"] for vlan in vlans)
                     or mac_stale,
            "read_error": str(result.get("message") or "")[:200],
            "switch_model": snap.get("model"),
            "switch_version": snap.get("version"),
            "vlans": vlans,
            "ports": ports,
            "mac_table_observed_at": mac_table.get("collected_at"),
            "mac_table_count": mac_table.get("count"),
            "mac_table_stale": mac_stale,
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
