"""Optional TRTC FlowTTS read-back. Cloud credentials and audio cache stay on host."""
import base64
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import threading
import time

from .domain import DomainError

MAX_TEXT = 255  # International TextToSpeech API limit, not the mainland limit.
MAX_RESPONSE = 4 * 1024 * 1024
HELP = {
    'TTS_SETUP': 'Tencent speech synthesis is not configured. Check SDKAppID and cloud API SecretId / SecretKey.',
    'TTS_AUTH': 'Tencent rejected the speech credentials or permission. Check the cloud API key and TRTC access permission.',
    'TTS_SERVICE': 'Speech synthesis is not enabled for this application. Check its TRTC service entitlement.',
    'TTS_QUOTA': 'Tencent speech synthesis has no available quota or balance. Check TRTC billing.',
    'TTS_BUSY': 'Speech synthesis is busy. Wait a moment before trying again.',
    'TTS_NETWORK': 'Could not reach Tencent speech synthesis. Check the server connection.',
    'TTS_RESPONSE': 'Tencent returned invalid speech audio. Try again later.',
    'TTS_INPUT': 'Speech text must contain 1–255 characters per segment.',
    'TTS_FAILED': 'Tencent could not generate this speech. Try again later.',
}


@dataclass(frozen=True)
class TTSCredentials:
    sdk_app_id: int = field(repr=False)
    secret_id: str = field(repr=False)
    secret_key: str = field(repr=False)


