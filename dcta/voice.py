"""Final-only ASR evidence and atomic handoff to the existing conversation."""
import hashlib
import json
from .domain import DomainError, canonical
from .store import uid, now

MAX_AUDIO_BYTES = 6 * 1024 * 1024
MAX_AUDIO_SECONDS = 30
MIMES = {'audio/webm', 'audio/ogg', 'audio/wav', 'audio/x-wav', 'audio/mp4', 'audio/mpeg'}


class VoiceService:
    def __init__(self, store):
        self.store = store

    def row(self, user, session, voice_id):
        self.store.session(user, session)
        row = self.store.db.execute('SELECT * FROM voice_inputs WHERE id=? AND session_id=?', (voice_id, session)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', 'Recording unavailable in this session.')
        return dict(row)

    def view(self, user, session, voice_id):
        self.store.session(user,session)
        row=self.store.db.execute('SELECT id,status,transcript,error_code,input_id,audio_sha256,created_at,asr_json FROM voice_inputs WHERE id=? AND session_id=?',(voice_id,session)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND','Recording unavailable in this session.')
        result = {k: row[k] for k in ('id', 'status', 'transcript', 'error_code', 'input_id', 'audio_sha256', 'created_at')}
        result['asr'] = json.loads(row['asr_json']) if row['asr_json'] else None
        return result

    def start(self, user, session, key, mime, audio):
        if type(key) is not str or not 1 <= len(key) <= 100:
            raise DomainError('INPUT', 'A bounded recording key is required.')
        if type(audio) is not bytes or not 100 <= len(audio) <= MAX_AUDIO_BYTES:
            raise DomainError('AUDIO_SIZE', 'Recording must be between 100 bytes and 6 MB.')
        if type(mime) is not str or mime.split(';')[0].lower() not in MIMES:
            raise DomainError('AUDIO_FORMAT', 'Use WebM, Ogg, WAV, MP4 or MP3 audio.')
        mime = mime.split(';')[0].lower()
        sha = hashlib.sha256(audio).hexdigest()
        with self.store.transaction():
            current = self.store.session(user, session)
            old = self.store.db.execute('SELECT id,audio_sha256,mime FROM voice_inputs WHERE session_id=? AND request_key=?', (session, key)).fetchone()
            if old:
                if old['audio_sha256'] != sha or old['mime'] != mime:
                    raise DomainError('CONFLICT', 'Recording key belongs to different audio.')
                return self.view(user, session, old['id']), False
            # Local-demo retention budget: no silent deletion of evidence.
            total = self.store.db.execute('SELECT COALESCE(SUM(length(audio)),0) FROM voice_inputs').fetchone()[0]
            if total + len(audio) > 200 * 1024 * 1024:
                raise DomainError('AUDIO_STORAGE', 'Audio archive is full. Start with a separate database or use text.')
            self.store.db.execute("UPDATE voice_inputs SET status='CANCELLED' WHERE session_id=? AND status IN ('PROCESSING','READY')", (session,))
            voice_id = uid('voice')
            self.store.db.execute('INSERT INTO voice_inputs(id,session_id,request_key,base_revision,status,mime,audio,audio_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                                  (voice_id, session, key, current['revision'], 'PROCESSING', mime, audio, sha, now()))
            self.store.audit(session, 'VOICE_RECORDED', {'voice_id': voice_id, 'audio_sha256': sha, 'bytes': len(audio)})
            return self.view(user, session, voice_id), True

    def finish(self, user, session, voice_id, transcript=None, metadata=None, error=None):
        with self.store.transaction():
            row = self.row(user, session, voice_id)
            if row['status'] != 'PROCESSING':
                return self.view(user, session, voice_id)
            if self.store.session(user, session)['revision'] != row['base_revision']:
                self.store.db.execute("UPDATE voice_inputs SET status='CANCELLED' WHERE id=?", (voice_id,))
            elif error:
                self.store.db.execute("UPDATE voice_inputs SET status='FAILED',error_code=?,asr_json=? WHERE id=?", (error, canonical(metadata) if metadata else None, voice_id))
            else:
                if type(transcript) is not str or not transcript.strip() or len(transcript) > 2000:
                    self.store.db.execute("UPDATE voice_inputs SET status='FAILED',error_code='ASR_EMPTY' WHERE id=?", (voice_id,))
                else:
                    self.store.db.execute("UPDATE voice_inputs SET status='READY',transcript=?,asr_json=? WHERE id=?",
                                          (transcript, canonical(metadata or {}), voice_id))
            result = self.view(user, session, voice_id)
            self.store.audit(session, 'VOICE_FINALIZED', {'voice_id': voice_id, 'status': result['status'], 'error_code': result['error_code']})
            return result

    def cancel(self, user, session, voice_id):
        with self.store.transaction():
            row = self.row(user, session, voice_id)
            if row['status'] in ('PROCESSING', 'READY'):
                self.store.db.execute("UPDATE voice_inputs SET status='CANCELLED' WHERE id=?", (voice_id,))
                self.store.audit(session, 'VOICE_CANCELLED', {'voice_id': voice_id})
            return self.view(user, session, voice_id)

    def submit(self, user, session, voice_id, conversation):
        with self.store.transaction():
            row = self.row(user, session, voice_id)
            if row['status'] == 'SUBMITTED':
                token = self.store.begin_turn(user, session, 'voice:' + voice_id, row['transcript'])
            else:
                if row['status'] != 'READY' or self.store.session(user, session)['revision'] != row['base_revision']:
                    raise DomainError('VOICE_STALE', 'Recording is not ready or was superseded. Record again or use text.')
                # Claim before begin_turn cancels all other pending recordings.
                self.store.db.execute("UPDATE voice_inputs SET status='SUBMITTED' WHERE id=?", (voice_id,))
                token = self.store.begin_turn(user, session, 'voice:' + voice_id, row['transcript'])
                self.store.db.execute('UPDATE voice_inputs SET input_id=? WHERE id=?', (token['input_id'], voice_id))
                self.store.audit(session, 'VOICE_SUBMITTED', {'voice_id': voice_id}, token['input_id'])
        return conversation.complete_turn(user, session, row['transcript'], token)
