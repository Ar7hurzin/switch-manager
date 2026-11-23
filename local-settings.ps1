# Configurações locais do Quidway Manager



$env:QM_MODE = 'serial'
$env:QM_SERIAL_PORT = '/dev/ttyUSB0'
$env:QM_SERIAL_BAUD = '115200'
$env:QM_WEB_PORT = '8787'
$env:QM_ENABLE_CONFIG_WRITES = 'true'
$env:QM_ENABLE_CLI = 'true'

# Login SSO (OpenID Connect). Nenhuma senha de usuário é armazenada.
$env:QM_OIDC_ISSUER = 'https://accounts.google.com'
$env:QM_OIDC_CLIENT_ID = '393829623221-rim0c2p7iua0bflq24b7fqknvgvvob9a.apps.googleusercontent.com'
$env:QM_OIDC_CLIENT_SECRET = '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'
$env:QM_OIDC_REDIRECT_URI = 'http://127.0.0.1:8787/auth/callback'
$env:QM_OIDC_ALLOWED_EMAILS = 'arthurflopes13@gmail.com'
$env:QM_OIDC_REQUIRE_MFA = 'false'
# Valores 'amr' aceitos como 2FA (padrão: mfa,otp,totp,hwk,fido,webauthn). Keycloak/Authentik podem usar QM_OIDC_ACCEPTED_ACR.
$env:QM_SECRET_KEY = 'UYIOneSPg3oZSDUZthKWFpXgwc1O9ejz0tFxNfYSPPzMLiJUm1wUR6KVYqjq5V-t'
