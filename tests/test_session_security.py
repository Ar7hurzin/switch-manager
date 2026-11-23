"""HTTP session revocation and CLI reconciliation using fake connections only."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from dataclasses import replace

from test_manager import manager, login as sso_login
from serial_cli import SerialCLISession
from session_registry import SessionRegistry


class SessionSecurityTest(unittest.TestCase):
    def setUp(self):
        manager._login_attempts.clear()
        self.client = manager.app.test_client()
        self.serial_guard = patch('serial.Serial', side_effect=AssertionError('no physical console'))
        self.serial_guard.start()
        self.addCleanup(self.serial_guard.stop)

    def login(self, client=None):
        return sso_login(client or self.client)

    def test_logout_revokes_copied_cookie_but_not_another_login(self):
        csrf = self.login()
        cookie_name = manager.app.config['SESSION_COOKIE_NAME']
        saved_cookie = self.client.get_cookie(cookie_name).value
        other = manager.app.test_client()
        self.login(other)
        self.assertEqual(self.client.post('/logout', data={'csrf_token': csrf}).status_code, 302)
        replay = manager.app.test_client()
        replay.set_cookie(cookie_name, saved_cookie)
        self.assertEqual(replay.get('/api/status').status_code, 401)
        self.assertEqual(other.get('/api/status').status_code, 200)
        self.assertEqual(replay.get('/login').status_code, 200)

    def test_presence_expiry_does_not_revoke_authorization(self):
        self.login()
        with manager._active_sessions_lock:
            manager._active_sessions.clear()
        self.assertEqual(self.client.get('/api/status').status_code, 200)

    def test_server_restart_and_credential_change_invalidate_cookie(self):
        self.login()
        with patch.object(manager, '_session_registry', SessionRegistry()):
            self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.login()
        # Changing who may sign in revokes sessions, even for accounts still allowed.
        changed = replace(manager.oidc_settings, allowed_emails=manager.oidc_settings.allowed_emails + ('new@example.test',))
        with patch.object(manager, 'oidc_settings', changed):
            self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_cli_status_validates_header_without_disclosing_token(self):
        self.login()
        cli = SerialCLISession()
        cli._connection = Mock()
        cli._token = 'review-owned-token'
        cli._last_activity = time.monotonic()
        self.addCleanup(cli.shutdown)
        with patch.object(manager, 'cli_session', cli):
            result = self.client.get('/api/cli/status', headers={'X-CLI-Token': 'review-owned-token'}).get_json()
            self.assertTrue(result['token_valid'])
            self.assertNotIn('review-owned-token', str(result))
            self.assertFalse(self.client.get('/api/cli/status', headers={'X-CLI-Token': 'different-token'}).get_json()['token_valid'])

    def test_cli_status_does_not_wait_for_inflight_serial_command(self):
        cli = SerialCLISession()
        acquired = threading.Event()
        release = threading.Event()
        def inflight():
            with cli._lock:
                acquired.set()
                release.wait(3)
        thread = threading.Thread(target=inflight)
        thread.start()
        self.assertTrue(acquired.wait(1))
        try:
            result = cli.status('token')
            self.assertTrue(result['busy'])
            self.assertIsNone(result['token_valid'])
        finally:
            release.set()
            thread.join(1)

    def test_logout_closes_only_owned_cli(self):
        csrf = self.login()
        cli = SerialCLISession()
        connection = Mock()
        cli._connection = connection
        cli._token = 'owned-token'
        cli._last_activity = time.monotonic()
        self.addCleanup(cli.shutdown)
        with self.client.session_transaction() as sess: sess['qm_cli_token'] = 'owned-token'
        with patch.object(manager, 'cli_session', cli):
            self.client.post('/logout', data={'csrf_token': csrf})
        connection.close.assert_called_once()


class DeferredCLICloseTest(unittest.TestCase):
    def test_logout_revokes_immediately_and_closes_after_inflight_send(self):
        case = SessionSecurityTest()
        case.setUp()
        self.addCleanup(case.doCleanups)
        csrf = case.login()
        cli = SerialCLISession()
        connection = Mock()
        cli._connection = connection
        cli._token = 'inflight-token'
        cli._settings = Mock(timeout_seconds=3)
        cli._last_activity = time.monotonic()
        self.addCleanup(cli.shutdown)
        started, finish = threading.Event(), threading.Event()
        failures = []
        def read(timeout):
            started.set()
            finish.wait(3)
            return b'<Quidway>', True
        def send():
            try:
                with manager._serial_request_lock:
                    cli.send('inflight-token', 'display version')
            except Exception as exc:
                failures.append(exc)
        with case.client.session_transaction() as sess: sess['qm_cli_token'] = 'inflight-token'
        with patch.object(manager, 'cli_session', cli), patch.object(cli, '_read_response', side_effect=read):
            thread = threading.Thread(target=send)
            thread.start()
            self.assertTrue(started.wait(1))
            try:
                self.assertEqual(case.client.post('/logout', data={'csrf_token': csrf}).status_code, 302)
                self.assertEqual(case.client.get('/api/status').status_code, 401)
                self.assertTrue(cli.status()['busy'])
            finally:
                finish.set()
                thread.join(2)
        self.assertFalse(failures)
        self.assertFalse(cli.connected)
        connection.close.assert_called_once()

    def test_delayed_request_cannot_close_other_session(self):
        cli = SerialCLISession()
        cli._connection = Mock()
        cli._token = 'new-token'
        cli._last_activity = time.monotonic()
        self.addCleanup(cli.shutdown)
        cli.request_close('old-token')
        self.assertTrue(cli.connected)


if __name__ == '__main__': unittest.main()
