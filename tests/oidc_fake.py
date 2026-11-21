"""In-process fake OpenID Connect provider: no network, real RSA signatures."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = 'https://idp.example.test'
CLIENT_ID = 'quidway-test'
CLIENT_SECRET = 'client-secret-for-tests-only-0123456789'
ADMIN = 'admin@example.test'
SECOND = 'second@example.test'
# Tests must never touch the real instance/access-list.json of this checkout.
ACCESS_FILE = os.path.join(tempfile.mkdtemp(prefix='qm-access-test-'), 'access-list.json')
ENV = dict(QM_OIDC_ISSUER=ISSUER, QM_OIDC_CLIENT_ID=CLIENT_ID, QM_OIDC_CLIENT_SECRET=CLIENT_SECRET,
           QM_OIDC_ALLOWED_EMAILS=f'{ADMIN},{SECOND}', QM_OIDC_REDIRECT_URI='http://127.0.0.1:8787/auth/callback',
           QM_ACCESS_FILE=ACCESS_FILE,
           QM_CAPABILITIES_FILE=os.path.join(os.path.dirname(ACCESS_FILE), 'switch-capabilities.json'))

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = 'test-key-1'
DISCOVERY = {
    'issuer': ISSUER,
    'authorization_endpoint': ISSUER + '/authorize',
    'token_endpoint': ISSUER + '/token',
    'jwks_uri': ISSUER + '/jwks',
    'end_session_endpoint': ISSUER + '/logout',
    'id_token_signing_alg_values_supported': ['RS256'],
    'token_endpoint_auth_methods_supported': ['client_secret_basic'],
}


def jwks():
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    public.update(kid=KID, use='sig', alg='RS256')
    return {'keys': [public]}


def sign(claims, *, key=KEY, algorithm='RS256', kid=KID):
    return jwt.encode(claims, key, algorithm=algorithm, headers={'kid': kid})


class FakeProvider:
    """Answers discovery/JWKS/token requests; checks PKCE like a real IdP."""

    def __init__(self, **claims):
        self.overrides = claims
        self.nonce = None
        self.challenge = None
        self.token_requests = []
        self.id_token = None  # set to force a specific token

    def claims(self):
        now = int(time.time())
        claims = {'iss': ISSUER, 'aud': CLIENT_ID, 'sub': 'user-123', 'iat': now, 'exp': now + 300,
                  'nonce': self.nonce, 'email': ADMIN, 'email_verified': True, 'amr': ['pwd', 'mfa']}
        claims.update(self.overrides)
        return {k: v for k, v in claims.items() if v is not None}

    def __call__(self, url, *, form=None, basic_auth=None):
        if url == ISSUER + '/.well-known/openid-configuration':
            return dict(DISCOVERY)
        if url == ISSUER + '/jwks':
            return jwks()
        if url == ISSUER + '/token':
            self.token_requests.append((form, basic_auth))
            verifier = form['code_verifier']
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            assert digest == self.challenge, 'PKCE verifier does not match challenge'
            assert basic_auth == (CLIENT_ID, CLIENT_SECRET)
            return {'access_token': 'opaque', 'token_type': 'Bearer',
                    'id_token': self.id_token or sign(self.claims())}
        raise AssertionError('unexpected URL ' + url)


@contextmanager
def provider(**claims):
    fake = FakeProvider(**claims)
    with patch('oidc_auth._http_json', side_effect=fake):
        yield fake


def start_login(client, fake):
    """POST /login and capture the authorization request parameters."""
    client.get('/login')
    with client.session_transaction() as sess:
        csrf = sess['csrf_token']
    response = client.post('/login', data={'csrf_token': csrf})
    assert response.status_code == 302, response.status_code
    query = parse_qs(urlsplit(response.headers['Location']).query)
    fake.nonce = query['nonce'][0]
    fake.challenge = query['code_challenge'][0]
    return query


def sso_login(client, **claims):
    """Complete a full SSO login; returns the callback response."""
    with provider(**claims) as fake:
        query = start_login(client, fake)
        return client.get('/auth/callback?' + urlencode({'state': query['state'][0], 'code': 'auth-code'}))
