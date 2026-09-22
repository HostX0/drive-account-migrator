"""Local Google installed-app OAuth, no browser session extraction or API key.

Run once per account. Writes an authorization URL; open it in the real browser.
Uses PKCE/state, loopback only, explicit account verification, private token files.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import pathlib
import secrets
import time
import urllib.parse
import urllib.request


def atomic_private(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def json_request(url, data=None, token=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        data = urllib.parse.urlencode(data).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def connect(client_path, role, folder, expected):
    expected = expected.strip().lower()
    scope = 'https://www.googleapis.com/auth/drive' + ('.readonly' if role == 'source' else '')
    client = json.loads(pathlib.Path(client_path).read_text())['installed']
    if client['auth_uri'] not in {'https://accounts.google.com/o/oauth2/auth', 'https://accounts.google.com/o/oauth2/v2/auth'}:
        raise ValueError('Unexpected authorization endpoint')
    if client['token_uri'] != 'https://oauth2.googleapis.com/token':
        raise ValueError('Unexpected token endpoint')
    token_path = pathlib.Path(folder) / (role + '-token.json')
    if token_path.exists():
        raise ValueError('Token already exists; verify/refresh rather than overwrite it')
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != '/oauth2callback' or query.get('state') != [state]:
                self.send_error(400, 'Invalid OAuth callback')
                return
            if 'error' in query:
                result['error'] = query['error'][0]
            elif len(query.get('code', [])) == 1:
                result['code'] = query['code'][0]
            else:
                self.send_error(400, 'Missing authorization code')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b'Google authorization received. Account verification is running locally. You can close this tab.')

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    server.timeout = 1
    redirect = 'http://127.0.0.1:%d/oauth2callback' % server.server_address[1]
    url = client['auth_uri'] + '?' + urllib.parse.urlencode({
        'client_id': client['client_id'], 'redirect_uri': redirect,
        'response_type': 'code', 'scope': scope, 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256',
        'access_type': 'offline', 'prompt': 'consent', 'login_hint': expected,
        'include_granted_scopes': 'false',
    })
    atomic_private(pathlib.Path(folder) / (role + '-authorize.json'), {'url': url, 'email': expected, 'scope': scope})
    print('Open this URL locally and sign in as ' + expected + ':\n' + url, flush=True)
    import webbrowser
    webbrowser.open(url)
    deadline = time.monotonic() + 900
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if 'code' not in result:
        raise RuntimeError('Authorization not completed; no token saved')
    token = json_request(client['token_uri'], {
        'client_id': client['client_id'], 'client_secret': client['client_secret'],
        'code': result.pop('code'), 'code_verifier': verifier,
        'grant_type': 'authorization_code', 'redirect_uri': redirect,
    })
    identity = json_request('https://www.googleapis.com/drive/v3/about?fields=user', token=token['access_token'])['user']
    if identity.get('emailAddress', '').lower() != expected:
        raise RuntimeError('Wrong authenticated account; no token saved')
    granted = set(token.get('scope', '').split())
    if granted != {scope}:
        raise RuntimeError('Unexpected granted scopes; no token saved')
    if not token.get('refresh_token'):
        raise RuntimeError('No refresh token returned; no token saved')
    token.update({'email': expected, 'expires_at': time.time() + token['expires_in'],
                  'client_id': client['client_id'], 'client_secret': client['client_secret'],
                  'token_uri': client['token_uri']})
    atomic_private(token_path, token)
    print('Verified and saved local OAuth connection: ' + expected, flush=True)
