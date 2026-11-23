"""Real loopback server smoke test, using isolated settings and a free port."""
import json, os, secrets, socket, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
class LauncherTest(unittest.TestCase):
    def test_launch_duplicate_and_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'local-settings.ps1'
            # SSO settings only: discovery is lazy, so no network is needed to start.
            p.write_text("$env:QM_OIDC_ISSUER = 'https://idp.example.test'\n$env:QM_OIDC_CLIENT_ID = 'launcher-test'\n$env:QM_OIDC_ALLOWED_EMAILS = 'admin@example.test'\n$env:QM_SECRET_KEY = '"+secrets.token_urlsafe(48)+"'\n",encoding='utf-8')
            with socket.socket() as s:
                s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
            env={k:v for k,v in os.environ.items() if not k.startswith('QM_')}; env.update(QM_SETTINGS_FILE=str(p),QM_WEB_PORT=str(port),QM_MODE='demo',PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
            cmd=[sys.executable,str(ROOT/'start.py')]
            proc=subprocess.Popen(cmd,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                base=f'http://127.0.0.1:{port}'
                for _ in range(60):
                    try:
                        with urlopen(base+'/api/health',timeout=1) as response: health=json.load(response)
                        break
                    except OSError: time.sleep(.15)
                else: self.fail('Server did not start')
                self.assertTrue(health['auth_configured']); self.assertEqual(health['mode'],'demo'); self.assertEqual(health['auth'],'oidc')
                with urlopen(base+'/login',timeout=3) as response: self.assertIn(b'sso-login',response.read())
                duplicate=subprocess.run(cmd,env=env,capture_output=True,text=True,encoding='utf-8',timeout=10)
                self.assertEqual(duplicate.returncode,0,duplicate.stderr)
            finally:
                proc.terminate()
                try: proc.wait(timeout=8)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)


class OutdatedInstanceTest(unittest.TestCase):
    def test_password_era_instance_is_not_reused(self):
        """A pre-SSO Manager on the port must be reported, never silently reused."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class OldHealth(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({'ok': True, 'service': 'Quidway Manager', 'mode': 'serial',  # name before the rename
                                   'auth_required': True, 'auth_configured': True}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        server = HTTPServer(('127.0.0.1', 0), OldHealth)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'local-settings.ps1'
            p.write_text("$env:QM_OIDC_ISSUER = 'https://idp.example.test'\n$env:QM_OIDC_CLIENT_ID = 'x'\n$env:QM_OIDC_ALLOWED_EMAILS = 'a@example.test'\n$env:QM_SECRET_KEY = '"+secrets.token_urlsafe(48)+"'\n", encoding='utf-8')
            env = {k: v for k, v in os.environ.items() if not k.startswith('QM_')}
            # Windows consoles default to a legacy code page; the message contains accents.
            env.update(QM_SETTINGS_FILE=str(p), QM_WEB_PORT=str(server.server_address[1]), PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
            result = subprocess.run([sys.executable, str(ROOT/'start.py')], env=env, capture_output=True,
                                    text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn('versão anterior', result.stderr)
