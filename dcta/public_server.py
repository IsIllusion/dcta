"""Public demo host: one opaque browser cookie and one database per visitor.

Run one process behind the bundled HTTPS reverse proxy. The copied transaction
handler receives a request-local application; it never selects a database from
an API parameter, account name or conversation identifier.
"""
import argparse
from collections import deque
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
from http.cookies import SimpleCookie, CookieError
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from .domain import DomainError
from .gui import Application, Handler, Server
from .runtime import DatabaseLease
from . import payments

COOKIE = 'dcta_visitor'
CLOUD_PATHS = {'/api/turn', '/api/validate', '/api/audio', '/api/voice/submit', '/api/speech'}
BLOCKED_PREFIXES = ('/api/demo/', '/api/study/', '/api/runtime')


@dataclass(frozen=True)
class Settings:
    origin: str
    data_dir: Path
    bind: str = '127.0.0.1'
    port: int = 8780
    local_preview: bool = False
    visitor_ttl: int = 86400
    max_visitors: int = 100
    visitor_ai_requests: int = 120
    daily_ai_requests: int = 2000
    workspace_bytes: int = 64 * 1024 * 1024
    http_workers: int = 16

    def __post_init__(self):
        u = urlsplit(self.origin)
        if (not u.hostname or u.username or u.password or u.path or u.query or u.fragment
                or self.origin != f'{u.scheme}://{u.netloc}' or not 0 <= self.port <= 65535):
            raise ValueError('PUBLIC_ORIGIN must be an origin without a path or trailing slash.')
        if self.local_preview:
            if u.scheme != 'http' or u.hostname not in ('localhost', '127.0.0.1') or self.bind != '127.0.0.1':
                raise ValueError('Local preview must use HTTP on the loopback interface.')
        elif u.scheme != 'https':
            raise ValueError('Public deployment requires an HTTPS PUBLIC_ORIGIN.')
        if not (1 <= self.visitor_ttl <= 604800 and 1 <= self.max_visitors <= 500
                and 1 <= self.visitor_ai_requests <= 1000 and 1 <= self.daily_ai_requests <= 100000
                and self.workspace_bytes >= 1024 * 1024 and 1 <= self.http_workers <= 32):
            raise ValueError('Invalid public demo capacity settings.')


