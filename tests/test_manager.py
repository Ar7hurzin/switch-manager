"""Functional regression checks; no network or physical switch required."""
import os
import secrets
import unittest
from unittest.mock import patch

from oidc_fake import ENV, provider, sso_login

os.environ.update(ENV, QM_SECRET_KEY=secrets.token_urlsafe(48), QM_MODE='demo', QM_ENABLE_CONFIG_WRITES='false', QM_ENABLE_CLI='false')
import app as manager


def login(client):
    """Sign in through the fake SSO provider; returns the new CSRF token."""
    response = sso_login(client)
    assert response.status_code == 302 and response.headers['Location'] == '/', (response.status_code, response.data[:300])
    with client.session_transaction() as sess: return sess['csrf_token']

class ManagerTest(unittest.TestCase):
    def setUp(self):
        manager._login_attempts.clear()
        self.client = manager.app.test_client()
    def login(self):
        return login(self.client)
    def test_public_health(self):
        result=self.client.get('/api/health').get_json()
        self.assertTrue(result['auth_configured']); self.assertEqual(result['mode'],'demo')
    def test_protected_pages_and_api(self):
        self.assertEqual(self.client.get('/').status_code,302)
        self.assertEqual(self.client.get('/api/status').status_code,401)
    def test_session_cookie_is_secure_when_served_over_https(self):
        def cookie(**kwargs):
            headers = manager.app.test_client().get('/login', **kwargs).headers.getlist('Set-Cookie')
            return next(h for h in headers if h.startswith(manager.app.config['SESSION_COOKIE_NAME'] + '='))
        self.assertNotIn('Secure', cookie())  # plain local HTTP keeps working
        self.assertIn('Secure', cookie(base_url='http://qm.example-tailnet.ts.net'))  # Tailscale Serve
        self.assertIn('Secure', cookie(base_url='https://manager.example.test'))
        self.assertIn('Secure', cookie(headers={'X-Forwarded-Proto': 'https'}))
    def test_login_and_pages(self):
        page=self.client.get('/login'); self.assertIn(b'sso-login',page.data); self.assertNotIn(b'type="password"',page.data)
        self.login()
        for page in ('/','/sistema','/monitoramento','/configuracao','/cli','/api/status','/api/demo','/api/config/status','/api/cli/status','/api/connections'):
            with self.subTest(page=page): self.assertEqual(self.client.get(page).status_code,200)
        for path in ('/static/styles.css','/static/app.js'):
            response = self.client.get(path)
            try: self.assertEqual(response.status_code,200)
            finally: response.close()
    def test_login_csrf_and_logout(self):
        self.assertEqual(self.client.post('/login',data={}).status_code,400)
        token=self.login()
        self.assertEqual(self.client.post('/logout').status_code,400)
        self.assertEqual(self.client.post('/logout',data={'csrf_token':token}).status_code,302)
        self.assertEqual(self.client.get('/api/demo').status_code,401)
    def test_login_rate_limit(self):
        self.client.get('/login')
        with self.client.session_transaction() as sess: token=sess['csrf_token']
        with provider():
            for _ in range(8): self.assertEqual(self.client.post('/login',data={'csrf_token':token}).status_code,302)
            self.assertEqual(self.client.post('/login',data={'csrf_token':token}).status_code,429)
    def test_login_rate_limit_is_per_browser_behind_a_shared_proxy(self):
        # Behind Tailscale Serve/Docker every browser arrives as 127.0.0.1: one
        # anonymous client exhausting its bucket must not lock others out of SSO.
        self.client.get('/login')
        with self.client.session_transaction() as sess: token=sess['csrf_token']
        with provider():
            for _ in range(8): self.client.post('/login',data={'csrf_token':token})
            self.assertEqual(self.client.post('/login',data={'csrf_token':token}).status_code,429)
            victim=manager.app.test_client(); victim.get('/login')
            with victim.session_transaction() as sess: victim_token=sess['csrf_token']
            self.assertEqual(victim.post('/login',data={'csrf_token':victim_token}).status_code,302)
    def test_demo_and_write_guards(self):
        token=self.login(); headers={'X-CSRF-Token':token}
        self.assertEqual(self.client.post('/api/refresh',json={},headers=headers).status_code,200)
        self.assertEqual(self.client.post('/api/config/apply',json={},headers=headers).status_code,409)
        with patch.dict(os.environ,{'QM_MODE':'serial'}):
            self.assertEqual(self.client.post('/api/config/apply',json={},headers=headers).status_code,403)
            self.assertEqual(self.client.post('/api/cli/open',json={},headers=headers).status_code,403)
            self.assertFalse(self.client.get('/api/cli/status').get_json()['available'])
    def test_serial_exclusion(self):
        token=self.login(); headers={'X-CSRF-Token':token}
        with patch.dict(os.environ,{'QM_MODE':'serial'}):
            with manager._serial_request_lock:
                self.assertEqual(self.client.post('/api/refresh',json={},headers=headers).status_code,409)
    def test_session_expiry(self):
        self.login()
        with self.client.session_transaction() as sess: sess['qm_login_time']=0
        self.assertEqual(self.client.get('/api/status').status_code,401)
    def test_unicode_csrf_rejected(self):
        self.client.get("/login")
        self.assertEqual(self.client.post("/login", data={"csrf_token":"ç"}).status_code,400)

    def test_secure_headers(self):
        response=self.client.get('/login')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(response.headers['X-Frame-Options'],'DENY')

if __name__ == '__main__': unittest.main()
