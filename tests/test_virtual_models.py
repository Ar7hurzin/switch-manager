"""End-to-end guided configuration on every simulated VRP 3 switch, through the web API.

Each model runs the full catalog (preview → apply → read-back verification).
The 3Com 4500 simulation rejects the hybrid cleanup command: that operation
must become CLI-only for the model while every other operation keeps working.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_manager import manager, login

MODELS = {
    # model: (expected model text, ports, first port, last port, operations the switch rejects)
    "s3900": ("Quidway 3900-SI 28-Port", 28, "Ethernet1/0/1", "GigabitEthernet1/1/4", set()),
    "s3026c": ("Quidway S3026C", 26, "Ethernet0/1", "GigabitEthernet2/1", set()),
    "3com4500": ("3Com Switch 4500 26-Port", 26, "Ethernet1/0/1", "GigabitEthernet1/0/26", {"set_hybrid_port"}),
}


class VirtualModelsTest(unittest.TestCase):
    def run_model(self, model):
        expected_model, total, first, last, rejected = MODELS[model]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, QM_MODE="virtual", QM_VIRTUAL_MODEL=model,
                        QM_VIRTUAL_STATE=str(Path(tmp) / "state.json")), \
             patch.object(manager, "switch_capabilities",
                          manager.SwitchCapabilities(Path(tmp) / "caps.json")), \
             patch("serial.Serial", side_effect=AssertionError("no physical console")):
            manager._login_attempts.clear()
            client = manager.app.test_client()
            headers = {"X-CSRF-Token": login(client)}

            device = client.get("/api/device").get_json()["device"]
            self.assertEqual((device["model"], len(device["ports"]), device["ports"][0], device["ports"][-1]),
                             (expected_model, total, first, last))
            status = client.get("/api/config/status").get_json()
            self.assertTrue(status["writes_enabled"], status)

            def apply(payload):
                preview = client.post("/api/config/preview", json=payload, headers=headers)
                body = preview.get_json()
                if preview.status_code != 200:
                    return preview.status_code, body
                if not device["validated_writes"]:
                    self.assertTrue(any("ainda não validado" in w for w in body["warnings"]), body["warnings"])
                result = client.post("/api/config/apply", json={"token": body["token"]}, headers=headers)
                return result.status_code, {**result.get_json(), "commands": body["commands"]}

            operations = [
                {"action": "create_vlan", "vlan_id": 10, "name": "LAB"},
                {"action": "create_vlan", "vlan_id": 20, "name": "TEMP"},
                {"action": "rename_vlan", "vlan_id": 10, "name": "LAB2"},
                {"action": "set_access_port", "port": 1, "vlan_id": 10},
                {"action": "set_trunk_port", "port": total, "vlans": [1, 10], "pvid": 1},
                {"action": "set_hybrid_port", "port": 2, "tagged_vlans": [10], "untagged_vlans": [1], "pvid": 1},
                {"action": "set_port_description", "port": 3, "description": "uplink lab"},
                {"action": "set_port_state", "port": 4, "state": "down"},
                {"action": "set_sysname", "name": "LabSwitch"},
                {"action": "set_management_ip", "vlan_id": 10, "ip": "192.168.10.2", "prefix": 24},
                {"action": "add_static_route", "destination": "10.0.0.0", "prefix": 8, "next_hop": "192.168.10.1"},
                {"action": "delete_static_route", "destination": "10.0.0.0", "prefix": 8, "next_hop": "192.168.10.1"},
                {"action": "delete_vlan", "vlan_id": 20},
                {"action": "save"},
            ]
            for payload in operations:
                with self.subTest(model=model, action=payload["action"]):
                    code, body = apply(payload)
                    if payload["action"] in rejected:
                        self.assertEqual(code, 400, body)
                        self.assertTrue(body.get("cli_only"), body)
                        # Partial change is reported: commands applied before the rejection are listed.
                        self.assertIn("alterações parciais", body["error"])
                        self.assertIn("port link-type hybrid", body["error"])
                        again = client.post("/api/config/preview", json=payload, headers=headers)
                        self.assertEqual(again.status_code, 409)
                        self.assertIn(payload["action"], client.get("/api/config/status").get_json()["unsupported_actions"])
                    else:
                        self.assertEqual(code, 200, body)
                        self.assertIn("confirmado", json.dumps(body["outputs"], ensure_ascii=False))

            # Commands target this model's real port names.
            code, body = apply({"action": "set_port_description", "port": total, "description": "last port"})
            self.assertEqual(code, 200, body)
            self.assertIn("interface " + last, body["commands"])

            state = client.get("/api/virtual/state").get_json()
            ports = {p["number"]: p for p in state["ports"]}
            self.assertEqual(state["name"], "LabSwitch")
            self.assertEqual((ports[1]["mode"], ports[1]["pvid"]), ("access", 10))
            self.assertEqual(ports[total]["mode"], "trunk")
            self.assertEqual(ports[3]["description"], "uplink lab")
            self.assertFalse(ports[4]["admin"])
            self.assertEqual([v["id"] for v in state["vlans"]], [1, 10])
            self.assertEqual(state["ips"], {"10": {"ip": "192.168.10.2", "mask": "255.255.255.0"}})
            self.assertEqual(state["routes"], [])
            self.assertIsNotNone(state["last_saved"])  # "save" ran
            self.assertTrue(state["dirty"])            # the later port description is not saved yet
            # Known limit (no automatic rollback): when the switch rejects the hybrid cleanup,
            # the port is left in hybrid mode with default VLANs, as reported to the operator.
            self.assertEqual(ports[2]["mode"], "hybrid")
            if "set_hybrid_port" in rejected:
                self.assertEqual((ports[2]["tagged"], ports[2]["untagged"]), ([], [1]))
            else:
                self.assertEqual((ports[2]["tagged"], ports[2]["untagged"]), ([10], [1]))
            backup = client.post("/api/config/backup", headers=headers)
            self.assertEqual(backup.status_code, 200)
            self.assertIn(b"sysname LabSwitch", backup.data)

    def test_s3900(self):
        self.run_model("s3900")

    def test_s3026c(self):
        self.run_model("s3026c")

    def test_3com4500_rejected_operation_moves_to_cli(self):
        self.run_model("3com4500")

    def test_simulated_rejections_do_not_mark_real_switches(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, QM_MODE="virtual", QM_VIRTUAL_MODEL="3com4500",
                        QM_VIRTUAL_STATE=str(Path(tmp) / "state.json")), \
             patch.object(manager, "switch_capabilities", manager.SwitchCapabilities(Path(tmp) / "caps.json")):
            device = manager.current_device()
            manager.switch_capabilities.mark_unsupported(manager._capability_key(device), "set_hybrid_port", "x", "m")
            self.assertEqual(manager.switch_capabilities.unsupported(device.identity_key), {})


    def test_monitor_detects_each_simulated_switch_and_cable_swap(self):
        from physical_dashboard import PhysicalDashboard
        from serial_readonly import SerialSettings
        settings = SerialSettings("virtual", 9600, 3, virtual=True)
        dashboard = PhysicalDashboard()
        changes = []
        dashboard.listeners.append(lambda device: changes.append(device.model))
        with tempfile.TemporaryDirectory() as tmp:
            for model in ("s3026c", "3com4500", "s3900"):
                expected_model, total, first, last, _ = MODELS[model]
                with self.subTest(model=model), \
                     patch.dict(os.environ, QM_VIRTUAL_MODEL=model, QM_VIRTUAL_STATE=str(Path(tmp) / f"{model}.json")):
                    result = dashboard.collect(settings, force=True)
                    self.assertEqual(result["device"]["model"], expected_model)
                    names = [p["interface"] for p in result["snapshot"]["ports"]]
                    self.assertEqual((len(names), names[0], names[-1]), (total, first, last))
                    port = dashboard.collect(settings, force=True)["snapshot"]["ports"][0]
                    self.assertEqual(port["state"], "up")  # first port read through its real name
        self.assertEqual(changes, ["3Com Switch 4500 26-Port", "Quidway 3900-SI 28-Port"])


if __name__ == "__main__":
    unittest.main()
