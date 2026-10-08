"""Local Whisper in a bounded, killable process. No cloud audio upload."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from .domain import DomainError


MODEL_FILES = ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')
PREFERRED_MODELS = ('small.en', 'base.en')


def default_model_dir(models_root='work/models'):
    """Most accurate installed model: small.en when Setup-Voice.ps1 -Model small.en was run, else base.en."""
    root = Path(models_root)
    if not root.is_dir():
        # Launched from a different working directory: fall back to the project-root
        # copy so voice is not silently unavailable because of a relative path.
        fallback = Path(__file__).resolve().parents[1] / models_root
        if fallback.is_dir():
            root = fallback
    for name in PREFERRED_MODELS:
        if all((root / name / f).is_file() for f in MODEL_FILES):
            return str(root / name)
    return str(root / 'base.en')


class LocalWhisper:
    provider = 'local'
    processing_message = 'Recognizing locally… You can discard this recording or continue with text.'

    def __init__(self, model_dir, timeout=90):
        self.model_dir = Path(model_dir).resolve()
        self.timeout = timeout

    @property
    def label(self):
        return f'Whisper {self.model_dir.name} · local CPU'

    @property
    def available(self):
        import importlib.util
        return importlib.util.find_spec('faster_whisper') is not None and all((self.model_dir / name).is_file() for name in MODEL_FILES)

    def transcribe(self, audio, vocabulary=None):
        """vocabulary: the user's own saved payee/account names, used only to bias recognition locally."""
        if not self.available:
            raise DomainError('ASR_SETUP', 'Run Setup-Voice.ps1 first; text input remains available.')
        with tempfile.TemporaryDirectory(prefix='dcta-asr-') as temp:
            source, result = Path(temp) / 'input.audio', Path(temp) / 'result.json'
            source.write_bytes(audio)
            argv = [sys.executable, '-m', 'dcta.asr_worker', str(source), str(result), str(self.model_dir)]
            if vocabulary:
                terms = Path(temp) / 'vocabulary.json'
                terms.write_text(json.dumps([str(x) for x in vocabulary][:50], ensure_ascii=False), encoding='utf-8')
                argv.append(str(terms))
            try:
                run = subprocess.run(argv,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=self.timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except subprocess.TimeoutExpired:
                raise DomainError('ASR_TIMEOUT', 'Recognition timed out. Record again or use text.') from None
            if not result.exists() or run.returncode != 0:
                raise DomainError('ASR_FAILED', 'Could not recognize this recording. Record again or use text.')
            data = json.loads(result.read_text(encoding='utf-8'))
            if data.get('error'):
                raise DomainError(data['error'], 'Recording could not be used. Record clear English within 30 seconds, or use text.')
            return data['transcript'], data['metadata']
