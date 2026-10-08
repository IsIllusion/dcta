"""Opt-in TRTC ASR v3 adapter; credentials stay in the host and worker pipe."""
from dataclasses import dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .domain import DomainError
from .voice import MAX_AUDIO_BYTES


@dataclass(frozen=True)
class TRTCCredentials:
    sdk_app_id: int = field(repr=False)
    sdk_secret_key: str = field(repr=False)


def load_credentials(key_file=None):
    """Explicit file wins; never confuse cloud SecretKey with SDKSecretKey."""
    values = {}
    if key_file is None:
        app = os.environ.get('TRTC_ASR_SDK_APP_ID', '').strip()
        key = os.environ.get('TRTC_ASR_SECRET_KEY', '').strip()
        if app or key:
            values = {'sdkappid': app, 'sdksecretkey': key}
        else:
            key_file = os.environ.get('TRTC_KEY_FILE') or (
                Path(__file__).resolve().parents[2] / 'keys' / 'Tencent API.txt')
    if not values:
        try:
            path = Path(key_file)
            if path.stat().st_size > 16384:
                raise ValueError()
            content = path.read_text(encoding='utf-8-sig')
            for line in content.splitlines():
                match = re.fullmatch(r'\s*(SDKAppID|SDKSecretKey|TRTC_ASR_SDK_APP_ID|TRTC_ASR_SECRET_KEY)\s*[:=：]\s*(.*?)\s*', line, re.I)
                if not match:
                    continue
                name = {'trtc_asr_sdk_app_id': 'sdkappid', 'trtc_asr_secret_key': 'sdksecretkey'}.get(match[1].lower(), match[1].lower())
                value = match[2]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if name in values:
                    raise ValueError()
                values[name] = value
        except (OSError, UnicodeError, ValueError):
            raise DomainError('ASR_SETUP', 'TRTC credentials are unavailable or ambiguous. Supply SDKAppID and SDKSecretKey.') from None
    app, key = values.get('sdkappid', ''), values.get('sdksecretkey', '')
    if not re.fullmatch(r'[1-9][0-9]{5,19}', app) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', key):
        raise DomainError('ASR_SETUP', 'TRTC requires SDKAppID and the application SDKSecretKey, not the cloud API SecretKey.')
    return TRTCCredentials(int(app), key)


ERROR_HELP = {
    'ASR_SETUP': 'TRTC recognition is not configured. Check the application credentials and install requirements-trtc.txt.',
    'ASR_AUTH': 'TRTC rejected the application signature. Check SDKAppID, SDKSecretKey and the application site.',
    'ASR_SERVICE': 'TRTC speech recognition is not enabled for this application. Check its service entitlement in the TRTC console.',
    'ASR_QUOTA': 'TRTC speech recognition has no available quota or balance. Check Package Management and billing.',
    'ASR_RATE_LIMIT': 'TRTC is limiting recognition requests. Wait before recording again.',
    'ASR_NETWORK': 'The server could not reach TRTC. Check its connection or use text.',
    'ASR_RESPONSE': 'TRTC returned an incomplete or invalid recognition result. Use text or record again.',
    'ASR_FAILED': 'TRTC could not recognize the recording. Use text or record again.',
    'ASR_TIMEOUT': 'TRTC recognition timed out. Use a shorter recording or type your request.',
    'ASR_EMPTY': 'No clear speech was recognized. Record again or use text.',
    'AUDIO_FORMAT': 'The recording could not be decoded. Use WebM, Ogg, WAV, MP4 or MP3.',
    'AUDIO_DURATION': 'Keep the recording within 30 seconds.',
}


class TRTCASR:
    provider = 'trtc'
    label = 'Tencent TRTC · English · cloud'
    processing_message = 'Recognizing with Tencent TRTC… You can discard this recording or continue with text.'

    def __init__(self, *, key_file=None, credentials=None, site='intl', timeout=60):
        if site not in ('intl', 'cn') or not 1 <= timeout <= 90:
            raise DomainError('ASR_SETUP', 'Invalid TRTC site or timeout.')
        self._credentials = credentials or load_credentials(key_file)
        self.site, self.timeout = site, timeout

    @property
    def available(self):
        return all(importlib.util.find_spec(name) is not None for name in ('trtc_asr', 'av', 'numpy'))

    def transcribe(self, audio, vocabulary=None):
        if not self.available:
            raise DomainError('ASR_SETUP', ERROR_HELP['ASR_SETUP'])
        if type(audio) is not bytes or not 100 <= len(audio) <= MAX_AUDIO_BYTES:
            raise DomainError('AUDIO_SIZE', 'Recording must be between 100 bytes and 6 MB.')
        from .asr_worker import clean_vocabulary
        config = {'sdk_app_id': self._credentials.sdk_app_id, 'sdk_secret_key': self._credentials.sdk_secret_key,
                  'site': self.site, 'vocabulary': clean_vocabulary(vocabulary), 'timeout': min(40, self.timeout)}
        with tempfile.TemporaryDirectory(prefix='dcta-trtc-') as temp:
            source, result = Path(temp) / 'input.audio', Path(temp) / 'result.json'
            source.write_bytes(audio)
            try:
                run = subprocess.run([sys.executable, '-m', 'dcta.trtc_asr_worker', str(source), str(result)],
                    input=json.dumps(config).encode('utf-8'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    cwd=Path(__file__).resolve().parents[1], timeout=self.timeout,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except subprocess.TimeoutExpired:
                raise DomainError('ASR_TIMEOUT', ERROR_HELP['ASR_TIMEOUT']) from None
            try:
                if run.returncode or result.stat().st_size > 32768:
                    raise ValueError()
                data = json.loads(result.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    raise ValueError()
            except (OSError, ValueError):
                raise DomainError('ASR_FAILED', ERROR_HELP['ASR_FAILED']) from None
            if data.get('error'):
                code = data['error'] if data['error'] in ERROR_HELP else 'ASR_FAILED'
                error = DomainError(code, ERROR_HELP[code])
                error.asr_metadata = data.get('metadata', {})
                raise error
            text = data.get('transcript')
            if not isinstance(text, str) or not text.strip() or len(text) > 2000 or data.get('metadata', {}).get('final') is not True:
                raise DomainError('ASR_RESPONSE', ERROR_HELP['ASR_RESPONSE'])
            return text.strip(), data['metadata']
