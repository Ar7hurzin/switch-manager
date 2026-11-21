"""Physical dashboard regression tests with captured-format VRP responses."""
import unittest
from unittest.mock import patch, Mock
from datetime import datetime, timedelta, timezone
from physical_dashboard import (PhysicalDashboard, parse_identity, parse_port,
                                parse_brief, parse_vlan_detail, parse_mac_table,
                                reconcile_vlan_members)
from serial_config import _parse_vlan_inventory
from serial_readonly import SerialSettings, SerialReadOnlyError

VERSION = """VRP (R) Software, Version 3.10(CN), RELEASE 0002
Quidway 3900-SI 28-Port uptime is 0 week,0 day,0 hour,32 minutes
Hardware Version is SI_REV.C
CPLD Version is CPLD 001
Bootrom Version is 213"""
VLAN = "Now, the following VLAN exist(s):\n  1(default), 11-12,\u25ab\n<Quidway>"
PORT = """Ethernet1/0/1 current state : UP
PVID: 12
Port link-type: access
 Tagged VLAN ID : none
 Untagged VLAN ID : 12
Last 300 seconds input: 0 packets/sec 0 bytes/sec
Last 300 seconds output: 1 packets/sec 100 bytes/sec"""
DETAIL = """VLAN ID: 12
VLIN Type: static
Description: teste_notebook
Tagged Ports: none
Untagged Ports: Ethernet1/0/1"""

