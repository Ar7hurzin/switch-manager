"""Exercise virtual hardware through the same console and HTTP routes."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_manager import manager, login
from serial_readonly import SerialSettings, collect_serial_diagnostics
from serial_config import prepare_plan, execute_plan, export_current_configuration
from switch_config import build_plan, ConfigurationError
from virtual_switch import snapshot, VirtualConsole

class VirtualTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{"QM_MODE":"virtual","QM_VIRTUAL_STATE":str(Path(self.tmp.name)/"state.json")})
        self.env.start(); self.addCleanup(self.env.stop)
        self.settings=SerialSettings("virtual",9600,3,virtual=True)
        self.mock=patch("serial.Serial",side_effect=AssertionError("physical serial must never open"))
        self.mock.start(); self.addCleanup(self.mock.stop)
        self.addCleanup(manager.cli_session.shutdown)
    def apply(self,action,**params):
        return execute_plan(self.settings,prepare_plan(self.settings,build_plan({"action":action,**params})))
    def test_vlan_port_and_persistence(self):
        self.assertEqual(len(snapshot()["ports"]),28)
        self.apply("create_vlan",vlan_id=100,name="LAB")
        self.assertEqual(snapshot()["vlans"][1]["name"], "LAB")
        self.apply("set_access_port",port=25,vlan_id=100)
        self.apply("set_port_description",port=25,description="Uplink test")
        self.apply("set_port_state",port=25,state="down")
        self.assertEqual(snapshot()["ports"][24]["pvid"],100)
        self.assertEqual(snapshot()["ports"][24]["state"],"down")
        self.assertEqual(snapshot()["ports"][24]["description"],"Uplink test")
        self.assertIn("100",collect_serial_diagnostics(self.settings)[1]["output"])
        self.apply("save")
        self.assertFalse(snapshot()["dirty"])
        self.assertTrue(snapshot()["last_saved"])
        self.assertIn(b"vlan 100",export_current_configuration(self.settings)[0])
        self.assertIn(b"description LAB",export_current_configuration(self.settings)[0])
        self.apply("rename_vlan",vlan_id=100,name="NEW")
        self.assertEqual(snapshot()["vlans"][1]["name"], "NEW")
        self.assertTrue(snapshot()["dirty"])
        self.apply("delete_vlan",vlan_id=100)
        self.assertEqual(snapshot()["ports"][24]["pvid"],1)
    def test_trunk_hybrid_ip_routes(self):
        for v in (10,20): self.apply("create_vlan",vlan_id=v)
        self.apply("set_trunk_port",port=2,vlans=[1,10,20],pvid=10)
        self.assertEqual(snapshot()["ports"][1]["tagged"],[1,20])
        self.apply("set_hybrid_port",port=3,tagged_vlans=[20],untagged_vlans=[10],pvid=10)
        self.assertEqual(snapshot()["ports"][2]["pvid"],10)
        self.apply("set_management_ip",vlan_id=10,ip="192.168.10.2",prefix=24)
        self.apply("add_static_route",destination="10.0.0.0",prefix=8,next_hop="192.168.10.1")
        self.assertEqual(snapshot()["ips"]["10"]["ip"],"192.168.10.2")
        self.assertEqual(len(snapshot()["routes"]),1)
        self.apply("delete_static_route",destination="10.0.0.0",prefix=8,next_hop="192.168.10.1")
        self.assertEqual(snapshot()["routes"],[])
    def test_rejections(self):
        with self.assertRaises(ConfigurationError): self.apply("set_access_port",port=1,vlan_id=999)
        self.apply("create_vlan",vlan_id=10)
        with self.assertRaises(ConfigurationError): self.apply("create_vlan",vlan_id=10)
        c=VirtualConsole(); c.write(b"not-supported\r\n")
        self.assertIn(b"Error:",c.read(4096))
    def test_api_end_to_end(self):
        manager._login_attempts.clear()
        c=manager.app.test_client()
        self.assertEqual(c.get("/api/virtual/state").status_code,401)
        headers={"X-CSRF-Token":login(c)}
        self.assertEqual(c.get("/api/status").json["mode"],"virtual")
        self.assertTrue(c.get("/api/config/status").json["writes_enabled"])
        missing=c.post("/api/config/preview",json={"action":"set_management_ip","vlan_id":2,"ip":"192.168.100.1","prefix":24},headers=headers)
        self.assertEqual(missing.status_code,400)
        self.assertIn("2",missing.json["error"])
        valid=c.post("/api/config/preview",json={"action":"set_management_ip","vlan_id":1,"ip":"192.168.100.1","prefix":24},headers=headers)
        self.assertEqual(valid.status_code,200)
        applied=c.post("/api/config/apply",json={"token":valid.json["token"]},headers=headers)
        self.assertEqual(applied.status_code,200)
        self.assertEqual(applied.json["snapshot"]["ips"]["1"]["ip"],"192.168.100.1")

        preview=c.post("/api/config/preview",json={"action":"create_vlan","vlan_id":42,"name":"TEST"},headers=headers)
        self.assertEqual(preview.status_code,200)
        result=c.post("/api/config/apply",json={"token":preview.json["token"]},headers=headers)
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json["snapshot"]["vlans"][1]["id"],42)
        self.assertEqual(c.post("/api/config/apply",json={"token":preview.json["token"]},headers=headers).status_code,400)
        self.assertTrue(c.post("/api/refresh",json={},headers=headers).json["ok"])
        cli=c.post("/api/cli/open",json={},headers=headers)
        self.assertEqual(cli.status_code,200,cli.json)
        token=cli.json["token"]
        for cmd in ("system-view","sysname Virtual-Lab","return","save","y"):
            response=c.post("/api/cli/send",json={"token":token,"command":cmd},headers=headers)
            self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(c.get("/api/virtual/state").json["name"],"Virtual-Lab")
        self.assertTrue(c.post("/api/cli/close",json={"token":token},headers=headers).json["ok"])
        self.assertEqual(c.post("/api/config/backup",json={},headers=headers).status_code,200)
        self.assertFalse(c.get("/api/virtual/state").json["dirty"])

if __name__=="__main__": unittest.main()
