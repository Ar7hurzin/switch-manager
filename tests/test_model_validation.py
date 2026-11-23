"""`python start.py validate`: guided operations tested on the switch, then undone.

Runs against the simulated switches (same executor, preconditions and read-back
verification as the panel). The real S3026C run is what marks the real model.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_manager import manager, login
from model_validation import (GUIDED_ACTIONS, ValidatedModels, ValidationRefused, run_validation)
from serial_config import execute_plan, prepare_plan
from serial_readonly import SerialSettings
from switch_capabilities import SwitchCapabilities
from switch_config import build_plan
from switch_profiles import Inventory
import virtual_switch

SETTINGS = SerialSettings("virtual", 9600, 3, virtual=True)
FREE_PORT = 5  # no link and factory defaults in every simulated model


def fast():
    # The simulator answers at once; the settle time only matters on a real console.
    patch("serial_config.PROMPT_SETTLE_SECONDS", 0).start()
    patch("serial_readonly.PROMPT_SETTLE_SECONDS", 0).start()


class ValidationRunTest(unittest.TestCase):
    def use_model(self, model):
        self.tmp = Path(tempfile.mkdtemp())
        env = patch.dict(os.environ, QM_VIRTUAL_MODEL=model, QM_VIRTUAL_STATE=str(self.tmp / "state.json"))
        env.start()
        self.addCleanup(env.stop)
        guard = patch("serial.Serial", side_effect=AssertionError("physical serial must never open"))
        guard.start()
        self.addCleanup(guard.stop)
        self.store = ValidatedModels(self.tmp / "validated.json")
        self.capabilities = SwitchCapabilities(self.tmp / "caps.json")
        fast()
        self.addCleanup(patch.stopall)

    def run_it(self, port=FREE_PORT, **kwargs):
        return run_validation(SETTINGS, port, output_dir=self.tmp / "out", store=self.store,
                              capabilities=self.capabilities, **kwargs)

    def state(self):
        state = virtual_switch._load()
        return {k: v for k, v in state.items() if k not in {"saved", "last_saved"}}

    def test_full_run_validates_tested_operations_and_restores_the_switch(self):
        self.use_model("s3026c")
        before = self.state()
        report = self.run_it(route_next_hop="10.0.0.1")
        self.assertTrue(report.cleanup_ok, report.cleanup_notes)
        self.assertEqual(report.failed, {})
        self.assertEqual(set(report.untested), {"set_management_ip", "save"})
        self.assertEqual(self.state(), before)  # port, VLANs, name and routes exactly as before
        identity = report.identity
        self.assertTrue(identity.startswith("virtual:"))  # a simulation never validates the real model
        self.assertEqual(self.store.actions(identity), set(GUIDED_ACTIONS) - {"set_management_ip", "save"})
        self.assertIn("sysname S3026C-Virtual", report.backup_path.read_text(encoding="utf-8"))
        self.assertEqual(report.backup_path.stat().st_mode & 0o777, 0o600)
        transcript = report.transcript_path.read_text(encoding="utf-8")
        self.assertIn("OK    Porta 5 hybrid", transcript)
        self.assertIn("port hybrid pvid vlan 3999", transcript)

    def test_save_runs_only_after_a_verified_cleanup_and_routes_need_a_next_hop(self):
        self.use_model("s3026c")
        report = self.run_it(include_save=True)
        self.assertEqual(set(report.untested), {"set_management_ip", "add_static_route", "delete_static_route"})
        self.assertIn("save", report.passed)
        self.assertEqual(report.results[-1].step.action, "save")
        saved = virtual_switch._load()["saved"]
        self.assertNotIn("3998", saved["vlans"])  # what got saved is the restored configuration

    def test_rejected_command_marks_cli_only_and_still_restores_the_port(self):
        self.use_model("3com4500")  # rejects `undo port hybrid vlan 1 to 4094`
        before = self.state()
        report = self.run_it()
        self.assertIn("set_hybrid_port", report.failed)
        self.assertNotIn("set_hybrid_port", self.store.actions(report.identity))
        self.assertIn("set_trunk_port", report.passed)
        rejected = self.capabilities.unsupported(report.identity)
        self.assertEqual(rejected["set_hybrid_port"]["command"], "undo port hybrid vlan 1 to 4094")
        self.assertTrue(report.cleanup_ok, report.cleanup_notes)
        self.assertEqual(self.state(), before)

    def test_a_later_failure_revokes_an_earlier_validation(self):
        self.use_model("s3026c")
        self.store.record("id", "M", "V", {"set_hybrid_port": "ok"}, {})
        self.store.record("id", "M", "V", {}, {"set_hybrid_port": "recusado"})
        self.assertEqual(self.store.actions("id"), set())
        data = json.loads((self.tmp / "validated.json").read_text(encoding="utf-8"))
        self.assertEqual(data["id"]["failed"]["set_hybrid_port"]["error"], "recusado")

    def test_configured_port_is_refused_without_changes(self):
        self.use_model("s3026c")
        inventory = Inventory(tuple(virtual_switch.port_names()))
        execute_plan(SETTINGS, build_plan({"action": "set_port_description", "port": 6, "description": "servidor"},
                                          inventory=inventory))
        before = self.state()
        with self.assertRaisesRegex(ValidationRefused, "configuração de fábrica"):
            self.run_it(port=6)
        self.assertEqual(self.state(), before)
        self.assertFalse((self.tmp / "validated.json").exists())

    def test_existing_test_vlan_is_refused_without_changes(self):
        self.use_model("s3026c")
        execute_plan(SETTINGS, prepare_plan(SETTINGS, build_plan({"action": "create_vlan", "vlan_id": 3999})))
        before = self.state()
        with self.assertRaisesRegex(ValidationRefused, "VLAN 3999 já existe"):
            self.run_it()
        self.assertEqual(self.state(), before)

    def test_port_with_link_needs_explicit_confirmation(self):
        self.use_model("s3026c")  # port 1 has link in the simulation
        asked = []
        with self.assertRaisesRegex(ValidationRefused, "link ativo"):
            self.run_it(port=1, accept_link_up=lambda interface: asked.append(interface) or False)
        self.assertEqual(asked, ["Ethernet0/1"])

    def test_invalid_port_and_vlans_are_refused(self):
        self.use_model("s3026c")
        with self.assertRaisesRegex(ValidationRefused, "não existe"):
            self.run_it(port=27)
        with self.assertRaises(ValidationRefused):
            self.run_it(vlans=(1, 3999))
        with self.assertRaises(ValidationRefused):
            self.run_it(vlans=(3999, 3999))


class PanelUsesValidationTest(unittest.TestCase):
    def test_validated_operations_lose_the_warning_and_the_banner_counts_them(self):
        fast()
        self.addCleanup(patch.stopall)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, QM_MODE="virtual", QM_VIRTUAL_MODEL="s3026c",
                        QM_VIRTUAL_STATE=str(Path(tmp) / "state.json")), \
             patch.object(manager, "switch_capabilities", SwitchCapabilities(Path(tmp) / "caps.json")), \
             patch.object(manager, "validated_models", ValidatedModels(Path(tmp) / "validated.json")), \
             patch("serial.Serial", side_effect=AssertionError("no physical console")):
            manager._login_attempts.clear()
            client = manager.app.test_client()
            headers = {"X-CSRF-Token": login(client)}

            def warned(payload):
                body = client.post("/api/config/preview", json=payload, headers=headers).get_json()
                return any("ainda não validado" in w for w in body["warnings"])

            device = client.get("/api/device").get_json()["device"]
            self.assertEqual(device["validated_actions"], [])
            self.assertFalse(device["validated_writes"])
            self.assertTrue(warned({"action": "create_vlan", "vlan_id": 50}))

            report = run_validation(SETTINGS, FREE_PORT, output_dir=Path(tmp) / "out",
                                    store=manager.validated_models, capabilities=manager.switch_capabilities)
            self.assertTrue(report.cleanup_ok)

            device = client.get("/api/device").get_json()["device"]
            self.assertIn("create_vlan", device["validated_actions"])
            self.assertNotIn("set_management_ip", device["validated_actions"])
            self.assertFalse(device["validated_writes"])  # management IP was not tested
            self.assertFalse(warned({"action": "create_vlan", "vlan_id": 50}))
            self.assertTrue(warned({"action": "set_management_ip", "vlan_id": 1, "ip": "192.168.100.1", "prefix": 24}))

    def test_serial_status_counts_validated_operations(self):
        device = manager.identify(virtual_switch.MODELS["s3026c"]["version"]).with_inventory(
            Inventory(tuple(virtual_switch.MODELS["s3026c"]["ports"])))
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, QM_MODE="serial", QM_ENABLE_CONFIG_WRITES="true"), \
             patch.object(manager.physical_dashboard, "device", device), \
             patch.object(manager, "validated_models", ValidatedModels(Path(tmp) / "validated.json")):
            manager.validated_models.record(device.identity_key, device.model, device.version,
                                            {"create_vlan": "ok", "set_access_port": "ok"}, {})
            manager._login_attempts.clear()
            client = manager.app.test_client()
            login(client)
            status = client.get("/api/config/status").get_json()
            self.assertIn(f"validada neste switch em 2 de {len(GUIDED_ACTIONS)}", status["message"])
            self.assertEqual(status["device"]["validated_actions"], ["create_vlan", "set_access_port"])

    def test_validated_profile_reports_every_operation(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, QM_MODE="virtual", QM_VIRTUAL_MODEL="s3900",
                        QM_VIRTUAL_STATE=str(Path(tmp) / "state.json")), \
             patch("serial.Serial", side_effect=AssertionError("no physical console")):
            manager._login_attempts.clear()
            client = manager.app.test_client()
            login(client)
            device = client.get("/api/device").get_json()["device"]
            self.assertTrue(device["validated_writes"])
            self.assertEqual(device["validated_actions"], list(GUIDED_ACTIONS))


if __name__ == "__main__":
    unittest.main()
