"""Bounded audio conversion and one TRTC request. No retries or raw error logs."""
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .domain import DomainError
from .voice import MAX_AUDIO_SECONDS

SAMPLE_RATE = 16000
MAX_RESPONSE_BYTES = 65536


def decode_pcm(source):
    """Convert real audio (including WebM) to s16le mono, enforcing decoded duration."""
    import av
    import numpy as np
    chunks, count = [], 0

    def collect(frames):
        nonlocal count
        for frame in frames:
            count += frame.samples
            if count > SAMPLE_RATE * MAX_AUDIO_SECONDS:
                raise DomainError('AUDIO_DURATION', 'Recording exceeds 30 seconds.')
            chunks.append(frame.to_ndarray().flatten().astype('<i2').tobytes())

    try:
        with av.open(str(source)) as container:
            if not container.streams.audio:
                raise DomainError('AUDIO_FORMAT', 'No audio stream.')
            resampler = av.AudioResampler(format='s16', layout='mono', rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                collect(resampler.resample(frame))
            collect(resampler.resample(None))
    except DomainError:
        raise
    except Exception:
        raise DomainError('AUDIO_FORMAT', 'Audio could not be decoded.') from None
    pcm = b''.join(chunks)
    if count < 3200:
        raise DomainError('ASR_EMPTY', 'Recording is too short.')
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
    if float(np.max(np.abs(samples - samples.mean()))) < 65:
        raise DomainError('ASR_EMPTY', 'No audible speech.')
    return pcm, count / SAMPLE_RATE


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_recognizer(config):
    from trtc_asr import v3

    class BoundedRecognizer(v3.SentenceRecognizer):
        # Let the SDK build/sign the request; constrain transport and validate the
        # envelope before its permissive response parser supplies defaults.
        def _post(self, path, body):
            endpoint = 'https://asr-intl.cloud-rtc.com' if config['site'] == 'intl' else 'https://asr.cloud-rtc.com'
            request = Request(endpoint + '/v3/transcribe', data=json.dumps(body).encode('utf-8'),
                              headers={'Content-Type': 'application/json'}, method='POST')
            try:
                try:
                    response = build_opener(NoRedirect()).open(request, timeout=config['timeout'])
                except HTTPError as exc:
                    response = exc
                with response:
                    status = response.code
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
            except TimeoutError:
                raise DomainError('ASR_TIMEOUT', 'Recognition timed out.') from None
            except URLError as exc:
                self.safe_metadata['network_error'] = type(exc.reason).__name__
                if isinstance(getattr(exc.reason, 'errno', None), int):
                    self.safe_metadata['network_errno'] = exc.reason.errno
                code = 'ASR_TIMEOUT' if isinstance(exc.reason, TimeoutError) else 'ASR_NETWORK'
                raise DomainError(code, 'TRTC connection failed.') from None
            if len(raw) > MAX_RESPONSE_BYTES:
                raise DomainError('ASR_RESPONSE', 'Response is too large.')
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                data = None
            if status == 429:
                raise DomainError('ASR_RATE_LIMIT', 'Recognition rate limit reached.')
            if status in (401, 403) and not isinstance(data, dict):
                raise DomainError('ASR_AUTH', 'TRTC rejected authentication.')
            if not isinstance(data, dict) or type(data.get('code')) is not int:
                raise DomainError('ASR_RESPONSE', 'Missing response status.')
            self.safe_metadata = {'provider_code': data['code']}
            request_id = data.get('request_id')
            if isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request_id):
                self.safe_metadata['request_id'] = request_id
            if data['code'] != 0:
                message = str(data.get('message', '')).lower()
                code = ('ASR_AUTH' if data['code'] == 4002 else
                        'ASR_QUOTA' if any(x in message for x in ('quota', 'balance', 'arrear', 'insufficient', '余额', '欠费')) else
                        'ASR_SERVICE' if any(x in message for x in ('not enabled', 'not activated', 'not open', 'subscription', 'package', 'permission', '未开通', '套餐')) else
                        'ASR_RATE_LIMIT' if any(x in message for x in ('concurrency', 'rate limit', 'too many')) else 'ASR_FAILED')
                raise DomainError(code, 'TRTC rejected recognition.')
            if not 200 <= status < 300 or not isinstance(data.get('result'), str):
                raise DomainError('ASR_RESPONSE', 'Incomplete recognition result.')
            if not data['result'].strip():
                raise DomainError('ASR_EMPTY', 'No speech recognized.')
            if len(data['result']) > 2000:
                raise DomainError('ASR_RESPONSE', 'Recognition text is too long.')
            return data

    credential = v3.new_credential(config['sdk_app_id'], config['sdk_secret_key'])
    credential.set_site(config['site'])
    recognizer = BoundedRecognizer(credential)
    recognizer.safe_metadata = {}
    return recognizer


def transcribe(source, config):
    started = time.monotonic()
    metadata = {'adapter': 'trtc-asr-v3', 'model': 'bigmodel', 'language': 'en', 'site': config['site'], 'final': False}
    recognizer = None
    try:
        from trtc_asr import v3
        pcm, duration = decode_pcm(source)
        metadata['duration_seconds'] = round(duration, 3)
        recognizer = make_recognizer(config)
        from .asr_worker import clean_vocabulary
        terms = clean_vocabulary(config.get('vocabulary'))
        request = v3.TranscribeRequest(engine_model_type='bigmodel', voice_format='pcm', language='en',
                                      hotword_list=','.join(f'{term[:30]}|5' for term in terms))
        response = recognizer.recognize_data_with_options(pcm, request)
        metadata.update(recognizer.safe_metadata)
        metadata.update(final=True, latency_ms=round((time.monotonic() - started) * 1000), vocabulary_terms=len(terms))
        return {'transcript': response.result.strip(), 'metadata': metadata}
    except Exception as exc:
        if recognizer is not None:
            metadata.update(recognizer.safe_metadata)
        metadata['latency_ms'] = round((time.monotonic() - started) * 1000)
        # Never forward a raw SDK exception: it may contain request contents.
        code = exc.code if isinstance(exc, DomainError) else 'ASR_SETUP' if isinstance(exc, ImportError) else 'ASR_FAILED'
        return {'error': code, 'metadata': metadata}


def main():
    try:
        config = json.loads(sys.stdin.buffer.read(16384))
        result = transcribe(Path(sys.argv[1]), config)
    except Exception:
        result = {'error': 'ASR_FAILED'}
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
