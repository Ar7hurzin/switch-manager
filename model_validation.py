"""Validate the guided configuration of a switch model on the real equipment.

Run with the web server stopped (the serial port is exclusive):
    python start.py validate

Every guided operation is applied by the SAME executor the panel uses
(`serial_config.execute_plan`: target check, preconditions, stop at the first
rejected command, verification by reading the switch afterwards). Only a free
test port and two test VLANs that did not exist are touched, and everything is
undone at the end. Nothing is saved to the startup configuration unless the
operator explicitly chooses to test `save`: rebooting the switch without
saving always restores the configuration it had before the run.

Operations that passed are recorded per switch identity (profile + model +
firmware) in instance/validated-models.json. The panel stops showing the
"modelo ainda não validado" warning for them. Operations that were not tested
(management IP; static routes and save when not chosen) keep the warning.
"""
from __future__ import annotations

import contextlib
import getpass
import json
import os
import re
import sys
import tempfile
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import serial_config
from console_auth import ConsoleLoginFailed, ConsoleLoginRequired, console_credentials, open_console
from serial_config import (CommandRejected, _configuration_blocks, _ensure_user_view, _exchange, _existing_vlans,
                           _open, _port_mode, _query_vlan_presence, execute_plan, prepare_plan)
from serial_readonly import SerialReadOnlyError, SerialSettings
from switch_capabilities import SwitchCapabilities
from switch_config import ConfigurationError, build_plan
from switch_profiles import identify, parse_interface_list
from switch_text import decode_switch_text

# Every operation of the guided configuration area.
GUIDED_ACTIONS = ("create_vlan", "rename_vlan", "delete_vlan", "set_access_port", "set_trunk_port",
                  "set_hybrid_port", "set_port_description", "set_port_state", "set_sysname",
                  "set_management_ip", "add_static_route", "delete_static_route", "save")
DEFAULT_TEST_VLANS = (3998, 3999)
TEST_SYSNAME = "SM-VALIDACAO"
TEST_DESCRIPTION = "SM VALIDACAO"
TEST_ROUTE = ("192.0.2.0", 24)  # TEST-NET-1 (RFC 5737): never a real destination
# A port at factory defaults has no configuration lines, or only these.
DEFAULT_PORT_LINES = {"port link-type access", "port access vlan 1"}
VALIDATION_TIMEOUT_SECONDS = 60  # a full configuration at 9600 baud takes a while
MAX_TRANSCRIPT_BYTES = 2_000_000


def identity_key(device, virtual: bool) -> str:
    """Same key as the panel's capability store: simulators never validate real models."""
    return ("virtual:" if virtual else "") + device.identity_key


def default_path() -> Path:
    settings = Path(os.getenv("QM_SETTINGS_FILE", Path(__file__).with_name("local-settings.ps1"))).expanduser()
    return Path(os.getenv("QM_VALIDATION_FILE", settings.parent / "instance" / "validated-models.json")).expanduser()


