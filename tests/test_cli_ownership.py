"""Web authorization owns the CLI throughout opening, not just after a cookie reply."""
import threading
import unittest
from unittest.mock import Mock, patch

from test_manager import manager, login as sso_login
from serial_cli import SerialCLISession


class CLIOwnershipTest(unittest.TestCase):
    def setUp(self):
        manager._login_attempts.clear()
        self.client = manager.app.test_client()
        self.cli = SerialCLISession()
        self.connection = Mock()
        self.addCleanup(self.cli.shutdown)
        for guard in (patch.object(manager, 'cli_session', self.cli),
                      patch('serial_cli.open_connection', return_value=self.connection),
                      patch('serial.Serial', side_effect=AssertionError('physical access forbidden')),
                      patch.dict('os.environ', QM_MODE='serial', QM_ENABLE_CLI='true')):
            guard.start()
            self.addCleanup(guard.stop)
        self.csrf = self.login(self.client)

    def login(self, client):
        return sso_login(client)

    def clone_browser_session(self):
        other = manager.app.test_client()
        name = manager.app.config['SESSION_COOKIE_NAME']
        other.set_cookie(name, self.client.get_cookie(name).value)
        return other

    def open(self):
        return self.client.post('/api/cli/open', json={}, headers={'X-CSRF-Token':self.csrf})

    def test_logout_from_old_cookie_cancels_inflight_open(self):
        tab = self.clone_browser_session()  # no qm_cli_token exists yet
        started, finish = threading.Event(), threading.Event()
        results = []
        def read(timeout):
            started.set()
            if not finish.wait(3): raise AssertionError('test did not release open')
            return b'<Review>', True
        with patch.object(self.cli, '_read_response', side_effect=read):
            thread = threading.Thread(target=lambda: results.append(self.open()))
            thread.start()
            self.assertTrue(started.wait(1))
            try:
                self.assertEqual(tab.post('/logout', data={'csrf_token':self.csrf}).status_code, 302)
                self.assertTrue(self.cli.status()['busy'])
            finally:
                finish.set()
                thread.join(2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status_code, 401)
        self.assertFalse(self.cli.connected)
        self.connection.close.assert_called_once()
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_logout_before_cli_records_owner_is_rechecked_after_open(self):
        tab = self.clone_browser_session()
        started, finish = threading.Event(), threading.Event()
        results = []
        original = self.cli.open
        def delayed(*args, **kwargs):
            started.set()
            if not finish.wait(3): raise AssertionError('test did not release open')
            return original(*args, **kwargs)
        with patch.object(self.cli, 'open', side_effect=delayed), patch.object(self.cli, '_read_response', return_value=(b'<Review>', True)):
            thread = threading.Thread(target=lambda: results.append(self.open()))
            thread.start()
            self.assertTrue(started.wait(1))
            try:
                self.assertEqual(tab.post('/logout', data={'csrf_token':self.csrf}).status_code, 302)
            finally:
                finish.set()
                thread.join(2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status_code, 401)
        self.assertFalse(self.cli.connected)
        self.connection.close.assert_called_once()

    def test_server_owner_closes_cli_even_when_cookie_has_no_cli_token(self):
        tab = self.clone_browser_session()
        with patch.object(self.cli, '_read_response', return_value=(b'<Review>', True)):
            self.assertEqual(self.open().status_code, 200)
        self.assertTrue(self.cli.connected)
        self.assertEqual(tab.post('/logout', data={'csrf_token':self.csrf}).status_code, 302)
        self.assertFalse(self.cli.connected)

    def test_other_web_session_cannot_close_owned_cli_on_logout(self):
        other = manager.app.test_client()
        other_csrf = self.login(other)
        with patch.object(self.cli, '_read_response', return_value=(b'<Review>', True)):
            self.assertEqual(self.open().status_code, 200)
        other.post('/logout', data={'csrf_token':other_csrf})
        self.assertTrue(self.cli.connected)
        self.connection.close.assert_not_called()

    def test_expired_owner_is_closed_when_api_rejects_its_cookie(self):
        with patch.object(self.cli, '_read_response', return_value=(b'<Review>', True)):
            self.assertEqual(self.open().status_code, 200)
        with self.client.session_transaction() as sess: sess['qm_login_time'] = 0
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.assertFalse(self.cli.connected)


if __name__ == '__main__': unittest.main()
