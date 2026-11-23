"""Serial execution for typed, reviewed Quidway configuration plans."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from dataclasses import replace

from serial_readonly import SerialReadOnlyError, SerialSettings, open_connection
from switch_config import ConfigurationError, ConfigurationPlan
from switch_text import decode_switch_text, command_rejected
from switch_profiles import PHYSICAL_INTERFACE, identify
from console_auth import open_console


PROMPT = re.compile(rb"(?m)(?:<[^>\r\n]+>|\[[^\]\r\n]+\])\s*$")
CONFIRM = re.compile(rb"(?i)(?:\[\s*y\s*/\s*n\s*\]|\(y/n\)|\[y/n\])")
MORE_MARKERS = (b"---- More ----", b"More: ")
# Quiet time after a prompt before trusting it: late bytes from a slow console may follow.
PROMPT_SETTLE_SECONDS = 0.20


def _read_response(connection, timeout_seconds: int, allow_confirmation: bool = False, max_bytes: int = 512_000) -> tuple[bytes, bool]:
    started = time.monotonic()
    data = bytearray()
    handled_more = 0
    last_data = started
    while time.monotonic() - started < timeout_seconds:
        block = connection.read(4096)
        if block:
            last_data = time.monotonic()
            data.extend(block)
            if len(data) > max_bytes:
                raise ConfigurationError("A resposta do switch excedeu o limite seguro de leitura.")
            raw = bytes(data)
            if allow_confirmation and CONFIRM.search(raw):
                return raw, True
            marker_count = max((raw.count(marker) for marker in MORE_MARKERS), default=0)
            if marker_count > handled_more:
                connection.write(b" ")
                handled_more = marker_count
        elif data and PROMPT.search(bytes(data).rstrip()) and time.monotonic() - last_data >= PROMPT_SETTLE_SECONDS:
            return bytes(data), False
        else:
            time.sleep(0.01)
    raise ConfigurationError("O switch não respondeu a tempo. Confira o prompt no console; não reenviei o comando.")


class CommandRejected(ConfigurationError):
    """The switch answered a command with a VRP syntax/feature error."""

    def __init__(self, command: str, message: str | None = None):
        super().__init__(message or f"O switch rejeitou um comando do plano: {command}. O restante não foi enviado; "
                                    "confira a configuração em execução.")
        self.command = command


def _exchange(connection, command: str, settings: SerialSettings, allow_confirmation: bool = False) -> tuple[str, bool]:
    connection.write(command.encode("ascii") + b"\r")
    raw, confirmation = _read_response(connection, settings.timeout_seconds, allow_confirmation=allow_confirmation)
    text = decode_switch_text(raw).replace("\r", "").strip()
    if command_rejected(text):
        raise CommandRejected(command)
    return text, confirmation


USER_VIEW = re.compile(r"<[^>\n]+>\s*$")
SUB_VIEW = re.compile(r"\[[^\]\n]+\]\s*$")


def _ensure_user_view(connection, settings: SerialSettings, wake: bytes) -> None:
    """Plans start with system-view, which VRP accepts only from the user view.

    A console left in a configuration subview (e.g. by a closed CLI on a physical
    switch) would otherwise make the switch reject system-view, and that rejection
    would wrongly mark the operation as CLI-only for the model.
    """
    if not SUB_VIEW.search(decode_switch_text(wake).replace("\r", "").rstrip()):
        return
    response, _ = _exchange(connection, "return", settings)
    if not USER_VIEW.search(response):
        raise ConfigurationError("O console não voltou à visão de usuário. Nenhuma configuração foi enviada.")


def _check_target(connection, settings: SerialSettings, plan: ConfigurationPlan | None = None,
                  require_writes: bool = False) -> str:
    """Identify the switch on the wire before touching it.

    - unsupported systems are refused;
    - writes require a profile validated on real hardware (today: Quidway S3900-SI);
    - a preview is only applied to the same switch type/firmware it was built for.
    """
    response, _ = _exchange(connection, "display version", settings)
    device = identify(response)
    if not device.profile.monitoring:
        raise ConfigurationError(f"Switch não suportado pela configuração guiada ({device.vendor} {device.model}): "
                                 "use a CLI. Nenhuma alteração foi enviada.")
    if require_writes and not device.profile.guided_config:
        raise ConfigurationError(f"{device.model} ({device.version}) não tem configuração guiada: use a CLI. "
                                 "Nenhuma alteração foi enviada.")
    if plan is not None and plan.device_identity and device.identity_key != plan.device_identity:
        raise ConfigurationError("O switch conectado não é o mesmo da prévia. Gere uma nova prévia; nenhuma alteração foi enviada.")
    return response


def _parse_vlan_inventory(response: str) -> set[int]:
    """Parse VRP inventory, including legacy trailing comma/control characters."""
    vlan_ids: set[int] = set()
    response = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", response)
    inventory = re.search(r"(?i)following VLAN exist\(s\):[ \t]*\r?\n?", response)
    if inventory:
        token = r"([0-9]{1,4})(?:[ \t]*-[ \t]*([0-9]{1,4}))?(?:[ \t]*\([^()\r\n]*\))?"
        row = re.compile(token + r"(?:[ \t]*,[ \t]*" + token + r")*[ \t]*,?")
        for raw_line in response[inventory.end():].split("\n"):
            line = raw_line.strip().rstrip("\x00\x07\x08\x1a\x7f\ufffd\u25a1\u25ab").rstrip()
            if not line:
                if vlan_ids:
                    break
                continue
            if line.startswith(("<", "[")) or not line[0].isdigit():
                break
            if not row.fullmatch(line):
                raise ConfigurationError("Não consegui interpretar toda a lista de VLANs; nenhuma alteração foi enviada.")
            for item in line.rstrip(",").split(","):
                match = re.fullmatch(token, item.strip())
                if not match or not 1 <= int(match.group(1)) <= int(match.group(2) or match.group(1)) <= 4094:
                    raise ConfigurationError("ID de VLAN inválido no retorno do switch; nenhuma alteração foi enviada.")
                vlan_ids.update(range(int(match.group(1)), int(match.group(2) or match.group(1)) + 1))
        # Never fall back to the first ID of a malformed comma-separated list.
    else:
        vlan_ids.update(
            int(match.group(1))
            for match in re.finditer(r"(?mi)^[ \t]*([0-9]{1,4})(?:[ \t]*\(|[ \t]+(?:common|static|dynamic|default)\b)", response)
        )
        vlan_ids.update(
            int(match.group(1))
            for match in re.finditer(r"(?mi)^[ \t]*VLAN[ \t]+ID[ \t]*:[ \t]*([0-9]{1,4})\b", response)
        )
    if not vlan_ids or any(not 1 <= v <= 4094 for v in vlan_ids):
        raise ConfigurationError("Não consegui interpretar a lista atual de VLANs; nenhuma alteração foi enviada.")
    return vlan_ids


def _existing_vlans(connection, settings: SerialSettings) -> set[int]:
    response, _ = _exchange(connection, "display vlan", settings)
    return _parse_vlan_inventory(response)


def _query_vlan_presence(connection, settings: SerialSettings, vlan: int) -> bool:
    """Verify the exact VLAN on legacy VRP; never infer absence from a bad list."""
    if not 1 <= vlan <= 4094:
        raise ConfigurationError("ID de VLAN inválido.")
    connection.write(f"display vlan {vlan}".encode("ascii") + b"\r")
    raw, _ = _read_response(connection, settings.timeout_seconds)
    text = decode_switch_text(raw).replace("\r", "")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    ids = {int(value) for value in re.findall(
        r"(?mi)^[ \t]*VLAN[ \t]+ID[ \t]*:[ \t]*([0-9]{1,4})\b", text
    )}
    if ids == {vlan} and not command_rejected(text):
        return True
    # Only an explicit, unambiguous missing-VLAN message can authorize creation.
    absent = re.search(
        rf"(?mi)^[ \t]*(?:%[ \t]*)?(?:Error:[ \t]*)?(?:The[ \t]+)?VLAN(?:[ \t]+{vlan})?[ \t]+(?:does[ \t]+not[ \t]+exist|doesn't[ \t]+exist|not[ \t]+found|does[ \t]+not[ \t]+exist[ \t]+in[ \t]+the[ \t]+system)[.!]?[ \t]*$",
        text,
    )
    if not ids and absent:
        return False
    raise ConfigurationError(
        f"Não consegui confirmar a VLAN {vlan} pela consulta display vlan {vlan}. "
        "Nenhuma alteração foi enviada; confira a resposta na CLI."
    )


def _verify_vlan_preconditions(connection, settings: SerialSettings, plan: ConfigurationPlan) -> None:
    needs_catalog = plan.action in {
        "create_vlan", "rename_vlan", "delete_vlan", "set_access_port",
        "set_trunk_port", "set_hybrid_port", "set_management_ip",
    }
    if not needs_catalog:
        return
    required: set[int] = set()
    for command in plan.commands:
        if command.startswith("vlan ") or command.startswith("undo vlan "):
            required.add(int(command.split()[-1]))
        elif command.startswith("port access vlan ") or command.startswith("port trunk pvid vlan ") or command.startswith("port hybrid pvid vlan "):
            required.add(int(command.split()[-1]))
        elif command.startswith("port trunk permit vlan "):
            required.update(int(part) for part in command.removeprefix("port trunk permit vlan ").split())
        elif command.startswith("port hybrid vlan "):
            match = re.fullmatch(r"port hybrid vlan ([0-9 ]+) (?:tagged|untagged)", command)
            if match:
                required.update(int(part) for part in match.group(1).split())
        elif command.startswith("interface Vlan-interface "):
            required.add(int(command.rsplit(" ", 1)[1]))

    if not required:
        raise ConfigurationError("Não consegui identificar as VLANs do plano; nenhuma alteração foi enviada.")
    if settings.virtual:
        existing = _existing_vlans(connection, settings)
    else:
        # The physical VRP 3.10 returns a reliable per-VLAN detail, even when
        # its global inventory contains unexpected terminal characters.
        existing = {vlan for vlan in sorted(required)
                    if _query_vlan_presence(connection, settings, vlan)}

    if plan.action == "create_vlan":
        if required & existing:
            raise ConfigurationError(f"A VLAN {min(required & existing)} já existe. Atualize a prévia ou escolha outra ação.")
    else:
        missing = required - existing
        if missing:
            raise ConfigurationError("As VLANs necessárias não existem: " + ", ".join(str(vlan) for vlan in sorted(missing)) + ". Nenhuma mudança foi enviada.")


def _open(settings: SerialSettings):
    try:
        connection = open_connection(settings)
    except Exception as exc:
        raise ConfigurationError(f"Não foi possível abrir {settings.port}. Feche Tera Term/MobaXterm e confirme o cabo.") from exc
    return connection


def _parse_vlan_values(value: str) -> set[int]:
    """Read an explicit interface VLAN list; malformed/partial lists fail closed."""
    value = value.strip().lower()
    if value == "none":
        return set()
    if value == "all":
        return set(range(1, 4095))
    tokens = value.replace(",", " ").split()
    values: set[int] = set()
    while tokens:
        token = tokens.pop(0)
        match = re.fullmatch(r"([0-9]{1,4})(?:-([0-9]{1,4}))?", token)
        if not match:
            raise ConfigurationError("Lista de VLANs da verificação está incompleta ou não reconhecida.")
        start, end = int(match[1]), int(match[2] or match[1])
        if tokens and tokens[0] == "to":
            if match[2]:
                raise ConfigurationError("Intervalo de VLANs ambíguo na verificação.")
            tokens.pop(0)
            if not tokens or not tokens[0].isdigit():
                raise ConfigurationError("Intervalo de VLANs não reconhecido na verificação.")
            end = int(tokens.pop(0))
        if not 1 <= start <= end <= 4094:
            raise ConfigurationError("Intervalo de VLANs inválido na verificação.")
        values.update(range(start, end + 1))
    if not value:
        raise ConfigurationError("Lista de VLANs ausente na verificação.")
    return values


def _interface_vlan_sections(text: str) -> tuple[set[int], set[int]]:
    """Read complete tagged/untagged sections, including wrapped numeric rows.

    Unknown continuations, repeated headers and lists outside a section are
    ambiguous. Never silently reduce a longer observed list to its first row.
    """
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "")
    sections: dict[str, list[str]] = {}
    current = None
    header = re.compile(r"^[ \t]*(Tagged|Untagged)[ \t]+VLAN[ \t]+ID[ \t]*:[ \t]*(.*)$", re.I)
    field = re.compile(r"^[A-Za-z][A-Za-z0-9 /()_.-]*:")
    for line in text.splitlines():
        value = line.strip()
        match = header.fullmatch(line)
        if match:
            current = match[1].lower()
            if current in sections:
                raise ConfigurationError("Seção VLAN repetida na verificação; resposta ambígua.")
            sections[current] = [match[2]] if match[2].strip() else []
            continue
        if current:
            if not value or value.startswith(("<", "[")) or field.match(value):
                current = None
            elif line.startswith((" ", "\t")):
                sections[current].append(value)
                continue
            else:
                raise ConfigurationError("Continuação VLAN não reconhecida na verificação.")
        elif sections and re.fullmatch(r"[0-9, \t-]+", value):
            raise ConfigurationError("Lista VLAN fora de uma seção reconhecida; resposta ambígua.")
    if set(sections) != {"tagged", "untagged"}:
        raise ConfigurationError("Listas tagged/untagged ausentes na verificação; resultado não confirmado.")
    tagged = _parse_vlan_values(" ".join(sections["tagged"]))
    untagged = _parse_vlan_values(" ".join(sections["untagged"]))
    if tagged & untagged:
        raise ConfigurationError("A mesma VLAN foi reportada tagged e untagged; resposta ambígua.")
    return tagged, untagged


def _port_mode(text: str, interface: str) -> str:
    headers = re.findall(r"(?mi)^[ \t]*" + PHYSICAL_INTERFACE.pattern + r"[ \t]+(?:current[ \t]+state[ \t]*:|is)[ \t]*", text)
    modes = re.findall(r"(?mi)^[ \t]*(?:Mdi[ \t]+type:[^\r\n]*?[ \t]+)?Port[ \t]+link-type[ \t]*:[ \t]*(access|trunk|hybrid)\b", text)
    if headers != [interface] or len(modes) != 1:
        raise ConfigurationError("Interface ou modo atual ausente/ambíguo; gere uma nova prévia após validar a leitura.")
    return modes[0].lower()


def prepare_plan(settings: SerialSettings, plan: ConfigurationPlan) -> ConfigurationPlan:
    """Resolve a mode transition before issuing its exact, immutable preview."""
    if plan.action not in {"set_trunk_port", "set_hybrid_port"}:
        return plan
    connection = _open(settings)
    try:
        open_console(connection, settings.timeout_seconds)
        _check_target(connection, settings, plan)
        interface = next(command.removeprefix("interface ") for command in plan.commands if command.startswith("interface "))
        text, _ = _exchange(connection, "display interface " + interface, settings)
        mode = _port_mode(text, interface)
        desired = plan.action.removeprefix("set_").removesuffix("_port")
        commands = [command for command in plan.commands if command != "port link-type access"]
        if mode not in {"access", desired}:
            index = commands.index("port link-type " + desired)
            commands.insert(index, "port link-type access")
            warnings = plan.warnings + (f"A porta está em modo {mode}: passará por access antes de {desired}, conforme o manual S3900. Todos os comandos estão nesta prévia.",)
        else:
            warnings = plan.warnings
        return replace(plan, commands=tuple(commands), warnings=warnings, expected_port_mode=mode)
    except (SerialReadOnlyError, OSError) as exc:
        raise ConfigurationError("Não foi possível preparar a transição; nenhuma configuração foi enviada. " + str(exc)) from exc
    finally:
        connection.close()


def _configuration_blocks(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Require a complete configuration before interpreting absent settings."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "")
    raw = [line.strip() for line in text.splitlines() if line.strip()]
    if not any(re.fullmatch(r"sysname [A-Za-z0-9_.-]+", line) for line in raw) or "return" not in raw:
        raise ConfigurationError("Não foi possível confirmar uma configuração completa na leitura posterior.")
    lines = []
    blocks: dict[str, list[str]] = {}
    block = None
    for line in raw:
        if line == "#":
            block = None
            continue
        if line == "return":
            break
        if line.startswith(("<", "[", "display ", "Current configuration", "Saved configuration")):
            continue
        lines.append(line)
        if re.fullmatch(r"(?:interface .+|vlan [0-9]+)", line):
            block = re.sub(r"^interface Vlan-interface\s*([0-9]+)$", r"interface Vlan-interface \1", line, flags=re.I)
            blocks[block] = []
        elif line.startswith(("sysname ", "ip route-static ")):
            block = None
        elif block is not None:
            blocks[block].append(line)
    return lines, blocks


def _verify_result(connection, settings: SerialSettings, plan: ConfigurationPlan) -> str:
    """Verify observed state, not merely the absence of a CLI error."""
    def read(command):
        return _exchange(connection, command, settings)[0]

    if plan.action.startswith("set_") and any(re.fullmatch(r"interface " + PHYSICAL_INTERFACE.pattern, command) for command in plan.commands):
        interface = next(command.removeprefix("interface ") for command in plan.commands if command.startswith("interface "))
        text = read("display interface " + interface)
        mode = _port_mode(text, interface)
        pvids = re.findall(r"(?mi)^[ \t]*PVID[ \t]*:[ \t]*([0-9]{1,4})\b", text)
        if plan.action in {"set_access_port", "set_trunk_port", "set_hybrid_port"}:
            expected_mode = plan.action.removeprefix("set_").removesuffix("_port")
            expected_pvid = int(next(command.split()[-1] for command in plan.commands if re.fullmatch(r"port (?:access vlan|trunk pvid vlan|hybrid pvid vlan) [0-9]+", command)))
            if mode != expected_mode or len(pvids) != 1 or int(pvids[0]) != expected_pvid:
                raise ConfigurationError("Modo/PVID lido após aplicar difere do solicitado.")
            if plan.action in {"set_trunk_port", "set_hybrid_port"}:
                observed_tagged, observed_untagged = _interface_vlan_sections(text)
                if plan.action == "set_trunk_port":
                    allowed = set()
                    for command in plan.commands:
                        if command.startswith("port trunk permit vlan "):
                            allowed.update(_parse_vlan_values(command.removeprefix("port trunk permit vlan ")))
                    expected_tagged, expected_untagged = allowed - {expected_pvid}, {expected_pvid}
                else:
                    expected_tagged, expected_untagged = set(), set()
                    for command in plan.commands:
                        match = re.fullmatch(r"port hybrid vlan ([0-9 ]+) (tagged|untagged)", command)
                        if match:
                            (expected_tagged if match[2] == "tagged" else expected_untagged).update(_parse_vlan_values(match[1]))
                if observed_tagged != expected_tagged or observed_untagged != expected_untagged:
                    raise ConfigurationError("As VLANs lidas após aplicar diferem das listas solicitadas; resultado não confirmado.")
        elif plan.action == "set_port_description":
            expected = next(command.removeprefix("description ") for command in plan.commands if command.startswith("description "))
            if not re.search(r"(?mi)^\s*Description\s*:\s*" + re.escape(expected) + r"\s*$", text):
                raise ConfigurationError("Descrição lida após aplicar difere da solicitada.")
        else:
            # DOWN alone means no physical link, not necessarily shutdown.
            _, blocks = _configuration_blocks(read("display current-configuration"))
            key = "interface " + interface
            if key not in blocks:
                raise ConfigurationError("Interface ausente na configuração posterior; estado administrativo não confirmado.")
            shutdown = "shutdown" in blocks[key]
            if shutdown != ("shutdown" in plan.commands):
                raise ConfigurationError("Estado administrativo lido após aplicar difere do solicitado.")
        return "display interface " + interface

    current = read("display current-configuration")
    lines, blocks = _configuration_blocks(current)
    if plan.action in {"create_vlan", "rename_vlan", "delete_vlan"}:
        vlan = next(command.removeprefix("undo ") for command in plan.commands if re.fullmatch(r"(?:undo )?vlan [0-9]+", command))
        present = vlan in blocks
        if present != (plan.action != "delete_vlan"):
            raise ConfigurationError("Existência da VLAN após aplicar difere do solicitado.")
        descriptions = [command for command in plan.commands if command.startswith("description ")]
        if descriptions and descriptions[0] not in blocks.get(vlan, []):
            raise ConfigurationError("Descrição da VLAN após aplicar difere da solicitada.")
    elif plan.action == "set_sysname":
        expected = next(command for command in plan.commands if command.startswith("sysname "))
        if expected not in lines:
            raise ConfigurationError("Nome do equipamento após aplicar difere do solicitado.")
    elif plan.action == "set_management_ip":
        interface = next(command for command in plan.commands if command.startswith("interface "))
        expected = next(command for command in plan.commands if command.startswith("ip address "))
        if expected not in blocks.get(interface, []):
            raise ConfigurationError("IP de gestão após aplicar difere do solicitado.")
    elif plan.action in {"add_static_route", "delete_static_route"}:
        expected = next(command.removeprefix("undo ") for command in plan.commands if re.fullmatch(r"(?:undo )?ip route-static .+", command))
        if (expected in lines) != (plan.action == "add_static_route"):
            raise ConfigurationError("Rota estática após aplicar difere do solicitado.")
    elif plan.action == "save":
        saved, _ = _configuration_blocks(read("display saved-configuration"))
        if lines != saved:
            raise ConfigurationError("Configuração salva difere da configuração em execução.")
    else:
        raise ConfigurationError("A operação não possui verificação posterior implementada.")
    return "display saved-configuration" if plan.action == "save" else "display current-configuration"


def execute_plan(settings: SerialSettings, plan: ConfigurationPlan) -> list[dict[str, str]]:
    connection = _open(settings)
    output: list[dict[str, str]] = []
    completed: list[str] = []
    try:
        wake = open_console(connection, settings.timeout_seconds)
        _ensure_user_view(connection, settings, wake)
        version = _check_target(connection, settings, plan, require_writes=True)
        output.append({"command": "display version", "output": version})
        _verify_vlan_preconditions(connection, settings, plan)
        if plan.action in {"set_trunk_port", "set_hybrid_port"}:
            if plan.expected_port_mode is None:
                raise ConfigurationError("A transição requer uma prévia preparada com leitura do modo atual. Nenhuma configuração foi enviada.")
            interface = next(command.removeprefix("interface ") for command in plan.commands if command.startswith("interface "))
            observed, _ = _exchange(connection, "display interface " + interface, settings)
            if _port_mode(observed, interface) != plan.expected_port_mode:
                raise ConfigurationError("O modo da porta mudou desde a prévia. Gere uma nova prévia; nenhuma configuração foi enviada.")
        for command in plan.commands:
            response, confirmation = _exchange(connection, command, settings, allow_confirmation=(command == "save"))
            if confirmation:
                response, _ = _exchange(connection, "y", settings)
            completed.append(command)
            output.append({"command": command, "output": response})
        verified_command = _verify_result(connection, settings, plan)
        # Raw configuration may contain secrets; return only the verification result.
        output.append({"command": verified_command, "output": "Estado solicitado confirmado por leitura posterior."})
        return output
    except (ConfigurationError, SerialReadOnlyError, OSError) as exc:
        # Best effort: leave a command subview without issuing any other configuration changes.
        try:
            connection.write(b"return\r")
        except Exception:
            pass
        if completed:
            message = ("Operação não confirmada; pode haver alterações parciais, sem rollback automático. "
                       "Comandos concluídos: " + "; ".join(completed) + ". " + str(exc))
            if isinstance(exc, CommandRejected):
                raise CommandRejected(exc.command, message) from exc
            raise ConfigurationError(message) from exc
        raise
    finally:
        connection.close()


def export_current_configuration(settings: SerialSettings) -> tuple[bytes, str]:
    connection = _open(settings)
    try:
        open_console(connection, settings.timeout_seconds)
        version = _check_target(connection, settings)
        response, _ = _exchange(connection, "display current-configuration", settings)
        content = ("; Backup de leitura do Switch Manager\r\n; Criado em " + datetime.now(timezone.utc).isoformat() + "\r\n; Identificação do switch:\r\n" + version + "\r\n\r\n" + response).encode("utf-8", errors="replace")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return content, f"quidway-config-{timestamp}.txt"
    except SerialReadOnlyError as exc:
        raise ConfigurationError(str(exc)) from exc
    finally:
        connection.close()