class ValidatedModels:
    """Guided operations that passed a validation run, per switch identity."""

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
        fd, temporary = tempfile.mkstemp(prefix=".validated-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def actions(self, identity: str) -> set[str]:
        with self._lock:
            entry = self._load().get(identity)
        validated = entry.get("validated") if isinstance(entry, dict) else None
        return {a for a in validated if a in GUIDED_ACTIONS} if isinstance(validated, dict) else set()

    def record(self, identity: str, model: str, version: str, passed: dict[str, str], failed: dict[str, str]) -> None:
        """passed/failed: {action: detail}. A later failure revokes an earlier pass."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            data = self._load()
            entry = data.get(identity) if isinstance(data.get(identity), dict) else {}
            validated = entry.get("validated") if isinstance(entry.get("validated"), dict) else {}
            failures = entry.get("failed") if isinstance(entry.get("failed"), dict) else {}
            for action, detail in passed.items():
                validated[action] = {"at": stamp, "detail": detail}
                failures.pop(action, None)
            for action, error in failed.items():
                validated.pop(action, None)
                failures[action] = {"at": stamp, "error": error}
            data[identity] = {"model": model, "version": version, "validated": validated, "failed": failures}
            self._store(data)


class _Recording:
    """Serial connection wrapper that keeps what the switch printed (never what was typed:
    a console password is not echoed, so it never reaches the transcript)."""

    def __init__(self, connection, sink: bytearray):
        self._connection = connection
        self._sink = sink

    def read(self, size):
        data = self._connection.read(size)
        if data and len(self._sink) < MAX_TRANSCRIPT_BYTES:
            self._sink.extend(data)
        return data

    def write(self, data):
        return self._connection.write(data)

    def reset_input_buffer(self):
        return self._connection.reset_input_buffer()

    def close(self):
        return self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


@contextlib.contextmanager
def _recording(sink: bytearray):
    # The executor opens its own connections; record all of them for the report.
    original = serial_config.open_connection
    serial_config.open_connection = lambda settings: _Recording(original(settings), sink)
    try:
        yield
    finally:
        serial_config.open_connection = original


@dataclass
class Step:
    label: str
    action: str
    payload: dict
    # The run stops (and cleans up) if this step fails: later steps depend on it.
    essential: bool = False


@dataclass
class StepResult:
    step: Step
    ok: bool
    message: str
    rejected_command: str | None = None


@dataclass
class ValidationReport:
    model: str
    version: str
    identity: str
    port: str
    results: list[StepResult] = field(default_factory=list)
    passed: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    untested: list[str] = field(default_factory=list)
    cleanup_ok: bool = False
    cleanup_notes: list[str] = field(default_factory=list)
    backup_path: Path | None = None
    transcript_path: Path | None = None


class ValidationRefused(RuntimeError):
    """Preconditions not met; nothing was changed on the switch."""


def build_steps(port: int, vlan_a: int, vlan_b: int, original_sysname: str | None,
                route_next_hop: str | None, include_save: bool) -> list[Step]:
    steps = [
        Step(f"Criar VLAN {vlan_a} com descrição", "create_vlan", {"vlan_id": vlan_a, "name": "SM-TESTE-A"}, essential=True),
        Step(f"Alterar descrição da VLAN {vlan_a}", "rename_vlan", {"vlan_id": vlan_a, "name": "SM-TESTE-A2"}),
        Step(f"Criar VLAN {vlan_b} sem descrição", "create_vlan", {"vlan_id": vlan_b}, essential=True),
        Step(f"Porta {port} access na VLAN {vlan_a}", "set_access_port", {"port": port, "vlan_id": vlan_a}),
        Step(f"Porta {port} trunk ({vlan_a},{vlan_b}; PVID {vlan_a})", "set_trunk_port",
             {"port": port, "vlans": [vlan_a, vlan_b], "pvid": vlan_a}),
        Step(f"Porta {port} hybrid (tagged {vlan_a}, untagged {vlan_b}, PVID {vlan_b})", "set_hybrid_port",
             {"port": port, "tagged_vlans": [vlan_a], "untagged_vlans": [vlan_b], "pvid": vlan_b}),
        Step(f"Porta {port} de volta a access na VLAN 1", "set_access_port", {"port": port, "vlan_id": 1}),
        Step(f"Descrição da porta {port}", "set_port_description", {"port": port, "description": TEST_DESCRIPTION}),
        Step(f"Desativar porta {port} (shutdown)", "set_port_state", {"port": port, "state": "down"}),
        Step(f"Reativar porta {port} (undo shutdown)", "set_port_state", {"port": port, "state": "up"}),
    ]
    if original_sysname:
        steps += [
            Step(f"Nome do switch para {TEST_SYSNAME}", "set_sysname", {"name": TEST_SYSNAME}),
            Step(f"Nome do switch de volta para {original_sysname}", "set_sysname", {"name": original_sysname}),
        ]
    if route_next_hop:
        route = {"destination": TEST_ROUTE[0], "prefix": TEST_ROUTE[1], "next_hop": route_next_hop}
        steps += [
            Step(f"Adicionar rota de teste {TEST_ROUTE[0]}/{TEST_ROUTE[1]} via {route_next_hop}", "add_static_route", route),
            Step("Remover a rota de teste", "delete_static_route", route),
        ]
    steps += [
        Step(f"Excluir VLAN {vlan_b}", "delete_vlan", {"vlan_id": vlan_b}),
        Step(f"Excluir VLAN {vlan_a}", "delete_vlan", {"vlan_id": vlan_a}),
    ]
    if include_save:
        steps.append(Step("Salvar a configuração (save)", "save", {}))
    return steps


def _vlan_exists(connection, settings: SerialSettings, vlan: int) -> bool:
    if settings.virtual:
        return vlan in _existing_vlans(connection, settings)
    return _query_vlan_presence(connection, settings, vlan)


def _preflight(settings: SerialSettings, port: int, vlans: tuple[int, int], accept_link_up: Callable[[str], bool]):
    """Identify the switch and check the test port/VLANs. Sends display commands only."""
    connection = _open(settings)
    try:
        wake = open_console(connection, settings.timeout_seconds)
        _ensure_user_view(connection, settings, wake)
        version_text, _ = _exchange(connection, "display version", settings)
        device = identify(version_text)
        if not device.profile.monitoring or not device.profile.guided_config:
            raise ValidationRefused(f"{device.vendor} {device.model}: sem configuração guiada neste programa; nada a validar.")
        config, _ = _exchange(connection, "display current-configuration", settings)
        inventory = device.profile.fixed_inventory or parse_interface_list(config)
        if not inventory:
            raise ValidationRefused("Não consegui listar as portas do switch pela configuração atual.")
        device = device.with_inventory(inventory)
        if not 1 <= port <= len(inventory):
            raise ValidationRefused(f"Porta {port} não existe: este switch tem {len(inventory)} portas.")
        interface = inventory.name(port)
        lines, blocks = _configuration_blocks(config)
        extra = [line for line in blocks.get("interface " + interface, []) if line not in DEFAULT_PORT_LINES]
        if extra:
            raise ValidationRefused(f"A porta {port} ({interface}) não está na configuração de fábrica "
                                    f"({'; '.join(extra[:4])}). Escolha uma porta livre, sem configuração.")
        detail, _ = _exchange(connection, "display interface " + interface, settings)
        if _port_mode(detail, interface) != "access":
            raise ValidationRefused(f"A porta {port} não está em modo access.")
        if re.search(rf"(?mi)^[ \t]*{re.escape(interface)}[ \t]+current[ \t]+state[ \t]*:[ \t]*UP\b", detail):
            if not accept_link_up(interface):
                raise ValidationRefused(f"A porta {port} tem link ativo. Desconecte o cabo dela ou escolha outra porta.")
        for vlan in vlans:
            if _vlan_exists(connection, settings, vlan):
                raise ValidationRefused(f"A VLAN {vlan} já existe no switch. Escolha VLANs de teste que não existam.")
        sysname = next((line.split(" ", 1)[1] for line in lines if line.startswith("sysname ")), None)
        return device, interface, sysname, config
    except (CommandRejected, ConfigurationError) as exc:
        raise ValidationRefused(f"Leitura inicial não confirmada; nada foi alterado. {exc}") from exc
    finally:
        connection.close()


def _run_step(settings, device, step: Step, identity: str, capabilities: SwitchCapabilities,
              confirm_relogin: Callable[[], bool]) -> StepResult:
    for attempt in range(2):
        try:
            plan = build_plan({"action": step.action, **step.payload}, inventory=device.inventory,
                              device_identity=device.identity_key)
            plan = prepare_plan(settings, plan)
            execute_plan(settings, plan)
            return StepResult(step, True, "confirmado por leitura posterior")
        except ConsoleLoginRequired:
            # Asked before any command of the step was sent: safe to retry once.
            if attempt == 0 and console_credentials.available and confirm_relogin() and console_credentials.confirm():
                continue
            return StepResult(step, False, "o console pediu login novamente; etapa não executada")
        except CommandRejected as exc:
            if exc.command in plan.commands:
                capabilities.mark_unsupported(identity, step.action, exc.command, device.model)
            return StepResult(step, False, str(exc), rejected_command=exc.command)
        except (ConfigurationError, SerialReadOnlyError, OSError, ValueError) as exc:
            return StepResult(step, False, str(exc))
    return StepResult(step, False, "etapa não executada")


def _cleanup(settings, interface: str, vlans: tuple[int, int], original_sysname: str | None) -> tuple[bool, list[str]]:
    """Best effort: put the test port back to factory defaults, remove the test VLANs
    and restore the name. Success is decided only by reading the configuration
    afterwards; a refused cleanup command is just reported."""
    notes = []
    problems = []
    connection = _open(settings)
    try:
        wake = open_console(connection, settings.timeout_seconds)
        _ensure_user_view(connection, settings, wake)

        def send(command, *alternatives_before):
            try:
                _exchange(connection, command, settings)
                return True
            except CommandRejected:
                for alternative in alternatives_before:
                    try:
                        _exchange(connection, alternative, settings)
                    except CommandRejected:
                        pass
                if alternatives_before:
                    try:
                        _exchange(connection, command, settings)
                        return True
                    except CommandRejected:
                        pass
                notes.append(f"recusado na limpeza: {command}")
                return False

        _exchange(connection, "system-view", settings)
        _exchange(connection, "interface " + interface, settings)
        send("undo shutdown")
        send("undo description")
        send("port link-type access", "undo port trunk permit vlan all", "undo port hybrid vlan 1 to 4094")
        send("port access vlan 1")
        _exchange(connection, "quit", settings)
        for vlan in sorted(vlans, reverse=True):
            if _vlan_exists(connection, settings, vlan):
                send(f"undo vlan {vlan}")
        if original_sysname:
            send(f"sysname {original_sysname}")
        _exchange(connection, "return", settings)

        config, _ = _exchange(connection, "display current-configuration", settings)
        lines, blocks = _configuration_blocks(config)
        extra = [line for line in blocks.get("interface " + interface, []) if line not in DEFAULT_PORT_LINES]
        if extra:
            problems.append(f"{interface} ainda tem: {'; '.join(extra)}")
        for vlan in vlans:
            if f"vlan {vlan}" in blocks:
                problems.append(f"VLAN {vlan} ainda existe")
        if original_sysname and f"sysname {original_sysname}" not in lines:
            problems.append(f"o nome do switch não voltou para {original_sysname}")
        return not problems, problems + notes
    except (ConfigurationError, SerialReadOnlyError, OSError, ValueError) as exc:
        return False, [f"limpeza interrompida: {exc}"] + notes
    finally:
        connection.close()


def _private_file(path: Path, text: str) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def run_validation(settings: SerialSettings, port: int, *, vlans: tuple[int, int] = DEFAULT_TEST_VLANS,
                   route_next_hop: str | None = None, include_save: bool = False,
                   output_dir: Path, store: ValidatedModels | None = None,
                   capabilities: SwitchCapabilities | None = None,
                   accept_link_up: Callable[[str], bool] = lambda interface: False,
                   confirm_relogin: Callable[[], bool] = lambda: False,
                   progress: Callable[[str], None] = lambda line: None) -> ValidationReport:
    if len(set(vlans)) != 2 or any(not 2 <= v <= 4094 for v in vlans):
        raise ValidationRefused("Informe duas VLANs de teste diferentes entre 2 e 4094.")
    store = store or ValidatedModels()
    capabilities = capabilities or SwitchCapabilities()
    settings = replace(settings, timeout_seconds=max(settings.timeout_seconds, VALIDATION_TIMEOUT_SECONDS))
    transcript = bytearray()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with _recording(transcript):
        device, interface, sysname, config = _preflight(settings, port, vlans, accept_link_up)
        identity = identity_key(device, settings.virtual)
        slug = re.sub(r"[^A-Za-z0-9]+", "-", device.model).strip("-")[:40] or "switch"
        report = ValidationReport(device.model, device.version, identity, interface)
        # Full copy of the running configuration before any change (may contain secrets: mode 600).
        report.backup_path = _private_file(output_dir / f"backup-{slug}-{stamp}.txt", config)
        original_sysname = sysname
        try:
            build_plan({"action": "set_sysname", "name": sysname or ""})
        except ConfigurationError:
            original_sysname = None  # a name the guided form cannot type back: skip that test
        progress(f"Switch: {device.vendor} {device.model} · {device.version}")
        progress(f"Porta de teste: {port} ({interface}) · VLANs de teste: {vlans[0]} e {vlans[1]}")
        try:
            for step in build_steps(port, vlans[0], vlans[1], original_sysname, route_next_hop, include_save):
                if step.action == "save":
                    continue  # only after a verified cleanup, below
                result = _run_step(settings, device, step, identity, capabilities, confirm_relogin)
                report.results.append(result)
                progress(("  OK    " if result.ok else "  FALHA ") + step.label
                         + ("" if result.ok else f": {result.message}"))
                if not result.ok and step.essential:
                    progress("  Etapa essencial falhou: interrompendo e desfazendo as mudanças.")
                    break
        finally:
            report.cleanup_ok, report.cleanup_notes = _cleanup(settings, interface, vlans, sysname)
            progress("Limpeza confirmada: porta, VLANs e nome como antes."
                     if report.cleanup_ok else "ATENÇÃO, limpeza incompleta: " + "; ".join(report.cleanup_notes))
        if include_save and report.cleanup_ok:
            step = Step("Salvar a configuração (save)", "save", {})
            result = _run_step(settings, device, step, identity, capabilities, confirm_relogin)
            report.results.append(result)
            progress(("  OK    " if result.ok else "  FALHA ") + step.label + ("" if result.ok else f": {result.message}"))

    # An operation is validated only when every one of its steps passed.
    by_action: dict[str, list[StepResult]] = {}
    for result in report.results:
        by_action.setdefault(result.step.action, []).append(result)
    for action, results in by_action.items():
        failures = [r for r in results if not r.ok]
        if failures:
            report.failed[action] = failures[0].message
        else:
            report.passed[action] = f"{len(results)} etapa(s) na porta {interface}"
    report.untested = [a for a in GUIDED_ACTIONS if a not in by_action]
    if not report.cleanup_ok:
        # Results are kept for analysis but nothing is marked validated on an unclean run.
        report.passed = {}
    store.record(identity, device.model, device.version, report.passed, report.failed)

    from device_capture import sanitize
    summary = [f"; Validação da configuração guiada · {device.model} · {device.version}",
               f"; Porta {interface} · VLANs {vlans[0]}/{vlans[1]} · {stamp}", ""]
    summary += [("OK    " if r.ok else "FALHA ") + r.step.label + ("" if r.ok else f" :: {r.message}")
                for r in report.results]
    summary += ["", "Limpeza: " + ("confirmada" if report.cleanup_ok else "; ".join(report.cleanup_notes)),
                "Não testadas: " + (", ".join(report.untested) or "nenhuma"), "",
                "===== saída do console (sanitizada) ====="]
    body = "\n".join(summary) + "\n" + sanitize(decode_switch_text(bytes(transcript)).replace("\r", ""))
    report.transcript_path = _private_file(output_dir / f"validacao-{slug}-{stamp}.txt", body)
    return report


def _ask(prompt: str, default: str = "") -> str:
    value = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return value or default


def _yes(prompt: str) -> bool:
    return _ask(prompt + " (s/N)").lower() in {"s", "sim", "y", "yes"}


def main() -> int:
    settings_file = Path(os.environ.get("QM_SETTINGS_FILE", Path(__file__).with_name("local-settings.ps1"))).expanduser()
    output_dir = settings_file.parent / "instance" / "validation"
    try:
        settings = SerialSettings.from_environment()
    except SerialReadOnlyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Validação da configuração guiada em {settings.port} ({settings.baud_rate} baud).\n")
    print("Este roteiro ALTERA o switch durante o teste:")
    print("  - cria duas VLANs de teste que não existam e depois as exclui;")
    print("  - muda o modo, a descrição e o estado de UMA porta livre e depois a devolve à configuração de fábrica;")
    print("  - troca o nome do switch e depois volta ao nome original.")
    print("Antes de tudo, a configuração em execução é copiada para instance/validation/.")
    print("Nada é salvo na configuração de inicialização, a menos que você escolha testar o 'save':")
    print("se algo sair errado, reiniciar o switch sem salvar volta tudo ao que era.")
    print("Use um switch de laboratório ou uma porta sem nada conectado.\n")
    try:
        port = int(_ask("Número da porta livre para o teste (1, 2, ...)"))
        a = int(_ask("Primeira VLAN de teste", str(DEFAULT_TEST_VLANS[0])))
        b = int(_ask("Segunda VLAN de teste", str(DEFAULT_TEST_VLANS[1])))
    except ValueError:
        print("Informe números inteiros.", file=sys.stderr)
        return 2
    next_hop = _ask("Testar rotas estáticas? Informe um próximo salto IPv4 de uma rede do switch (Enter para pular)") or None
    include_save = _yes("Testar também o 'save'? Isso grava a configuração em execução atual na inicialização, "
                        "inclusive mudanças suas ainda não salvas")
    if not _yes("Confirma que pode alterar este switch agora"):
        print("Nada foi alterado.")
        return 1
    accept = lambda interface: _yes(f"{interface} está com link ativo; o que estiver ligado nela vai cair. Continuar")
    relogin = lambda: _yes("O console pediu login de novo. O cabo continua no mesmo switch")
    for attempt in range(2):
        try:
            report = run_validation(settings, port, vlans=(a, b), route_next_hop=next_hop, include_save=include_save,
                                    output_dir=output_dir, accept_link_up=accept, confirm_relogin=relogin,
                                    progress=print)
            break
        except ConsoleLoginRequired:
            if attempt:
                print("O console continuou pedindo login. Confira usuário e senha.", file=sys.stderr)
                return 2
            print("O console pede login. As credenciais ficam só na memória desta validação.")
            console_credentials.set(_ask("Usuário do switch (Enter se só pedir senha)"),
                                    getpass.getpass("Senha do console: "))
        except (ConsoleLoginFailed, ValidationRefused, SerialReadOnlyError, ConfigurationError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    console_credentials.clear()
    print()
    print("Validadas: " + (", ".join(sorted(report.passed)) or "nenhuma"))
    if report.failed:
        print("Falharam (passam a ser feitas pela CLI quando o switch recusou o comando):")
        for action, message in sorted(report.failed.items()):
            print(f"  {action}: {message}")
    print("Não testadas (o aviso continua nelas): " + (", ".join(report.untested) or "nenhuma"))
    print(f"\nRelatório com a saída do console: {report.transcript_path}")
    print(f"Cópia da configuração anterior: {report.backup_path}")
    if not report.cleanup_ok:
        print("\nA limpeza não foi confirmada; nada foi marcado como validado. Confira o switch pela CLI "
              "ou reinicie-o sem salvar para voltar à configuração de inicialização.", file=sys.stderr)
        return 1
    print("Reinicie o Switch Manager para o painel usar o resultado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