def load_credentials(key_file=None):
    values = {}
    if key_file is None:
        values = {k: os.environ.get(v, '').strip() for k, v in {
            'sdkappid': 'TRTC_TTS_SDK_APP_ID', 'secretid': 'TENCENTCLOUD_SECRET_ID',
            'secretkey': 'TENCENTCLOUD_SECRET_KEY'}.items()}
        if not any(values.values()):
            values = {}
            key_file = os.environ.get('TRTC_KEY_FILE') or Path(__file__).resolve().parents[2] / 'keys' / 'Tencent API.txt'
    if not values:
        try:
            path = Path(key_file)
            if path.stat().st_size > 16384:
                raise ValueError()
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                match = re.fullmatch(r'\s*(SDKAppID|SecretId|SecretKey)\s*[:=：]\s*(.*?)\s*', line, re.I)
                if not match:
                    continue
                name, value = match[1].lower(), match[2]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if name in values:
                    raise ValueError()
                values[name] = value
        except (OSError, UnicodeError, ValueError):
            raise DomainError('TTS_SETUP', HELP['TTS_SETUP']) from None
    app, sid, key = (values.get(k, '') for k in ('sdkappid', 'secretid', 'secretkey'))
    if (not re.fullmatch(r'[1-9][0-9]{5,19}', app) or
            not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', sid) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key)):
        raise DomainError('TTS_SETUP', HELP['TTS_SETUP'])
    return TTSCredentials(int(app), sid, key)


def signed_headers(credentials, body, host, region, timestamp):
    """Tencent Cloud API 3.0 TC3-HMAC-SHA256 (UTC date; exact transmitted body)."""
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d')
    content_type = 'application/json; charset=utf-8'
    canonical = f'POST\n/\n\ncontent-type:{content_type}\nhost:{host}\n\ncontent-type;host\n{hashlib.sha256(body).hexdigest()}'
    scope = f'{date}/trtc/tc3_request'
    to_sign = f'TC3-HMAC-SHA256\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    def sign(key, message):
        return hmac.new(key, message.encode(), hashlib.sha256).digest()
    key = sign(sign(sign(('TC3' + credentials.secret_key).encode(), date), 'trtc'), 'tc3_request')
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {'Content-Type': content_type, 'Host': host,
            'Authorization': f'TC3-HMAC-SHA256 Credential={credentials.secret_id}/{scope}, SignedHeaders=content-type;host, Signature={signature}',
            'X-TC-Action': 'TextToSpeech', 'X-TC-Version': '2019-07-22',
            'X-TC-Region': region, 'X-TC-Timestamp': str(timestamp)}


class TRTCTTS:
    provider = 'trtc'
    label = 'Tencent TRTC FlowTTS · English · cloud'

    def __init__(self, *, key_file=None, credentials=None, site='intl', timeout=25):
        if site not in ('intl', 'cn') or not 1 <= timeout <= 40:
            raise DomainError('TTS_SETUP', HELP['TTS_SETUP'])
        self._credentials = credentials or load_credentials(key_file)
        self.host = 'trtc.intl.tencentcloudapi.com' if site == 'intl' else 'trtc.tencentcloudapi.com'
        self.region = 'ap-singapore' if site == 'intl' else 'ap-beijing'
        self.timeout = timeout
        self._slot = threading.Lock()
        self._cache = OrderedDict()

    def _post(self, body, headers):
        # Fixed HTTPS destination, no redirect or automatic retry of billable calls.
        connection = http.client.HTTPSConnection(self.host, timeout=self.timeout)
        try:
            connection.request('POST', '/', body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE or response.status not in (200, 400, 401, 403, 429):
                raise DomainError('TTS_RESPONSE', HELP['TTS_RESPONSE'])
            return json.loads(raw)
        except (OSError, http.client.HTTPException):
            raise DomainError('TTS_NETWORK', HELP['TTS_NETWORK']) from None
        except (ValueError, UnicodeError):
            raise DomainError('TTS_RESPONSE', HELP['TTS_RESPONSE']) from None
        finally:
            connection.close()

    def synthesize(self, text):
        if type(text) is not str or not 1 <= len(text.strip()) <= MAX_TEXT or any(ord(c) < 32 and c not in '\n\t' for c in text):
            raise DomainError('TTS_INPUT', HELP['TTS_INPUT'])
        text = text.strip()
        if not self._slot.acquire(blocking=False):
            raise DomainError('TTS_BUSY', HELP['TTS_BUSY'])
        try:
            now = time.monotonic()
            for key in list(self._cache):
                if now - self._cache[key][0] >= 300:
                    del self._cache[key]
            digest = hashlib.sha256(text.encode()).digest()
            if digest in self._cache:
                _, audio, metadata = self._cache[digest]
                self._cache.move_to_end(digest)
                return audio, {**metadata, 'cache_hit': True}
            payload = {'Text': text, 'SdkAppId': self._credentials.sdk_app_id,
                       'Model': 'flow_01_turbo', 'Language': 'en',
                       'Voice': {'VoiceId': 'v-female-R2s4N9qJ', 'Speed': 1.0, 'Volume': 1.0, 'Pitch': 0},
                       'AudioFormat': {'Format': 'mp3', 'SampleRate': 24000}}
            body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
            result = self._post(body, signed_headers(self._credentials, body, self.host, self.region, int(time.time())))
            response = result.get('Response') if isinstance(result, dict) else None
            if not isinstance(response, dict):
                raise DomainError('TTS_RESPONSE', HELP['TTS_RESPONSE'])
            request_id = response.get('RequestId', '')
            request_id = request_id if isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id) else ''
            if 'Error' in response:
                error = response['Error']
                provider_code = error.get('Code', '') if isinstance(error, dict) else ''
                provider_code = provider_code if isinstance(provider_code, str) and re.fullmatch(r'[A-Za-z0-9_.]{1,100}', provider_code) else 'Unknown'
                code = ('TTS_AUTH' if provider_code.startswith(('AuthFailure', 'UnauthorizedOperation')) else
                        'TTS_QUOTA' if provider_code.startswith(('ResourceInsufficient', 'FailedOperation.Insufficient')) else
                        'TTS_BUSY' if provider_code.startswith(('LimitExceeded', 'RequestLimitExceeded')) else
                        'TTS_SERVICE' if provider_code == 'UnsupportedOperation.NotAllowed' else 'TTS_FAILED')
                exc = DomainError(code, HELP[code])
                exc.tts_metadata = {'request_id': request_id, 'provider_code': provider_code}
                raise exc
            try:
                audio = base64.b64decode(response['Audio'], validate=True)
                if len(audio) < 100 or not (audio.startswith(b'ID3') or (audio[0] == 255 and audio[1] & 224 == 224)):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise DomainError('TTS_RESPONSE', HELP['TTS_RESPONSE']) from None
            metadata = {'provider': 'trtc', 'model': 'flow_01_turbo', 'request_id': request_id,
                        'characters': len(text), 'latency_ms': round((time.monotonic() - now) * 1000), 'cache_hit': False}
            self._cache[digest] = (now, audio, metadata)
            while len(self._cache) > 32 or sum(len(v[1]) for v in self._cache.values()) > 8 * 1024 * 1024:
                self._cache.popitem(last=False)
            return audio, metadata
        finally:
            self._slot.release()
