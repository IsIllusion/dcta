"""Read-only operational tracing, separate from the authoritative SQLite audit.

No prompts, transcripts, financial values, keys, signatures or exception messages
are recorded. ContextVars isolate concurrent request traces. Logging is fail-open:
a disk failure is visible in the monitor but never changes a business decision.
GUI requests opt in. The monitor only reads these operational files.
"""
from collections import OrderedDict, deque
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

LOG_NAME = re.compile(r'runtime-\d{8}T\d{6}Z-([0-9a-f]{32})-(\d{6})\.jsonl')


def log_files(directory, boot_id=None):
    """Only owned regular log files in this exact directory, never linked targets."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        return []
    root = directory.resolve()
    result = []
    for path in directory.iterdir():
        match = LOG_NAME.fullmatch(path.name)
        if match and (boot_id is None or match[1] == boot_id) and not path.is_symlink():
            if path.is_file() and path.resolve().parent == root:
                result.append(path)
    return sorted(result)

_current = ContextVar('dcta_runtime_run', default=None)
_parent = ContextVar('dcta_runtime_span', default=None)
FIELDS = frozenset('account_id payee_id draft_id name_query proposal sources expected_version patch '
                   'source_account_id amount_minor currency fee_minor actions action_id type schema_version '
                   'version current_version content_hash state status kind code decision error result response '
                   'receipt id accounts payees candidates input_id revision session_id balance_minor '
                   'name ending active user_id created_at'.split())
STATES = frozenset('STARTED OK ERROR FAILED SUCCEEDED PENDING PROCESSING CANCELLED DRAFTED '
                   'PASS FAIL DISCREPANCY CLARIFY DRAFT INFO BALANCE ESTIMATE UNSUPPORTED '
                   'STALE BLOCKED EXPIRED READY REJECTED COLLECTING COMPLETED UNKNOWN '
                   'AUTHORIZED AUTHORIZED_FOR_EXECUTION GREETING CHAT GUIDANCE HELP RESTRICTED '
                   'NEEDS_CLARIFICATION ALREADY_COMPLETED INTERRUPTED'.split())


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def code(value):
    # Error codes are host-defined symbols, never exception messages.
    return value if type(value) is str and re.fullmatch(r'[A-Z][A-Z0-9_]{0,47}', value) else 'UNKNOWN'


def shape(value, depth=0):
    """Allowlisted field names + types/counts only. Never stringify arbitrary data."""
    if depth > 2:
        return {'type': type(value).__name__ if type(value) in (dict, list, str, int, bool) else 'value'}
    if type(value) is dict:
        return {'type': 'object', 'fields': {key: shape(item, depth + 1)
                for key, item in value.items() if key in FIELDS},
                'other_fields': sum(key not in FIELDS for key in value)}
    if type(value) is list:
        return {'type': 'array', 'count': len(value)}
    if type(value) is str:
        return {'type': 'string', 'value': '[omitted]'}
    if type(value) in (float, int):
        return {'type': 'number', 'value': '[omitted]'}
    if type(value) is bool:
        return {'type': 'boolean'}
    return {'type': 'null' if value is None else 'omitted'}


def outcome(value):
    if type(value) is not dict:
        return {}
    result = {}
    for key in ('status', 'kind', 'state', 'decision'):
        if type(value.get(key)) is str and value[key] in STATES:
            result[key] = value[key]
    if 'error' in value or 'code' in value:
        result['code'] = code(value.get('error', value.get('code')))
    if value.get('cached') is True:
        result['cached'] = True
    for key in ('response', 'receipt', 'result'):
        if type(value.get(key)) is dict:
            result.update(outcome(value[key]))
    return result


def provider_metadata(value):
    if type(value) is not dict:
        return {}
    result = {}
    for key in ('attempts', 'http_status', 'api_calls', 'max_tokens', 'choice_count'):
        if type(value.get(key)) is int:
            result[key] = value[key]
    if value.get('finish_reason') in ('stop','length','content_filter','tool_calls','insufficient_system_resource','unknown'):
        result['finish_reason'] = value['finish_reason']
    usage = value.get('usage', {})
    if type(usage) is dict:
        result['usage'] = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                           if type(usage.get(k)) is int and 0 <= usage[k] <= 100000000}
    return result


class RuntimeRecorder:
    def __init__(self, directory, capacity=4000, run_limit=200, segment_bytes=5*1024*1024, file_limit=20):
        self.directory = Path(directory).absolute()
        self.boot_id = uuid.uuid4().hex
        self.events = deque(maxlen=capacity)
        self.runs = OrderedDict()
        self.run_limit = run_limit
        self.seq = 0
        self.lock = threading.RLock()
        self.logging_error = False
        self.segment_bytes, self.file_limit = segment_bytes, file_limit
        self.prefix = 'runtime-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + self.boot_id
        self.segment, self.segment_size = 1, 0
        self.closed = False
        self.application_event('start')

    def _prune_runs(self):
        while len(self.runs) > self.run_limit:
            victim = next((k for k, v in self.runs.items() if v.status != 'RUNNING'), None)
            if victim is None:
                break  # In-flight work remains visible until its own finalization.
            del self.runs[victim]

    def _record(self, event):
        self.seq += 1
        event.update(seq=self.seq, at=stamp(), boot_id=self.boot_id)
        self.events.append(event)
        try:
            if self.directory.is_symlink():
                raise OSError('Linked log directory')
            self.directory.mkdir(parents=True, exist_ok=True)
            line = (json.dumps(event, ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')
            if self.segment_size and self.segment_size + len(line) > self.segment_bytes:
                self.segment += 1
                self.segment_size = 0
            path = self.directory / f'{self.prefix}-{self.segment:06d}.jsonl'
            if path.is_symlink() or path.resolve().parent != self.directory.resolve():
                raise OSError('Invalid log target')
            with path.open('ab') as handle:
                handle.write(line)
                handle.flush()
            first = self.segment_size == 0
            self.segment_size += len(line)
            if first:
                # Single-file removals only, exact owned names, within this database's log directory.
                owned = log_files(self.directory)
                for old in [p for p in owned if p != path][:max(0, len(owned)-self.file_limit)]:
                    if old.resolve().parent == self.directory.resolve() and not old.is_symlink():
                        old.unlink()
            return True
        except (OSError, ValueError):
            if not self.logging_error:
                try:
                    print('Runtime log write failed. Application decisions are unaffected; check log storage.', file=sys.stderr, flush=True)
                except (OSError, ValueError):
                    pass
            self.logging_error = True
            return False

    def application_event(self, phase):
        with self.lock:
            self._record({'run_id': None, 'operation': 'application', 'stage': 'application',
                          'phase': phase, 'span_id': None, 'parent_span': None, 'detail': {}})

    def close(self):
        with self.lock:
            if not self.closed:
                self.application_event('end')
                self.closed = True

    def reference(self, value):
        if type(value) is not str or not value:
            return None
        return hashlib.sha256((self.boot_id + value).encode('utf-8', 'replace')).hexdigest()[:12]

    def start(self, operation, parent_run=None):
        run = Run(self, operation, parent_run)
        with self.lock:
            self.runs[run.id] = run
            self._prune_runs()
        run.emit('run', 'start', {'parent_run': parent_run} if parent_run else {})
        return run

    def append(self, run, stage, phase, detail, span_id=None, parent=None):
        # Callers supply projected data only. Never accept HTTP bodies here.
        with self.lock:
            event = {'run_id': run.id,
                     'operation': run.operation, 'stage': stage, 'phase': phase,
                     'span_id': span_id, 'parent_span': parent, 'detail': detail}
            run.event_count += 1
            if stage == 'tool' and phase == 'start':
                run.tool_count += 1
            if not self._record(event):
                run.persisted = False

    def snapshot(self, after=0):
        with self.lock:
            oldest = self.events[0]['seq'] if self.events else 0
            events = [e for e in self.events if e['seq'] > after][:500]
            return {'boot_id': self.boot_id, 'events': events,
                    'cursor': events[-1]['seq'] if events else self.seq,
                    'gap': bool(after and oldest > after + 1),
                    'runs': [r.summary() for r in reversed(list(self.runs.values()))],
                    'log_directory': str(self.directory),
                    'logging_error': self.logging_error,
                    'privacy': 'Values, raw text, model messages, audio, credentials and signatures are omitted.',
                    'scope': 'Current server process. Use Run-Monitor.ps1 -List for retained launch logs.',
                    'retention': {'segment_bytes': self.segment_bytes, 'files': self.file_limit}}

    def download(self, run_id):
        # Bounded current-request view; full retained launch history is read by the local CLI.
        with self.lock:
            run = self.runs.get(run_id)
            if run is None or not re.fullmatch(r'[0-9a-f]{32}', run_id):
                raise FileNotFoundError()
            events = [event for event in self.events if event['run_id'] == run_id]
            if len(events) != run.event_count:
                raise FileNotFoundError()  # Never return an apparently complete, truncated request.
            return b''.join((json.dumps(event, ensure_ascii=True)+'\n').encode() for event in events)


class Run:
    def __init__(self, recorder, operation, parent_run=None):
        self.recorder, self.operation = recorder, operation
        self.id = uuid.uuid4().hex
        self.started, self.started_clock = stamp(), time.monotonic()
        self.status, self.result = 'RUNNING', {}
        self.event_count, self.tool_count, self.duration_ms = 0, 0, None
        self.persisted, self.session_ref, self.parent_run = True, None, parent_run
        self.context_token, self.parent_token = None, None

    def attach(self):
        self.context_token = _current.set(self)
        self.parent_token = _parent.set(None)
        return self

    def bind(self, session=None, key=None):
        self.session_ref = self.recorder.reference(session)
        detail = {'session_ref': self.session_ref}
        if key:
            detail['input_ref'] = self.recorder.reference(key)
        self.emit('request', 'context', detail)

    def response(self, value, http_status=200):
        self.result = {'http_status': http_status, **outcome(value)}

    def emit(self, stage, phase, detail=None, span_id=None, parent=None):
        try:
            # Bound one log, reserving room for its terminal event.
            if self.event_count < 500 or stage == 'run' and phase == 'end':
                self.recorder.append(self, stage, phase, detail or {}, span_id, parent)
        except Exception:
            self.recorder.logging_error = True
            self.persisted = False

    def finish(self):
        if self.status != 'RUNNING':
            return
        try:
            self.duration_ms = round((time.monotonic() - self.started_clock) * 1000, 2)
            bad = self.result.get('http_status', 200) >= 400 or (self.result.get('code') and self.result.get('kind') != 'CLARIFY') or any(
                self.result.get(k) in {'ERROR', 'FAILED', 'STALE', 'DISCREPANCY', 'BLOCKED'}
                for k in ('kind', 'state', 'status', 'decision'))
            self.status = 'ERROR' if bad else 'COMPLETED'
            if not self.result:
                self.status = 'INTERRUPTED'
            self.emit('run', 'end', {**self.result, 'status': self.status, 'duration_ms': self.duration_ms,
                                    'tool_calls': self.tool_count, 'events_capped': self.event_count >= 500,
                                    'logging_error': self.recorder.logging_error})
        finally:
            if self.context_token is not None:
                _current.reset(self.context_token)
                _parent.reset(self.parent_token)
                self.context_token = self.parent_token = None
            with self.recorder.lock:
                self.recorder._prune_runs()

    def summary(self):
        return {'id': self.id, 'operation': self.operation, 'started_at': self.started,
                'status': self.status, 'duration_ms': self.duration_ms,
                'tool_calls': self.tool_count, 'event_count': self.event_count,
                'session_ref': self.session_ref, 'parent_run': self.parent_run,
                'persisted': self.persisted, 'result': self.result}


def current_run():
    return _current.get()


def emit(stage, phase, detail=None):
    run = current_run()
    if run:
        run.emit(stage, phase, detail, parent=_parent.get())


def observed(stage, name=None, tool=False, provider=False):
    """Wrap only observation, never exception handling or decisions of the target."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            run = current_run()
            if run is None:
                return function(*args, **kwargs)
            span_id, parent = uuid.uuid4().hex[:12], _parent.get()
            detail = {'name': name or function.__name__}
            if tool:
                from .tools import TOOL_FIELDS
                requested = args[1] if len(args) > 1 else kwargs.get('name')
                detail['name'] = requested if type(requested) is str and requested in TOOL_FIELDS else 'unavailable_tool'
                raw = args[2] if len(args) > 2 else kwargs.get('arguments_json')
                try:
                    detail['input'] = shape(json.loads(raw)) if type(raw) is str and len(raw) <= 16000 else {'type': 'omitted'}
                except (TypeError, ValueError, RecursionError):
                    detail['input'] = {'type': 'invalid_json'}
            run.emit(stage, 'start', detail, span_id, parent)
            token, started = _parent.set(span_id), time.monotonic()
            try:
                result = function(*args, **kwargs)
                try:
                    end = {**detail, 'output': shape(result), **outcome(result)}
                    if provider:
                        end.update(provider_metadata(getattr(args[0], 'last_metadata', {})))
                    end['duration_ms'] = round((time.monotonic() - started) * 1000, 2)
                    run.emit(stage, 'end', end, span_id, parent)
                except Exception:
                    run.recorder.logging_error = True
                return result
            except BaseException as exc:
                run.emit(stage, 'error', {'name': detail['name'], 'code': code(getattr(exc, 'code', None)),
                    'duration_ms': round((time.monotonic() - started) * 1000, 2)}, span_id, parent)
                raise
            finally:
                _parent.reset(token)
        return wrapped
    return decorate
