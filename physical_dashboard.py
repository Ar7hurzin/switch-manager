"""Read-only physical S3900 dashboard. Unknown observations stay unknown."""
from __future__ import annotations

import copy
import re
import time
import threading
from datetime import datetime, timezone

from serial_readonly import (SerialReadOnlyError, _read_until_prompt, _clean_output,
                             open_connection)
from serial_config import _parse_vlan_inventory
from switch_config import ConfigurationError
from switch_text import command_rejected, decode_switch_text
from switch_profiles import (PHYSICAL_INTERFACE, S3900_INVENTORY, identify, parse_interface_list)
from console_auth import ConsoleLoginFailed, ConsoleLoginRequired, console_credentials, open_console

INTERVAL = 4
STALE_AFTER_SECONDS = 180
IDENTITY_RECHECK_SECONDS = 60
PROMPT_NAME = __import__("re").compile(r"(?:<([^<>\r\n]+)>|\[([^\]\r\n]+)\])\s*$")
PORT_FIELDS = ("state", "admin", "pvid", "mode", "description", "tagged", "untagged", "rx_bps", "tx_bps")


def now():
    return datetime.now(timezone.utc).isoformat()


def interface_name(n, inventory=S3900_INVENTORY):
    return inventory.name(n)


def prompt_name(raw):
    """Switch name from a trailing user-view <Name> prompt, or None.

    System-view prompts ([Name], [Name-Ethernet0/1]) are ignored: a name with a
    hyphen cannot be told apart from a subview, so they never signal a new switch.
    """
    match = PROMPT_NAME.search(terminal_text(decode_switch_text(raw) if isinstance(raw, bytes) else raw).rstrip())
    return match.group(1) if match and match.group(1) else None


def terminal_text(text):
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "").replace("\x00", "")


def _field(text, pattern):
    m = re.search(pattern, text, re.I | re.M)
    return m.group(1).strip() if m else None


def parse_identity(text):
    """Identity fields of a monitored switch; unsupported systems are refused."""
    device = identify(text)
    if not device.profile.monitoring:
        raise SerialReadOnlyError("Identificação incompleta ou sistema não suportado (somente VRP 3 / Comware 3); "
                                  "não vou atribuir dados a outro modelo.")
    return {"model": device.model, "version": device.version, "hardware": device.hardware,
            "cpld": device.cpld, "bootrom": device.bootrom}


def parse_port(text, number, inventory=S3900_INVENTORY):
    text = terminal_text(text)
    interface = interface_name(number, inventory)
    headers = re.findall(
        r"(?mi)^[ \t]*" + PHYSICAL_INTERFACE.pattern + r"[ \t]+"
        r"(?:current[ \t]+state[ \t]*:|is)[ \t]*", text)
    # Field labels have no interface identity of their own. Require the exact
    # section and reject mixed/late responses before associating any field.
    if not headers or any(name.lower() != interface.lower() for name in headers):
        return None
    link = _field(text, rf"^[ \t]*{re.escape(interface)}[ \t]+(?:current[ \t]+state[ \t]*:|is)[ \t]*(UP|DOWN|Administratively[ \t]+DOWN)\b")
    pvid = _field(text, r"\bPVID[ \t]*:[ \t]*(\d{1,4})\b")
    mode = _field(text, r"\bPort[ \t]+link-type[ \t]*:[ \t]*(access|trunk|hybrid)\b")
    desc = _field(text, r"^[ \t]*Description[ \t]*:[ \t]*([^\n]*)")
    tagged = _field(text, r"^[ \t]*Tagged[ \t]+VLAN[ \t]+ID[ \t]*:[ \t]*([^\n]+)")
    untagged = _field(text, r"^[ \t]*Untagged[ \t]+VLAN[ \t]+ID[ \t]*:[ \t]*([^\n]+)")
    rx = _field(text, r"^[ \t]*Last[ \t]+\d+[ \t]+seconds[ \t]+input:[ \t]*\d+[ \t]+packets/sec[ \t]+(\d+)[ \t]+bytes/sec")
    tx = _field(text, r"^[ \t]*Last[ \t]+\d+[ \t]+seconds[ \t]+output:[ \t]*\d+[ \t]+packets/sec[ \t]+(\d+)[ \t]+bytes/sec")
    if not any(v is not None for v in (link, pvid, mode, desc, tagged, untagged, rx, tx)):
        return None
    return {"number": number, "interface": interface,
            "state": ("up" if link.lower() == "up" else "down") if link else None,
            "admin": (False if "administratively" in link.lower() else True if link.lower() == "up" else None) if link else None,
            "pvid": int(pvid) if pvid and 1 <= int(pvid) <= 4094 else None,
            "mode": mode.lower() if mode else None, "description": desc,
            "tagged": tagged, "untagged": untagged,
            "rx_bps": int(rx)*8 if rx is not None else None,
            "tx_bps": int(tx)*8 if tx is not None else None, "collected_at": now()}