class VisitorRegistry:
    def __init__(self, settings, factory, clock=time.time):
        self.settings, self.factory, self.clock = settings, factory, clock
        if Path(settings.data_dir).is_symlink():
            raise ValueError('Choose a separate data directory for the public demo.')
        self.root = Path(settings.data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / '.dcta-public-demo'
        if not marker.exists():
            if any(self.root.iterdir()):
                raise ValueError('Choose an empty data directory for the public demo.')
            marker.write_text('DCTA public demo data\n', encoding='utf-8')
        if self.root.is_symlink() or marker.read_text(encoding='utf-8') != 'DCTA public demo data\n':
            raise ValueError('Invalid public demo data directory.')
        self.path = self.root / 'visitors.sqlite'
        self.lease = DatabaseLease(self.root / 'public-host')
        self.lease.__enter__()
        self.lock = threading.RLock()
        self.apps, self.refs = {}, {}
        self.creations = deque()
        try:
            with self.db() as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS visitors (id TEXT PRIMARY KEY, expires REAL NOT NULL, ai_requests INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS daily_usage (day INTEGER PRIMARY KEY, ai_requests INTEGER NOT NULL);
                ''')
            self.purge_expired()
        except BaseException:
            self.lease.__exit__(None, None, None)
            raise

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            with db:
                yield db

    def directory(self, identity):
        if not re.fullmatch(r'[0-9a-f]{64}', identity):
            raise ValueError('Invalid visitor identifier.')
        p = self.root / identity
        if p.is_symlink() or p.resolve().parent != self.root:
            raise ValueError('Invalid visitor directory.')
        return p

    def purge_expired(self):
        with self.lock, self.db() as db:
            expired = db.execute('SELECT id FROM visitors WHERE expires<=?', (self.clock(),)).fetchall()
            for (identity,) in expired:
                app = self.apps.get(identity)
                if self.refs.get(identity) or (app and not app.voice_slot.acquire(blocking=False)):
                    continue
                if app:
                    app.voice_slot.release()
                    app.runtime.close()
                    self.apps.pop(identity)
                p = self.directory(identity)
                # Remove only this registry's marked, resolved child directory.
                if p.exists():
                    owned = p / '.visitor'
                    if not owned.is_file() or owned.read_text(encoding='ascii') != identity:
                        continue
                    if any(child.is_symlink() for child in p.rglob('*')):
                        continue
                    assert p.resolve().parent == self.root and p != self.root
                    shutil.rmtree(p)
                db.execute('DELETE FROM visitors WHERE id=?', (identity,))
            db.execute('DELETE FROM daily_usage WHERE day<?', (int(self.clock() // 86400) - 2,))

    def create(self):
        self.purge_expired()
        with self.lock, self.db() as db:
            now = self.clock()
            while self.creations and self.creations[0] <= now - 60:
                self.creations.popleft()
            if len(self.creations) >= 20 or db.execute('SELECT COUNT(*) FROM visitors').fetchone()[0] >= self.settings.max_visitors:
                raise DomainError('DEMO_CAPACITY', 'The demo is busy. Please try again later.')
            cookie = secrets.token_urlsafe(32)
            identity = hashlib.sha256(cookie.encode('ascii')).hexdigest()
            p = self.directory(identity)
            p.mkdir()
            (p / '.visitor').write_text(identity, encoding='ascii')
            # Creating an application seeds only the built-in synthetic banking data.
            app = self.factory(p / 'bank.sqlite', identity)
            self.apps[identity] = app
            db.execute('INSERT INTO visitors(id,expires) VALUES (?,?)', (identity, now + self.settings.visitor_ttl))
            self.creations.append(now)
            return cookie, identity

    def identify(self, cookie):
        if not isinstance(cookie, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', cookie):
            return None
        identity = hashlib.sha256(cookie.encode('ascii')).hexdigest()
        with self.lock, self.db() as db:
            row = db.execute('SELECT expires FROM visitors WHERE id=?', (identity,)).fetchone()
            return identity if row and row[0] > self.clock() else None

    @contextmanager
    def application(self, identity):
        with self.lock, self.db() as db:
            row = db.execute('SELECT expires FROM visitors WHERE id=?', (identity,)).fetchone()
            if not row or row[0] <= self.clock():
                raise DomainError('VISITOR_EXPIRED', 'Your demo session has expired. Reload to start a new one.')
            if identity not in self.apps:
                p = self.directory(identity)
                if not (p / 'bank.sqlite').is_file() or (p / '.visitor').read_text(encoding='ascii') != identity:
                    raise DomainError('VISITOR_EXPIRED', 'Reload to start a new demo session.')
                self.apps[identity] = self.factory(p / 'bank.sqlite', identity)
            app = self.apps[identity]
            self.refs[identity] = self.refs.get(identity, 0) + 1
        try:
            yield app
        finally:
            with self.lock:
                self.refs[identity] -= 1

    def reserve_ai_request(self, identity, audio=False):
        with self.lock, self.db() as db:
            day = int(self.clock() // 86400)
            used = db.execute('SELECT ai_requests FROM visitors WHERE id=?', (identity,)).fetchone()[0]
            total = db.execute('SELECT ai_requests FROM daily_usage WHERE day=?', (day,)).fetchone()
            if used >= self.settings.visitor_ai_requests or (total and total[0] >= self.settings.daily_ai_requests):
                raise DomainError('DEMO_QUOTA', 'The demo usage limit has been reached. Please try again later.')
            if audio and sum(p.stat().st_size for p in self.directory(identity).rglob('*') if p.is_file()) > self.settings.workspace_bytes:
                raise DomainError('DEMO_STORAGE', 'This demo session has reached its recording limit. Please continue with text.')
            db.execute('UPDATE visitors SET ai_requests=ai_requests+1 WHERE id=?', (identity,))
            db.execute('INSERT INTO daily_usage VALUES (?,1) ON CONFLICT(day) DO UPDATE SET ai_requests=ai_requests+1', (day,))

    def close(self):
        with self.lock:
            for app in self.apps.values():
                app.runtime.close()
            self.apps.clear()
            self.lease.__exit__(None, None, None)


class RequestServer:
    """A handler-local view; never mutate the server's application globally."""
    def __init__(self, server, app, identity):
        self._server, self.app, self.identity = server, app, identity

    def __getattr__(self, name):
        return getattr(self._server, name)


class PublicHandler(Handler):
    def setup(self):
        super().setup()
        self.new_cookie = None

    def end_headers(self):
        if self.new_cookie:
            secure = '; Secure' if not self.server.settings.local_preview else ''
            self.send_header('Set-Cookie', f'{COOKIE}={self.new_cookie}; Path=/; Max-Age={self.server.settings.visitor_ttl}; HttpOnly; SameSite=Lax{secure}')
        self.send_header('Permissions-Policy', 'camera=(), geolocation=(), microphone=(self)')
        super().end_headers()

    def check(self, api=False):
        settings = self.server.settings
        expected = urlsplit(settings.origin).netloc
        if len(self.headers.get_all('Host', [])) != 1 or self.headers.get('Host') != expected:
            raise DomainError('FORBIDDEN', 'Use the configured demo address.')
        origins = self.headers.get_all('Origin', [])
        if len(origins) > 1 or (origins and origins[0] != settings.origin):
            raise DomainError('FORBIDDEN', 'This request came from a different website.')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' and not (
                self.command == 'GET' and self.path == '/' and self.headers.get('Sec-Fetch-Mode') == 'navigate'):
            raise DomainError('FORBIDDEN', 'This request came from a different website.')
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
            raise DomainError('INPUT', 'Invalid request framing.')
        if api and (not isinstance(self.server, RequestServer) or not secrets.compare_digest(
                self.headers.get('X-DCTA-Token', ''), self.server.app.token)):
            raise DomainError('FORBIDDEN', 'Reload the page to reconnect to your demo session.')

    def require_webauthn_origin(self):
        self.check(True)
        return self.server.origin

    def reply(self, data, status=200, content_type='application/json; charset=utf-8'):
        if isinstance(data, bytes) and content_type.startswith('text/html') and isinstance(self.server, RequestServer):
            data = data.replace(b'__PUBLIC_WORKSPACE__', self.server.identity.encode('ascii'))
        if isinstance(data, dict) and urlsplit(self.path).path == '/api/config' and status == 200:
            data = {**data, 'demo_reset': False, 'study': {'enabled': False},
                    'webauthn_rp_id': urlsplit(self.server.origin).hostname,
                    'workspace_id': self.server.identity, 'deployment': 'public-demo'}
        super().reply(data, status, content_type)

    def dispatch(self, method):
        server = self.server
        identity = None
        try:
            path = urlsplit(self.path).path
            if method == 'GET' and path == '/healthz':
                self.reply({'status': 'ok'})
                return
            self.check(False)
            if path.startswith(BLOCKED_PREFIXES):
                self.reply({'error': 'NOT_FOUND', 'message': 'This feature is not available in the public demo.'}, 404)
                return
            cookies = SimpleCookie()
            raw_cookie = self.headers.get('Cookie', '')
            if len(raw_cookie) <= 4096:
                try:
                    cookies.load(raw_cookie)
                except CookieError:
                    pass
            identity = server.registry.identify(cookies[COOKIE].value if COOKIE in cookies else None)
            if identity is None and method == 'GET' and path == '/':
                self.new_cookie, identity = server.registry.create()
            if identity is None:
                raise DomainError('VISITOR_EXPIRED', 'Open the demo home page to begin your session.')
            with server.registry.application(identity) as app:
                self.server = RequestServer(server, app, identity)
                self.check(path.startswith('/api/'))
                if method == 'POST' and path in CLOUD_PATHS:
                    server.registry.reserve_ai_request(identity, audio=path == '/api/audio')
                super().dispatch(method)
        except DomainError as exc:
            status = 429 if exc.code in {'DEMO_CAPACITY', 'DEMO_QUOTA', 'DEMO_STORAGE'} else 403
            self.reply({'error': exc.code, 'message': str(exc)}, status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass
        except Exception:
            self.reply({'error': 'SERVER', 'message': 'The demo could not complete this request. Please reload.'}, 500)
        finally:
            self.server = server


class PublicServer(Server):
    def __init__(self, settings, registry):
        self.settings, self.registry = settings, registry
        self.origin = self.webauthn_origin = settings.origin
        self.slots = threading.BoundedSemaphore(settings.http_workers)
        payments.RP_ID = urlsplit(settings.origin).hostname
        ThreadingHTTPServer.__init__(self, (settings.bind, settings.port), PublicHandler)


class SharedSpeechCapacity:
    """Bound simultaneous ASR subprocesses across visitors on a small host."""
    def __init__(self, provider, slot):
        self.provider_impl, self.slot = provider, slot

    def __getattr__(self, name):
        return getattr(self.provider_impl, name)

    def transcribe(self, audio, vocabulary=None):
        if not self.slot.acquire(blocking=False):
            raise DomainError('ASR_RATE_LIMIT', 'Speech recognition is busy. Please try again shortly.')
        try:
            return self.provider_impl.transcribe(audio, vocabulary)
        finally:
            self.slot.release()


def application_factory(offline=False, voice=True):
    if offline:
        # No substitute validator: offline preview cannot authorize a payment.
        return lambda path, identity: Application(path)
    from .adp import ADPApplications
    from .trtc_asr import TRTCASR, TRTCCredentials
    from .trtc_tts import TRTCTTS, TTSCredentials
    applications = ADPApplications.load(require_environment=True)
    if voice:
        for name in ('TRTC_ASR_SDK_APP_ID', 'TRTC_ASR_SECRET_KEY', 'TRTC_TTS_SDK_APP_ID', 'TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY'):
            if not os.environ.get(name, '').strip():
                raise DomainError('ASR_SETUP', f'Set {name} for the public demo.')
        # Only explicitly configured environment credentials; no local key-file fallback.
        from .trtc_asr import load_credentials as asr_credentials
        from .trtc_tts import load_credentials as tts_credentials
        asr_config, tts_config = asr_credentials(), tts_credentials()
        if not TRTCASR(credentials=asr_config).available:
            raise DomainError('ASR_SETUP', 'Install the public demo speech dependencies.')

    voice_workers = int(os.environ.get('VOICE_WORKERS', '2'))
    if not 1 <= voice_workers <= 4:
        raise ValueError('VOICE_WORKERS must be between 1 and 4.')
    speech_capacity = threading.BoundedSemaphore(voice_workers)

    def create(path, identity):
        visitor_id = 'public-' + identity[:32]
        interpreter = lambda: applications.interpreter(visitor_id)
        validator = lambda: applications.checker(visitor_id)
        asr = SharedSpeechCapacity(TRTCASR(credentials=asr_config), speech_capacity) if voice else None
        tts = TRTCTTS(credentials=tts_config) if voice else None
        app = Application(path, interpreter, asr, 'adp', 'Published ADP application', validator, tts)
        app.validator_label = 'Tencent ADP · Independent Validator'
        return app
    return create


def main():
    p = argparse.ArgumentParser(description='DCTA public demonstration host')
    p.add_argument('--origin', default=os.environ.get('PUBLIC_ORIGIN', ''))
    p.add_argument('--bind', default=os.environ.get('BIND_ADDRESS', '127.0.0.1'))
    p.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8780')))
    p.add_argument('--data-dir', default=os.environ.get('DCTA_DATA_DIR', 'data'))
    p.add_argument('--local-preview', action='store_true')
    p.add_argument('--offline', action='store_true')
    p.add_argument('--no-voice', action='store_true')
    args = p.parse_args()
    settings = Settings(args.origin, Path(args.data_dir), args.bind, args.port, args.local_preview,
        visitor_ttl=int(os.environ.get('VISITOR_TTL_SECONDS', '86400')),
        max_visitors=int(os.environ.get('MAX_VISITORS', '100')),
        visitor_ai_requests=int(os.environ.get('VISITOR_AI_REQUESTS', '120')),
        daily_ai_requests=int(os.environ.get('DAILY_AI_REQUESTS', '2000')),
        http_workers=int(os.environ.get('HTTP_WORKERS', '16')))
    if args.offline and not args.local_preview:
        p.error('Offline mode is for local preview only. Public mode uses ADP and independent validation.')
    registry = VisitorRegistry(settings, application_factory(args.offline, not args.no_voice))
    server = None
    try:
        server = PublicServer(settings, registry)
        stop = lambda *_: threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        print(f'DCTA public demo: {settings.origin}', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        registry.close()


if __name__ == '__main__':
    try:
        main()
    except (DomainError, ValueError) as exc:
        print(str(exc), flush=True)
        raise SystemExit(2)
