"""Automatic switch identification, dynamic port inventory, switch swap and console login."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_console import (COM4500_VERSION, S3026C_VERSION, S3900_VERSION, FakeConsole, s3026c)
from switch_profiles import (LEGACY_PROFILE, S3900_INVENTORY, S3900_PROFILE, Inventory, identify,
                             parse_interface_list)
from physical_dashboard import (PhysicalDashboard, parse_mac_table, parse_port, parse_vlan_detail,
                                prompt_name)
from console_auth import ConsoleCredentials, ConsoleLoginFailed, ConsoleLoginRequired, open_console
from serial_readonly import SerialReadOnlyError, SerialSettings
from switch_config import ConfigurationError, build_plan
import device_capture

SETTINGS = SerialSettings("fake", 9600, 3)


class IdentifyTest(unittest.TestCase):
    def test_profiles(self):
        cases = {
            S3900_VERSION: ("quidway-s3900-si", True, "Quidway 3900-SI 28-Port"),
            S3026C_VERSION: ("vrp3-legacy", False, "Quidway S3026C"),
            COM4500_VERSION: ("vrp3-legacy", False, "3Com Switch 4500 26-Port"),
        }
        for text, (profile, writes, model) in cases.items():
            with self.subTest(model=model):
                device = identify(text)
                self.assertEqual((device.profile.id, device.profile.validated_writes, device.model),
                                 (profile, writes, model))
        self.assertEqual(identify(S3900_VERSION).inventory, S3900_INVENTORY)

    def test_unsupported_systems(self):
        for text in ("VRP (R) software, Version 5.110 (S5700 V200R001C00)\nQuidway S5700-28C-EI uptime is 1 week",
                     "Cisco IOS Software, C2960", "garbage"):
            with self.subTest(text=text[:20]):
                device = identify(text)
                self.assertFalse(device.profile.monitoring)
                self.assertFalse(device.profile.validated_writes)

    def test_other_s3900_release_is_not_trusted_for_writes(self):
        device = identify(S3900_VERSION.replace("RELEASE 0002", "RELEASE 0009"))
        self.assertEqual(device.profile, LEGACY_PROFILE)

    def test_inventory_discovery_and_order(self):
        inv = parse_interface_list("#\ninterface Ethernet0/10\n#\ninterface Ethernet0/2\n#\ninterface Aux0/0\n"
                                   "#\ninterface GigabitEthernet2/1\n#\ninterface GigabitEthernet1/1\n"
                                   "#\ninterface Vlan-interface1\n description Ethernet0/9\n")
        self.assertEqual(inv.names, ("Ethernet0/2", "Ethernet0/10", "GigabitEthernet1/1", "GigabitEthernet2/1"))
        self.assertEqual(inv.number("ethernet0/10"), 2)
        self.assertIsNone(parse_interface_list("nothing here"))
        from fake_console import S3026C_BRIEF
        brief = parse_interface_list(S3026C_BRIEF)
        self.assertEqual(len(brief), 26)
        self.assertEqual(brief.summary(), "24 Ethernet · 2 GigabitEthernet")

    def test_fingerprint_changes_with_switch(self):
        a = identify(S3026C_VERSION).with_inventory(Inventory(("Ethernet0/1",)))
        b = identify(COM4500_VERSION).with_inventory(Inventory(("Ethernet0/1",)))
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        self.assertNotEqual(a.identity_key, b.identity_key)

    def test_prompt_name_ignores_system_view(self):
        self.assertEqual(prompt_name(b"output\r\n<S3026C>"), "S3026C")
        self.assertIsNone(prompt_name(b"[S3900-Virtual]"))
        self.assertIsNone(prompt_name(b"[Quidway-Ethernet0/1]"))


class LegacyParsersTest(unittest.TestCase):
    inv = Inventory.from_names([f"Ethernet0/{n}" for n in range(1, 25)] + ["GigabitEthernet1/1", "GigabitEthernet2/1"])

    def test_port_vlan_and_mac_with_legacy_names(self):
        from fake_console import S3026C_PORT
        port = parse_port(S3026C_PORT, 1, self.inv)
        self.assertEqual((port["interface"], port["state"], port["pvid"], port["rx_bps"]), ("Ethernet0/1", "up", 10, 2000))
        self.assertIsNone(parse_port(S3026C_PORT, 2, self.inv))
        detail = parse_vlan_detail("VLAN ID: 10\nTagged Ports: GigabitEthernet1/1\nUntagged Ports: Ethernet0/1 Ethernet0/24", 10, self.inv)
        self.assertEqual((detail["tagged_numbers"], detail["untagged_numbers"]), ([25], [1, 24]))
        macs = parse_mac_table("000f-e212-3456  10  Learned  GigabitEthernet2/1  AGING", self.inv)
        self.assertEqual(macs["entries"][0]["port"], 26)

    def test_port_names_never_match_inside_longer_names(self):
        inv = Inventory.from_names(["Ethernet1/0/1", "GigabitEthernet1/0/1", "Ethernet1/0/10"])
        detail = parse_vlan_detail("VLAN ID: 5\nTagged Ports: none\nUntagged Ports: GigabitEthernet1/0/1 Ethernet1/0/10", 5, inv)
        self.assertEqual(sorted(inv.name(n) for n in detail["untagged_numbers"]), ["Ethernet1/0/10", "GigabitEthernet1/0/1"])

    def test_plan_uses_real_interface_name_and_bounds(self):
        plan = build_plan({"action": "set_port_description", "port": 26, "description": "uplink"}, self.inv, "abc")
        self.assertIn("interface GigabitEthernet2/1", plan.commands)
        self.assertEqual(plan.device_identity, "abc")
        with self.assertRaisesRegex(ConfigurationError, "26 portas"):
            build_plan({"action": "set_port_description", "port": 27, "description": "x"}, self.inv)
        # Default stays the validated S3900 layout.
        self.assertIn("interface GigabitEthernet1/1/4", build_plan({"action": "set_port_state", "port": 28, "state": "up"}).commands)


class DetectionTest(unittest.TestCase):
    def dashboard(self, console):
        holder = {"console": console}
        guard = patch("physical_dashboard.open_connection", side_effect=lambda s: holder["console"])
        guard.start()
        self.addCleanup(guard.stop)
        creds = patch("physical_dashboard.console_credentials", ConsoleCredentials())
        creds_obj = creds.start()
        self.addCleanup(creds.stop)
        return PhysicalDashboard(), holder, creds_obj

    def test_legacy_switch_is_detected_with_discovered_ports(self):
        dashboard, _, _ = self.dashboard(s3026c())
        result = dashboard.collect(SETTINGS, force=True)
        device = result["device"]
        self.assertEqual((device["profile"], device["model"], device["validated_writes"]), ("vrp3-legacy", "Quidway S3026C", False))
        self.assertEqual(len(result["snapshot"]["ports"]), 26)
        self.assertEqual(result["snapshot"]["ports"][25]["interface"], "GigabitEthernet2/1")
        self.assertEqual(result["snapshot"]["expected_ports"], 26)
        second = dashboard.collect(SETTINGS, force=True)  # round-robin reads Ethernet0/1
        port = second["snapshot"]["ports"][0]
        self.assertEqual((port["state"], port["pvid"], port["description"]), ("up", 10, "uplink"))

    def test_swapping_the_cable_reidentifies_and_notifies(self):
        dashboard, holder, _ = self.dashboard(s3026c())
        changes = []
        dashboard.listeners.append(changes.append)
        dashboard.collect(SETTINGS, force=True)
        self.assertEqual(dashboard.device.profile, LEGACY_PROFILE)
        holder["console"] = FakeConsole(S3900_VERSION, prompt="Quidway",
                                        responses={"display vlan": "Now, the following VLAN exist(s):\n 1(default)"})
        result = dashboard.collect(SETTINGS, force=True)
        self.assertEqual(result["device"]["profile"], "quidway-s3900-si")
        self.assertEqual(len(result["snapshot"]["ports"]), 28)
        self.assertEqual([d.profile.id for d in changes], ["quidway-s3900-si"])

    def test_same_prompt_different_switch_found_by_periodic_check(self):
        dashboard, holder, _ = self.dashboard(s3026c(prompt="Quidway"))
        changes = []
        dashboard.listeners.append(changes.append)
        dashboard.collect(SETTINGS, force=True)
        holder["console"] = FakeConsole(COM4500_VERSION, prompt="Quidway",
                                        responses={"display brief interface": "Ethernet1/0/1 UP\nEthernet1/0/2 DOWN"})
        dashboard.identity_checked = 0  # recheck is due
        dashboard.collect(SETTINGS, force=True)
        result = dashboard.collect(SETTINGS, force=True)
        self.assertEqual(result["device"]["model"], "3Com Switch 4500 26-Port")
        self.assertEqual(len(changes), 1)

    def test_same_prompt_swap_is_found_on_next_poll_after_unrecognized_port(self):
        s3900_ports = {f"display interface {name}": f"{name} current state : UP\nPVID: 1\nPort link-type: access"
                       for name in S3900_INVENTORY.names}
        dashboard, holder, _ = self.dashboard(FakeConsole(S3900_VERSION, prompt="Quidway", responses={
            "display vlan": "Now, the following VLAN exist(s):\n 1(default)", **s3900_ports}))
        changes = []
        dashboard.listeners.append(changes.append)
        dashboard.collect(SETTINGS, force=True)
        dashboard.collect(SETTINGS, force=True)
        self.assertEqual(dashboard.device.profile, S3900_PROFILE)
        self.assertFalse(dashboard.identity_suspect)
        # Same default prompt, periodic 60 s recheck not due yet.
        holder["console"] = s3026c(prompt="Quidway")
        dashboard.collect(SETTINGS, force=True)  # S3900 port name rejected by the S3026C
        self.assertTrue(dashboard.identity_suspect)
        dashboard.collect(SETTINGS, force=True)  # display version runs now
        result = dashboard.collect(SETTINGS, force=True)
        self.assertEqual(result["device"]["model"], "Quidway S3026C")
        self.assertEqual(len(result["snapshot"]["ports"]), 26)
        self.assertEqual([d.model for d in changes], ["Quidway S3026C"])

    def test_swap_back_to_s3900_is_identified_in_the_same_poll(self):
        dashboard, holder, _ = self.dashboard(s3026c(prompt="Quidway"))
        dashboard.collect(SETTINGS, force=True)
        dashboard.collect(SETTINGS, force=True)
        self.assertEqual(dashboard.brief_command, "display brief interface")
        brief = "\n".join(f"{name} UP" for name in S3900_INVENTORY.names)
        holder["console"] = FakeConsole(S3900_VERSION, prompt="Quidway", responses={
            "display brief interface": brief, "display vlan": "Now, the following VLAN exist(s):\n 1(default)"})
        result = dashboard.collect(SETTINGS, force=True)  # S3026C port names absent from the summary
        self.assertEqual(result["device"]["profile"], "quidway-s3900-si")
        self.assertEqual(len(result["snapshot"]["ports"]), 28)

    def test_console_failure_forces_identity_check_on_reconnect(self):
        dashboard, holder, _ = self.dashboard(s3026c(prompt="Quidway"))
        dashboard.collect(SETTINGS, force=True)
        dashboard.identity_suspect = False
        holder["console"] = FakeConsole("", prompt="Quidway")
        holder["console"].write = lambda data: len(data)  # unplugged: nothing answers
        with patch("physical_dashboard._read_until_prompt", side_effect=SerialReadOnlyError("Tempo esgotado")):
            dashboard.collect(SETTINGS, force=True)
        self.assertTrue(dashboard.identity_suspect)

    def test_unsupported_switch_is_reported_without_monitoring(self):
        dashboard, _, _ = self.dashboard(FakeConsole("Cisco IOS Software, C2960", prompt="Switch"))
        with self.assertRaises(SerialReadOnlyError):
            dashboard.collect(SETTINGS, force=True)
        result = dashboard.cached()
        self.assertFalse(result["device"]["monitoring"])
        self.assertIsNone(result["snapshot"])


class ConsoleLoginTest(unittest.TestCase):
    def test_login_flow_memory_only(self):
        console = s3026c(login=("admin", "segredo-console"))
        creds = ConsoleCredentials()
        with self.assertRaises(ConsoleLoginRequired):
            open_console(console, 3, creds)
        creds.set("admin", "segredo-console")
        raw = open_console(console, 3, creds)
        self.assertTrue(raw.rstrip().endswith(b"<S3026C>"))
        self.assertNotIn("segredo", repr(creds))

    def test_wrong_password_is_discarded_without_retry(self):
        console = s3026c(login=("admin", "certa"))
        creds = ConsoleCredentials()
        creds.set("admin", "errada")
        with self.assertRaises(ConsoleLoginFailed):
            open_console(console, 3, creds)
        self.assertFalse(creds.available)

    def test_credentials_are_dropped_when_another_switch_appears(self):
        creds = ConsoleCredentials()
        creds.set("admin", "x")
        creds.bind("switch-a")
        creds.bind("switch-a")
        self.assertTrue(creds.available)
        creds.bind("switch-b")
        self.assertFalse(creds.available)

    def test_dashboard_asks_confirmation_after_idle_timeout_and_never_exposes_secret(self):
        console = s3026c(login=("admin", "segredo-console"))
        creds = ConsoleCredentials()
        with patch("physical_dashboard.open_connection", return_value=console), \
             patch("physical_dashboard.console_credentials", creds), \
             patch("console_auth.console_credentials", creds):
            dashboard = PhysicalDashboard()
            with self.assertRaises(SerialReadOnlyError):
                dashboard.collect(SETTINGS, force=True)
            self.assertTrue(dashboard.cached()["login_required"])
            creds.set("admin", "segredo-console")
            with patch("physical_dashboard.open_console",
                       side_effect=lambda c, t, reader=None: open_console(c, t, creds, reader)):
                result = dashboard.collect(SETTINGS, force=True)
                self.assertFalse(result["login_required"])
                console.logout()
                # A new login prompt is never answered on its own: the operator confirms first.
                written = len(console.commands)
                result = dashboard.collect(SETTINGS, force=True)
                self.assertTrue(result["login_required"])
                self.assertIn("confirmar", result["message"])
                self.assertEqual(len(console.commands), written)
                self.assertTrue(dashboard.cached()["console_credentials"])
                self.assertTrue(creds.confirm())
                result = dashboard.collect(SETTINGS, force=True)
            self.assertFalse(result["login_required"])
            self.assertEqual(result["device"]["model"], "Quidway S3026C")
            self.assertNotIn("segredo", str(result))

    def test_stored_credentials_are_not_replayed_to_another_device(self):
        class Recorder(FakeConsole):
            def write(self, data):
                received.append(bytes(data))
                return super().write(data)

        received = []
        switch_a = s3026c(login=("admin", "DUMMY-SECRET"))
        creds = ConsoleCredentials()
        creds.set("admin", "DUMMY-SECRET")
        open_console(switch_a, 3, creds)
        creds.bind("switch-a")
        rogue = Recorder(COM4500_VERSION, login=("other", "other"))
        with self.assertRaises(ConsoleLoginRequired):
            open_console(rogue, 3, creds)
        self.assertFalse(any(b"DUMMY-SECRET" in chunk or b"admin" in chunk for chunk in received))
        self.assertTrue(creds.available)

    def test_confirm_needs_stored_credentials_and_arms_one_login(self):
        creds = ConsoleCredentials()
        self.assertFalse(creds.confirm())
        creds.set("admin", "x")
        self.assertTrue(creds.armed)
        self.assertEqual(creds.take_for_login(), ("admin", "x"))
        self.assertIsNone(creds.take_for_login())
        self.assertTrue(creds.confirm())
        self.assertEqual(creds.take_for_login(), ("admin", "x"))


class CaptureTest(unittest.TestCase):
    def test_allowlist(self):
        self.assertTrue(device_capture.allowed("display version"))
        self.assertTrue(device_capture.allowed("display interface Ethernet0/1"))
        for command in ("system-view", "save", "display interface Ethernet0/1\rsave", "reset saved-configuration"):
            with self.subTest(command=command):
                self.assertFalse(device_capture.allowed(command))

    def test_sanitize_removes_secrets(self):
        text = device_capture.sanitize(" local-user admin\n password cipher ABC123==\n snmp-agent community read public\n sysname Lab")
        self.assertNotIn("ABC123", text)
        self.assertNotIn("public", text)
        self.assertIn("sysname Lab", text)

    def test_sanitize_removes_snmp_trap_community(self):
        text = device_capture.sanitize(
            " snmp-agent target-host trap address udp-domain 192.0.2.10 params securityname DUMMYTRAP v2c\n"
            " snmp-agent target-host trap address udp-domain 192.0.2.11 params security-name DUMMYTRAP2\n"
            " snmp-agent sys-info version v2c")
        self.assertNotIn("DUMMYTRAP", text)
        self.assertEqual(text.count("<removido>"), 3)

    def test_capture_file(self):
        console = s3026c(responses={"display current-configuration":
                                    "#\n sysname S3026C\n super password level 3 cipher XYZ\n#\ninterface Ethernet0/1\n#\nreturn"})
        with tempfile.TemporaryDirectory() as tmp, patch("device_capture.open_connection", return_value=console):
            target = device_capture.capture(SETTINGS, ConsoleCredentials(), Path(tmp))
            text = target.read_text()
            if os.name == "posix":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertIn("perfil vrp3-legacy", text)
        self.assertIn("Quidway S3026C", text)
        self.assertNotIn("XYZ", text)
        self.assertIn("[COMANDO NÃO SUPORTADO]", text)  # display interface brief on this firmware
        self.assertTrue(all(device_capture.allowed(c) for c in console.commands))


if __name__ == "__main__":
    unittest.main()


class ExecutorIdentityTest(unittest.TestCase):
    def test_writes_follow_profile_and_identity(self):
        from serial_config import _check_target
        legacy = s3026c()
        legacy.write(b"\r"); legacy.read(4096)
        self.assertIn("S3026C", _check_target(legacy, SETTINGS, require_writes=True))  # adapted guided config
        cisco = FakeConsole("Cisco IOS Software, C2960", prompt="Switch")
        cisco.write(b"\r"); cisco.read(4096)
        with self.assertRaisesRegex(ConfigurationError, "use a CLI"):
            _check_target(cisco, SETTINGS, require_writes=True)
        s3900 = FakeConsole(S3900_VERSION)
        s3900.write(b"\r"); s3900.read(4096)
        plan = build_plan({"action": "set_sysname", "name": "lab"}, device_identity=identify(S3026C_VERSION).identity_key)
        with self.assertRaisesRegex(ConfigurationError, "não é o mesmo da prévia"):
            _check_target(s3900, SETTINGS, plan, require_writes=True)
        ok_plan = build_plan({"action": "set_sysname", "name": "lab"}, device_identity=identify(S3900_VERSION).identity_key)
        self.assertIn("RELEASE 0002", _check_target(s3900, SETTINGS, ok_plan, require_writes=True))


class AppDeviceRoutesTest(unittest.TestCase):
    def setUp(self):
        from test_manager import manager, login
        self.manager = manager
        manager._login_attempts.clear()
        self.client = manager.app.test_client()
        self.headers = {"X-CSRF-Token": login(self.client)}
        legacy = identify(S3026C_VERSION).with_inventory(LegacyParsersTest.inv)
        self.creds = ConsoleCredentials()
        for guard in (patch.dict(os.environ, QM_MODE="serial", QM_ENABLE_CONFIG_WRITES="true"),
                      patch.object(manager.physical_dashboard, "device", legacy),
                      patch.object(manager, "console_credentials", self.creds),
                      patch("serial.Serial", side_effect=AssertionError("no physical console"))):
            guard.start()
            self.addCleanup(guard.stop)

    def test_unvalidated_switch_gets_adapted_guided_config(self):
        status = self.client.get("/api/config/status").get_json()
        self.assertTrue(status["writes_enabled"])
        self.assertIn("adaptada", status["message"])
        self.assertEqual(status["device"]["model"], "Quidway S3026C")
        preview = self.client.post("/api/config/preview", json={"action": "set_sysname", "name": "lab"}, headers=self.headers)
        self.assertEqual(preview.status_code, 200)
        self.assertIn("ainda não validado", preview.get_json()["warnings"][0])

    def test_incompatible_system_is_cli_only(self):
        cisco = identify("Cisco IOS Software, C2960")
        with patch.object(self.manager.physical_dashboard, "device", cisco):
            status = self.client.get("/api/config/status").get_json()
            self.assertFalse(status["writes_enabled"])
            self.assertFalse(status["available"])
            self.assertEqual(self.client.post("/api/config/apply", json={"token": "x"}, headers=self.headers).status_code, 403)
            self.assertEqual(self.client.post("/api/config/preview", json={"action": "save"}, headers=self.headers).status_code, 409)

    def test_rejected_operation_becomes_cli_only_for_that_model(self):
        from serial_config import CommandRejected
        preview = self.client.post("/api/config/preview", json={"action": "set_port_description", "port": 1, "description": "uplink"},
                                   headers=self.headers).get_json()
        rejected = CommandRejected("description uplink", "Operação não confirmada; ... rejeitou description uplink.")
        with patch.object(self.manager, "execute_plan", side_effect=rejected):
            response = self.client.post("/api/config/apply", json={"token": preview["token"]}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.get_json()["cli_only"])
        status = self.client.get("/api/config/status").get_json()
        self.assertEqual(status["unsupported_actions"], {"set_port_description": "description uplink"})
        again = self.client.post("/api/config/preview", json={"action": "set_port_description", "port": 2, "description": "x"},
                                 headers=self.headers)
        self.assertEqual(again.status_code, 409)
        self.assertIn("CLI", again.get_json()["error"])
        # Other operations keep working, and only the admin can allow a retry.
        self.assertEqual(self.client.post("/api/config/preview", json={"action": "set_sysname", "name": "lab"},
                                          headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/api/config/capabilities/reset", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/config/status").get_json()["unsupported_actions"], {})

    def test_rejected_verification_display_does_not_block_operation(self):
        from serial_config import CommandRejected
        preview = self.client.post("/api/config/preview", json={"action": "set_sysname", "name": "lab"}, headers=self.headers).get_json()
        with patch.object(self.manager, "execute_plan", side_effect=CommandRejected("display current-configuration")):
            self.client.post("/api/config/apply", json={"token": preview["token"]}, headers=self.headers)
        self.assertEqual(self.client.get("/api/config/status").get_json()["unsupported_actions"], {})

    def test_preview_uses_detected_ports(self):
        response = self.client.post("/api/config/preview", json={"action": "set_port_state", "port": 26, "state": "down"},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIn("interface GigabitEthernet2/1", response.get_json()["commands"])
        bad = self.client.post("/api/config/preview", json={"action": "set_port_state", "port": 28, "state": "down"},
                               headers=self.headers)
        self.assertEqual(bad.status_code, 400)

    def test_device_endpoint_and_memory_only_credentials(self):
        device = self.client.get("/api/device").get_json()
        self.assertEqual((device["device"]["ports"][0], device["console_credentials"]), ("Ethernet0/1", False))
        response = self.client.post("/api/console/credentials", json={"username": "admin", "password": "segredo-console"},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("segredo", response.get_data(as_text=True))
        self.assertEqual(self.creds.get(), ("admin", "segredo-console"))
        self.assertNotIn("segredo", self.client.get("/api/device").get_data(as_text=True))
        self.assertTrue(self.client.get("/api/device").get_json()["console_credentials"])
        bad = self.client.post("/api/console/credentials", json={"username": "a", "password": "x\nsave"}, headers=self.headers)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.creds.take_for_login(), ("admin", "segredo-console"))
        self.assertFalse(self.client.get("/api/device").get_json()["console_credentials_armed"])
        confirm = self.client.post("/api/console/credentials/confirm", headers=self.headers)
        self.assertEqual(confirm.status_code, 200)
        self.assertNotIn("segredo", confirm.get_data(as_text=True))
        self.assertTrue(self.creds.armed)
        self.assertEqual(self.client.post("/api/console/credentials/confirm").status_code, 400)  # CSRF
        self.assertEqual(self.client.post("/api/console/credentials/forget", headers=self.headers).status_code, 200)
        self.assertFalse(self.creds.available)
        self.assertEqual(self.client.post("/api/console/credentials/confirm", headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post("/api/console/credentials", json={"password": "x"}).status_code, 400)  # CSRF

    def test_switch_change_drops_pending_previews(self):
        from switch_config import _PREVIEWS
        response = self.client.post("/api/config/preview", json={"action": "set_sysname", "name": "lab"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(_PREVIEWS)
        self.manager._on_switch_changed(identify(COM4500_VERSION))
        self.assertFalse(_PREVIEWS)


class ExecutorRejectionTest(unittest.TestCase):
    def test_first_rejected_command_stops_the_plan(self):
        from serial_config import CommandRejected, execute_plan
        legacy = s3026c(responses={"system-view": "", "interface Ethernet0/1": ""})  # 'description' unknown
        legacy.prompt = "S3026C"
        inv = LegacyParsersTest.inv
        plan = build_plan({"action": "set_port_description", "port": 1, "description": "uplink"}, inv,
                          identify(S3026C_VERSION).identity_key)
        with patch("serial_config.open_connection", return_value=legacy):
            with self.assertRaises(CommandRejected) as caught:
                execute_plan(SETTINGS, plan)
        self.assertEqual(caught.exception.command, "description uplink")
        self.assertIn("alterações parciais", str(caught.exception))
        self.assertNotIn("quit", legacy.commands)  # nothing after the rejected command


class HardeningTest(unittest.TestCase):
    def test_description_control_characters_rejected(self):
        for value in ("lab\rreboot", "lab\nsave", "lab\x00", "lab\x7f"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                build_plan({"action": "set_port_description", "port": 1, "description": value})

    def test_csp_header_and_no_inline_script(self):
        from test_manager import manager
        response = manager.app.test_client().get("/login")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        index = Path(__file__).resolve().parents[1] / "static" / "index.html"
        self.assertNotRegex(index.read_text(), r"<script>(?!</script>)")
        self.assertNotIn("<script>", response.get_data(as_text=True))

    def test_provider_redirect_to_plain_http_is_refused(self):
        from oidc_auth import _SecureRedirects
        from urllib.error import HTTPError
        from urllib.request import Request
        handler = _SecureRedirects()
        with self.assertRaises(HTTPError):
            handler.redirect_request(Request("https://idp.example.test/x"), None, 302, "Found", {}, "http://idp.example.test/x")
        self.assertIsNotNone(handler.redirect_request(Request("https://idp.example.test/x"), None, 302, "Found", {},
                                                      "https://idp.example.test/y"))
