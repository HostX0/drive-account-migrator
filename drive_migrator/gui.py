"""Loopback-only browser interface with session authorization and CSRF checks."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from .settings import DEFAULT
from .oauth import atomic_private


def drive_id(text):
    text = text.strip()
    match = re.search(r'/(?:folders|d)/([A-Za-z0-9_-]+)', text)
    value = match.group(1) if match else text
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('Enter a Drive folder URL or ID.')
    return value


def command(config, action, *extra):
    return [sys.executable, '-m', 'drive_migrator', '--config', str(config), action, *map(str, extra)]


def checked_config(value):
    config = {key: value.get(key, default) for key, default in DEFAULT.items()}
    for key in ('source_email', 'destination_email'):
        config[key] = str(config[key]).strip().lower()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', config[key]):
            raise ValueError('Enter both email addresses.')
    if config['source_email'] == config['destination_email']:
        raise ValueError('Choose two different accounts.')
    for key in ('source_root_id', 'destination_root_id', 'controller_id'):
        config[key] = drive_id(config[key])
    if config['source_root_id'] == config['destination_root_id']:
        raise ValueError('Source and destination folders must be different.')
    if type(config['share_source']) is not bool:
        raise ValueError('Sharing must be on or off.')
    if not isinstance(config['blocked_source_ids'], list):
        raise ValueError('Invalid exclusions.')
    config['blocked_source_ids'] = [drive_id(v) for v in config['blocked_source_ids']]
    state = Path(config['state_dir']).expanduser().resolve()
    checkout = Path(__file__).resolve().parent.parent
    if state == checkout or checkout in state.parents:
        raise ValueError('Use a private state directory outside the repository.')
    config['state_dir'] = str(state)
    return config


class LocalApp:
    def __init__(self, config_path=None):
        self.config_path = config_path or Path.home()/'.config/drive-account-migrator/config.json'
        self.config = dict(DEFAULT)
        self.config.update(source_email='', destination_email='', source_root_id='', destination_root_id='')
        if self.config_path.exists():
            self.config = checked_config(json.loads(self.config_path.read_text()))
        self.process = None
        self.action = None
        self.lines = []
        self.lock = threading.RLock()
        self.last_exit = None
        self.auth_url = None

    def state_dir(self):
        return Path(self.config['state_dir']).expanduser()

    def busy(self):
        return self.process is not None and self.process.poll() is None

    def snapshot(self):
        with self.lock:
            base = self.state_dir()
            return {'config': self.config, 'config_path': str(self.config_path),
                    'busy': self.busy(), 'action': self.action, 'last_exit': self.last_exit,
                    'log': self.lines[-100:], 'auth_url': self.auth_url,
                    'connections': {role: (base/'auth'/(role+'-token.json')).exists()
                                    for role in ('source', 'destination', 'source-cleanup')},
                    'has_client': (base/'auth/client.json').exists()}

    def save(self, value):
        with self.lock:
            if self.busy():
                raise ValueError('Wait for the running operation before changing setup.')
            config = checked_config(value)
            identity = Path(config['state_dir'])/'identity.json'
            if identity.exists():
                binding = json.loads(identity.read_text())
                if any(config[key] != binding[key] for key in binding):
                    raise ValueError('This state directory belongs to a different migration. Load its setup or choose a new private state directory.')
            atomic_private(self.config_path, config)
            self.config = config
            return {'saved': True}

    def upload_client(self, value):
        with self.lock:
            if self.busy():
                raise ValueError('Wait for the running operation before replacing credentials.')
            client = value.get('installed', {})
            if (client.get('auth_uri') not in {'https://accounts.google.com/o/oauth2/auth', 'https://accounts.google.com/o/oauth2/v2/auth'}
                    or client.get('token_uri') != 'https://oauth2.googleapis.com/token'
                    or not client.get('client_id') or not client.get('client_secret')):
                raise ValueError('Choose the Desktop app OAuth JSON downloaded from Google. An API key is not sufficient.')
            # Never return or log the credential contents.
            atomic_private(self.state_dir()/'auth/client.json', value)
            return {'saved': True}

    def report(self, name):
        if name not in ('plan', 'trash-plan', 'purge-plan', 'live-status', 'cleanup-status'):
            raise ValueError('Unknown report.')
        path = self.state_dir()/(name+'.json')
        if not path.exists():
            raise ValueError('Run the preview first.')
        return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'report': json.loads(path.read_text())}

    def launch(self, value):
        with self.lock:
            if self.busy():
                raise ValueError('An operation is already running.')
            self.save(self.config)
            action = value.get('action')
            args = []
            if action == 'connect':
                role = value.get('role')
                if role not in ('source', 'destination', 'source-cleanup'):
                    raise ValueError('Invalid account role.')
                client = self.state_dir()/'auth/client.json'
                if not client.exists():
                    raise ValueError('Select your OAuth client JSON in Setup first.')
                args = ['--role', role, '--client', str(client)]
            elif action == 'init-controller':
                args = ['--apply']
            elif action == 'cleanup-plan':
                if value.get('phase') not in ('trash', 'purge'):
                    raise ValueError('Choose a cleanup phase.')
                args = ['--phase', value['phase']]
            elif action in ('copy', 'trash', 'purge'):
                count = int(value.get('max_items', 100))
                if not 1 <= count <= 10000:
                    raise ValueError('Use 1–10000 items per run.')
                args = ['--minutes', '30', '--max-items', str(count)]
                if action == 'copy':
                    args += ['--apply']
                else:
                    expected = 'TRASH_VERIFIED_ORIGINALS' if action == 'trash' else 'PERMANENTLY_DELETE_VERIFIED_ORIGINALS'
                    current = self.report(action+'-plan')
                    if value.get('acknowledge') != expected or value.get('plan_sha256') != current['sha256']:
                        raise ValueError('Review the current plan and type the exact acknowledgement.')
                    args += ['--plan-sha256', current['sha256'], '--acknowledge', expected]
            elif action not in ('plan', 'status'):
                raise ValueError('Unknown action.')
            self.state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.state_dir()/'STOP').unlink(missing_ok=True)
            env = dict(os.environ)
            env.pop('DRIVE_MIGRATOR_CONFIG', None)
            env['PYTHONUNBUFFERED'] = '1'
            self.lines, self.last_exit, self.auth_url = [], None, None
            self.process = subprocess.Popen(command(self.config_path, action, *args),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                cwd=Path(__file__).resolve().parent.parent)
            self.action = action
            process = self.process
            def collect():
                for line in process.stdout:
                    with self.lock:
                        if line.startswith('https://accounts.google.com/'):
                            self.auth_url = line.strip()
                            self.lines.append('Google authorization opened. Check the account in your browser.')
                        else:
                            self.lines.append(line.strip())
                        self.lines = self.lines[-100:]
                code = process.wait()
                with self.lock:
                    self.last_exit, self.auth_url = code, None
                    if action == 'init-controller' and code == 0:
                        self.config = checked_config(json.loads(self.config_path.read_text()))
            threading.Thread(target=collect, daemon=True).start()
            return {'started': action}

    def stop(self):
        with self.lock:
            if self.busy():
                if self.action == 'connect':
                    self.process.terminate()
                else:
                    (self.state_dir()/'STOP').touch(mode=0o600)
            return {'stop_requested': True}


def handler(app, session_token):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data, content_type='application/json'):
            payload = json.dumps(data).encode() if content_type == 'application/json' else data
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            host = '127.0.0.1:'+str(self.server.server_address[1])
            if self.headers.get('Host') != host:
                return False
            origin = self.headers.get('Origin')
            if origin and origin != 'http://'+host:
                return False
            return secrets.compare_digest(self.headers.get('X-Session-Token', ''), session_token)

        def do_GET(self):
            route = urllib.parse.urlparse(self.path).path
            if route.startswith('/api/'):
                if not self.authorized():
                    return self.respond(403, {'error': 'Open the local URL printed by the launcher.'})
                try:
                    if route == '/api/state':
                        value = app.snapshot()
                    elif route.startswith('/api/report/'):
                        value = app.report(route.removeprefix('/api/report/'))
                    else:
                        return self.respond(404, {'error': 'Unknown endpoint.'})
                    return self.respond(200, value)
                except Exception as error:
                    return self.respond(400, {'error': str(error) if isinstance(error, ValueError) else type(error).__name__})
            files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if route not in files:
                return self.respond(404, {'error': 'Not found.'})
            name, mime = files[route]
            self.respond(200, (Path(__file__).parent/'web'/name).read_bytes(), mime)

        def do_POST(self):
            if not self.authorized():
                return self.respond(403, {'error': 'Invalid local session or origin.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 262144 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('Expected a small JSON request.')
                value = json.loads(self.rfile.read(length))
                routes = {'/api/config': app.save, '/api/client': app.upload_client, '/api/run': app.launch,
                          '/api/stop': lambda _: app.stop()}
                if self.path not in routes:
                    return self.respond(404, {'error': 'Unknown endpoint.'})
                self.respond(200, routes[self.path](value))
            except Exception as error:
                self.respond(400, {'error': str(error) if isinstance(error, ValueError) else type(error).__name__})
    return Handler


def main(config_path=None):
    os.umask(0o077)
    app = LocalApp(config_path)
    token = secrets.token_urlsafe(32)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler(app, token))
    url = 'http://127.0.0.1:'+str(server.server_address[1])+'/#'+token
    print('Local interface: '+url, flush=True)
    print('Keep this terminal open. Your credentials stay on this computer. Ctrl+C requests a safe stop.', flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        app.stop()
        if app.process and app.busy():
            print('Waiting for the worker to checkpoint and release its lease…', flush=True)
            app.process.wait()
    finally:
        server.server_close()
    return 0
