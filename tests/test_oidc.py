"""SSO (OpenID Connect) login: success path and every rejection that matters."""
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from test_manager import manager, login
from oidc_fake import (ADMIN, CLIENT_ID, CLIENT_SECRET, ISSUER, SECOND, FakeProvider, provider, sign,
                       sso_login, start_login)
from oidc_auth import OIDCClient, OIDCError, OIDCSettings


class SSOLoginTest(unittest.TestCase):
    def setUp(self):
        manager._login_attempts.clear()
        self.client = manager.app.test_client()

    def assertRejected(self, response, text=None):
        self.assertEqual(response.status_code, 401, response.data[:300])
        if text:
            self.assertIn(text, response.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_success_with_mfa_creates_session_for_verified_email(self):
        login(self.client)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['qm_user'], ADMIN)
            self.assertNotIn('qm_oidc_pending', sess)
        self.assertEqual(self.client.get('/api/status').status_code, 200)
        users = self.client.get('/api/connections').get_json()['manager']['users']
        self.assertEqual([u['username'] for u in users if u['username'] == ADMIN], [ADMIN])

    def test_authorization_request_uses_pkce_state_and_nonce(self):
        with provider() as fake:
            query = start_login(self.client, fake)
        self.assertEqual(query['response_type'], ['code'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['client_id'], [CLIENT_ID])
        self.assertIn('openid', query['scope'][0].split())
        self.assertGreaterEqual(len(query['state'][0]), 32)
        self.assertNotEqual(query['state'], query['nonce'])

    def test_email_case_is_normalized_and_second_account_allowed(self):
        self.assertEqual(sso_login(self.client, email='SECOND@Example.TEST').status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['qm_user'], SECOND)

    def test_rejects_without_second_factor(self):
        self.assertRejected(sso_login(self.client, amr=['pwd']), 'dois fatores')

    def test_rejects_token_without_amr(self):
        self.assertRejected(sso_login(self.client, amr=None), 'dois fatores')

    def test_accepted_acr_satisfies_mfa(self):
        settings = replace(manager.oidc_settings, accepted_acr=('gold',))
        client = OIDCClient(settings)
        with patch.object(manager, 'oidc_settings', settings), patch.object(manager, 'oidc_client', client):
            self.assertEqual(sso_login(self.client, amr=None, acr='gold').status_code, 302)

    def test_mfa_can_be_explicitly_disabled(self):
        settings = replace(manager.oidc_settings, require_mfa=False)
        client = OIDCClient(settings)
        with patch.object(manager, 'oidc_settings', settings), patch.object(manager, 'oidc_client', client):
            self.assertEqual(sso_login(self.client, amr=None).status_code, 302)

    def test_account_outside_allowlist_gets_no_session(self):
        response = sso_login(self.client, email='intruder@example.test')
        self.assertEqual(response.status_code, 403)
        self.assertIn('administrador', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_rejects_unverified_email(self):
        self.assertRejected(sso_login(self.client, email_verified=False), 'não confirmou o e-mail')

    def test_rejects_wrong_audience_issuer_and_expiry(self):
        now = int(time.time())
        for claims in ({'aud': 'other-app'}, {'iss': 'https://evil.example.test'},
                       {'exp': now - 3600, 'iat': now - 7200}, {'azp': 'other-app'},
                       {'aud': [CLIENT_ID, 'other-app']}):
            with self.subTest(claims=claims):
                self.client = manager.app.test_client()
                self.assertRejected(sso_login(self.client, **claims))

    def test_rejects_wrong_nonce(self):
        self.assertRejected(sso_login(self.client, nonce='attacker-nonce'), 'não corresponde')

    def test_rejects_forged_signatures(self):
        attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cases = {
            'chave desconhecida': lambda claims: sign(claims, key=attacker),
            'kid desconhecido': lambda claims: sign(claims, key=attacker, kid='other'),
            'HS256 com o client secret': lambda claims: jwt.encode(claims, CLIENT_SECRET, algorithm='HS256', headers={'kid': 'test-key-1'}),
            'alg none': lambda claims: jwt.encode(claims, None, algorithm='none'),
        }
        for label, forge in cases.items():
            with self.subTest(label):
                self.client = manager.app.test_client()
                with provider() as fake:
                    query = start_login(self.client, fake)
                    fake.id_token = forge(fake.claims())
                    response = self.client.get('/auth/callback?' + urlencode({'state': query['state'][0], 'code': 'c'}))
                self.assertRejected(response)

    def test_rejects_wrong_missing_and_replayed_state(self):
        with provider() as fake:
            query = start_login(self.client, fake)
            self.assertRejected(self.client.get('/auth/callback?' + urlencode({'state': 'forged', 'code': 'c'})))
            # The failed attempt consumed the pending login: the real state no longer works.
            self.assertRejected(self.client.get('/auth/callback?' + urlencode({'state': query['state'][0], 'code': 'c'})))
            self.assertEqual(fake.token_requests, [])

        other = manager.app.test_client()  # callback in a browser that never started a login
        with provider():
            response = other.get('/auth/callback?' + urlencode({'state': query['state'][0], 'code': 'c'}))
        self.assertEqual(response.status_code, 401)

    def test_successful_callback_cannot_be_replayed(self):
        with provider() as fake:
            query = start_login(self.client, fake)
            callback = '/auth/callback?' + urlencode({'state': query['state'][0], 'code': 'c'})
            self.assertEqual(self.client.get(callback).status_code, 302)
            with self.client.session_transaction() as sess: csrf = sess['csrf_token']
            self.client.post('/logout', data={'csrf_token': csrf})
            self.assertRejected(self.client.get(callback))
            self.assertEqual(len(fake.token_requests), 1)

    def test_rejects_expired_login_attempt(self):
        with provider() as fake:
            query = start_login(self.client, fake)
            with self.client.session_transaction() as sess:
                pending = dict(sess['qm_oidc_pending']); pending['created'] -= 11 * 60
                sess['qm_oidc_pending'] = pending
            response = self.client.get('/auth/callback?' + urlencode({'state': query['state'][0], 'code': 'c'}))
        self.assertRejected(response, '10 minutos')

    def test_provider_error_is_reported_without_details(self):
        with provider() as fake:
            query = start_login(self.client, fake)
            response = self.client.get('/auth/callback?' + urlencode({'state': query['state'][0], 'error': 'access_denied', 'error_description': '<script>x</script>'}))
        self.assertRejected(response, 'não autorizou')
        self.assertNotIn('<script>', response.get_data(as_text=True))

    def test_login_requires_csrf_token(self):
        self.client.get('/login')
        with provider() as fake:
            self.assertEqual(self.client.post('/login', data={}).status_code, 400)
            self.assertEqual(fake.token_requests, [])

    def test_unreachable_provider_shows_message(self):
        self.client.get('/login')
        with self.client.session_transaction() as sess: csrf = sess['csrf_token']
        client = OIDCClient(manager.oidc_settings)
        with patch.object(manager, 'oidc_client', client), \
             patch('oidc_auth._http_json', side_effect=OIDCError('Não foi possível falar com o provedor de login.')):
            response = self.client.post('/login', data={'csrf_token': csrf})
        self.assertEqual(response.status_code, 502)
        self.assertIn('provedor de login', response.get_data(as_text=True))

    def test_discovery_issuer_mismatch_is_refused(self):
        client = OIDCClient(manager.oidc_settings)
        fake = FakeProvider()
        def mismatched(url, **kwargs):
            document = fake(url, **kwargs)
            if url.endswith('openid-configuration'):
                document['issuer'] = 'https://evil.example.test'
            return document
        with patch('oidc_auth._http_json', side_effect=mismatched):
            with self.assertRaises(OIDCError):
                client.begin()

    def test_logout_revokes_cookie_and_optionally_logs_out_at_provider(self):
        csrf = login(self.client)
        name = manager.app.config['SESSION_COOKIE_NAME']
        saved = self.client.get_cookie(name).value
        response = self.client.post('/logout', data={'csrf_token': csrf})
        self.assertEqual(response.headers['Location'], '/login')
        replay = manager.app.test_client()
        replay.set_cookie(name, saved)
        self.assertEqual(replay.get('/api/status').status_code, 401)

        settings = replace(manager.oidc_settings, provider_logout=True)
        client = OIDCClient(settings)
        with patch.object(manager, 'oidc_settings', settings), patch.object(manager, 'oidc_client', client):
            csrf = login(self.client)
            location = self.client.post('/logout', data={'csrf_token': csrf}).headers['Location']
        self.assertTrue(location.startswith(ISSUER + '/logout?'))
        self.assertEqual(parse_qs(urlsplit(location).query)['post_logout_redirect_uri'], ['http://127.0.0.1:8787/login'])


TENANT = '11111111-2222-3333-4444-555555555555'
MS_ISSUER = f'https://login.microsoftonline.com/{TENANT}/v2.0'


class MicrosoftEntraTest(unittest.TestCase):
    """Work/school accounts: no email_verified claim, identified by UPN in one tenant."""

    def setUp(self):
        env = {'QM_OIDC_ISSUER': MS_ISSUER, 'QM_OIDC_CLIENT_ID': CLIENT_ID,
               'QM_OIDC_ALLOWED_EMAILS': 'Voce@Empresa.com.br'}
        with patch.dict('os.environ', env):
            self.settings = OIDCSettings.from_environment()
        self.client = OIDCClient(self.settings)
        discovery = {'issuer': MS_ISSUER,
                     'authorization_endpoint': f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize',
                     'token_endpoint': f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token',
                     'jwks_uri': f'https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys',
                     'id_token_signing_alg_values_supported': ['RS256']}
        from oidc_fake import jwks
        responses = {discovery['jwks_uri']: jwks(),
                     MS_ISSUER + '/.well-known/openid-configuration': discovery}
        guard = patch('oidc_auth._http_json', side_effect=lambda url, **_: responses[url])
        guard.start()
        self.addCleanup(guard.stop)

    def token(self, **changes):
        now = int(time.time())
        claims = {'iss': MS_ISSUER, 'aud': CLIENT_ID, 'sub': 'pairwise-sub', 'tid': TENANT, 'oid': 'object-id',
                  'iat': now, 'exp': now + 300, 'nonce': 'n1', 'preferred_username': 'voce@empresa.com.br',
                  'amr': ['pwd', 'mfa']}
        claims.update(changes)
        return sign({k: v for k, v in claims.items() if v is not None})

    def test_defaults_for_microsoft_issuer(self):
        self.assertEqual(self.settings.user_claim, 'preferred_username')
        self.assertFalse(self.settings.require_email_verified)
        self.assertEqual(self.settings.problems(), [])

    def test_work_account_with_mfa_is_accepted_without_email_verified(self):
        identity = self.client.validate_id_token(self.token(), 'n1')
        self.assertEqual(identity['email'], 'voce@empresa.com.br')

    def test_work_account_without_mfa_is_refused(self):
        with self.assertRaisesRegex(OIDCError, 'dois fatores'):
            self.client.validate_id_token(self.token(amr=['pwd']), 'n1')

    def test_email_claim_is_not_trusted_for_identity(self):
        # A mutable 'email' attribute must not grant access in place of the UPN.
        with self.assertRaisesRegex(OIDCError, 'não está autorizada'):
            self.client.validate_id_token(self.token(preferred_username='outro@empresa.com.br',
                                                     email='voce@empresa.com.br'), 'n1')

    def test_token_from_another_tenant_is_refused(self):
        other = 'https://login.microsoftonline.com/99999999-2222-3333-4444-555555555555/v2.0'
        with self.assertRaises(OIDCError):
            self.client.validate_id_token(self.token(iss=other), 'n1')

    def test_shared_or_domain_tenants_are_rejected(self):
        for issuer in ('https://login.microsoftonline.com/common/v2.0',
                       'https://login.microsoftonline.com/organizations/v2.0',
                       'https://login.microsoftonline.com/empresa.com.br/v2.0',
                       f'https://login.microsoftonline.com/{TENANT}'):
            with self.subTest(issuer=issuer):
                self.assertTrue(replace(self.settings, issuer=issuer).problems())


class SettingsTest(unittest.TestCase):
    def settings(self, **changes):
        base = OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret='', redirect_uri='http://127.0.0.1:8787/auth/callback',
                            allowed_emails=(ADMIN,))
        return replace(base, **changes)

    def test_valid_settings(self):
        self.assertEqual(self.settings().problems(), [])
        self.assertEqual(self.settings(redirect_uri='https://host.tailnet.ts.net/auth/callback').problems(), [])

    def test_invalid_settings(self):
        for changes in ({'issuer': ''}, {'issuer': 'http://idp.example.test'}, {'client_id': ''},
                        {'allowed_emails': ()}, {'allowed_emails': ('not-an-email',)},
                        {'redirect_uri': 'http://192.168.0.5:8787/auth/callback'},
                        {'redirect_uri': 'http://127.0.0.1:8787/other'},
                        {'require_mfa': True, 'mfa_amr': (), 'accepted_acr': ()}):
            with self.subTest(changes=changes):
                self.assertTrue(self.settings(**changes).problems())

    def test_loopback_issuer_allowed_for_local_keycloak(self):
        self.assertEqual(self.settings(issuer='http://localhost:8080/realms/lab').problems(), [])

    def test_version_changes_with_allowlist(self):
        self.assertNotEqual(self.settings().version(), self.settings(allowed_emails=(ADMIN, SECOND)).version())

    def test_health_reports_unconfigured_sso(self):
        with patch.object(manager, 'oidc_settings', self.settings(client_id='')):
            client = manager.app.test_client()
            self.assertFalse(client.get('/api/health').get_json()['auth_configured'])
            self.assertEqual(client.get('/login').status_code, 503)


if __name__ == '__main__':
    unittest.main()
