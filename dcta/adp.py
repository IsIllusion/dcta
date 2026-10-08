"""Published Tencent International ADP intent agent; final SSE replies only.

Each request gets a fresh remote conversation. DCTA supplies its own complete,
transaction-scoped evidence, so ADP history cannot resurrect a settled payment.
The public router and validator use separate calls and contracts.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
import uuid

from .deepseek import DeepSeekCompletion, NoRedirect
from .domain import DomainError, canonical
from .observability import observed
from .structured import PLAN_SCHEMA, ROUTE_SCHEMA


ADP_ENDPOINT = 'https://wss.lke.tencentcloud.com/adp/v2/chat'
MAX_EVENT_BYTES = 512 * 1024
MAX_STREAM_BYTES = 4 * 1024 * 1024


def load_adp_key(key_file=None):
    if key_file is None:
        value = os.environ.get('ADP_APPKEY', '').strip()
        if value:
            return _check_key(value)
        key_file = os.environ.get('ADP_KEY_FILE') or Path(__file__).resolve().parents[1] / 'adp.key'
    try:
        path = Path(key_file)
        if path.stat().st_size > 4096:
            raise DomainError('PROVIDER_CONFIG', 'ADP credential file is too large.')
        value = path.read_text(encoding='utf-8-sig').strip()
    except (OSError, UnicodeError):
        raise DomainError('PROVIDER_CONFIG', 'Save the ADP AppKey in adp.key or set ADP_APPKEY.') from None
    return _check_key(value)


def _check_key(value):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{16,512}', value):
        raise DomainError('PROVIDER_CONFIG', 'The ADP credential must contain exactly one AppKey, with no label or quotes.')
    return value


@dataclass(frozen=True)
class ADPConfig:
    endpoint: str = ADP_ENDPOINT
    timeout_seconds: float = 60.0
    total_seconds: float = 60.0

    def __post_init__(self):
        if self.endpoint != ADP_ENDPOINT:
            raise DomainError('PROVIDER_CONFIG', 'Only the official Tencent International ADP HTTPS endpoint is permitted.')
        if not 0 < self.timeout_seconds <= self.total_seconds <= 120:
            raise DomainError('PROVIDER_CONFIG', 'ADP request limits are invalid.')


def _events(response, deadline):
    """Bound both complete events and the entire stream; discard partial EOF."""
    data, event, event_size, total = [], '', 0, 0
    while True:
        if time.monotonic() >= deadline:
            raise DomainError('PROVIDER_TIMEOUT', 'ADP response timed out.')
        line = response.readline(MAX_EVENT_BYTES + 1)
        if not line:
            return
        total += len(line)
        event_size += len(line)
        if event_size > MAX_EVENT_BYTES or total > MAX_STREAM_BYTES:
            raise DomainError('PROVIDER_RESPONSE_SIZE', 'ADP response exceeded the allowed size.')
        line = line.rstrip(b'\r\n')
        if not line:
            if data:
                raw = b'\n'.join(data)
                if raw == b'[DONE]':
                    return
                obj = json.loads(raw.decode('utf-8'))
                if type(obj) is not dict or type(obj.get('Type')) is not str:
                    raise ValueError('Invalid SSE event')
                if event and event != obj['Type']:
                    raise ValueError('Mismatched SSE event')
                yield obj
            data, event, event_size = [], '', 0
        elif line.startswith(b'data:'):
            data.append(line[5:].lstrip(b' '))
        elif line.startswith(b'event:'):
            event = line[6:].strip().decode('utf-8').strip('"')


def _final_reply(record, conversation_id):
    if type(record) is not dict or record.get('Role') != 'assistant':
        raise ValueError('Invalid completed response')
    if record.get('Status') != 'success':
        raise DomainError('PROVIDER_INCOMPLETE', 'ADP did not complete the response successfully.')
    if record.get('ConversationId') != conversation_id:
        raise ValueError('Mismatched remote conversation')
    messages = record.get('Messages')
    if type(messages) is not list or any(type(m) is not dict for m in messages):
        raise ValueError('Invalid messages')
    if any(m.get('Type') == 'tool_call' for m in messages):
        raise DomainError('PROVIDER_UNEXPECTED_TOOL', 'The ADP intent agent must not call tools.')
    replies = [m for m in messages if m.get('Type') == 'reply']
    if len(replies) != 1 or replies[0].get('Status') != 'success':
        raise ValueError('Expected one successful final reply')
    contents = replies[0].get('Contents')
    if not isinstance(contents, list) or not contents or any(
        type(c) is not dict or c.get('Type') != 'text' or type(c.get('Text')) is not str for c in contents
    ):
        raise ValueError('Expected text-only reply')
    text = ''.join(c['Text'] for c in contents)
    if not text.strip() or len(text) > 16000:
        raise DomainError('PROVIDER_EMPTY_OR_OVERSIZE', 'ADP returned an empty or oversized reply.')
    return text


class ADPCompletion(DeepSeekCompletion):
    """Reuse bounded workers and sanitized errors, with ADP-specific transport.

