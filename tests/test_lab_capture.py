"""Opt-in telemetry recording is local, expiring and excludes raw console data."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from lab_capture import record_lab_reading


class LabCaptureTest(unittest.TestCase):
    def test_opt_in_expiry_and_sanitized_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reading = {'snapshot': {'collected_at': '2026-11-17T00:00:00Z',
                                   'ports': [{'number': 1, 'state': 'up', 'rx_bps': 80}],
                                   'vlans': [{'id': 12, 'name': 'LAB'}]},
                       'outputs': [{'command': 'display current-configuration', 'output': 'SECRET'}]}
            self.assertFalse(record_lab_reading(reading, root))
            config = {'session_id': 'test-recording', 'expires_utc': (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
            (root / 'active.json').write_text(json.dumps(config))
            self.assertTrue(record_lab_reading(reading, root))
            content = (root / 'test-recording.jsonl').read_text()
            self.assertNotIn('SECRET', content)
            self.assertEqual(json.loads(content)['ports'][0]['rx_bps'], 80)
            config['expires_utc'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            (root / 'active.json').write_text(json.dumps(config))
            self.assertFalse(record_lab_reading(reading, root))
            self.assertEqual((root / 'test-recording.jsonl').read_text(), content)

    def test_invalid_identifier_and_malformed_marker_do_not_interrupt_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for marker in ('not-json', '[]', '{"session_id":"../escape"}'):
                (root / 'active.json').write_text(marker)
                self.assertFalse(record_lab_reading({'snapshot': {}}, root))

    def capture(self, reading):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'active.json').write_text(json.dumps({
                'session_id': 'freshness',
                'expires_utc': (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}))
            self.assertTrue(record_lab_reading(reading, root))
            return json.loads((root / 'freshness.jsonl').read_text())

    def test_field_timestamps_and_age_survive_newer_link_observation(self):
        stamp = datetime.now(timezone.utc).isoformat()
        old = '2000-01-01T00:00:00+00:00'
        reading = {'stale': False, 'snapshot': {'collected_at': stamp,
            'ports': [{'number':1, 'state':'up', 'rx_bps':1234, 'collected_at':stamp,
                       'field_observed_at':{'state':stamp, 'rx_bps':old},
                       'field_stale':{'state':False, 'rx_bps':True}, 'stale':True}], 'vlans':[]}}
        result = self.capture(reading)
        self.assertFalse(result['collection_stale'])
        self.assertTrue(result['stale'])
        port = result['ports'][0]
        self.assertEqual(port['rx_bps'], 1234)
        self.assertEqual(port['field_observed_at']['rx_bps'], old)
        self.assertEqual(port['field_observed_at']['state'], stamp)
        self.assertTrue(port['field_stale']['rx_bps'])
        self.assertFalse(port['field_stale']['state'])
        self.assertTrue(port['stale'])

    def test_export_does_not_invent_field_times_for_legacy_or_unknown_data(self):
        stamp = datetime.now(timezone.utc).isoformat()
        result = self.capture({'stale':False, 'snapshot':{'collected_at':stamp,
            'ports':[{'number':1, 'rx_bps':80, 'collected_at':stamp}], 'vlans':[]}})
        self.assertTrue(result['stale'])
        self.assertIsNone(result['ports'][0]['field_observed_at']['rx_bps'])
        self.assertTrue(result['ports'][0]['field_stale']['rx_bps'])

    def test_vlan_export_keeps_effective_membership_and_field_freshness(self):
        stamp = datetime.now(timezone.utc).isoformat()
        old = '2000-01-01T00:00:00+00:00'
        result = self.capture({'stale':False, 'snapshot':{'collected_at':stamp, 'ports':[],
            'vlans':[{'id':12, 'name':'LAB', 'tagged_numbers':[], 'untagged_numbers':[1],
                      'effective_tagged_numbers':[], 'effective_untagged_numbers':[],
                      'collected_at':stamp, 'field_observed_at':{'membership':old, 'name':stamp},
                      'membership_stale':True, 'stale':True}]}})
        vlan = result['vlans'][0]
        self.assertEqual(vlan['ports_untagged'], [])
        self.assertEqual(vlan['field_observed_at']['membership'], old)
        self.assertTrue(vlan['membership_stale'])
        self.assertTrue(result['stale'])

    def test_expired_mac_table_cannot_make_export_look_current(self):
        result = self.capture({'stale':False, 'snapshot':{'ports':[], 'vlans':[],
            'mac_table':{'collected_at':'2000-01-01T00:00:00+00:00', 'count':1,
                         'entries':[{'port':1,'vlan':12,'mac':'00:11:22:33:44:55'}]}}})
        self.assertTrue(result['mac_table_stale'])
        self.assertTrue(result['stale'])


    def test_complete_current_observations_can_be_exported_as_current(self):
        stamp = datetime.now(timezone.utc).isoformat()
        result = self.capture({'stale':False, 'snapshot':{'collected_at':stamp, 'ports':[
            {'number':1, 'state':'up', 'rx_bps':80, 'collected_at':stamp,
             'field_observed_at':{'state':stamp, 'rx_bps':stamp, 'raw_cli':'SECRET'},
             'field_stale':{'state':False, 'rx_bps':False}}], 'vlans':[
            {'id':12, 'name':'LAB', 'collected_at':stamp, 'membership_partial':False,
             'field_observed_at':{'membership':stamp, 'name':stamp}}],
            'mac_table':{'collected_at':stamp, 'count':0, 'entries':[]}}})
        self.assertFalse(result['stale'])
        self.assertFalse(result['mac_table_stale'])
        self.assertFalse(result['coverage_partial'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_export_checks_timestamp_age_even_if_supplied_flags_are_false(self):
        stamp = datetime.now(timezone.utc).isoformat()
        result = self.capture({'stale':False, 'snapshot':{'ports':[
            {'number':1, 'rx_bps':80, 'collected_at':stamp, 'stale':False,
             'field_observed_at':{'rx_bps':'2000-01-01T00:00:00+00:00'},
             'field_stale':{'rx_bps':False}}], 'vlans':[]}})
        self.assertTrue(result['ports'][0]['field_stale']['rx_bps'])
        self.assertTrue(result['stale'])


if __name__ == '__main__': unittest.main()