def parse_brief(text, inventory=S3900_INVENTORY):
    """Accept only full, recognized interface IDs and explicit state columns."""
    result = {}
    for n in range(1, len(inventory) + 1):
        interface = interface_name(n, inventory)
        m = re.search(rf"(?mi)^[ \t]*{re.escape(interface)}[ \t]+(UP|DOWN|ADM|ADMIN[ \t]+DOWN)\b", terminal_text(text))
        if m:
            result[n] = {"state": "up" if m[1].lower() == "up" else "down",
                         "admin": False if m[1].lower().startswith("adm") else None,
                         "link_at": now()}
    return result


def _vlan_member_sections(text, inventory=S3900_INVENTORY):
    """Parse VRP multiline VLAN member lists, without guessing damaged port IDs."""
    recognized = {}
    partial = False
    current = None
    # Exact names of this switch's ports; never a prefix of a longer name.
    entry = re.compile(r"(?i)(?<![A-Za-z0-9/-])(?:" + inventory.pattern() + r")(?![0-9/])")
    header = re.compile(r"(?i)^[ \t]*(Tagged|Untagged)[ \t]+Ports[ \t]*:[ \t]*(.*)$")
    for raw in terminal_text(text).splitlines():
        match = header.match(raw)
        if match:
            current = match[1].lower()
            recognized[current] = {"ports": set(), "explicit_none": False}
            value = match[2].strip()
        elif current and raw.startswith((" ", "\t")):
            value = raw.strip()
        else:
            current = None
            continue
        if not value:
            continue
        if value.lower().strip(' *"') == "none":
            recognized[current]["explicit_none"] = True
            continue
        matches = list(entry.finditer(value))
        for m in matches:
            recognized[current]["ports"].add(inventory.number(m[0]))
        residual = entry.sub(" ", value)
        # A damaged Ethernet name is incomplete information, not a missing port.
        if re.search(r"(?i)\b(?:Ethernet|GigabitEthernet)", residual) or "\ufffd" in residual:
            partial = True
    return recognized, partial


def _members_label(tagged, untagged, *, partial=False):
    labels = []
    if tagged:
        labels.append("Tagged: " + ", ".join(map(str, sorted(tagged))))
    if untagged:
        labels.append("Untagged: " + ", ".join(map(str, sorted(untagged))))
    if not labels:
        return "Nenhuma" if not partial else None
    return "; ".join(labels) + (" (leitura parcial)" if partial else "")


def parse_vlan_detail(text, vlan, inventory=S3900_INVENTORY):
    text = terminal_text(text)
    actual = _field(text, r"^[ \t]*VLAN[ \t]+ID[ \t]*:[ \t]*(\d{1,4})\b")
    if actual is None or int(actual) != vlan:
        return None
    name = _field(text, r"^[ \t]*(?:VLAN[ \t]+)?Description[ \t]*:[ \t]*([^\n]+)")
    sections, partial = _vlan_member_sections(text, inventory)
    tagged = sections.get("tagged", {}).get("ports", set())
    untagged = sections.get("untagged", {}).get("ports", set())
    both_present = "tagged" in sections and "untagged" in sections
    if not both_present:
        partial = True
    all_none = both_present and all(sections[k]["explicit_none"]
                                    for k in ("tagged", "untagged"))
    members = _members_label(tagged, untagged, partial=partial)
    if not tagged and not untagged and not all_none:
        members = None
    return {
        "id": vlan, "name": name, "members": members,
        "tagged_numbers": sorted(tagged),
        "untagged_numbers": sorted(untagged),
        "membership_partial": partial,
        "membership_reported": both_present,
        "note": "Console", "collected_at": now(),
    }


