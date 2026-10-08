"""Isolated decoder and Whisper worker; launched only with host-owned paths.

Recognition pipeline (CV14):
1. Decode with a hard sample bound (never trust container duration).
2. Condition the signal: remove DC offset, bounded peak normalisation for quiet
   microphones, and a short silence pad so words at the very edges of a clip are
   not lost when the user presses/releases the button while speaking.
3. Decode with a banking-domain prompt plus the user's own saved payee and account
   names as hotwords, beam search and temperature fallback.
4. Soft VAD first; if VAD drops everything, retry on the whole clip without VAD.
5. Keep a segment unless Whisper is confident it is silence (no_speech AND low
   log-probability), and reject output that only echoes the prompt vocabulary.
"""
import json
from pathlib import Path
import re
import sys
import time

SAMPLE_RATE = 16000
MIN_PEAK = 0.002
TARGET_PEAK = 0.8
MAX_GAIN = 12.0
EDGE_PAD_SECONDS = 0.3
MAX_VOCAB_TERMS = 24
MAX_TERM_CHARS = 40
DOMAIN_PROMPT = ('Transfer 50 Singapore dollars from my savings account. Send SGD 200 to my payee. '
                 'Show my balance and my transaction history. No, make it 30 dollars. Cancel it. Confirm.')
VAD_OPTIONS = {'threshold': 0.35, 'min_speech_duration_ms': 0, 'min_silence_duration_ms': 600,
               'speech_pad_ms': 500}
TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8)
_TERM = re.compile(r"[^A-Za-z0-9 .'&-]")


def clean_vocabulary(vocabulary):
    """Host-supplied payee/account names -> bounded, de-duplicated plain terms."""
    terms, seen = [], set()
    for raw in vocabulary or ():
        if not isinstance(raw, str):
            continue
        term = ' '.join(_TERM.sub(' ', raw).split())[:MAX_TERM_CHARS].strip()
        if len(term) < 2 or term.lower() in seen:
            continue
        seen.add(term.lower())
        terms.append(term)
        if len(terms) >= MAX_VOCAB_TERMS:
            break
    return terms


def build_prompt(terms):
    return DOMAIN_PROMPT + (' Names: ' + ', '.join(terms) + '.' if terms else '')


def condition(audio, np):
    """DC removal, bounded gain for quiet input and edge padding. Returns (audio, gain)."""
    audio = audio - float(np.mean(audio))
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    gain = 1.0
    if 0 < peak < TARGET_PEAK:
        gain = min(MAX_GAIN, TARGET_PEAK / peak)
        audio = audio * gain
    np.clip(audio, -1.0, 1.0, out=audio)
    pad = np.zeros(int(EDGE_PAD_SECONDS * SAMPLE_RATE), dtype=np.float32)
    return np.concatenate([pad, audio.astype(np.float32), pad]), gain


def keep_segment(segment):
    # Whisper's own silence rule: drop only when it is BOTH likely silence and low confidence.
    return not (segment.no_speech_prob > 0.6 and segment.avg_logprob < -1.0)


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def echoes_prompt(transcript, prompt):
    """True when the output is nothing but prompt vocabulary (a known Whisper failure on near-silence)."""
    words = _words(transcript)
    if not words:
        return True
    vocab = set(_words(prompt))
    return len(words) >= 3 and all(w in vocab for w in words) and ' '.join(words) in ' '.join(_words(prompt))


def _decode(model, audio, prompt, hotwords, vad):
    segments, _ = model.transcribe(
        audio, language='en', beam_size=5, best_of=5, patience=1.0, temperature=list(TEMPERATURES),
        condition_on_previous_text=False, initial_prompt=prompt or None, hotwords=hotwords or None,
        vad_filter=vad, vad_parameters=VAD_OPTIONS if vad else None, without_timestamps=True,
        compression_ratio_threshold=2.4, log_prob_threshold=-1.0, no_speech_threshold=0.6)
    return ' '.join(s.text.strip() for s in segments if keep_segment(s)).strip()


def transcribe(source, model_dir, vocabulary=None):
    import av
    import numpy as np
    from faster_whisper import WhisperModel
    from .voice import MAX_AUDIO_SECONDS
    started = time.monotonic()
    # Bound decoded samples while reading, before running the model. Do not trust container duration.
    chunks, count = [], 0
    with av.open(str(source)) as container:
        if not container.streams.audio:
            return {'error': 'AUDIO_FORMAT'}
        resampler = av.AudioResampler(format='s16', layout='mono', rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            for item in resampler.resample(frame):
                count += item.samples
                if count > MAX_AUDIO_SECONDS * SAMPLE_RATE:
                    return {'error': 'AUDIO_DURATION'}
                chunks.append(item.to_ndarray().flatten())
        for item in resampler.resample(None):
            count += item.samples
            chunks.append(item.to_ndarray().flatten())
    if count > MAX_AUDIO_SECONDS * SAMPLE_RATE:
        return {'error': 'AUDIO_DURATION'}
    if count < 3200:
        return {'error': 'ASR_EMPTY'}
    audio = np.concatenate(chunks).astype(np.float32) / 32768.0
    if float(np.max(np.abs(audio - float(np.mean(audio))))) < MIN_PEAK:
        return {'error': 'ASR_EMPTY'}
    audio, gain = condition(audio, np)
    terms = clean_vocabulary(vocabulary)
    prompt = build_prompt(terms)
    hotwords = ' '.join(terms)
    model = WhisperModel(str(model_dir), device='cpu', compute_type='int8', cpu_threads=4,
                         local_files_only=True)
    passes = 1
    transcript = _decode(model, audio, prompt, hotwords, vad=True)
    if not transcript or echoes_prompt(transcript, prompt):
        passes += 1  # VAD may have cut soft speech: whole clip, same biasing.
        transcript = _decode(model, audio, prompt, hotwords, vad=False)
    if transcript and echoes_prompt(transcript, prompt):
        passes += 1  # Still only prompt words: decode without any biasing and trust that.
        transcript = _decode(model, audio, None, None, vad=True)
    if not transcript:
        return {'error': 'ASR_EMPTY'}
    return {'transcript': transcript, 'metadata': {'adapter': 'faster-whisper-1.2.1', 'model': model_name(model_dir),
            'language': 'en', 'final': True, 'duration_seconds': round(count / SAMPLE_RATE, 3),
            'latency_ms': round((time.monotonic() - started) * 1000), 'device': 'cpu', 'compute_type': 'int8',
            'decode_passes': passes, 'vocabulary_terms': len(terms), 'input_gain': round(gain, 2)}}


def model_name(model_dir):
    name = Path(model_dir).name
    return name if name in ('base.en', 'small.en') else 'custom'


def main(argv):
    vocabulary = None
    if len(argv) > 4:
        try:
            vocabulary = json.loads(Path(argv[4]).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            vocabulary = None
    try:
        result = transcribe(Path(argv[1]), Path(argv[3]), vocabulary if isinstance(vocabulary, list) else None)
    except Exception:
        result = {'error': 'ASR_FAILED'}
    Path(argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main(sys.argv)
