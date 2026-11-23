"""Runtime access list: only the administrator manages it; removals apply immediately."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_manager import manager, login
from oidc_fake import ADMIN, SECOND, sso_login
from access_list import AccessList, AccessListError, normalize
from oidc_auth import GOOGLE_ISSUER, OIDCClient, OIDCSettings


class AdminRoutesTest(unittest.TestCase):
    def setUp(self):
        manager._login_attempts.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        guard = patch.object(manager, 'access_list', AccessList(Path(self.temp.name) / 'access.json'))
        guard.start()
        self.addCleanup(guard.stop)
        self.admin = manager.app.test_client()
        self.headers = {'X-CSRF-Token': login(self.admin)}

    def post(self, path, value, client=None, headers=None):
        return (client or self.admin).post(path, json={'value': value}, headers=headers or self.headers)

    def test_admin_sees_link_and_page(self):
        self.assertIn(b'href="/admin"', self.admin.get('/').data)
        page = self.admin.get('/admin')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'add-form', page.data)
        data = self.admin.get('/api/admin/access').get_json()
        self.assertEqual(data['admin'], ADMIN)

    def test_other_users_cannot_administer(self):
        other = manager.app.test_client()
        self.assertEqual(sso_login(other, email=SECOND).status_code, 302)
        with other.session_transaction() as sess: csrf = sess['csrf_token']
        self.assertNotIn(b'href="/admin"', other.get('/').data)
        self.assertEqual(other.get('/admin').status_code, 403)
        self.assertEqual(other.get('/api/admin/access').status_code, 403)
        self.assertEqual(self.post('/api/admin/access', 'x@example.test', other, {'X-CSRF-Token': csrf}).status_code, 403)
        self.assertEqual(manager.access_list.entries(), [])

    def test_add_then_remove_revokes_immediately(self):
        newcomer = manager.app.test_client()
        self.assertEqual(sso_login(newcomer, email='new@example.test').status_code, 403)
        self.assertEqual(newcomer.get('/api/status').status_code, 401)
        self.assertEqual(self.post('/api/admin/access', 'New@Example.test').status_code, 200)
        newcomer = manager.app.test_client()
        self.assertEqual(sso_login(newcomer, email='new@example.test').status_code, 302)
        self.assertEqual(newcomer.get('/api/status').status_code, 200)
        self.assertEqual(newcomer.get('/admin').status_code, 403)
        self.assertEqual(self.post('/api/admin/access/remove', 'new@example.test').status_code, 200)
        self.assertEqual(newcomer.get('/api/status').status_code, 401)

    def _copied_cookie_client(self, source):
        copy = manager.app.test_client()
        cookie = source.get_cookie(manager.app.config['SESSION_COOKIE_NAME'])
        copy.set_cookie(cookie.key, cookie.value)
        return copy

    def test_removal_revokes_idle_sessions_so_readding_does_not_revive_them(self):
        self.assertEqual(self.post('/api/admin/access', 'idle@example.test').status_code, 200)
        user = manager.app.test_client()
        self.assertEqual(sso_login(user, email='idle@example.test').status_code, 302)
        stale = self._copied_cookie_client(user)
        self.assertEqual(stale.get('/api/status').status_code, 200)
        removed = self.post('/api/admin/access/remove', 'idle@example.test')
        self.assertEqual(removed.get_json()['revoked_sessions'], 1)
        self.assertEqual(self.post('/api/admin/access', 'idle@example.test').status_code, 200)
        self.assertEqual(stale.get('/api/status').status_code, 401)
        self.assertEqual(user.get('/api/status').status_code, 401)
        # The administrator's own session is untouched.
        self.assertEqual(self.admin.get('/api/status').status_code, 200)

    def test_domain_removal_revokes_its_sessions(self):
        self.assertEqual(self.post('/api/admin/access', '@corp.example.test').status_code, 200)
        member = manager.app.test_client()
        self.assertEqual(sso_login(member, email='someone@corp.example.test').status_code, 302)
        stale = self._copied_cookie_client(member)
        self.assertEqual(self.post('/api/admin/access/remove', '@corp.example.test').status_code, 200)
        self.assertEqual(self.post('/api/admin/access', '@corp.example.test').status_code, 200)
        self.assertEqual(stale.get('/api/status').status_code, 401)

    def test_domain_entry_admits_whole_domain(self):
        self.assertEqual(self.post('/api/admin/access', '@corp.example.test').status_code, 200)
        member = manager.app.test_client()
        self.assertEqual(sso_login(member, email='anyone@corp.example.test').status_code, 302)
        outsider = manager.app.test_client()
        self.assertEqual(sso_login(outsider, email='anyone@evil-corp.example.test').status_code, 403)

    def test_validation_and_csrf(self):
        for bad in ('@gmail.com', 'not-an-email', '', '@', 'a@b'):
            with self.subTest(bad=bad):
                self.assertEqual(self.post('/api/admin/access', bad).status_code, 400)
        self.assertEqual(self.post('/api/admin/access', 'dup@example.test').status_code, 200)
        self.assertEqual(self.post('/api/admin/access', 'dup@example.test').status_code, 400)
        self.assertEqual(self.post('/api/admin/access/remove', ADMIN).status_code, 400)
        self.assertEqual(self.admin.post('/api/admin/access', json={'value': 'x@example.test'}).status_code, 400)


class AccessRequestTest(AdminRoutesTest):
    """Unknown verified accounts ask for access; only the administrator approves."""

    def act(self, action, email, client=None, headers=None):
        return (client or self.admin).post(f'/api/admin/requests/{action}', json={'email': email},
                                           headers=headers or self.headers)

    def test_request_approve_then_login(self):
        visitor = manager.app.test_client()
        response = sso_login(visitor, email='visitor@example.test', name='Visitante Teste')
        self.assertEqual(response.status_code, 403)
        self.assertIn('pedido foi enviado', response.get_data(as_text=True))
        self.assertEqual(visitor.get('/api/status').status_code, 401)

        requests = self.admin.get('/api/admin/access').get_json()['requests']
        self.assertEqual([(r['email'], r['name']) for r in requests], [('visitor@example.test', 'Visitante Teste')])
        self.assertIn(b'admin-pending-count', self.admin.get('/').data)

        again = sso_login(manager.app.test_client(), email='visitor@example.test')
        self.assertIn('já está com o administrador', again.get_data(as_text=True))
        self.assertEqual(len(manager.access_list.pending()), 1)

        self.assertEqual(self.act('approve', 'visitor@example.test').status_code, 200)
        self.assertEqual(manager.access_list.pending(), [])
        visitor = manager.app.test_client()
        self.assertEqual(sso_login(visitor, email='visitor@example.test').status_code, 302)
        self.assertEqual(visitor.get('/api/status').status_code, 200)

    def test_reject_blocks_new_request_for_cooldown(self):
        sso_login(manager.app.test_client(), email='spam@example.test')
        self.assertEqual(self.act('reject', 'spam@example.test').status_code, 200)
        response = sso_login(manager.app.test_client(), email='spam@example.test')
        self.assertEqual(response.status_code, 403)
        self.assertIn('recusado', response.get_data(as_text=True))
        self.assertEqual(manager.access_list.pending(), [])
        self.assertEqual(self.act('approve', 'spam@example.test').status_code, 400)

    def test_request_requires_full_verification(self):
        # Without 2FA or with an unverified e-mail no request is recorded.
        sso_login(manager.app.test_client(), email='nomfa@example.test', amr=['pwd'])
        sso_login(manager.app.test_client(), email='unverified@example.test', email_verified=False)
        self.assertEqual(manager.access_list.pending(), [])

    def test_only_admin_can_decide(self):
        sso_login(manager.app.test_client(), email='visitor@example.test')
        other = manager.app.test_client()
        sso_login(other, email=SECOND)
        with other.session_transaction() as sess: csrf = sess['csrf_token']
        self.assertEqual(self.act('approve', 'visitor@example.test', other, {'X-CSRF-Token': csrf}).status_code, 403)
        self.assertEqual(self.admin.post('/api/admin/requests/approve', json={'email': 'visitor@example.test'}).status_code, 400)
        self.assertEqual(len(manager.access_list.pending()), 1)


class AccessListTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'sub' / 'access.json'
        self.access = AccessList(self.path)

    def test_persists_with_private_permissions(self):
        self.access.add('a@ufu.br', added_by=ADMIN)
        self.assertTrue(AccessList(self.path).allows('A@ufu.br', domain_verified_by=None))
        if os.name == 'posix':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('senha', self.path.read_text().lower())

    def test_google_domain_requires_workspace_hd(self):
        self.access.add('@ufu.br', added_by=ADMIN)
        # Personal Google account with a verified external @ufu.br address: no hd → refused.
        self.assertFalse(self.access.allows('aluno@ufu.br', domain_verified_by=''))
        self.assertFalse(self.access.allows('aluno@ufu.br', domain_verified_by='outra.edu.br'))
        self.assertTrue(self.access.allows('aluno@ufu.br', domain_verified_by='ufu.br'))
        self.assertFalse(self.access.allows('aluno@sub.ufu.br', domain_verified_by=None))

    def test_corrupt_file_grants_nobody(self):
        self.access.add('a@ufu.br', added_by=ADMIN)
        self.path.write_text('{not json')
        self.assertFalse(self.access.allows('a@ufu.br', domain_verified_by=None))

    def test_pending_requests_are_bounded(self):
        with patch('access_list.MAX_PENDING_REQUESTS', 2):
            self.assertEqual(self.access.request('a@x.test', 'A'), 'pending')
            self.assertEqual(self.access.request('b@x.test', 'B'), 'pending')
            self.assertEqual(self.access.request('c@x.test', 'C'), 'full')
        self.assertFalse(self.access.allows('a@x.test', domain_verified_by=None))

    def test_normalize(self):
        self.assertEqual(normalize(' Pessoa@UFU.br '), 'pessoa@ufu.br')
        self.assertEqual(normalize('@UFU.br'), '@ufu.br')
        for bad in ('@gmail.com', '@hotmail.com', 'x@', '@', 'a b@c.d', None):
            with self.subTest(bad=bad), self.assertRaises(AccessListError):
                normalize(bad)

    def test_google_organization_domain_comes_from_hd_only(self):
        settings = OIDCSettings(issuer=GOOGLE_ISSUER, client_id='c', client_secret='s',
                                redirect_uri='http://127.0.0.1:8787/auth/callback', allowed_emails=(ADMIN,))
        client = OIDCClient(settings)
        self.assertEqual(client.organization_domain({'hd': 'UFU.br'}), 'ufu.br')
        self.assertEqual(client.organization_domain({}), '')
        other = OIDCClient(OIDCSettings(issuer='https://idp.example.test', client_id='c', client_secret='',
                                        redirect_uri='http://127.0.0.1:8787/auth/callback', allowed_emails=(ADMIN,)))
        self.assertIsNone(other.organization_domain({'hd': 'x'}))

    def test_admin_defaults_to_first_configured_email(self):
        with patch.dict('os.environ', {'QM_OIDC_ISSUER': GOOGLE_ISSUER, 'QM_OIDC_CLIENT_ID': 'c',
                                       'QM_OIDC_ALLOWED_EMAILS': 'Eu@gmail.com,b@gmail.com'}, clear=False):
            os.environ.pop('QM_OIDC_ADMIN_EMAIL', None)
            settings = OIDCSettings.from_environment()
        self.assertEqual(settings.admin, 'eu@gmail.com')
        self.assertTrue(settings.is_admin('EU@gmail.com'))
        self.assertFalse(settings.is_admin('b@gmail.com'))
        no_admin = OIDCSettings(issuer=GOOGLE_ISSUER, client_id='c', client_secret='s',
                                redirect_uri='http://127.0.0.1:8787/auth/callback', allowed_emails=())
        self.assertTrue(any('ADMIN' in p for p in no_admin.problems()))


if __name__ == '__main__':
    unittest.main()