def _epoch(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def _expired(value, instant=None):
    stamp = _epoch(value)
    return stamp is None or (time.time() if instant is None else instant) - stamp > STALE_AFTER_SECONDS


def reconcile_vlan_members(data):
    """Use newer explicit access observations without reviving older membership."""
    for row in data.get("vlans") or []:
        tagged = set(row.get("tagged_numbers", []))
        untagged = set(row.get("untagged_numbers", []))
        extra = set()
        observed = row.get("field_observed_at")
        # A later name/partial response must not refresh retained membership.
        # Legacy/parser-only rows predate per-field timestamps.
        detail_at = _epoch(observed.get("membership") if observed is not None
                           else row.get("collected_at"))
        for port in data.get("ports", []):
            observed = port.get("field_observed_at", {})
            stamps = [observed.get(key, port.get("collected_at")) for key in ("mode", "pvid")]
            if (port.get("read_error") or any(_expired(stamp) for stamp in stamps)
                    or any(key in port.get("unconfirmed_fields", []) for key in ("mode", "pvid"))):
                continue
            # The pair is only as new as its oldest constituent field.
            port_at = min(_epoch(stamp) for stamp in stamps)
            if detail_at is not None and port_at <= detail_at:
                continue
            if port.get("mode") == "access":
                number = port["number"]
                tagged.discard(number)
                untagged.discard(number)
                if port.get("pvid") == row["id"]:
                    untagged.add(number)
                    extra.add(number)
        partial = bool(row.get("membership_partial"))
        row["membership_from_ports"] = sorted(extra)
        row["members"] = _members_label(tagged, untagged, partial=partial)
        # Effective lists are derived afresh from source observations, never fed back.
        row["effective_tagged_numbers"] = sorted(tagged)
        row["effective_untagged_numbers"] = sorted(untagged)
        if not tagged and not untagged and not row.get("membership_reported"):
            row["members"] = None


def parse_mac_table(text, inventory=S3900_INVENTORY):
    """Recognize legacy learned-MAC rows; never guess from arbitrary text."""
    text = terminal_text(text)
    pattern = re.compile(
        r"(?mi)^[ \t]*([0-9a-f]{4}(?:-[0-9a-f]{4}){2}|[0-9a-f]{2}(?::[0-9a-f]{2}){5})"
        r"[ \t]+([0-9]{1,4})[ \t]+(?:learned|dynamic)[ \t]+"
        + PHYSICAL_INTERFACE.pattern
    )
    rows = []
    interfaces = {name.lower(): n for n, name in enumerate(inventory.names, start=1)}
    for m in pattern.finditer(text):
        port = interfaces.get(m[3].lower())
        vlan = int(m[2])
        if port is None or not 1 <= vlan <= 4094:
            continue
        mac = re.sub(r"[:-]", "", m[1]).lower()
        mac = ":".join(mac[i:i+2] for i in range(0,12,2))
        rows.append({"mac": mac, "vlan": vlan, "port": port})
    if rows:
        return {"entries": rows, "count": len({r["mac"] for r in rows}), "collected_at": now()}
    if re.search(r"(?mi)^[ \t]*(?:Total(?:[ \t]+MAC[ \t]+addresses?|[ \t]+items?|[ \t]+entries)?[ \t]*:[ \t]*0|No[ \t]+MAC[ \t]+address(?:es)?(?:[ \t]+found)?)[.!]?[ \t]*$", text):
        return {"entries": [], "count": 0, "collected_at": now()}
    return None


class PhysicalDashboard:
    """Read-only collector with atomic publications and fair per-port scheduling."""
    def __init__(self):
        self._lock = threading.RLock()
        # Called with (old_device, new_device) when a different switch is identified.
        self.listeners = []
        self.last_fingerprint = None
        self.reset()

    @property
    def inventory(self):
        device = self.device
        return device.inventory if device is not None and device.inventory else S3900_INVENTORY

    def _port_template(self, inventory):
        return [{"number": n, "interface": name, **dict.fromkeys(PORT_FIELDS), "collected_at": None,
                 "field_observed_at": {}} for n, name in enumerate(inventory.names, start=1)]

    def _remember_device(self, device):
        """Record the identified switch and tell listeners if it is a different one."""
        previous = self.last_fingerprint
        console_credentials.bind(device.fingerprint)
        self.device = device
        self.last_fingerprint = device.fingerprint
        if previous is not None and previous != device.fingerprint:
            for listener in list(self.listeners):
                try:
                    listener(device)
                except Exception:  # a listener must never break monitoring
                    pass

    def reset(self):
        self.data = None
        self.outputs = []
        self.last_attempt = 0.0
        self.last_success = 0.0
        self.port_cursor = 0
        self.vlan_cursor = 0
        self.stage = 0
        self.mac_attempt = 0.0
        self.priority_ports = set()
        self.priority_vlans = set()
        self.priority_vlan_cursor = 0
        self.priority_vlan_turn = True
        self.settings_key = None
        self.dirty = True
        self.error = None
        self.device = None
        self.prompt = None
        self.identity_checked = 0.0
        self.login_required = False

    def invalidate(self, plan=None):
        with self._lock:
            self.dirty = True
            self.last_attempt = 0.0
            self.mac_attempt = 0.0
            if plan is None:
                if self.data:
                    for port in self.data["ports"]:
                        port["read_error"] = "Estado pendente após uso da CLI."
                    for row in self.data.get("vlans") or []:
                        row["read_error"] = "Estado pendente após uso da CLI."
                return
            inventory = self.inventory
            for command in plan.commands:
                for n in range(1, len(inventory) + 1):
                    if command.lower() == ("interface " + interface_name(n, inventory)).lower():
                        self.priority_ports.add(n)
                        if self.data:
                            self.data["ports"][n - 1]["read_error"] = "Alteração aguardando nova leitura."
                        break
                if re.fullmatch(r"(?:undo )?vlan [0-9]+|interface Vlan-interface [0-9]+|port (?:access vlan|trunk pvid vlan|hybrid pvid vlan) [0-9]+", command):
                    vlan = int(command.rsplit(" ", 1)[1])
                    if 1 <= vlan <= 4094:
                        self.priority_vlans.add(vlan)
                        for row in (self.data or {}).get("vlans") or []:
                            if row["id"] == vlan:
                                row["read_error"] = "Alteração aguardando nova leitura."

    def cached(self):
        with self._lock:
            snapshot = copy.deepcopy(self.data)
            outputs = copy.deepcopy(self.outputs)
            stale = self.dirty or bool(self.error) or time.monotonic() - self.last_success > INTERVAL * 3
            error = self.error
        if snapshot:
            instant = time.time()
            for port in snapshot["ports"]:
                observed = port.get("field_observed_at", {})
                port["field_stale"] = {
                    key: bool(port.get("read_error")) or key in port.get("unconfirmed_fields", [])
                         or _expired(observed.get(key), instant)
                    for key in PORT_FIELDS
                }
                known = [key for key in PORT_FIELDS if port.get(key) is not None]
                port["stale"] = not known or any(port["field_stale"][key] for key in known)
            for row in snapshot.get("vlans") or []:
                observed = row.get("field_observed_at", {})
                row["membership_stale"] = bool(row.get("read_error")) or bool(row.get("membership_partial")) or _expired(observed.get("membership"), instant)
                row["field_stale"] = {"membership": row["membership_stale"],
                                      "name": _expired(observed.get("name"), instant)}
                row["stale"] = (bool(row.get("read_error")) or _expired(row.get("collected_at"), instant)
                                or row["membership_stale"] or (row.get("name") is not None and row["field_stale"]["name"]))
            mac = snapshot.get("mac_table")
            if mac is not None:
                mac["stale"] = bool(mac.get("read_error")) or _expired(mac.get("collected_at"), instant)
            snapshot["inventory_stale"] = bool(snapshot.get("inventory_error")) or bool(snapshot.get("inventory_missing")) or _expired(snapshot.get("inventory_at"), instant)
            snapshot["stale_after_seconds"] = STALE_AFTER_SECONDS
            snapshot["coverage_partial"] = any(port["stale"] for port in snapshot["ports"]) or snapshot["inventory_stale"] or any(row["stale"] or row["membership_stale"] for row in snapshot.get("vlans") or [])
        device = self.device
        return {"snapshot": snapshot, "outputs": outputs, "stale": stale,
                "message": error, "collected_at": snapshot.get("collected_at") if snapshot else None,
                "device": device.public() if device is not None else None,
                "login_required": self.login_required,
                "console_credentials": console_credentials.available}

    def collect(self, settings, force=False):
        key = (settings.port, settings.baud_rate)
        if self.settings_key != key:
            self.reset()
            self.settings_key = key
        if not force and not self.dirty and time.monotonic() - self.last_attempt < INTERVAL:
            return self.cached()
        self.last_attempt = time.monotonic()
        def fresh():
            # Ports are known only after identification (fixed or discovered inventory).
            return {"ports": self._port_template(self.inventory) if self.device else [], "vlans": None}

        data = copy.deepcopy(self.data) if self.data else fresh()
        outputs, notes, failures = [], [], []
        observed_at = None
        active_command = "conexão serial"
        deadline = time.monotonic() + 22

        def observe():
            nonlocal observed_at
            observed_at = now()
            return observed_at

        def fail(message):
            notes.append(message)
            failures.append(message)

        def query(connection, command):
            nonlocal active_command
            active_command = command
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialReadOnlyError("Limite da coleta serial atingido.")
            connection.write(command.encode("ascii") + b"\r")
            raw = _read_until_prompt(connection, min(settings.timeout_seconds, 10, remaining))
            output = _clean_output(raw, command)
            outputs.append({"command": command, "output": output})
            return output

        def publish():
            if observed_at:
                data["collected_at"] = observed_at
            data.update(port=settings.port, baud_rate=settings.baud_rate,
                        notes=list(notes), expected_ports=len(data.get("ports") or []))
            reconcile_vlan_members(data)
            with self._lock:
                # Failed bootstrap must never create a usable unidentified
                # snapshot or bypass the version check on the next attempt.
                self.data = copy.deepcopy(data) if data.get("identity_verified") else None
                self.outputs = copy.deepcopy(outputs)
                if observed_at and data.get("identity_verified"):
                    self.last_success = time.monotonic()
                self.error = " ".join(failures) or None
                self.dirty = False

        def refresh_inventory(connection):
            data["inventory_error"] = "Leitura do inventário não concluída."
            try:
                text = query(connection, "display vlan")
                if command_rejected(text):
                    raise ConfigurationError("Consulta rejeitada.")
                inventory = _parse_vlan_inventory(text)
            except ConfigurationError:
                data["inventory_error"] = "Inventário VLAN não reconhecido; dados anteriores preservados."
                fail(data["inventory_error"])
                return
            old = {v["id"]: v for v in data.get("vlans") or []}
            confirmed = {vid for vid, row in old.items() if row.get("verified_individually")}
            missing = confirmed - inventory
            data["vlans"] = [old.get(v, {"id": v, "name": None, "members": None,
                                       "note": "Inventário", "collected_at": None})
                             for v in sorted(inventory | confirmed)]
            data["inventory_at"] = observe()
            data["inventory_error"] = None
            data["inventory_missing"] = sorted(missing)
            self.priority_vlans.update(missing)
            for row in data["vlans"]:
                if row["id"] in missing:
                    row["note"] = "Consulta individual anterior · ausente no inventário"
                    row["read_error"] = "Ausência no inventário aguardando confirmação individual."
            if missing:
                notes.append("Inventário incompleto; confirmando individualmente as VLANs ausentes.")

        def refresh_port(connection, number):
            old = data["ports"][number - 1]
            old["read_error"] = "Leitura da porta não concluída."
            text = query(connection, "display interface " + interface_name(number, self.inventory))
            new = None if command_rejected(text) else parse_port(text, number, self.inventory)
            if not new:
                old["read_error"] = f"Porta {number}: resposta não reconhecida; leitura anterior preservada."
                fail(old["read_error"])
                return
            stamp = observe()
            observed = old.setdefault("field_observed_at", {})
            old["unconfirmed_fields"] = [key for key in PORT_FIELDS if new.get(key) is None]
            for field in PORT_FIELDS:
                if new.get(field) is not None:
                    old[field] = new[field]
                    observed[field] = stamp
            old["collected_at"] = stamp
            old["read_error"] = None
            if new.get("state") is not None:
                old["link_at"] = stamp
            if new.get("pvid") and not any(row["id"] == new["pvid"] for row in data.get("vlans") or []):
                # A port hint schedules a query; it never creates a VLAN row.
                self.priority_vlans.add(new["pvid"])

        def refresh_vlan_detail(connection, vlan):
            for row in data.get("vlans") or []:
                if row["id"] == vlan:
                    row["read_error"] = "Leitura da VLAN não concluída."
            text = query(connection, f"display vlan {vlan}")
            ids = re.findall(r"(?mi)^[ \t]*VLAN[ \t]+ID[ \t]*:[ \t]*([0-9]{1,4})\b", text)
            absent = re.search(rf"(?mi)^[ \t]*(?:%[ \t]*)?(?:Error:[ \t]*)?(?:The[ \t]+)?VLAN(?:[ \t]+{vlan})?[ \t]+(?:does[ \t]+not[ \t]+exist|doesn't[ \t]+exist|not[ \t]+found)[.!]?[ \t]*$", text)
            if absent and not ids:
                data["vlans"] = [row for row in data.get("vlans") or [] if row["id"] != vlan]
                data["inventory_missing"] = [v for v in data.get("inventory_missing", []) if v != vlan]
                observe()
                notes.append(f"VLAN {vlan}: ausência confirmada por consulta individual.")
                return
            new = None if command_rejected(text) else parse_vlan_detail(text, vlan, self.inventory)
            if not new:
                message = f"VLAN {vlan}: consulta individual não confirmada; dados anteriores preservados."
                for row in data.get("vlans") or []:
                    if row["id"] == vlan:
                        row["read_error"] = message
                fail(message)
                return
            stamp = observe()
            new.update(verified_individually=True, read_error=None,
                       note="Confirmada por display vlan " + str(vlan), collected_at=stamp)
            new["field_observed_at"] = {} if new["membership_partial"] else {"membership": stamp}
            if new.get("name") is not None:
                new["field_observed_at"]["name"] = stamp
            if data.get("vlans") is None:
                data["vlans"] = []
            for row in data["vlans"]:
                if row["id"] == vlan:
                    # A partial member list cannot erase historical members or
                    # make the retained membership appear newly verified.
                    if new["membership_partial"]:
                        for key in ("tagged_numbers", "untagged_numbers"):
                            new[key] = sorted(set(row.get(key, [])) | set(new[key]))
                        if row.get("field_observed_at", {}).get("membership"):
                            new["field_observed_at"]["membership"] = row["field_observed_at"]["membership"]
                    # Missing descriptions keep their original observation time.
                    if new["name"] is None:
                        new["name"] = row.get("name")
                        if row.get("field_observed_at", {}).get("name"):
                            new["field_observed_at"]["name"] = row["field_observed_at"]["name"]
                    row.update(new)
                    break
            else:
                data["vlans"].append(new)
            data["vlans"].sort(key=lambda row: row["id"])
            data["inventory_missing"] = [v for v in data.get("inventory_missing", []) if v != vlan]

        def refresh_macs(connection):
            self.mac_attempt = time.monotonic()
            if data.get("mac_table") is not None:
                data["mac_table"]["read_error"] = "Leitura da tabela MAC não concluída."
            text = query(connection, "display mac-address")
            macs = None if command_rejected(text) else parse_mac_table(text, self.inventory)
            if macs is None:
                message = "Tabela MAC não reconhecida; leitura anterior preservada."
                if data.get("mac_table") is not None:
                    data["mac_table"]["read_error"] = message
                fail(message)
                return
            macs["collected_at"] = observe()
            macs["entries"].sort(key=lambda row: (row["port"], row["vlan"], row["mac"]))
            data["mac_table"] = macs

        def discover_ports(connection):
            """Physical ports of a switch without a fixed profile inventory."""
            nonlocal active_command
            for command in ("display brief interface", "display interface brief"):
                text = query(connection, command)
                found = None if command_rejected(text) else parse_interface_list(text)
                if found:
                    return found
            # Slow at 9600 baud but universal on VRP 3 / Comware 3; done once per switch.
            active_command = "display current-configuration"
            connection.write(b"display current-configuration\r")
            raw = _read_until_prompt(connection, 240, max_bytes=1_000_000)
            text = _clean_output(raw, "display current-configuration")
            return None if command_rejected(text) else parse_interface_list(text)

        def identify_switch(connection):
            device = identify(query(connection, "display version"))
            self.identity_checked = time.monotonic()
            if not device.profile.monitoring:
                self._remember_device(device)
                raise SerialReadOnlyError(f"{device.vendor} {device.model}: {device.profile.note}")
            inventory = device.profile.fixed_inventory or discover_ports(connection)
            if not inventory:
                raise SerialReadOnlyError("Switch identificado, mas não consegui listar as portas físicas.")
            device = device.with_inventory(inventory)
            self._remember_device(device)
            return device

        try:
            with open_connection(settings) as connection:
                active_command = "prompt serial"
                try:
                    wake = open_console(connection, min(settings.timeout_seconds, 10),
                                        reader=lambda *a, **k: _read_until_prompt(*a, **k))
                    self.login_required = False
                except (ConsoleLoginRequired, ConsoleLoginFailed):
                    self.login_required = True
                    raise
                name = prompt_name(wake)
                if name and self.prompt and name != self.prompt and data.get("identity_verified"):
                    # Different prompt: possibly another switch on the cable. Identify again.
                    notes.append(f"Prompt mudou de <{self.prompt}> para <{name}>; identificando o switch novamente.")
                    self.reset()
                    self.settings_key = key
                    data = fresh()
                self.prompt = name or self.prompt
                if (data.get("identity_verified") and self.device is not None
                        and time.monotonic() - self.identity_checked >= IDENTITY_RECHECK_SECONDS):
                    current = identify(query(connection, "display version"))
                    self.identity_checked = time.monotonic()
                    if current.identity_key != self.device.identity_key:
                        notes.append(f"Novo switch detectado: {current.vendor} {current.model}.")
                        self.reset()
                        self.settings_key = key
                        data = fresh()
                if not data.get("identity_verified"):
                    device = identify_switch(connection)
                    data = {**fresh(), **{k: v for k, v in data.items() if k not in ("ports",)}}
                    data["ports"] = self._port_template(device.inventory)
                    data.update(model=device.model, version=device.version, hardware=device.hardware,
                                cpld=device.cpld, bootrom=device.bootrom)
                    data["identity_verified"] = True
                    data["name"] = data["model"]
                    observe()
                    publish()
                    refresh_inventory(connection)
                else:
                    # Every poll services a port. Priority work alternates with
                    # normal round-robin so repeated edits cannot starve others.
                    stage = self.stage
                    self.stage += 1
                    if self.priority_ports and stage % 2:
                        number = min(self.priority_ports)
                        self.priority_ports.discard(number)
                    else:
                        number = self.port_cursor % len(self.inventory) + 1
                        self.port_cursor += 1
                        self.priority_ports.discard(number)
                    refresh_port(connection, number)
                    maintenance = stage % 4
                    if maintenance == 0:
                        refresh_inventory(connection)
                    elif maintenance == 1:
                        # Alternate urgent and ordinary work. Rotate urgent IDs
                        # too: a truncated inventory may re-add the lowest ID on
                        # every poll, which must not starve later VLANs.
                        rows = data.get("vlans") or []
                        if self.priority_vlans and (self.priority_vlan_turn or not rows):
                            ordered = sorted(self.priority_vlans)
                            vlan = next((v for v in ordered if v > self.priority_vlan_cursor), ordered[0])
                            self.priority_vlan_cursor = vlan
                            self.priority_vlans.discard(vlan)
                            self.priority_vlan_turn = False
                            refresh_vlan_detail(connection, vlan)
                        elif rows:
                            vlan = rows[self.vlan_cursor % len(rows)]["id"]
                            self.vlan_cursor += 1
                            self.priority_vlans.discard(vlan)
                            self.priority_vlan_turn = True
                            refresh_vlan_detail(connection, vlan)
                    elif maintenance == 2 and (not self.mac_attempt or time.monotonic() - self.mac_attempt >= 60):
                        refresh_macs(connection)
                publish()
            return self.cached()
        except (SerialReadOnlyError, OSError, ValueError) as exc:
            fail(f"Falha em {active_command}: {exc}")
        except Exception:
            fail("Erro na leitura serial em " + active_command + ". Verifique o console.")
        publish()
        if not data.get("model"):
            raise SerialReadOnlyError(self.error)
        return self.cached()
