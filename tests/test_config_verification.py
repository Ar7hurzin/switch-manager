"""Replacement and read-back verification through isolated virtual console I/O."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from serial_config import prepare_plan, execute_plan, _parse_vlan_values, _configuration_blocks, _interface_vlan_sections, _verify_result
from serial_readonly import SerialSettings
from switch_config import ConfigurationError, build_plan
from virtual_switch import snapshot


def immediate_response(connection, timeout, allow_confirmation=False, **kwargs):
    raw = connection.read(512_000)
    if not raw:
        raise ConfigurationError('No simulated response')
    return raw, allow_confirmation and b'[y/n]' in raw


class ConfigVerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, QM_VIRTUAL_STATE=str(Path(self.temp.name) / 'state.json'))
        env.start()
        self.addCleanup(env.stop)
        reading = patch('serial_config._read_response', side_effect=immediate_response)
        reading.start()
        self.addCleanup(reading.stop)
        physical = patch('serial.Serial', side_effect=AssertionError('physical access forbidden'))
        physical.start()
        self.addCleanup(physical.stop)
        self.settings = SerialSettings('virtual', 9600, 3, virtual=True)
        for vlan in (10, 20, 30):
            self.apply('create_vlan', vlan_id=vlan)

    def apply(self, action, **payload):
        return execute_plan(self.settings, prepare_plan(self.settings, build_plan(dict(action=action, **payload))))

    def test_repeated_trunk_replaces_including_default_vlan(self):
        self.apply('set_trunk_port', port=2, vlans=[10,20], pvid=10)
        self.apply('set_trunk_port', port=2, vlans=[10], pvid=10)
        port = snapshot()['ports'][1]
        self.assertEqual(port['tagged'], [])
        self.assertEqual(port['untagged'], [10])
        self.apply('set_trunk_port', port=2, vlans=[1,30], pvid=30)
        port = snapshot()['ports'][1]
        self.assertEqual(port['tagged'], [1])
        self.assertEqual(port['untagged'], [30])

    def test_repeated_hybrid_replaces_and_changes_tagging(self):
        self.apply('set_hybrid_port', port=3, tagged_vlans=[20,30], untagged_vlans=[10], pvid=10)
        self.apply('set_hybrid_port', port=3, tagged_vlans=[10], untagged_vlans=[20], pvid=20)
        port = snapshot()['ports'][2]
        self.assertEqual(port['tagged'], [10])
        self.assertEqual(port['untagged'], [20])
        self.assertEqual(port['pvid'], 20)

    def test_preview_contains_clear_and_bounded_additions(self):
        plan = build_plan({'action':'set_trunk_port','port':2,'vlans':list(range(1,65)),'pvid':1})
        self.assertIn('undo port trunk permit vlan all', plan.commands)
        additions = [x for x in plan.commands if x.startswith('port trunk permit vlan ')]
        self.assertEqual(len(additions), 7)
        self.assertTrue(all(len(x.split()[4:]) <= 10 for x in additions))
        hybrid = build_plan({'action':'set_hybrid_port','port':3,'untagged_vlans':[10],'pvid':10})
        self.assertIn('undo port hybrid vlan 1 to 4094', hybrid.commands)

    def test_readback_is_required_and_does_not_leak_configuration(self):
        outputs = self.apply('set_port_description', port=2, description='UPLINK')
        self.assertIn('confirmado', outputs[-1]['output'])
        with patch('serial_config._verify_result', side_effect=ConfigurationError('readback mismatch')):
            with self.assertRaisesRegex(ConfigurationError, 'alterações parciais.*readback mismatch'):
                self.apply('set_sysname', name='TEST')
        self.assertEqual(snapshot()['name'], 'TEST')

    def test_rejected_clear_aborts_without_sending_additions(self):
        from serial_config import _exchange
        sent = []
        def exchange(connection, command, settings, allow_confirmation=False):
            sent.append(command)
            if command == 'undo port trunk permit vlan all':
                raise ConfigurationError('Unsupported clear')
            return _exchange(connection, command, settings, allow_confirmation)
        with patch('serial_config._exchange', side_effect=exchange):
            with self.assertRaisesRegex(ConfigurationError, 'alterações parciais'):
                self.apply('set_trunk_port', port=2, vlans=[10], pvid=10)
        self.assertNotIn('port trunk permit vlan 10', sent)

    def test_missing_interface_cannot_confirm_admin_up(self):
        from serial_config import _exchange
        def exchange(connection, command, settings, allow_confirmation=False):
            if command == 'display current-configuration':
                return 'sysname Fake\n#\nreturn\n<Fake>', False
            return _exchange(connection, command, settings, allow_confirmation)
        with patch('serial_config._exchange', side_effect=exchange):
            with self.assertRaisesRegex(ConfigurationError, 'Interface ausente'):
                self.apply('set_port_state', port=1, state='up')

    def test_wrong_membership_is_not_success(self):
        from serial_config import _exchange
        def exchange(connection, command, settings, allow_confirmation=False):
            text, confirm = _exchange(connection, command, settings, allow_confirmation)
            if command == 'display interface Ethernet1/0/2':
                text = text.replace('Tagged VLAN ID: none', 'Tagged VLAN ID: 1')
            return text, confirm
        with patch('serial_config._exchange', side_effect=exchange):
            with self.assertRaisesRegex(ConfigurationError, 'VLANs lidas.*diferem'):
                self.apply('set_trunk_port', port=2, vlans=[10], pvid=10)

    def test_verified_all_remaining_actions(self):
        self.apply('rename_vlan', vlan_id=10, name='LAB10')
        self.apply('set_access_port', port=1, vlan_id=10)
        self.apply('set_port_state', port=1, state='down')
        self.apply('set_port_state', port=1, state='up')
        self.apply('set_management_ip', vlan_id=10, ip='192.168.10.1', prefix=24)
        self.apply('add_static_route', destination='10.0.0.0', prefix=8, next_hop='192.168.10.2')
        self.apply('delete_static_route', destination='10.0.0.0', prefix=8, next_hop='192.168.10.2')
        self.apply('delete_vlan', vlan_id=30)
        self.apply('save')
        self.assertFalse(snapshot()['dirty'])

    def test_wrapped_vlan_sections_are_compared_in_full(self):
        plan = build_plan({'action':'set_trunk_port','port':2,'vlans':[10,20,30],'pvid':10})
        text = ('Ethernet1/0/2 current state : UP\nPVID: 10\nPort link-type: trunk\n'
                'Tagged VLAN ID: 20\n                 30\nUntagged VLAN ID:\n                 10\n'
                'Last 300 seconds input: 0 packets/sec 0 bytes/sec\n<Quidway>')
        self.assertEqual(_interface_vlan_sections(text), ({20,30}, {10}))
        with patch('serial_config._exchange', return_value=(text,False)):
            self.assertEqual(_verify_result(Mock(), self.settings, plan), 'display interface Ethernet1/0/2')
            narrower = build_plan({'action':'set_trunk_port','port':2,'vlans':[10,20],'pvid':10})
            with self.assertRaisesRegex(ConfigurationError, 'diferem'):
                _verify_result(Mock(), self.settings, narrower)

    def test_vlan_section_ambiguities_fail_closed(self):
        for text in (
            'Tagged VLAN ID: 20\n   unknown\nUntagged VLAN ID: 10',
            'Tagged VLAN ID: none\n   20\nUntagged VLAN ID: 10',
            'Tagged VLAN ID: 20\nTagged VLAN ID: 20\nUntagged VLAN ID: 10',
            'Tagged VLAN ID: 20\nUntagged VLAN ID: 20',
            'Tagged VLAN ID: 20\nDescription: field boundary\n   30\nUntagged VLAN ID: 10',
            'Tagged VLAN ID: 20\nUntagged VLAN ID: 10\n\n   30',
            'Tagged VLAN ID: 20\nUntagged VLAN ID:',
        ):
            with self.subTest(text=text), self.assertRaises(ConfigurationError):
                _interface_vlan_sections(text)

    def test_preview_materializes_mode_transition_and_preserves_same_mode(self):
        trunk = prepare_plan(self.settings, build_plan({'action':'set_trunk_port','port':2,'vlans':[10],'pvid':10}))
        self.assertEqual(trunk.expected_port_mode, 'access')
        self.assertNotIn('port link-type access', trunk.commands)
        execute_plan(self.settings, trunk)
        hybrid = prepare_plan(self.settings, build_plan({'action':'set_hybrid_port','port':2,'untagged_vlans':[10],'pvid':10}))
        self.assertEqual(hybrid.expected_port_mode, 'trunk')
        self.assertLess(hybrid.commands.index('port link-type access'), hybrid.commands.index('port link-type hybrid'))
        execute_plan(self.settings, hybrid)
        same = prepare_plan(self.settings, build_plan({'action':'set_hybrid_port','port':2,'untagged_vlans':[20],'pvid':20}))
        self.assertEqual(same.expected_port_mode, 'hybrid')
        self.assertNotIn('port link-type access', same.commands)
        execute_plan(self.settings, same)
        self.assertEqual(snapshot()['ports'][1]['pvid'],20)
        back = prepare_plan(self.settings, build_plan({'action':'set_trunk_port','port':2,'vlans':[10],'pvid':10}))
        self.assertLess(back.commands.index('port link-type access'), back.commands.index('port link-type trunk'))
        execute_plan(self.settings, back)

    def test_unprepared_or_changed_mode_rejected_before_write(self):
        unprepared = build_plan({'action':'set_hybrid_port','port':2,'untagged_vlans':[10],'pvid':10})
        before = snapshot()['ports'][1]
        with self.assertRaisesRegex(ConfigurationError, 'prévia preparada'):
            execute_plan(self.settings, unprepared)
        self.assertEqual(snapshot()['ports'][1],before)
        prepared = prepare_plan(self.settings,unprepared)
        self.apply('set_trunk_port',port=2,vlans=[20],pvid=20)
        before = snapshot()['ports'][1]
        with self.assertRaisesRegex(ConfigurationError, 'mudou desde a prévia'):
            execute_plan(self.settings,prepared)
        self.assertEqual(snapshot()['ports'][1],before)

    def test_prepare_rejects_wrong_target_or_ambiguous_interface(self):
        plan=build_plan({'action':'set_trunk_port','port':2,'vlans':[10],'pvid':10})
        with patch('serial_config._check_target',side_effect=ConfigurationError('wrong target')):
            with self.assertRaisesRegex(ConfigurationError,'wrong target'):
                prepare_plan(self.settings,plan)
        from serial_config import _exchange
        def exchange(connection,command,settings,allow_confirmation=False):
            if command.startswith('display interface '):
                return 'Ethernet1/0/3 current state : UP\nPort link-type: access',False
            return _exchange(connection,command,settings,allow_confirmation)
        with patch('serial_config._exchange',side_effect=exchange):
            with self.assertRaisesRegex(ConfigurationError,'ausente/ambíguo'):
                prepare_plan(self.settings,plan)

    def test_deleted_vlan_retains_trunk_and_hybrid_pvid(self):
        self.apply('set_access_port',port=1,vlan_id=10)
        self.apply('set_trunk_port',port=2,vlans=[10,20],pvid=10)
        self.apply('set_hybrid_port',port=3,tagged_vlans=[20],untagged_vlans=[10],pvid=10)
        self.apply('delete_vlan',vlan_id=10)
        ports=snapshot()['ports']
        self.assertEqual(ports[0]['pvid'],1)
        for port in (ports[1],ports[2]):
            self.assertEqual(port['pvid'],10)
            self.assertNotIn(10,port['tagged']+port['untagged'])

    def test_raw_simulator_rejects_direct_trunk_hybrid_jump(self):
        from virtual_switch import VirtualConsole,_initial
        state=_initial(); connection=VirtualConsole()
        for command in ('system-view','interface Ethernet1/0/2','port link-type trunk'):
            connection._execute(state,command)
        with self.assertRaisesRegex(ValueError,'must pass through access'):
            connection._execute(state,'port link-type hybrid')
        self.assertEqual(state['ports']['2']['mode'],'trunk')

    def test_parsers_reject_incomplete_or_ambiguous_output(self):
        self.assertEqual(_parse_vlan_values('1, 10 to 12 20-21'), {1,10,11,12,20,21})
        for raw in ('', '1 partial', '1 to', '4095', '10-2', '10-20 to 30'):
            with self.assertRaises(ConfigurationError):
                _parse_vlan_values(raw)
        with self.assertRaises(ConfigurationError):
            _configuration_blocks('sysname Foo\ninterface Ethernet1/0/2\n')
