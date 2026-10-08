"""Loopback-only GUI host. Fixed mock identity; not a multiuser deployment server."""
import argparse
from datetime import datetime, timezone
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
from urllib.parse import urlsplit, parse_qs
import webbrowser

from . import __version__
from .asr import LocalWhisper, default_model_dir
from .dialogue import Conversation, DemoInterpreter
from .domain import DomainError, canonical, strict_json
from .store import Store, bootstrap, runtime_authorizer
from .access import CONTROL_READS, GATEWAY_READS, read_allowed, columns
from .voice import VoiceService, MAX_AUDIO_BYTES
from .validation import ControlStore, ValidationService, DeepSeekValidator, ready_draft, clock_ms
from .payments import GatewayStore, PaymentService, DEMO_RESET_ENABLED
from . import study
from .purpose import PURPOSES, get_purpose, set_purpose, get_purposes, defaults_for
from . import spoken
from .stepup import StepUpService, key_path as stepup_key_path, pause_remaining, require_not_paused
from .runtime import DatabaseLease, new_demo_database
from .history import HistoryStore, recent_sessions, require_visible, rename_conversation, prepare_history_change, confirm_history_change
from .presentation import draft_changes
from .drafts import list_drafts, draft_detail, list_receipts, receipt_detail
from .observability import RuntimeRecorder, current_run, emit

STATIC = Path(__file__).with_name('web')
PAUSED_PATHS = {'/api/turn', '/api/voice/submit', '/api/validate', '/api/challenge', '/api/authorize', '/api/authorize/webauthn',
                '/api/execute', '/api/stepup/issue', '/api/stepup/verify', '/api/draft/amount-check', '/api/draft/purpose'}

DISPLAY_READS = {
    'conversation_names': columns('session_id title revision'),
    'hidden_conversations': columns('session_id'),
    'validation_results': columns('id status'),
    'authorization_challenges': CONTROL_READS['authorization_challenges'],
    'execution_requests': CONTROL_READS['execution_requests'],
    'execution_records': GATEWAY_READS['execution_records'],
}

def display_authorizer(action, table, column, database, trigger):
    if action == sqlite3.SQLITE_READ and read_allowed(DISPLAY_READS, table, column, database):
        return sqlite3.SQLITE_OK
    return runtime_authorizer(action, table, column, database, trigger)

class DisplayStore(Store):
    """Host state/receipt projection. Cannot write proofs, requests or money."""
    def __init__(self, path):
        super().__init__(path)
        self.db.set_authorizer(display_authorizer)


