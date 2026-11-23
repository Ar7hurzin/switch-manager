"""Identify legacy Huawei / Huawei-3Com / 3Com switches and describe their profile.

Only the Quidway S3900-SI with VRP 3.10(CN) RELEASE 0002 has been validated on
real hardware for guided configuration. Every other recognized VRP 3 / Comware 3
switch is monitored read-only until its own console captures are validated.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Physical port names used by VRP 3 / Comware 3 (never logical Vlan-interface/NULL/Aux).
PHYSICAL_INTERFACE = re.compile(
    r"\b((?:Ten-GigabitEthernet|GigabitEthernet|FastEthernet|Ethernet)[0-9]{1,2}/[0-9]{1,2}(?:/[0-9]{1,3})?)\b",
    re.I)
MAX_PORTS = 512


def terminal_text(text: str) -> str:
    return ANSI.sub("", text).replace("\r", "").replace("\x00", "")


def _canonical(name: str) -> str:
    """Canonical capitalization used by the VRP CLI."""
    for prefix in ("Ten-GigabitEthernet", "GigabitEthernet", "FastEthernet", "Ethernet"):
        if name.lower().startswith(prefix.lower()):
            return prefix + name[len(prefix):]
    return name


@dataclass(frozen=True)
class Inventory:
    """Physical ports in front-panel order; port N (1-based) maps to names[N-1]."""
    names: tuple[str, ...]

    @classmethod
    def from_names(cls, names) -> "Inventory":
        unique = {}
        for name in names:
            canonical = _canonical(name)
            unique.setdefault(canonical.lower(), canonical)
        def key(name):
            return (tuple(int(n) for n in re.findall(r"[0-9]+", name)), name.lower())
        return cls(tuple(sorted(unique.values(), key=key)))

    def __len__(self) -> int:
        return len(self.names)

    def name(self, number: int) -> str:
        if not 1 <= number <= len(self.names):
            raise ValueError(f"Porta {number} fora do inventário (1–{len(self.names)}).")
        return self.names[number - 1]

    def number(self, name: str) -> int | None:
        lowered = name.lower()
        for index, candidate in enumerate(self.names, start=1):
            if candidate.lower() == lowered:
                return index
        return None

    def pattern(self) -> str:
        """Regex alternation of every exact port name, longest first."""
        return "|".join(re.escape(name) for name in sorted(self.names, key=len, reverse=True))

    def summary(self) -> str:
        groups: dict[str, int] = {}
        for name in self.names:
            kind = re.match(r"[A-Za-z-]+", name).group(0)
            groups[kind] = groups.get(kind, 0) + 1
        return " · ".join(f"{count} {kind}" for kind, count in groups.items())


S3900_INVENTORY = Inventory(tuple([f"Ethernet1/0/{n}" for n in range(1, 25)]
                                  + [f"GigabitEthernet1/1/{n}" for n in range(1, 5)]))


def parse_interface_list(text: str) -> Inventory | None:
    """Physical ports from `display current-configuration` blocks or a brief table.

    Only names at the start of a line count (``interface X`` or a table row), so
    VLAN member lists or descriptions mentioning ports are never mistaken for ports.
    """
    names = []
    for line in terminal_text(text).splitlines():
        match = re.match(r"^[ \t]*(?:interface[ \t]+)?" + PHYSICAL_INTERFACE.pattern, line, re.I)
        if match:
            names.append(match.group(1))
    if not names or len(set(n.lower() for n in names)) > MAX_PORTS:
        return None
    return Inventory.from_names(names)


@dataclass(frozen=True)
class SwitchProfile:
    id: str
    label: str
    family: str
    validated_writes: bool
    monitoring: bool = True
    # Guided configuration available (validated or adapted from the VRP 3 manual).
    # Operations a given switch rejects are switched to CLI-only at runtime.
    guided_config: bool = True
    fixed_inventory: Inventory | None = None
    # VRP 3 / Comware 3 command syntax (from the S3900 validation and VRP 3 manuals).
    trunk_permit_prefix: str = "port trunk permit vlan "
    hybrid_prefix: str = "port hybrid vlan "
    vlans_per_line: int = 10
    note: str = ""


S3900_PROFILE = SwitchProfile(
    id="quidway-s3900-si", label="Quidway S3900-SI (validado)", family="VRP 3",
    validated_writes=True, fixed_inventory=S3900_INVENTORY,
    note="Perfil validado no equipamento real: monitoramento e configuração guiada.")
LEGACY_PROFILE = SwitchProfile(
    id="vrp3-legacy", label="Huawei/3Com legado VRP 3 · Comware 3", family="VRP 3",
    validated_writes=False,
    note="Configuração guiada adaptada pela sintaxe VRP 3 (ainda não validada neste modelo). "
         "Operações que o switch recusar passam a ser feitas pela CLI.")
UNSUPPORTED_PROFILE = SwitchProfile(
    id="unsupported", label="Equipamento não suportado", family="desconhecida",
    validated_writes=False, monitoring=False, guided_config=False,
    note="Sistema não reconhecido como VRP 3 / Comware 3. Use a CLI.")


@dataclass(frozen=True)
class DetectedDevice:
    vendor: str
    model: str
    version: str
    profile: SwitchProfile
    hardware: str | None = None
    cpld: str | None = None
    bootrom: str | None = None
    inventory: Inventory | None = field(default=None)

    def with_inventory(self, inventory: Inventory) -> "DetectedDevice":
        return DetectedDevice(self.vendor, self.model, self.version, self.profile,
                              self.hardware, self.cpld, self.bootrom, inventory)

    @property
    def identity_key(self) -> str:
        """Same switch type and firmware; inventory not included (read separately)."""
        return hashlib.sha256("\0".join((self.profile.id, self.model, self.version)).encode()).hexdigest()[:16]

    @property
    def fingerprint(self) -> str:
        names = ",".join(self.inventory.names) if self.inventory else ""
        return hashlib.sha256("\0".join((self.identity_key, names)).encode()).hexdigest()[:16]

    def public(self) -> dict:
        return {
            "vendor": self.vendor, "model": self.model, "version": self.version,
            "profile": self.profile.id, "profile_label": self.profile.label,
            "family": self.profile.family, "validated_writes": self.profile.validated_writes,
            "guided_config": self.profile.guided_config,
            "monitoring": self.profile.monitoring, "note": self.profile.note,
            "ports": list(self.inventory.names) if self.inventory else [],
            "port_summary": self.inventory.summary() if self.inventory else "",
            "fingerprint": self.fingerprint,
        }


def _field(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.I | re.M)
    return match.group(1).strip() if match else None


def _vendor(text: str) -> str | None:
    lowered = text.lower()
    if "3com" in lowered:
        return "3Com"
    if "huawei-3com" in lowered or "huawei 3com" in lowered or "h3c" in lowered:
        return "Huawei-3Com"
    if "huawei" in lowered or "quidway" in lowered or "vrp" in lowered:
        return "Huawei"
    return None


def identify(display_version: str) -> DetectedDevice:
    """Classify `display version` output. Never raises; unknown → UNSUPPORTED_PROFILE."""
    text = terminal_text(display_version)
    hardware = _field(text, r"^[ \t]*Hardware Version is[ \t]+([^\n]+)")
    cpld = _field(text, r"^[ \t]*CPLD Version is[ \t]+([^\n]+)")
    bootrom = _field(text, r"^[ \t]*Bootrom Version is[ \t]+([^\n]+)")

    # Validated profile: exact model and release, exactly as before this change.
    s3900_model = _field(text, r"\b(Quidway[ \t]+3900-SI[ \t]+28-Port)\b")
    s3900_version = _field(text, r"\b(Version[ \t]+3\.10\(CN\),[ \t]+RELEASE[ \t]+0002)\b")
    if s3900_model and s3900_version:
        return DetectedDevice("Huawei", s3900_model, s3900_version, S3900_PROFILE,
                              hardware, cpld, bootrom, S3900_INVENTORY)

    vendor = _vendor(text)
    version_match = re.search(
        r"\b(?:VRP|Comware)\b[^\n]*?\bVersion[ \t]+([0-9]+)\.([0-9]+)([^\n]*)", text, re.I)
    model = _field(text, r"^[ \t]*((?:Huawei[ -]3Com[ \t]+|Huawei[ \t]+|H3C[ \t]+|3Com[ \t]+)?"
                         r"(?:Quidway[ \t]+)?(?:Switch[ \t]+)?[A-Za-z]*[ \t]?[0-9][^\n]*?)[ \t]+uptime[ \t]+is\b")
    if not vendor or not version_match:
        return DetectedDevice(vendor or "Desconhecido", model or "Não identificado",
                              "Não identificada", UNSUPPORTED_PROFILE, hardware, cpld, bootrom)
    version = ("Version " + version_match.group(1) + "." + version_match.group(2)
               + version_match.group(3)).strip().rstrip(",")
    if version_match.group(1) != "3":
        unsupported = SwitchProfile(
            id="unsupported", label=f"{vendor} VRP {version_match.group(1)} (não suportado)",
            family=f"VRP {version_match.group(1)}", validated_writes=False, monitoring=False, guided_config=False,
            note="Este programa cobre switches antigos VRP 3 / Comware 3. Use a CLI.")
        return DetectedDevice(vendor, model or "Não identificado", version, unsupported, hardware, cpld, bootrom)
    return DetectedDevice(vendor, model or f"{vendor} (modelo não identificado)", version,
                          LEGACY_PROFILE, hardware, cpld, bootrom)
