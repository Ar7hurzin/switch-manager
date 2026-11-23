"""Regression tests for input validation and switch-response handling."""
import unittest
from collections import deque
from unittest.mock import patch, Mock

from switch_config import ConfigurationError, build_plan, issue_preview, consume_preview
from switch_text import command_rejected
from ssh_readonly import _read_until_prompt
from serial_config import _parse_vlan_inventory, _query_vlan_presence, _verify_vlan_preconditions
from serial_readonly import SerialSettings

class ReviewTest(unittest.TestCase):
    def test_malformed_action_and_state(self):
        for data in ({'action': []}, {'action': 'set_port_state', 'port': 1, 'state': []}):
            with self.subTest(data=data), self.assertRaises(ConfigurationError):
                build_plan(data)

    def test_reject_non_integer_port_and_vlan(self):
        for data in ({'action': 'create_vlan', 'vlan_id': 3.5}, {'action': 'set_port_state', 'port': 1.5, 'state': 'up'}, {'action': 'create_vlan', 'vlan_id': True}):
            with self.subTest(data=data), self.assertRaises(ConfigurationError):
                build_plan(data)
        self.assertIn('vlan 10', build_plan({'action': 'create_vlan', 'vlan_id': 10}).commands)

    def test_reject_network_and_broadcast_address(self):
        for ip in ('192.168.1.0', '192.168.1.255'):
            with self.assertRaises(ConfigurationError):
                build_plan({'action':'set_management_ip','vlan_id':1,'ip':ip,'prefix':24})

    def test_preview_owner_and_single_use(self):
        plan=build_plan({'action':'save'})
        token=issue_preview(plan, owner='session-a')
        with self.assertRaises(ConfigurationError): consume_preview(token, owner='session-b')
        self.assertEqual(consume_preview(token, owner='session-a'),plan)
        with self.assertRaises(ConfigurationError): consume_preview(token, owner='session-a')

    def test_preview_expiry(self):
        token=issue_preview(build_plan({'action':'save'}), lifetime_seconds=0, owner='session-a')
        with self.assertRaises(ConfigurationError): consume_preview(token, owner='session-a')

    def test_vrp_error_detection(self):
        self.assertTrue(command_rejected('display vlan\n  % Unrecognized command\n<Quidway>'))
        self.assertTrue(command_rejected('Error: access denied'))
        self.assertFalse(command_rejected('description Error: label'))

    def test_legacy_vlan_inventory(self):
        for suffix in ("", "\x00", "\x07", "\u25ab", "\ufffd"):
            with self.subTest(suffix=suffix):
                self.assertEqual(_parse_vlan_inventory("display vlan\nNow, the following VLAN exist(s):\n 1(default), 12," + suffix + "\n<Quidway>"), {1, 12})

    def test_vlan_inventory_wrapped_and_invalid(self):
        self.assertEqual(_parse_vlan_inventory("Following VLAN exist(s):\n1(default), 12,\n100, 4094,\n<Quidway>"), {1, 12, 100, 4094})
        self.assertEqual(_parse_vlan_inventory("VLAN ID: 12\nVLIN Type: static"), {12})
        with self.assertRaises(ConfigurationError):
            _parse_vlan_inventory("Following VLAN exist(s):\n1(default), 12, unknown\n<Quidway>")
        with self.assertRaises(ConfigurationError):
            _parse_vlan_inventory("Following VLAN exist(s):\n1(default), 4095,\n<Quidway>")

    def test_physical_vlan_detail_bypasses_legacy_catalog(self):
        settings = SerialSettings("/dev/ttyUSB0", 9600, 3)
        connection = Mock()
        raw = (b"display vlan 12\r\n VLAN ID: 12\r\n VLIN Type: static\r\n"
               b" Route Interface: not configured\r\n Tagged Ports: none\r\n"
               b" Untagged Ports: none\r\n<Quidway>")
        with patch("serial_config._read_response", return_value=(raw, False)), \
             patch("serial_config._existing_vlans", side_effect=AssertionError("catalog not needed")):
            for action in ("rename_vlan", "set_access_port", "set_management_ip", "delete_vlan"):
                plan = build_plan({"action": action, "vlan_id": 12, "name": "LAB",
                                   "port": 1, "ip": "192.168.100.1", "prefix": 24})
                _verify_vlan_preconditions(connection, settings, plan)
        self.assertEqual(connection.write.call_count, 4)
        connection.write.assert_called_with(b"display vlan 12\r")

    def test_physical_vlan_absence_and_ambiguous_responses(self):
        settings = SerialSettings("/dev/ttyUSB0", 9600, 3)
        connection = Mock()
        create = build_plan({"action": "create_vlan", "vlan_id": 12, "name": "LAB"})
        rename = build_plan({"action": "rename_vlan", "vlan_id": 12, "name": "LAB"})
        with patch("serial_config._read_response", return_value=(b"Error: The VLAN does not exist.\r\n<Quidway>", False)):
            self.assertFalse(_query_vlan_presence(connection, settings, 12))
            _verify_vlan_preconditions(connection, settings, create)
            with self.assertRaises(ConfigurationError):
                _verify_vlan_preconditions(connection, settings, rename)
        with patch("serial_config._read_response", return_value=(b"VLAN ID: 12\r\n<Quidway>", False)):
            with self.assertRaises(ConfigurationError):
                _verify_vlan_preconditions(connection, settings, create)
        for raw in (b"<Quidway>", b"VLAN ID: 13\r\n<Quidway>",
                    b"Error: access denied\r\n<Quidway>", b"VLAN 13 does not exist.\r\n<Quidway>"):
            with self.subTest(raw=raw), patch("serial_config._read_response", return_value=(raw, False)):
                with self.assertRaises(ConfigurationError):
                    _verify_vlan_preconditions(connection, settings, create)

    def test_ssh_pagination(self):
        class FakeChannel:
            closed=False
            def __init__(self): self.chunks=deque([b'first page\r\n---- More ----', b'\r\nsecond page\r\n<Quidway>']); self.sent=[]
            def recv_ready(self): return bool(self.chunks)
            def recv(self, size): return self.chunks.popleft()
            def exit_status_ready(self): return not self.chunks
            def sendall(self, data): self.sent.append(data)
        channel=FakeChannel()
        self.assertIn(b'second page',_read_until_prompt(channel,1))
        self.assertEqual(channel.sent,[' '])