class Application:
    def __init__(self, db, interpreter_factory=DemoInterpreter, asr=None, provider='offline', model='offline grammar', validator_factory=None, tts=None):
        self.db = Path(db).resolve()
        self.interpreter_factory = interpreter_factory
        self.asr = asr
        self.tts = tts
        self.provider, self.model = provider, model
        self.validator_factory = validator_factory
        self.study_mode = False  # CV14: only a Run-GUI.ps1 -Study launch (fresh demo database) shows the user study
        self.token = secrets.token_urlsafe(32)
        self.runtime = RuntimeRecorder(self.db.parent / 'runtime-logs' / self.db.stem)
        self.voice_slot = threading.BoundedSemaphore(1)
        bootstrap(self.db)
        store = ControlStore(self.db)
        try:
            with store.transaction():
                # A stopped process cannot deliver a final transcript on restart.
                rows = store.db.execute("SELECT id,session_id FROM voice_inputs WHERE status='PROCESSING'").fetchall()
                for row in rows:
                    store.db.execute("UPDATE voice_inputs SET status='FAILED',error_code='ASR_INTERRUPTED' WHERE id=?", (row['id'],))
                    store.audit(row['session_id'], 'VOICE_INTERRUPTED', {'voice_id': row['id']})
                store.db.execute("UPDATE validation_results SET status='ERROR',result_json=? WHERE status='PROCESSING'",(canonical({'result':{'decision':'ERROR','code':'VALIDATOR_INTERRUPTED'}}),))
        finally:
            store.close()

    @staticmethod
    def voice_vocabulary(store, user='U1'):
        """Only this user's saved payee and account names may bias recognition."""
        try:
            return [p['name'] for p in store.saved_payees(user)] + [a['name'] for a in store.accounts(user)]
        except Exception:
            return []

    def process_audio(self, session, voice_id, parent_run=None):
        trace = self.runtime.start('speech_recognition', parent_run).attach()
        trace.bind(session)
        store = None
        try:
            store = Store(self.db)
            voice = VoiceService(store)
            row = voice.row('U1', session, voice_id)
            if row['status'] != 'PROCESSING':
                trace.response({'status': row['status']})
                return
            try:
                if self.asr is None:
                    raise DomainError('ASR_SETUP', 'Configure the selected speech recognition provider first.')
                emit('asr', 'start', {'engine': getattr(self.asr, 'provider', 'local')})
                transcript, metadata = self.asr.transcribe(row['audio'], self.voice_vocabulary(store))
                result = voice.finish('U1', session, voice_id, transcript, metadata)
                trace.response(result)
                emit('asr', 'end', {'transcript': '[omitted]'})
            except Exception as exc:
                error_code = exc.code if isinstance(exc, DomainError) else 'ASR_FAILED'
                trace.response({'error': error_code})
                emit('asr', 'error', {'code': error_code})
                voice.finish('U1', session, voice_id, error=error_code, metadata=getattr(exc, 'asr_metadata', None))
        finally:
            try:
                if store:
                    store.close()
            finally:
                self.voice_slot.release()
                trace.finish()

    def upload(self, store, session, key, mime, audio):
        voice = VoiceService(store)
        result, created = voice.start('U1', session, key, mime, audio)
        if created:
            if self.voice_slot.acquire(blocking=False):
                parent = current_run()
                threading.Thread(target=self.process_audio, args=(session, result['id'], parent.id if parent else None), daemon=True).start()
            else:
                result = voice.finish('U1', session, result['id'], error='ASR_BUSY')
        return result

    def reset_everything(self):
        """DEMO ONLY: replace the whole database with a freshly seeded one (conversations, deleted conversations,
        drafts, receipts, ledger, audio, device keys, limits, balances). A full backup copy is written first."""
        from .payments import DEMO_RESET_ENABLED
        from datetime import datetime, timezone
        import tempfile
        if not DEMO_RESET_ENABLED:
            raise DomainError('DISABLED', 'Demo reset is disabled in this build.')
        if not self.voice_slot.acquire(blocking=False):
            raise DomainError('BUSY', 'A recording is still being transcribed. Try again in a moment.')
        try:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = self.db.parent / 'backups' / f'{self.db.stem}-before-reset-{stamp}.sqlite'
            backup.parent.mkdir(parents=True, exist_ok=True)
            live = sqlite3.connect(self.db, timeout=5)
            try:
                # `with sqlite3.connect(...)` only scopes a transaction; it does NOT close the
                # connection. Leaving the backup handle open keeps the file locked, which makes
                # any later cleanup of the backups directory fail with WinError 32 on Windows.
                copy = sqlite3.connect(backup)
                try:
                    with copy:
                        live.backup(copy)
                finally:
                    copy.close()
                with tempfile.TemporaryDirectory() as temp:
                    fresh_path = Path(temp) / 'fresh.sqlite'
                    bootstrap(fresh_path)
                    fresh = sqlite3.connect(fresh_path)
                    try:
                        # SQLite's online backup overwrites every page in one locked step: schema, triggers and seed data.
                        fresh.backup(live)
                    finally:
                        fresh.close()
            finally:
                live.close()
            return {'status': 'RESET', 'mode': 'DEMO_ONLY', 'backup': backup.name,
                    'message': f'Everything was reset to a clean demo: Savings SGD 4,000.00, 3 saved payees, no conversations, drafts, receipts or deleted items. A backup copy was saved as {backup.name}.'}
        finally:
            self.voice_slot.release()

    def state(self, store, session):
        with store.read_transaction():
            return self._state(store,session)

    def _state(self,store,session):
        require_visible(store,'U1',session)
        current=store.session('U1',session)
        snapshot={'session_id':session,'revision':current['revision'],'conversation':json.loads(current['state_json']),
                  'draft':store.active('U1',session),'accounts':[],'payees':[]}
        snapshot['draft_changes'] = draft_changes(store,'U1',snapshot['draft'])
        snapshot['eligibility']=PaymentService(store).eligibility('U1',snapshot['draft'])
        named=store.db.execute('SELECT title,revision FROM conversation_names WHERE session_id=?',(session,)).fetchone()
        snapshot['custom_title']=named['title'] if named else None
        snapshot['title_version']=named['revision'] if named else 0
        rows = list(reversed(store.db.execute('SELECT i.raw_text,i.id,i.ordinal,i.created_at,r.response_json,v.id AS voice_id FROM user_inputs i LEFT JOIN input_results r ON r.input_id=i.id LEFT JOIN voice_inputs v ON v.input_id=i.id WHERE i.session_id=? ORDER BY i.ordinal DESC LIMIT 80', (session,)).fetchall()))
        snapshot['history'] = [{'text': r['raw_text'], 'voice_id': r['voice_id'], 'ordinal': r['ordinal'], 'created_at': r['created_at'], 'response': json.loads(r['response_json']) if r['response_json'] else {'kind': 'PENDING', 'message': 'No result was recorded. Check the draft before sending another request.'}} for r in rows[-80:]]
        if snapshot['draft']:
            actions=snapshot['draft']['payload']['actions']
            # Only the objects this draft already references; never a payee directory or balances.
            accounts=list(dict.fromkeys(x['source_account_id'] for x in actions));payees=list(dict.fromkeys(x['payee_id'] for x in actions))
            snapshot['accounts'] = [dict(store.db.execute('SELECT id,name FROM accounts WHERE user_id=? AND id=?', ('U1',x)).fetchone()) for x in accounts]
            snapshot['payees'] = [dict(store.db.execute('SELECT id,name,ending FROM payees WHERE user_id=? AND id=?', ('U1',x)).fetchone()) for x in payees]
        snapshot['voices'] = [VoiceService(store).view('U1', session, r[0]) for r in store.db.execute('SELECT id FROM voice_inputs WHERE session_id=? ORDER BY created_at DESC LIMIT 20', (session,))]
        snapshot['audit_count'] = store.db.execute('SELECT COUNT(*) FROM audit_events WHERE session_id=?',(session,)).fetchone()[0]
        snapshot['state_sequence'] = store.db.execute("SELECT COALESCE(MAX(a.id),0) FROM audit_events a JOIN sessions s ON s.id=a.session_id WHERE s.user_id='U1'").fetchone()[0]
        snapshot['voice_epoch']=store.db.execute('SELECT COALESCE(MAX(rowid),0) FROM voice_inputs WHERE session_id=?',(session,)).fetchone()[0]
        try:
            ready_draft(store,'U1',session)
            snapshot['review_gate']={'ready':True}
        except DomainError as exc:
            snapshot['review_gate']={'ready':False,'code':exc.code,'message':str(exc)}
        snapshot['recoverable_requests']=PaymentService(store).recoverable('U1',session)
        receipt = store.db.execute('SELECT receipt_json FROM execution_records WHERE session_id=? ORDER BY created_ms DESC,rowid DESC LIMIT 1',(session,)).fetchone()
        snapshot['receipt'] = json.loads(receipt[0]) if receipt else None
        # Where each card belongs in the conversation: the ordinal of the last message sent before it happened.
        anchor = lambda moment: store.db.execute('SELECT MAX(ordinal) FROM user_inputs WHERE session_id=? AND created_at<=?', (session, moment)).fetchone()[0]
        executed = store.db.execute('SELECT created_ms FROM execution_records WHERE session_id=? ORDER BY created_ms DESC,rowid DESC LIMIT 1', (session,)).fetchone()
        snapshot['receipt_anchor'] = anchor(datetime.fromtimestamp(executed[0] / 1000, timezone.utc).isoformat()) if executed else None
        version = store.db.execute('SELECT created_at FROM draft_versions WHERE draft_id=? AND version=? LIMIT 1',
                                   (snapshot['draft']['payload']['draft_id'], snapshot['draft']['current_version'])).fetchone() if snapshot['draft'] else None
        snapshot['draft_anchor'] = anchor(version[0]) if version else None
        snapshot['draft_created_at'] = version[0] if version else None
        snapshot['amount_checks'] = spoken.pending_checks(store, session, snapshot['draft'])
        snapshot['security_pause_ms'] = pause_remaining(store, 'U1', clock_ms())
        snapshot['purpose'] = get_purpose(store, snapshot['draft']['payload']['draft_id']) if snapshot['draft'] else None
        snapshot['receipt_purpose'] = get_purpose(store, snapshot['receipt'].get('draft_id')) if snapshot['receipt'] else None
        # CV14: one purpose per action; defaults follow the destination (DBS shares -> Investment, bills -> Bills & utilities).
        if snapshot['draft']:
            payees = [a['payee_id'] for a in snapshot['draft']['payload']['actions']]
            snapshot['purposes'] = get_purposes(store, snapshot['draft']['payload']['draft_id'], len(payees))
            snapshot['purpose_defaults'] = defaults_for(store, 'U1', payees)
            kinds = store.profiles('U1')
            snapshot['kinds'] = [(kinds.get(p) or {}).get('kind', 'PERSON') for p in payees]
        if snapshot['receipt']:
            r = snapshot['receipt']
            payees = [a['payee']['id'] for a in r['actions']] if r.get('actions') else [r['payee']['id']]
            snapshot['receipt_purposes'] = get_purposes(store, r.get('draft_id'), len(payees))
            snapshot['receipt_purpose_defaults'] = defaults_for(store, 'U1', payees)
            kinds = store.profiles('U1')
            snapshot['receipt_kinds'] = [(kinds.get(p) or {}).get('kind', 'PERSON') for p in payees]
        return snapshot


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, app):
        if address[0] != '127.0.0.1':
            raise ValueError('GUI must bind only to 127.0.0.1.')
        self.app = app
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        # WebAuthn needs a domain RP ID; 'localhost' reaches the same loopback-only listener.
        self.webauthn_origin = f'http://localhost:{self.server_port}'

    def handle_error(self, request, client_address):
        """A client that went away is not a server fault.

        Closing a browser tab or navigating mid-request drops the socket, which surfaces as
        WinError 10053/10054 while http.server is still reading the request line - before our
        handler runs. Printing a traceback for that only adds noise to the console and the
        evidence logs, so those teardown errors are swallowed; real faults still surface.
        """
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)
        self._body_consumed = False

    def drain(self):
        """Read an unread request body before replying. A rejected POST (e.g. 403 before the body is parsed)
        otherwise leaves bytes in the socket; Windows then resets the connection (WinError 10053/10054)
        and the client never sees the refusal."""
        if self._body_consumed or self.command != 'POST':
            return
        self._body_consumed = True
        length = self.headers.get('Content-Length', '')
        if length.isdecimal() and 0 < int(length) <= MAX_AUDIO_BYTES + 65536:
            try:
                remaining = int(length)
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass
        else:
            self.close_connection = True

    def log_message(self, *args):
        pass  # Never log requests, transcripts, tokens or credential material.

    def reply(self, data, status=200, content_type='application/json; charset=utf-8'):
        trace = current_run()
        if trace:
            trace.response(data, status)
        if not isinstance(data, bytes):
            data = canonical(data).encode('utf-8')
        self.drain()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def check(self, api=False):
        host = self.headers.get('Host')
        if host not in (self.server.origin.removeprefix('http://'), self.server.webauthn_origin.removeprefix('http://')):
            raise DomainError('FORBIDDEN', 'Invalid local host.')
        origin = self.headers.get('Origin')
        if origin is not None and origin != 'http://' + host:
            raise DomainError('FORBIDDEN', 'Cross-origin requests are not allowed.')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise DomainError('FORBIDDEN', 'Cross-site requests are not allowed.')
        if api and not secrets.compare_digest(self.headers.get('X-DCTA-Token', ''), self.server.app.token):
            raise DomainError('FORBIDDEN', 'Reload the local app to reconnect.')

    def require_webauthn_origin(self):
        if self.headers.get('Host') != self.server.webauthn_origin.removeprefix('http://'):
            raise DomainError('WEBAUTHN_ORIGIN', 'Open the app on ' + self.server.webauthn_origin + ' to use device biometrics.')
        return self.server.webauthn_origin

    def body(self, limit):
        if self.headers.get('Transfer-Encoding'):
            raise DomainError('INPUT', 'Chunked requests are not accepted.')
        length = self.headers.get('Content-Length', '')
        if not length.isdecimal() or not 0 < int(length) <= limit:
            raise DomainError('INPUT', 'Request body is missing or too large.')
        data = self.rfile.read(int(length))
        self._body_consumed = True
        if len(data) != int(length):
            raise DomainError('INPUT', 'Incomplete request.')
        return data

    def dispatch(self, method):
        store = None
        trace = None
        try:
            url = urlsplit(self.path)
            api = url.path.startswith('/api/')
            self.check(api)
            if method == 'GET' and not api:
                assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8'),
                          # CV14 A: the simulated security token (a separate window, or a phone in the hosted demo).
                          '/token': ('token.html', 'text/html; charset=utf-8'), '/token.js': ('token.js', 'text/javascript; charset=utf-8')}
                # Exact bundled asset names only; never map arbitrary URL paths to disk.
                for icon in ('plus', 'microphone', 'arrow-up', 'stop', 'logo', 'cat-transfer', 'cat-family', 'cat-home', 'cat-key', 'cat-basket', 'cat-bill',
                             'cat-education', 'cat-medical', 'cat-travel', 'cat-investment', 'cat-salary', 'cat-other', 'cat-own'):
                    assets[f'/icons/{icon}.svg'] = (f'icons/{icon}.svg', 'image/svg+xml')
                if url.path not in assets:
                    self.reply({'error': 'NOT_FOUND'}, 404)
                    return
                name, content_type = assets[url.path]
                data = (STATIC / name).read_bytes().replace(b'__LOCAL_TOKEN__', self.server.app.token.encode())
                self.reply(data, content_type=content_type)
                return
            if not api:
                raise DomainError('NOT_FOUND', 'Endpoint unavailable.')
            app = self.server.app
            if method == 'GET' and url.path in {'/api/runtime', '/api/runtime/log'}:
                query = parse_qs(url.query, keep_blank_values=True)
                if any(len(v) != 1 for v in query.values()):
                    raise DomainError('INPUT', 'Repeated runtime query fields.')
                if url.path == '/api/runtime':
                    value = query.get('after', ['0'])[0]
                    if set(query) - {'after'} or not value.isascii() or not value.isdecimal() or len(value) > 12:
                        raise DomainError('INPUT', 'Use a bounded runtime cursor.')
                    self.reply(app.runtime.snapshot(int(value)))
                else:
                    if set(query) != {'id'}:
                        raise DomainError('INPUT', 'One runtime identifier is required.')
                    try:
                        self.reply(app.runtime.download(query['id'][0]), content_type='application/x-ndjson; charset=utf-8')
                    except (OSError, ValueError):
                        raise DomainError('NOT_FOUND', 'Runtime log is not available in this process.') from None
                return
            operations = {
                '/api/session': 'new_session', '/api/turn': 'agent_turn', '/api/audio': 'audio_upload',
                '/api/voice/submit': 'voice_turn', '/api/voice/cancel': 'voice_cancel',
                '/api/validate': 'independent_validation', '/api/challenge': 'prepare_authorization',
                '/api/authorize': 'verify_authorization', '/api/execute': 'execute_mock_transfer',
                '/api/review/cancel': 'cancel_review', '/api/demo-key': 'register_demo_key',
                '/api/session/rename': 'rename_conversation', '/api/session/delete': 'hide_conversation',
                '/api/session/restore': 'restore_conversation', '/api/session/delete/prepare': 'prepare_hide',
                '/api/session/restore/prepare': 'prepare_restore', '/api/execution': 'check_original_result',
                '/api/webauthn/register/begin': 'begin_device_key', '/api/webauthn/register/finish': 'register_device_key',
                '/api/webauthn/known': 'check_device_key', '/api/authorize/webauthn': 'verify_device_biometric',
                '/api/demo/reset-all': 'demo_reset_everything', '/api/stepup/issue': 'issue_security_check',
                '/api/stepup/verify': 'verify_security_check', '/api/speech': 'speech_synthesis'}
            if url.path in operations and (method == 'POST' or url.path == '/api/execution'):
                trace = app.runtime.start(operations[url.path]).attach()
            control_paths={'/api/stepup/issue','/api/stepup/verify','/api/token','/api/demo-key','/api/validate','/api/challenge','/api/authorize','/api/review/cancel',
                           '/api/webauthn/register/begin','/api/webauthn/register/finish','/api/webauthn/known','/api/authorize/webauthn'}
            history_actions={'/api/session/delete':'HIDE','/api/session/restore':'RESTORE'}
            history_prepares={path+'/prepare':action for path,action in history_actions.items()}
            history_paths={'/api/sessions','/api/sessions/deleted','/api/session/rename'} | history_actions.keys() | history_prepares.keys()
            store = GatewayStore(app.db) if url.path=='/api/execute' else ControlStore(app.db) if url.path in control_paths else HistoryStore(app.db) if url.path in history_paths else DisplayStore(app.db) if url.path in {'/api/state','/api/turn','/api/voice/submit','/api/execution','/api/drafts','/api/draft','/api/receipts','/api/receipt'} else Store(app.db)
            query = parse_qs(url.query, keep_blank_values=True)
            get = lambda name: query.get(name, [''])[0]
            if trace and 'session' in query:
                trace.bind(get('session'))
            if method == 'GET' and url.path == '/api/config':
                from importlib.util import find_spec
                self.reply({'version': __version__, 'provider': app.provider, 'model': app.model, 'asr': getattr(app.asr, 'label', 'Speech recognition'), 'asr_provider': getattr(app.asr, 'provider', 'local'), 'asr_processing_message': getattr(app.asr, 'processing_message', 'Recognizing audio…'), 'asr_ready': bool(app.asr and app.asr.available), 'tts_provider': getattr(app.tts, 'provider', 'browser'), 'tts': getattr(app.tts, 'label', 'Browser voice · on device'), 'language': 'English', 'max_seconds': 30,'payments_ready':bool(app.validator_factory and find_spec('cryptography')),'validator':getattr(app,'validator_label',None),'demo_reset':DEMO_RESET_ENABLED,'purposes':list(PURPOSES),'study':{'enabled':True,'tasks':list(study.TASKS),'database':study.display_path(app.db)} if study.STUDY_ENABLED and app.study_mode else {'enabled':False}})
            elif method == 'GET' and url.path in ('/api/sessions','/api/sessions/deleted'):
                if set(query) - {'q'} or any(len(values) != 1 for values in query.values()):
                    raise DomainError('INPUT', 'Unknown or repeated search fields.')
                self.reply(recent_sessions(store, 'U1', get('q'),removed=url.path.endswith('/deleted')))
            elif method == 'GET' and url.path == '/api/state':
                self.reply(app.state(store, get('session')))
            elif method == 'GET' and url.path in ('/api/drafts','/api/receipts'):
                allowed={'q','offset','status'} if url.path=='/api/drafts' else {'q','offset'}
                if set(query)-allowed or any(len(values)!=1 for values in query.values()):
                    raise DomainError('INPUT','Unknown or repeated draft search fields.')
                offset=get('offset') if 'offset' in query else '0'
                if not offset.isascii() or not offset.isdecimal() or len(offset)>7:
                    raise DomainError('INPUT','Use a bounded page offset.')
                self.reply(list_drafts(store,'U1',get('q'),get('status') or 'DRAFTED',offset=int(offset)) if url.path=='/api/drafts' else list_receipts(store,'U1',get('q'),offset=int(offset)))
            elif method == 'GET' and url.path == '/api/receipt':
                if set(query)!={'id'} or len(query['id'])!=1:
                    raise DomainError('INPUT','One receipt identifier is required.')
                self.reply(receipt_detail(store,'U1',get('id')))
            elif method == 'GET' and url.path == '/api/draft':
                if set(query)-{'id','version','compare'} or any(len(values)!=1 for values in query.values()) or 'id' not in query:
                    raise DomainError('INPUT','Unknown or repeated draft detail fields.')
                def number(name):
                    if name not in query:return None
                    value=get(name)
                    if not value.isascii() or not value.isdecimal() or len(value)>10:
                        raise DomainError('INPUT','Use a positive saved version number.')
                    return int(value)
                self.reply(draft_detail(store,'U1',get('id'),number('version'),number('compare')))
            elif method == 'GET' and url.path == '/api/token':
                if url.query:
                    raise DomainError('INPUT', 'The token page takes no query.')
                self.reply(StepUpService(store, stepup_key_path(app.db), clock_ms).token('U1'))
            elif method == 'GET' and url.path == '/api/execution':
                self.reply(PaymentService(store).status('U1',get('session'),get('key')))
            elif method == 'GET' and url.path == '/api/voice':
                self.reply(VoiceService(store).view('U1', get('session'), get('id')))
            elif method == 'GET' and url.path == '/api/audio':
                row = VoiceService(store).row('U1', get('session'), get('id'))
                self.reply(row['audio'], content_type=row['mime'])
            elif method == 'POST' and url.path == '/api/audio':
                self.reply(app.upload(store, get('session'), get('key'), self.headers.get('Content-Type', ''), self.body(MAX_AUDIO_BYTES)))
            elif method == 'POST':
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise DomainError('INPUT', 'JSON body required.')
                data = strict_json(self.body(16000).decode('utf-8'))
                if type(data) is not dict:
                    raise DomainError('INPUT', 'JSON object required.')
                session = data.get('session')
                if url.path in PAUSED_PATHS:
                    # CV14 A: after three wrong security codes, every action that can lead to money moving waits.
                    require_not_paused(store, 'U1', clock_ms())
                if trace:
                    trace.bind(session, data.get('key') if url.path == '/api/turn' else None)
                if url.path == '/api/session' and not data:
                    self.reply({'session_id': store.new_session('U1')})
                elif url.path == '/api/speech' and set(data) == {'session', 'text'} and not query:
                    require_visible(store, 'U1', session)
                    if app.tts is None:
                        raise DomainError('TTS_SETUP', 'Tencent speech synthesis is not enabled for this launch.')
                    try:
                        audio, metadata = app.tts.synthesize(data['text'])
                    except DomainError as exc:
                        emit('tts', 'error', {'code': exc.code, **getattr(exc, 'tts_metadata', {})})
                        raise
                    emit('tts', 'end', metadata)
                    self.reply(audio, content_type='audio/mpeg')
                elif url.path in history_prepares and set(data)=={'session'} and not query:
                    self.reply(prepare_history_change(store,'U1',session,history_prepares[url.path]))
                elif url.path in history_actions and set(data)=={'session','challenge_id'} and not query:
                    self.reply(confirm_history_change(store,'U1',session,history_actions[url.path],data['challenge_id']))
                elif url.path == '/api/session/rename' and set(data) == {'session','title','expected_version'} and not query:
                    self.reply(rename_conversation(store,'U1',session,data['title'],data['expected_version']))
                elif url.path == '/api/turn' and set(data) == {'session', 'text', 'key'}:
                    require_visible(store,'U1',session)
                    result = Conversation(store, app.interpreter_factory()).turn('U1', session, data['text'], data['key'])
                    if result.get('kind')=='DRAFT':
                        with store.transaction():
                            draft=store.active('U1',session)
                            # A concurrent correction must not be attributed to this older turn.
                            if draft and result.get('draft',{}).get('content_hash')==draft['content_hash']:
                                assessment=PaymentService(store).eligibility('U1',draft)
                                if assessment and assessment['blocked']:PaymentService(store).record_block('U1',draft,assessment['code'],'PREPARATION')
                    self.reply({'response': result, 'state': app.state(store, session)})
                elif url.path == '/api/voice/cancel' and set(data) == {'session', 'id'}:
                    self.reply(VoiceService(store).cancel('U1', session, data['id']))
                elif url.path == '/api/voice/submit' and set(data) == {'session', 'id'}:
                    result = VoiceService(store).submit('U1', session, data['id'], Conversation(store, app.interpreter_factory()))
                    if result.get('kind')=='DRAFT':
                        with store.transaction():
                            draft=store.active('U1',session)
                            # A concurrent correction must not be attributed to this older turn.
                            if draft and result.get('draft',{}).get('content_hash')==draft['content_hash']:
                                assessment=PaymentService(store).eligibility('U1',draft)
                                if assessment and assessment['blocked']:PaymentService(store).record_block('U1',draft,assessment['code'],'PREPARATION')
                    self.reply({'response': result, 'state': app.state(store, session)})
                elif url.path == '/api/demo-key' and set(data)=={'spki'}:
                    self.reply(PaymentService(store).register_key('U1',data['spki']))
                elif url.path == '/api/validate' and set(data)=={'session','draft_id','version','content_hash'}:
                    validator=app.validator_factory() if app.validator_factory else None
                    self.reply(ValidationService(store,validator).run('U1',session,data['draft_id'],data['version'],data['content_hash']))
                elif url.path == '/api/challenge' and set(data)=={'session','validation_id','key_id'}:
                    self.reply(PaymentService(store).challenge('U1',session,data['validation_id'],data['key_id']))
                elif url.path == '/api/authorize' and set(data)=={'session','challenge_id','key_id','signature'}:
                    self.reply(PaymentService(store).authorize('U1',session,data['challenge_id'],data['key_id'],data['signature']))
                elif url.path == '/api/execute' and set(data)=={'session','challenge_id','key'}:
                    self.reply({'receipt':PaymentService(store).execute('U1',session,data['challenge_id'],data['key'])})
                elif url.path == '/api/webauthn/register/begin' and set(data)=={'purpose'} and data['purpose']=='authorization':
                    self.require_webauthn_origin()
                    self.reply(PaymentService(store).begin_webauthn_registration('U1'))
                elif url.path == '/api/webauthn/register/finish' and set(data)=={'registration','credential_id','spki','client_data','authenticator_data'}:
                    self.reply(PaymentService(store).finish_webauthn_registration('U1',data['registration'],data['credential_id'],data['spki'],
                                                                                  data['client_data'],data['authenticator_data'],self.require_webauthn_origin()))
                elif url.path == '/api/demo/reset-all' and set(data)=={'confirm'} and data['confirm']=='RESET_EVERYTHING':
                    # DEMO ONLY: remove with DEMO_RESET_ENABLED for a formal release.
                    store.close();store=None
                    self.reply(app.reset_everything())
                elif url.path == '/api/stepup/issue' and set(data) == {'session','challenge_id'} and not query:
                    self.reply(StepUpService(store, stepup_key_path(app.db), clock_ms).issue('U1', session, data['challenge_id'], PaymentService(store)))
                elif url.path == '/api/stepup/verify' and set(data) == {'session','stepup_id','code'} and not query:
                    self.reply(StepUpService(store, stepup_key_path(app.db), clock_ms).verify('U1', session, data['stepup_id'], data['code'], PaymentService(store)))
                elif url.path == '/api/draft/amount-check' and set(data) == {'session','draft_id','version','action'} and not query:
                    self.reply(spoken.confirm(store, 'U1', session, data['draft_id'], data['version'], data['action']))
                elif url.path == '/api/draft/purpose' and set(data) in ({'session','draft_id','category','note'}, {'session','draft_id','category','note','action'}) and not query:
                    self.reply({'purpose': set_purpose(store, 'U1', session, data['draft_id'], data['category'], data['note'], data.get('action', 0)), 'action': data.get('action', 0)})
                elif url.path == '/api/study/event' and set(data) in ({'session','participant','task','event'},{'session','participant','task','event','keys','pointer'}) and not query:
                    # EVALUATION ONLY: timestamps + counters for the user study; never touches drafts or the ledger.
                    if not app.study_mode:
                        raise DomainError('DISABLED', 'Start the app with Run-GUI.ps1 -Study to run the user study.')
                    result = study.record(store, app.db, data)
                    if data.get('event') == 'start' and DEMO_RESET_ENABLED:
                        # Every task starts from the same balances and limits, so one task's spending
                        # (e.g. SGD 900) cannot block a later task through the daily limit.
                        gateway = GatewayStore(app.db)
                        try:
                            PaymentService(gateway).reset_demo('U1')
                        finally:
                            gateway.close()
                        result['fresh_limits'] = True
                    self.reply(result)
                elif url.path == '/api/webauthn/known' and set(data)=={'key_id'}:
                    self.reply(PaymentService(store).webauthn_known('U1',data['key_id']))
                elif url.path == '/api/authorize/webauthn' and set(data)=={'session','challenge_id','key_id','assertion'}:
                    self.reply(PaymentService(store).authorize_webauthn('U1',session,data['challenge_id'],data['key_id'],data['assertion']))
                elif url.path == '/api/review/cancel' and set(data)=={'session','challenge_id'}:
                    self.reply(PaymentService(store).cancel_review('U1',session,data['challenge_id']))
                else:
                    raise DomainError('INPUT', 'Unknown endpoint or fields.')
            else:
                self.reply({'error': 'NOT_FOUND'}, 404)
        except DomainError as exc:
            self.reply({'error': exc.code, 'message': str(exc)}, 403 if exc.code == 'FORBIDDEN' else 400)
        except (ValueError, UnicodeError, TypeError):
            self.reply({'error': 'INPUT', 'message': 'Invalid request.'}, 400)
        # ConnectionAbortedError is Windows WinError 10053: the client dropped the socket
        # (browser tab closed, navigation, or a refused request whose body was never read).
        # It must not fall through to the generic handler, which would try to reply on a dead socket.
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            if trace:
                trace.response({'error': 'RESPONSE_INTERRUPTED'}, 499)
        except Exception:
            self.reply({'error': 'SERVER', 'message': 'Request failed. Reload to inspect saved state before retrying.'}, 500)
        finally:
            try:
                if store:
                    store.close()
            finally:
                if trace:
                    trace.finish()

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')