class PhysicalDashboardTest(unittest.TestCase):
    def test_inventory_ranges(self):
        self.assertEqual(_parse_vlan_inventory(VLAN), {1,11,12})
        self.assertEqual(_parse_vlan_inventory("Following VLAN exist(s):\n1(default), 2-4, 4094,\n<Quidway>"), {1,2,3,4,4094})

    def test_identity_and_details(self):
        self.assertEqual(parse_identity(VERSION)["hardware"], "SI_REV.C")
        with self.assertRaises(SerialReadOnlyError):
            parse_identity("other switch")
        p = parse_port(PORT, 1)
        self.assertEqual((p["state"],p["pvid"],p["mode"],p["tx_bps"]), ("up",12,"access",800))
        self.assertEqual(p["untagged"],"12")
        self.assertIsNone(parse_port("PVID: 12\nPort link-type: access",1))
        self.assertIsNone(parse_port("Error: rejected",1))
        self.assertEqual(parse_vlan_detail(DETAIL,12)["name"],"teste_notebook")
        self.assertIsNone(parse_vlan_detail(DETAIL,11))
        self.assertEqual(parse_brief("Ethernet1/0/1 UP 100M\nEthernet1/0/2 DOWN\n")[2]["state"],"down")

    def test_multiline_vlan_members_damaged_serial_and_individual_port(self):
        raw = """VLAN ID: 12
 Description: teste0001
 Tagged   Ports: none
 Untagged Ports:
             Ethernet1/�/1         Ethernet1/0/2         Ethernet1/0/3 """
        detail = parse_vlan_detail(raw, 12)
        self.assertEqual(detail["name"], "teste0001")
        self.assertEqual(detail["tagged_numbers"], [])
        self.assertEqual(detail["untagged_numbers"], [2, 3])
        self.assertTrue(detail["membership_partial"])
        self.assertEqual(detail["members"], "Untagged: 2, 3 (leitura parcial)")

        # The missing port must only be added after separate CLI confirmation.
        individual = parse_port(
            "Ethernet1/0/1 current state : UP\n"
            "Mdi type: autoJ Port link-type: access\nPVID: 12",
            1,
        )
        self.assertEqual(individual["mode"], "access")
        self.assertEqual(individual["pvid"], 12)
        data = {"vlans": [detail], "ports": [individual]}
        reconcile_vlan_members(data)
        self.assertEqual(data["vlans"][0]["members"],
                         "Untagged: 1, 2, 3 (leitura parcial)")
        self.assertEqual(data["vlans"][0]["membership_from_ports"], [1])

    def test_vlan_does_not_invent_members_from_corrupt_name(self):
        detail = parse_vlan_detail(
            "VLAN ID: 12\nTagged Ports: none\nUntagged Ports: "
            "Ethernet1/�/1", 12
        )
        self.assertIsNone(detail["members"])
        self.assertTrue(detail["membership_partial"])
        self.assertEqual(detail["untagged_numbers"], [])
        empty = parse_vlan_detail(
            "VLAN ID: 12\nTagged Ports: none\nUntagged Ports: none", 12
        )
        self.assertEqual(empty["members"], "Nenhuma")
        self.assertFalse(empty["membership_partial"])

    def test_mac_table(self):
        result = parse_mac_table("00e0-fc12-3456 12 Learned Ethernet1/0/1 AGING\n00e0-fc12-3456 11 Dynamic Ethernet1/0/2\n")
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["entries"]), 2)
        self.assertIsNone(parse_mac_table("Unrecognized command"))
        self.assertEqual(parse_mac_table("Total MAC addresses: 0")["count"], 0)

    def make_collector(self):
        dashboard = PhysicalDashboard()
        conn = Mock()
        conn.__enter__ = Mock(return_value=conn)
        conn.__exit__ = Mock(return_value=False)
        fixtures = {"display version": VERSION, "display vlan": VLAN,
                    "display vlan 12": DETAIL, "display mac-address": "Total MAC addresses: 0"}
        def read(*args, **kwargs):  # mirrors _read_until_prompt(connection, timeout, ..., accept_login=...)
            command = conn.write.call_args.args[0].decode().strip()
            if not command:
                return b"<Quidway>"
            result = fixtures.get(command)
            if result is None and command.startswith("display interface "):
                name = command.removeprefix("display interface ")
                result = PORT.replace("Ethernet1/0/1", name)
            if result is None and command.startswith("display vlan "):
                vlan = int(command.split()[-1])
                result = f"VLAN ID: {vlan}\nTagged Ports: none\nUntagged Ports: none"
            if isinstance(result, Exception):
                raise result
            return result.encode()
        opened = patch("physical_dashboard.open_connection", return_value=conn)
        reader = patch("physical_dashboard._read_until_prompt", side_effect=read)
        opened.start(); reader.start()
        self.addCleanup(opened.stop); self.addCleanup(reader.stop)
        return dashboard, conn, fixtures, SerialSettings("fake", 9600, 3)

    def test_read_only_cache_and_fair_ports(self):
        d, conn, fixtures, settings = self.make_collector()
        first = d.collect(settings)
        self.assertEqual(len(first["snapshot"]["ports"]), 28)
        writes = conn.write.call_count
        self.assertEqual(d.collect(settings)["snapshot"], first["snapshot"])
        self.assertEqual(conn.write.call_count, writes)
        for _ in range(28):
            result = d.collect(settings, force=True)
        self.assertTrue(all(p["state"] == "up" for p in result["snapshot"]["ports"]))
        queried = [c.args[0] for c in conn.write.call_args_list]
        self.assertEqual(len([q for q in queried if q.startswith(b"display interface ")]), 28)
        self.assertTrue(all(q == b"\r" or q.startswith(b"display ") for q in queried))
        d.invalidate()
        self.assertTrue(d.cached()["stale"])
        with patch("physical_dashboard.open_connection", side_effect=OSError("cable disconnected")):
            result = d.collect(settings, force=True)
        self.assertTrue(result["stale"])
        self.assertIn("cable disconnected", result["message"])

    def test_invalid_port_keeps_values_but_not_freshness(self):
        d, conn, fixtures, settings = self.make_collector()
        d.collect(settings)
        d.collect(settings, force=True)
        old = d.cached()["snapshot"]["ports"][0]
        fixtures["display interface Ethernet1/0/1"] = "% Unrecognized command"
        d.port_cursor = 0
        failed = d.collect(settings, force=True)
        self.assertTrue(failed["stale"])
        port = failed["snapshot"]["ports"][0]
        self.assertEqual(port["pvid"], old["pvid"])
        self.assertEqual(port["field_observed_at"], old["field_observed_at"])
        self.assertTrue(port["stale"])
        self.assertTrue(port["field_stale"]["state"])

    def test_partial_port_never_refreshes_absent_field_timestamp(self):
        d, conn, fixtures, settings = self.make_collector()
        d.collect(settings); d.collect(settings, force=True)
        old = d.cached()["snapshot"]["ports"][0]
        fixtures["display interface Ethernet1/0/1"] = "Ethernet1/0/1 current state : DOWN\nPVID: 1"
        d.port_cursor = 0
        result = d.collect(settings, force=True)
        port = result["snapshot"]["ports"][0]
        self.assertEqual(port["rx_bps"], old["rx_bps"])
        self.assertEqual(port["field_observed_at"]["rx_bps"], old["field_observed_at"]["rx_bps"])
        self.assertTrue(port["field_stale"]["rx_bps"])
        self.assertFalse(port["field_stale"]["state"])

    def test_inventory_failure_preserves_old_values_and_is_stale(self):
        d, conn, fixtures, settings = self.make_collector()
        d.collect(settings)
        fixtures["display vlan"] = "partial, invalid response"
        result = d.collect(settings, force=True)
        self.assertEqual([v["id"] for v in result["snapshot"]["vlans"]], [1,11,12])
        self.assertTrue(result["stale"])
        self.assertTrue(result["snapshot"]["inventory_stale"])

    def test_missing_inventory_preserves_confirmed_vlan_until_explicit_absence(self):
        d, conn, fixtures, settings = self.make_collector()
        d.collect(settings)
        # Observe both port and explicit VLAN detail in the maintenance slot.
        d.stage = 1; d.priority_vlan_turn = True
        d.priority_vlans.add(12)
        d.collect(settings, force=True)
        fixtures["display vlan"] = "Following VLAN exist(s):\n1(default)"
        d.stage = 0
        result = d.collect(settings, force=True)
        self.assertEqual([v["id"] for v in result["snapshot"]["vlans"]], [1,12])
        self.assertTrue(result["snapshot"]["inventory_stale"])
        fixtures["display vlan 12"] = "Error: The VLAN does not exist."
        d.stage = 1; d.priority_vlan_turn = True
        result = d.collect(settings, force=True)
        self.assertEqual([v["id"] for v in result["snapshot"]["vlans"]], [1])
        self.assertFalse(result["stale"])
        self.assertFalse(result["snapshot"]["inventory_stale"])

    def test_ambiguous_vlan_error_does_not_authorize_deletion(self):
        d, conn, fixtures, settings = self.make_collector()
        d.collect(settings)
        d.stage = 1; d.priority_vlan_turn = True; d.priority_vlans.add(12)
        d.collect(settings, force=True)
        fixtures["display vlan 12"] = "Error: Access denied"
        d.stage = 1; d.priority_vlan_turn = True; d.priority_vlans.add(12)
        result = d.collect(settings, force=True)
        self.assertEqual([v["id"] for v in result["snapshot"]["vlans"]], [1,11,12])
        vlan = next(v for v in result["snapshot"]["vlans"] if v["id"] == 12)
        self.assertTrue(vlan["stale"])
        self.assertTrue(result["stale"])

    def test_newer_vlan_absence_wins_over_old_port_pvid(self):
        data = {"ports": [{"number": 1, "mode": "access", "pvid": 12,
                           "collected_at": "2000-01-01T00:00:00+00:00"}],
                "vlans": [parse_vlan_detail("VLAN ID: 12\nTagged Ports: none\nUntagged Ports: none",12)]}
        reconcile_vlan_members(data)
        self.assertEqual(data["vlans"][0]["members"], "Nenhuma")
        self.assertEqual(data["vlans"][0]["membership_from_ports"], [])

    def test_field_age_expires_even_when_other_port_was_just_collected(self):
        d, conn, fixtures, settings = self.make_collector()
        d.collect(settings); d.collect(settings, force=True)
        d.data["ports"][0]["field_observed_at"]["rx_bps"] = "2000-01-01T00:00:00+00:00"
        d.collect(settings, force=True)
        self.assertTrue(d.cached()["snapshot"]["ports"][0]["field_stale"]["rx_bps"])

    def test_priority_edits_do_not_starve_normal_round_robin(self):
        d, conn, fixtures, settings = self.make_collector()
        d.collect(settings)
        for _ in range(56):
            d.priority_ports.add(1)
            d.collect(settings, force=True)
        self.assertTrue(all(port["state"] == "up" for port in d.cached()["snapshot"]["ports"]))

    def test_bootstrap_does_not_query_a_hardcoded_lab_vlan(self):
        d, conn, fixtures, settings = self.make_collector()
        fixtures["display vlan"] = "Following VLAN exist(s):\n1(default)"
        d.collect(settings)
        self.assertNotIn(b"display vlan 12\r", [c.args[0] for c in conn.write.call_args_list])

    def test_partial_vlan_preserves_historical_members_without_refreshing_time(self):
        d, conn, fixtures, settings = self.make_collector()
        d.collect(settings)
        d.stage = 1; d.priority_vlan_turn = True; d.priority_vlans.add(12)
        d.collect(settings, force=True)
        row = next(v for v in d.cached()["snapshot"]["vlans"] if v["id"] == 12)
        stamp = row["field_observed_at"]["membership"]
        fixtures["display vlan 12"] = "VLAN ID: 12\nDescription: LAB\nTagged Ports: none\nUntagged Ports: Ethernet1/�/1 Ethernet1/0/2"
        d.stage = 1; d.priority_vlan_turn = True; d.priority_vlans.add(12)
        result = d.collect(settings, force=True)
        row = next(v for v in result["snapshot"]["vlans"] if v["id"] == 12)
        self.assertEqual(row["untagged_numbers"], [1,2])
        self.assertEqual(row["field_observed_at"]["membership"], stamp)
        self.assertTrue(row["membership_stale"])

    def test_new_access_port_removes_older_membership_from_other_vlan(self):
        row = parse_vlan_detail("VLAN ID: 12\nTagged Ports: none\nUntagged Ports: Ethernet1/0/1",12)
        row["collected_at"] = "2026-01-01T12:00:00+00:00"
        stamp = datetime.now(timezone.utc).isoformat()
        data = {"ports": [{"number":1, "mode":"access", "pvid":20, "collected_at":stamp}], "vlans":[row]}
        reconcile_vlan_members(data)
        self.assertEqual(row["effective_untagged_numbers"], [])
        self.assertEqual(row["members"], "Nenhuma")

    def test_partial_vlan_timestamp_does_not_override_newer_access_observation(self):
        instant = datetime.now(timezone.utc)
        membership_at = (instant - timedelta(seconds=120)).isoformat()
        port_at = (instant - timedelta(seconds=60)).isoformat()
        row = {"id": 12, "tagged_numbers": [], "untagged_numbers": [1],
               "membership_partial": True, "membership_reported": True,
               "collected_at": instant.isoformat(),
               "field_observed_at": {"membership": membership_at,
                                     "name": instant.isoformat()}}
        port = {"number": 1, "mode": "access", "pvid": 20,
                "collected_at": port_at,
                "field_observed_at": {"mode": port_at, "pvid": port_at}}
        reconcile_vlan_members({"vlans": [row], "ports": [port]})
        self.assertEqual(row["effective_untagged_numbers"], [])
        self.assertEqual(row["field_observed_at"]["membership"], membership_at)

    def test_failed_bootstrap_is_retried_when_console_returns(self):
        d, conn, fixtures, settings = self.make_collector()
        with patch("physical_dashboard.open_connection", side_effect=OSError("unplugged")):
            with self.assertRaises(SerialReadOnlyError):
                d.collect(settings, force=True)
        self.assertIsNone(d.cached()["snapshot"])
        self.assertTrue(d.cached()["stale"])
        recovered = d.collect(settings, force=True)
        self.assertTrue(recovered["snapshot"]["identity_verified"])
        self.assertEqual(recovered["snapshot"]["model"], "Quidway 3900-SI 28-Port")
        self.assertFalse(recovered["stale"])
        commands = [call.args[0] for call in conn.write.call_args_list]
        self.assertIn(b"display version\r", commands)
        self.assertFalse(any(c.startswith(b"display interface ") for c in commands))

    def test_wrong_profile_cannot_prime_cache_or_bypass_identity_on_retry(self):
        d, conn, fixtures, settings = self.make_collector()
        fixtures["display version"] = "Different VRP switch"
        with self.assertRaises(SerialReadOnlyError):
            d.collect(settings, force=True)
        self.assertIsNone(d.cached()["snapshot"])
        fixtures["display version"] = VERSION
        recovered = d.collect(settings, force=True)
        self.assertTrue(recovered["snapshot"]["identity_verified"])
        self.assertEqual(sum(c.args[0] == b"display version\r" for c in conn.write.call_args_list), 2)

    def test_other_or_mixed_interface_sections_are_rejected(self):
        wrong = "Ethernet1/0/2 current state : UP\nPVID: 20\nPort link-type: access"
        self.assertIsNone(parse_port(wrong, 1))
        self.assertIsNone(parse_port("Ethernet1/0/1 current state : UP\n" + wrong, 1))
        self.assertIsNone(parse_port("PVID: 20\nPort link-type: access", 1))
        d, conn, fixtures, settings = self.make_collector()
        d.collect(settings); d.collect(settings, force=True)
        old = d.cached()["snapshot"]["ports"][0]
        fixtures["display interface Ethernet1/0/1"] = wrong
        d.port_cursor = 0
        result = d.collect(settings, force=True)
        port = result["snapshot"]["ports"][0]
        self.assertEqual(port["pvid"], old["pvid"])
        self.assertEqual(port["field_observed_at"], old["field_observed_at"])
        self.assertTrue(port["stale"])

    def test_readded_low_priority_vlan_does_not_starve_later_or_regular_vlans(self):
        d, conn, fixtures, settings = self.make_collector()
        fixtures["display vlan"] = "Following VLAN exist(s):\n1(default)"
        d.collect(settings)
        stamp = datetime.now(timezone.utc).isoformat()
        d.data["vlans"] = [
            {"id":v, "verified_individually":True, "tagged_numbers":[], "untagged_numbers":[],
             "membership_reported":True, "membership_partial":False,
             "collected_at":stamp, "field_observed_at":{"membership":stamp}}
            for v in (1,12,20)
        ]
        for _ in range(80):
            d.collect(settings, force=True)
        queries = [call.args[0] for call in conn.write.call_args_list]
        for vlan in (1,12,20):
            self.assertIn(f"display vlan {vlan}\r".encode(), queries)
        # Repeated failure of the lowest missing ID also cannot block others.
        fixtures["display vlan 12"] = "Error: Access denied"
        conn.write.reset_mock()
        for _ in range(48):
            d.collect(settings, force=True)
        self.assertIn(b"display vlan 20\r", [c.args[0] for c in conn.write.call_args_list])
