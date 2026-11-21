"""Validated configuration plans for the Quidway S3900 console workflow.

The browser can only select one of these typed operations; arbitrary CLI text is never accepted.
Writes stay disabled unless QM_ENABLE_CONFIG_WRITES=true is set explicitly.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
import time
import threading
from dataclasses import dataclass, replace

from switch_profiles import S3900_INVENTORY, Inventory


class ConfigurationError(ValueError):
    """Safe, user-facing validation, firmware, or serial execution failure."""


@dataclass(frozen=True)
class ConfigurationPlan:
    action: str
    summary: str
    commands: tuple[str, ...]
    warnings: tuple[str, ...]
    expected_port_mode: str | None = None
    # identity_key of the switch the preview was built for; the executor refuses another one.
    device_identity: str | None = None


def _vlan_id(value: object) -> int:
    if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"[0-9]{1,4}", value))):
        raise ConfigurationError("O ID da VLAN deve ser um número entre 1 e 4094.")
    try:
        vlan_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("O ID de VLAN deve ser um número entre 1 e 4094.") from exc
    if not 1 <= vlan_id <= 4094:
        raise ConfigurationError("O ID da VLAN deve estar entre 1 e 4094.")
    return vlan_id


def _vlan_list(value: object, field: str = "VLANs") -> list[int]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise ConfigurationError(f"Selecione de 1 a 64 IDs para {field}.")
    values = sorted({_vlan_id(item) for item in value})
    return values


def _port(value: object, inventory: Inventory = S3900_INVENTORY) -> tuple[int, str]:
    total = len(inventory)
    if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"[0-9]{1,4}", value))):
        raise ConfigurationError(f"Selecione uma porta entre 1 e {total}.")
    number = int(value)
    if not 1 <= number <= total:
        raise ConfigurationError(f"Este switch tem {total} portas ({inventory.summary()}); use a numeração de 1 a {total}.")
    return number, inventory.name(number)


def _safe_text(value: object, label: str, max_length: int = 31) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"Informe {label}.")
    value = value.strip()
    if not value or len(value) > max_length or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ConfigurationError(f"{label.capitalize()} aceita de 1 a {max_length} caracteres: letras, números, ponto, hífen ou sublinhado.")
    return value


def _port_description(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("Informe a descrição da porta.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        # Reject (never silently rewrite) line breaks and control characters.
        raise ConfigurationError("A descrição não pode conter quebras de linha nem caracteres de controle.")
    value = " ".join(value.strip().split())
    if not value or len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9 _.-]+", value):
        raise ConfigurationError("A descrição aceita de 1 a 64 caracteres: letras, números, espaço, ponto, hífen ou sublinhado.")
    return value


def _vlan_words(values: list[int]) -> str:
    return " ".join(str(value) for value in values)


def _vlan_commands(prefix: str, values: list[int], suffix: str = "") -> list[str]:
    # The legacy S3900 command manual permits at most ten VLAN IDs/ranges per line.
    return [prefix + _vlan_words(values[i:i + 10]) + suffix
            for i in range(0, len(values), 10)]


def build_plan(payload: object, inventory: Inventory = S3900_INVENTORY,
               device_identity: str | None = None) -> ConfigurationPlan:
    """Typed plan for the identified switch; the port number indexes its real inventory."""
    plan = _build_plan(payload, inventory)
    return replace(plan, device_identity=device_identity)


def _build_plan(payload: object, inventory: Inventory) -> ConfigurationPlan:
    if not isinstance(payload, dict):
        raise ConfigurationError("A solicitação de configuração está inválida.")
    action = payload.get("action")
    if not isinstance(action, str):
        raise ConfigurationError("Operação de configuração não reconhecida.")
    warnings: list[str] = [
        "As mudanças entram imediatamente na configuração em execução. Use o console serial local para manter acesso ao switch.",
        "A sintaxe está baseada no Command Manual da série S3900; o release VRP 3.10(CN) RELEASE 0002 deste aparelho ainda requer validação específica antes de habilitar gravações.",
    ]
    if action == "create_vlan":
        vlan = _vlan_id(payload.get("vlan_id"))
        name = payload.get("name", "")
        commands = ["system-view", f"vlan {vlan}"]
        if name:
            commands.append(f"description {_safe_text(name, 'descrição da VLAN')}")
        commands.extend(("quit", "return"))
        return ConfigurationPlan(action, f"Criar VLAN {vlan}" + (f" ({name})" if name else ""), tuple(commands), tuple(warnings))

    if action == "rename_vlan":
        vlan = _vlan_id(payload.get("vlan_id"))
        name = _safe_text(payload.get("name"), "descrição da VLAN")
        commands = ("system-view", f"vlan {vlan}", f"description {name}", "quit", "return")
        return ConfigurationPlan(action, f"Alterar descrição da VLAN {vlan} para {name}", commands, tuple(warnings))

    if action == "delete_vlan":
        vlan = _vlan_id(payload.get("vlan_id"))
        if vlan == 1:
            raise ConfigurationError("A VLAN 1 padrão é protegida e não pode ser excluída pela interface.")
        warnings.extend(("A exclusão remove a VLAN e pode interromper todos os dispositivos associados.", "Confira portas e VLAN de gerenciamento antes de continuar."))
        commands = ("system-view", f"undo vlan {vlan}", "return")
        return ConfigurationPlan(action, f"Excluir VLAN {vlan}", commands, tuple(warnings))

    if action in {"set_access_port", "set_trunk_port", "set_hybrid_port", "set_port_description", "set_port_state"}:
        number, interface = _port(payload.get("port"), inventory)
        base = ["system-view", f"interface {interface}"]
        if action == "set_access_port":
            vlan = _vlan_id(payload.get("vlan_id"))
            commands = tuple(base + ["port link-type access", f"port access vlan {vlan}", "quit", "return"])
            summary = f"Configurar porta {number} como access na VLAN {vlan}"
            warnings.append("A troca para access remove o comportamento anterior de trunk/hybrid nesta porta.")
        elif action == "set_trunk_port":
            vlans = _vlan_list(payload.get("vlans"), "VLANs permitidas")
            pvid = _vlan_id(payload.get("pvid", 1))
            if pvid not in vlans:
                raise ConfigurationError("A VLAN nativa/PVID precisa estar incluída na lista permitida do trunk.")
            commands = tuple(base + ["port link-type trunk", "undo port trunk permit vlan all"]
                             + _vlan_commands("port trunk permit vlan ", vlans)
                             + [f"port trunk pvid vlan {pvid}", "quit", "return"])
            summary = f"Configurar porta {number} como trunk (PVID {pvid})"
            warnings.append("A lista substitui todas as VLANs anteriores: a prévia remove as permissões antes de reaplicá-las. Isso interrompe o tráfego momentaneamente; não há atomicidade nem rollback automático.")
        elif action == "set_hybrid_port":
            tagged = _vlan_list(payload.get("tagged_vlans"), "VLANs tagged") if payload.get("tagged_vlans") else []
            untagged = _vlan_list(payload.get("untagged_vlans"), "VLANs untagged") if payload.get("untagged_vlans") else []
            if not tagged and not untagged:
                raise ConfigurationError("Informe ao menos uma VLAN tagged ou untagged.")
            overlap = set(tagged) & set(untagged)
            if overlap:
                raise ConfigurationError("Uma VLAN não pode aparecer ao mesmo tempo como tagged e untagged.")
            pvid = _vlan_id(payload.get("pvid", 1))
            if pvid not in untagged:
                raise ConfigurationError("No modo hybrid, a PVID deve estar entre as VLANs untagged.")
            commands_list = base + ["port link-type hybrid", "undo port hybrid vlan 1 to 4094"]
            if tagged:
                commands_list.extend(_vlan_commands("port hybrid vlan ", tagged, " tagged"))
            if untagged:
                commands_list.extend(_vlan_commands("port hybrid vlan ", untagged, " untagged"))
            commands_list.extend((f"port hybrid pvid vlan {pvid}", "quit", "return"))
            commands = tuple(commands_list)
            summary = f"Configurar porta {number} como hybrid (PVID {pvid})"
            warnings.append("As listas substituem todas as VLANs anteriores: a prévia remove as associações antes de reaplicá-las. Isso interrompe o tráfego momentaneamente; não há atomicidade nem rollback automático.")
        elif action == "set_port_description":
            description = _port_description(payload.get("description"))
            commands = tuple(base + [f"description {description}", "quit", "return"])
            summary = f"Alterar descrição da porta {number}"
        else:
            state = payload.get("state")
            if not isinstance(state, str) or state not in {"up", "down"}:
                raise ConfigurationError("Estado da porta inválido.")
            command = "undo shutdown" if state == "up" else "shutdown"
            commands = tuple(base + [command, "quit", "return"])
            summary = f"{('Ativar' if state == 'up' else 'Desativar')} porta {number}"
            if state == "down":
                warnings.append("Desativar a porta interrompe imediatamente a conexão física dos dispositivos ligados a ela.")
        return ConfigurationPlan(action, summary, commands, tuple(warnings))

    if action == "set_sysname":
        name = _safe_text(payload.get("name"), "nome do equipamento")
        commands = ("system-view", f"sysname {name}", "return")
        return ConfigurationPlan(action, f"Alterar nome do switch para {name}", commands, tuple(warnings))

    if action == "set_management_ip":
        vlan = _vlan_id(payload.get("vlan_id"))
        try:
            interface = ipaddress.IPv4Interface(f"{payload.get('ip')}/{payload.get('prefix')}")
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("Informe um IPv4 válido e um prefixo de 1 a 30.") from exc
        if interface.network.prefixlen < 1 or interface.network.prefixlen > 30 or interface.ip.is_multicast or interface.ip.is_unspecified or interface.ip in {interface.network.network_address, interface.network.broadcast_address}:
            raise ConfigurationError("O endereço de gestão precisa ser IPv4 unicast com prefixo entre /1 e /30.")
        mask = str(interface.network.netmask)
        commands = ("system-view", f"interface Vlan-interface {vlan}", f"ip address {interface.ip} {mask}", "quit", "return")
        warnings.extend(("Esta alteração troca o endereço de gestão do switch. A sessão remota pode cair; use o console local e planeje o novo endereço antes de aplicar.", "O endereço anterior não é restaurado automaticamente."))
        return ConfigurationPlan(action, f"Alterar IP de gestão da VLAN {vlan} para {interface.ip}/{interface.network.prefixlen}", commands, tuple(warnings))

    if action in {"add_static_route", "delete_static_route"}:
        try:
            network = ipaddress.IPv4Network(f"{payload.get('destination')}/{payload.get('prefix')}", strict=False)
            next_hop = ipaddress.IPv4Address(str(payload.get("next_hop", "")))
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("Informe a rede IPv4 de destino, prefixo CIDR e próximo salto válidos.") from exc
        if next_hop.is_multicast or next_hop.is_unspecified or next_hop == ipaddress.IPv4Address("255.255.255.255"):
            raise ConfigurationError("O próximo salto precisa ser um endereço IPv4 unicast válido.")
        route = f"ip route-static {network.network_address} {network.netmask} {next_hop}"
        if action == "add_static_route":
            commands = ("system-view", route, "return")
            summary = f"Adicionar rota estática para {network} via {next_hop}"
            warnings.extend(("A rota altera o encaminhamento IPv4 e pode interromper o gerenciamento remoto se o destino ou próximo salto estiver incorreto.", "Este S3900-SI oferece recursos de roteamento limitados em comparação ao modelo EI. Confirme a topologia e a compatibilidade no firmware instalado."))
        else:
            commands = ("system-view", f"undo {route}", "return")
            summary = f"Excluir rota estática para {network} via {next_hop}"
            warnings.extend(("A exclusão pode remover o caminho usado por redes e serviços remotos.", "Confira a tabela de rotas e mantenha o console serial disponível."))
        return ConfigurationPlan(action, summary, commands, tuple(warnings))

    if action == "save":
        warnings.append("Salvar grava a configuração atual no arquivo de inicialização do switch; confirme depois de revisar as mudanças em execução.")
        return ConfigurationPlan(action, "Salvar a configuração atual no switch", ("save",), tuple(warnings))

    raise ConfigurationError("Operação de configuração não reconhecida.")


_PREVIEWS: dict[str, tuple[float, ConfigurationPlan, str]] = {}
_PREVIEWS_LOCK = threading.Lock()


def issue_preview(plan: ConfigurationPlan, lifetime_seconds: int = 300, *, owner: object) -> str:
    if not isinstance(owner, str) or not owner:
        raise ConfigurationError("Sessão inválida. Entre novamente.")
    now = time.monotonic()
    with _PREVIEWS_LOCK:
        for key, (expiry, _, _) in list(_PREVIEWS.items()):
            if expiry <= now:
                _PREVIEWS.pop(key, None)
        if len(_PREVIEWS) >= 256:
            raise ConfigurationError("Limite de prévias atingido. Aguarde a expiração de uma prévia.")
        token = secrets.token_urlsafe(32)
        _PREVIEWS[token] = (now + lifetime_seconds, plan, owner)
        return token


def clear_previews() -> None:
    """Drop every pending preview (e.g. a different switch was connected)."""
    with _PREVIEWS_LOCK:
        _PREVIEWS.clear()


def consume_preview(token: object, *, owner: object) -> ConfigurationPlan:
    if not isinstance(token, str) or not isinstance(owner, str):
        raise ConfigurationError("Prévia ausente. Gere uma nova prévia.")
    with _PREVIEWS_LOCK:
        entry = _PREVIEWS.get(token)
        if not entry or entry[0] <= time.monotonic() or entry[2] != owner:
            raise ConfigurationError("A prévia expirou, já foi usada ou pertence a outra sessão. Gere uma nova prévia.")
        del _PREVIEWS[token]
        return entry[1]