def main():
    parser = argparse.ArgumentParser(description='DCTA local mock draft GUI')
    parser.add_argument('--db', default='work/deepseek.sqlite')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--provider', choices=('offline', 'deepseek', 'adp'), default='deepseek')
    parser.add_argument('--key-file')
    parser.add_argument('--adp-key-file', help='ADP AppKey file (default: ADP_APPKEY / ADP_KEY_FILE / project adp.key).')
    parser.add_argument('--model', default='deepseek-flash')
    parser.add_argument('--validator-provider', choices=('same', 'hunyuan'), default='same',
                        help='Validator: same uses a separate direct DeepSeek call (also in ADP mode); hunyuan uses Tencent Hunyuan.')
    parser.add_argument('--validator-model', default='hunyuan-turbos-latest')
    parser.add_argument('--validator-key-file', help='File with exactly one Hunyuan API key (or set HUNYUAN_API_KEY).')
    parser.add_argument('--asr-model', default=None, help='Whisper model folder (default: small.en if installed, else base.en)')
    parser.add_argument('--asr-provider', choices=('local', 'trtc'), default='local')
    parser.add_argument('--tts-provider', choices=('browser', 'trtc'), default='browser')
    parser.add_argument('--trtc-key-file', help='File containing SDKAppID and SDKSecretKey; defaults to ../keys/Tencent API.txt.')
    parser.add_argument('--trtc-site', choices=('intl', 'cn'), default='intl')
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--monitor', action='store_true', help='Open a separate read-only runtime terminal on Windows.')
    parser.add_argument('--new-demo', action='store_true', help='Create a fresh isolated demo database; never reset the working database.')
    parser.add_argument('--study', action='store_true', help='User-study mode (evaluation only): implies --new-demo and shows the study panel.')
    args = parser.parse_args()
    if args.study:
        args.new_demo = True  # every study session starts from a clean, isolated demo database
    if args.new_demo:
        if args.db!='work/deepseek.sqlite':parser.error('--new-demo cannot be combined with --db')
        args.db=str(new_demo_database(Path(__file__).resolve().parents[1]))
        print(f'Fresh isolated demo database: {args.db}',flush=True)
    factory = DemoInterpreter
    validator_factory = None
    if args.provider in ('deepseek', 'adp'):
        from .deepseek import DeepSeekCompletion, DeepSeekConfig
        from .structured import StructuredInterpreter
        # Read/validate once; each request has its own metadata and interpreter.
        from .deepseek import load_api_key
        key = load_api_key(args.key_file)
        factory = lambda: StructuredInterpreter(DeepSeekCompletion(DeepSeekConfig(model=args.model), api_key=key))
        if args.provider == 'adp':
            from .adp import ADPCompletion, load_adp_key
            adp_key = load_adp_key(args.adp_key_file)
            # This host has one mock user. Scope its opaque visitor ID to this boot;
            # each ADP call still uses a fresh conversation with host-owned evidence.
            visitor_id = 'dcta-' + secrets.token_hex(16)
            factory = lambda: StructuredInterpreter(ADPCompletion(api_key=adp_key, visitor_id=visitor_id,
                router=DeepSeekCompletion(DeepSeekConfig(model=args.model), api_key=key)))
        validator_factory = lambda: DeepSeekValidator(DeepSeekCompletion(DeepSeekConfig(model=args.model),api_key=key))
        if args.validator_provider == 'hunyuan':
            from .hunyuan import HunyuanCompletion, HunyuanConfig, load_hunyuan_key
            validator_key = load_hunyuan_key(args.validator_key_file)
            validator_config = HunyuanConfig(model=args.validator_model)
            # Independent engine: a different provider and model family from the Business Agent.
            validator_factory = lambda: DeepSeekValidator(HunyuanCompletion(validator_config, api_key=validator_key))
    if args.asr_model and not Path(args.asr_model).exists():
        # Launched from another working directory: fall back to the project-root copy so
        # voice is not silently unavailable because of a relative model path.
        fallback = Path(__file__).resolve().parents[1] / args.asr_model
        if fallback.exists():
            args.asr_model = str(fallback)
    if args.asr_provider == 'trtc':
        from .trtc_asr import TRTCASR
        asr = TRTCASR(key_file=args.trtc_key_file, site=args.trtc_site)
        if not asr.available:
            raise DomainError('ASR_SETUP', 'Install requirements-trtc.txt in the project virtual environment.')
    else:
        asr = LocalWhisper(args.asr_model or default_model_dir())
    tts = None
    if args.tts_provider == 'trtc':
        from .trtc_tts import TRTCTTS
        tts = TRTCTTS(key_file=args.trtc_key_file, site=args.trtc_site)
    with DatabaseLease(args.db):
        model_label = 'Published ADP application' if args.provider == 'adp' else args.model if args.provider == 'deepseek' else 'offline grammar'
        app = Application(args.db, factory, asr, args.provider, model_label,validator_factory,tts=tts)
        app.study_mode = bool(args.study)
        app.validator_label = (f'Tencent Hunyuan · {args.validator_model} (independent provider)' if args.validator_provider == 'hunyuan'
                               else f'DeepSeek · {args.model} (separate call)') if validator_factory else None
        try:
            server = Server(('127.0.0.1', args.port), app)
        except OSError:
            app.runtime.close()
            raise DomainError('PORT_UNAVAILABLE',f'Local port {args.port} is unavailable. Open the running app or choose another port.') from None
        print(f'DCTA {__version__} | MOCK TRANSFERS ONLY\n{server.webauthn_origin}  (device biometrics: Windows Hello / Touch ID)\n{server.origin}  (demo key fallback)\nCtrl+C stops the local server.', flush=True)
        print(f'Speech recognition: {asr.label}', flush=True)
        if args.open:webbrowser.open(server.webauthn_origin)
        if args.monitor:
            from .monitor import launch_monitor
            if not launch_monitor(app.runtime.directory, app.runtime.boot_id):
                print('Runtime terminal unavailable. Use Run-Monitor.ps1 to read the saved logs.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            app.runtime.close()


if __name__ == '__main__':
    try:main()
    except DomainError as exc:
        print(f'{exc.code}: {exc}',flush=True)
        raise SystemExit(2)