Only PLAN_SCHEMA goes to the published app. ROUTE_SCHEMA is delegated explicitly;
validator schemas are refused, even if accidentally supplied by a future caller.
"""
    label = 'Tencent ADP'

    def __init__(self, config=None, *, key_file=None, api_key=None, router=None, visitor_id=None):
        self.config = config or ADPConfig()
        self._api_key = _check_key(api_key) if api_key is not None else load_adp_key(key_file)
        self.router = router
        self.visitor_id = visitor_id or 'dcta-' + uuid.uuid4().hex
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.visitor_id):
            raise DomainError('PROVIDER_CONFIG', 'Invalid ADP visitor identifier.')
        self.name = 'adp/published-app'
        self.last_metadata = {}
        self._turn_deadline = None

    def begin_budget(self):
        super().begin_budget()
        if self.router is not None:
            self.router.begin_budget()

    def _request(self, payload, timeout):
        request = Request(self.config.endpoint, data=canonical(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream'}, method='POST')
        try:
            with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                for event in _events(response, time.monotonic() + timeout):
                    if event['Type'] == 'error':
                        error = event.get('Error', {})
                        code = error.get('Code') if type(error) is dict else None
                        # Only an integer diagnostic may leave the transport, never raw error text.
                        return ('adp_error', code if type(code) is int else None)
                    if event['Type'] == 'request_ack' and event.get('RequestAck', {}).get('Status') != 'success':
                        return ('error', 'PROVIDER_UNAVAILABLE')
                    if event['Type'] == 'response.completed':
                        record = event.get('Response')
                        content = _final_reply(record, payload['ConversationId'])
                        usage = record.get('StatInfo', {})
                        usage = {target: usage[source] for source, target in (
                            ('InputTokens', 'prompt_tokens'), ('OutputTokens', 'completion_tokens'), ('TotalTokens', 'total_tokens'))
                            if type(usage) is dict and type(usage.get(source)) is int and 0 <= usage[source] <= 100000000}
                        return ('reply', (content, usage))
                return ('error', 'PROVIDER_INCOMPLETE')
        except HTTPError as error:
            status = error.code
            error.close()
            return ('http', status)
        except DomainError as error:
            return ('error', error.code)
        except (ValueError, TypeError, AttributeError, KeyError):
            return ('error', 'PROVIDER_PROTOCOL')
        except TimeoutError:
            return ('error', 'PROVIDER_TIMEOUT')
        except (URLError, OSError):
            return ('error', 'PROVIDER_NETWORK')

    @observed('provider', 'adp_completion', provider=True)
    def complete(self, system, user, schema):
        self.last_metadata = {'provider': 'adp', 'status': 'STARTED', 'attempts': 0}
        if schema == ROUTE_SCHEMA:
            if self.router is None:
                return self._fail('PROVIDER_CONFIG')
            try:
                return self.router.complete(system, user, schema)
            finally:
                self.last_metadata = {**self.router.last_metadata, 'provider': 'deepseek-public-router'}
        if schema != PLAN_SCHEMA:
            return self._fail('MODEL_CONTRACT')
        started = time.monotonic()
        remaining = self.config.total_seconds
        if self._turn_deadline is not None:
            remaining = min(remaining, self._turn_deadline - started)
        if remaining <= 0:
            return self._fail('PROVIDER_TIMEOUT')
        payload = {'AppKey': self._api_key, 'VisitorId': self.visitor_id,
                   'ConversationId': str(uuid.uuid4()), 'RequestId': str(uuid.uuid4()),
                   'Contents': [{'Type': 'text', 'Text': user}], 'Stream': 'enable',
                   'Incremental': False, 'SearchNetwork': 'disable', 'WorkflowStatus': 'disable',
                   'EnableMultiIntent': False}
        # Use the published role instructions. Never send the router/validator prompt
        # as a user message or override it with a different SystemRole.
        self.last_metadata['attempts'] = 1
        kind, value = self._bounded_request(payload, min(remaining, self.config.timeout_seconds))
        if kind == 'http':
            return self._fail({401: 'PROVIDER_AUTH', 403: 'PROVIDER_AUTH', 402: 'PROVIDER_BALANCE',
                               429: 'PROVIDER_RATE_LIMIT'}.get(value, 'PROVIDER_HTTP_ERROR'), status=value)
        if kind == 'adp_error':
            if value is not None:
                self.last_metadata['provider_error_code'] = value
            return self._fail('PROVIDER_UNAVAILABLE')
        if kind == 'error':
            return self._fail(value)
        content, usage = value
        self.last_metadata.update({'status': 'OK', 'usage': usage, 'finish_reason': 'stop',
                                   'latency_seconds': round(time.monotonic() - started, 4)})
        return content
