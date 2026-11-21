"""Read-only console capture used to validate a new switch profile.

Run with the web server stopped (the serial port is exclusive):
    python start.py capture

Only a fixed allowlist of `display` commands is sent. Secrets are removed
before anything is written, and the file is created with mode 600 under
instance/captures/. Review it before sending it to anyone.
"""
from __future__ import annotations

import getpass
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from console_auth import ConsoleCredentials, ConsoleLoginFailed, ConsoleLoginRequired, open_console
from serial_readonly import SerialReadOnlyError, SerialSettings, _read_until_prompt, open_connection
from switch_profiles import identify, parse_interface_list, terminal_text
from switch_text import decode_switch_text, command_rejected

# Fixed and read-only: nothing here can change the switch.
CAPTURE_COMMANDS = (
    "display version",
    "display current-configuration",
    "display brief interface",
    "display interface brief",
    "display vlan",
    "display vlan 1",
    "display mac-address",
)
SAMPLE_PORT_COMMAND = "display interface {}"
MAX_CAPTURE_BYTES = 1_000_000

SECRET_LINE = re.compile(
    r"(?i)\b(password|cipher|simple|local-user|super\s+password|community|authentication-mode|"
    r"key\s|secret|snmp-agent|securityname|security-name|irreversible-cipher|private-key|public-key)\b")


def allowed(command: str) -> bool:
    """Only exact allowlisted displays or `display interface <physical port>`."""
    if command in CAPTURE_COMMANDS:
        return True
    return bool(re.fullmatch(r"display interface (?:Ten-GigabitEthernet|GigabitEthernet|FastEthernet|Ethernet)"
                             r"[0-9]{1,2}/[0-9]{1,2}(?:/[0-9]{1,3})?", command))


def sanitize(text: str) -> str:
    lines = []
    for line in terminal_text(text).splitlines():
        if SECRET_LINE.search(line):
            indent = line[:len(line) - len(line.lstrip())]
            keyword = line.strip().split(" ", 1)[0]
            lines.append(f"{indent}{keyword} <removido>")
        else:
            lines.append(line)
    return "\n".join(lines)


def _run(connection, settings: SerialSettings, command: str) -> str:
    if not allowed(command):
        raise ValueError(f"Comando fora da lista de leitura: {command}")
    connection.write(command.encode("ascii") + b"\r")
    # At 9600 baud a full configuration can take minutes on old switches.
    timeout = 300 if command == "display current-configuration" else max(settings.timeout_seconds, 30)
    raw = _read_until_prompt(connection, timeout, max_bytes=512_000)
    return decode_switch_text(raw)


def capture(settings: SerialSettings, credentials: ConsoleCredentials, output_dir: Path) -> Path:
    sections: list[tuple[str, str]] = []
    with open_connection(settings) as connection:
        open_console(connection, settings.timeout_seconds, credentials)
        for command in CAPTURE_COMMANDS:
            try:
                output = _run(connection, settings, command)
                sections.append((command, "[COMANDO NÃO SUPORTADO]\n" + output if command_rejected(output) else output))
            except SerialReadOnlyError as exc:
                sections.append((command, f"[SEM RESPOSTA: {exc}]"))
        version_text = next((out for cmd, out in sections if cmd == "display version"), "")
        config_text = next((out for cmd, out in sections if cmd == "display current-configuration"), "")
        inventory = parse_interface_list(config_text) or parse_interface_list(
            next((out for cmd, out in sections if "brief" in cmd and not out.startswith("[")), ""))
        for name in (inventory.names[:1] + inventory.names[-1:]) if inventory else ():
            command = SAMPLE_PORT_COMMAND.format(name)
            try:
                sections.append((command, _run(connection, settings, command)))
            except SerialReadOnlyError as exc:
                sections.append((command, f"[SEM RESPOSTA: {exc}]"))

    device = identify(version_text)
    if inventory:
        device = device.with_inventory(inventory)
    header = [
        "; Captura de leitura do Quidway Manager (somente comandos display; segredos removidos)",
        f"; Criada em {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"; Porta serial: {settings.port} a {settings.baud_rate} baud",
        f"; Detecção: {device.vendor} · {device.model} · {device.version} · perfil {device.profile.id}",
        f"; Portas encontradas: {len(inventory) if inventory else 0} ({inventory.summary() if inventory else 'nenhuma'})",
        "",
    ]
    body = "\n".join(header) + "\n".join(f"===== {cmd} =====\n{sanitize(out)}\n" for cmd, out in sections)
    body = body[:MAX_CAPTURE_BYTES]
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", device.model).strip("-")[:40] or "switch"
    target = output_dir / f"{slug}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return target


def main() -> int:
    settings_file = Path(os.environ.get("QM_SETTINGS_FILE", Path(__file__).with_name("local-settings.ps1"))).expanduser()
    output_dir = settings_file.parent / "instance" / "captures"
    try:
        settings = SerialSettings.from_environment()
    except SerialReadOnlyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Captura somente leitura em {settings.port} ({settings.baud_rate} baud).")
    print("Feche o Quidway Manager e outros programas que usam a porta serial antes de continuar.")
    credentials = ConsoleCredentials()
    for attempt in range(2):
        try:
            target = capture(settings, credentials, output_dir)
            break
        except ConsoleLoginRequired:
            if attempt:
                print("O console continuou pedindo login. Confira usuário e senha.", file=sys.stderr)
                return 2
            print("O console pede login. As credenciais ficam só na memória desta captura.")
            credentials.set(input("Usuário do switch (Enter se só pedir senha): ").strip(),
                            getpass.getpass("Senha do console: "))
        except ConsoleLoginFailed as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except SerialReadOnlyError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    credentials.clear()
    print(f"\nCaptura gravada em: {target}")
    print("Abra o arquivo e confira se não sobrou nenhum dado sensível antes de enviá-lo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
