"""Preview and apply agree on the observed mode and exact transition commands."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_manager import manager, login
from serial_config import execute_plan, prepare_plan
from serial_readonly import SerialSettings
from switch_config import build_plan
from virtual_switch import snapshot


def immediate(connection, timeout, allow_confirmation=False, **kwargs):
    raw = connection.read(512_000)
    if not raw: raise AssertionError('No virtual response')
    return raw, bool(allow_confirmation and b'[y/n]' in raw)


class PreviewTransitionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for guard in (patch.dict(os.environ, QM_MODE='virtual', QM_VIRTUAL_STATE=str(Path(self.temp.name) / 'state.json')),
                      patch('serial_config._read_response', side_effect=immediate),
                      patch('serial.Serial', side_effect=AssertionError('physical access forbidden'))):
            guard.start()
            self.addCleanup(guard.stop)
        self.client = manager.app.test_client()
        manager._login_attempts.clear()
        self.headers = {'X-CSRF-Token': login(self.client)}
        self.settings = SerialSettings('virtual', 9600, 3, virtual=True)

    def apply_direct(self, action, **params):
        plan = prepare_plan(self.settings, build_plan({'action':action, **params}))
        return execute_plan(self.settings, plan)

    def preview(self, action, **params):
        return self.client.post('/api/config/preview', json={'action':action, **params}, headers=self.headers)

    def test_preview_includes_required_intermediate_mode_and_apply_verifies(self):
        self.apply_direct('set_trunk_port', port=2, vlans=[1], pvid=1)
        preview = self.preview('set_hybrid_port', port=2, untagged_vlans=[1], pvid=1)
        self.assertEqual(preview.status_code, 200, preview.json)
        commands = preview.json['commands']
        self.assertLess(commands.index('port link-type access'), commands.index('port link-type hybrid'))
        self.assertTrue(any('passará por access' in warning for warning in preview.json['warnings']))
        result = self.client.post('/api/config/apply', json={'token':preview.json['token']}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(snapshot()['ports'][1]['mode'], 'hybrid')
        self.assertIn('confirmado', result.json['outputs'][-1]['output'])

    def test_mode_change_after_preview_requires_new_preview_without_writing(self):
        preview = self.preview('set_trunk_port', port=2, vlans=[1], pvid=1)
        self.assertEqual(preview.status_code, 200)
        self.apply_direct('set_hybrid_port', port=2, untagged_vlans=[1], pvid=1)
        before = snapshot()
        result = self.client.post('/api/config/apply', json={'token':preview.json['token']}, headers=self.headers)
        self.assertEqual(result.status_code, 400)
        self.assertIn('mudou desde a prévia', result.json['error'])
        self.assertEqual(snapshot(), before)

    def test_preview_respects_serial_exclusivity_and_open_cli(self):
        with manager._serial_request_lock:
            self.assertEqual(self.preview('set_trunk_port', port=2, vlans=[1], pvid=1).status_code, 409)
        with patch.object(manager.cli_session, 'status', return_value={'connected':True, 'busy':False, 'token_valid':False}):
            self.assertEqual(self.preview('set_trunk_port', port=2, vlans=[1], pvid=1).status_code, 409)


if __name__ == '__main__': unittest.main()
