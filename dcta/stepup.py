"""Out-of-band second check for high amounts (CV14 A). MOCK ONLY: this imitates a bank's security token.

After the device approval (Windows Hello or the demo key) of a transfer or plan from the step-up threshold
(SGD 800), the host issues a check bound to that exact authorization challenge and amount. The 6-digit code
appears only on the separate "DCTA Security Token" page (/token), which plays the role of a hardware token or
a phone app. The code is never stored: the host and the token page both derive it with HMAC-SHA256 from a
random key kept in a file beside the database, outside every table. A check lasts 120 s (never beyond its
authorization), allows 3 attempts, and a new code replaces the old one. Execution stays blocked until a check
for that challenge is VERIFIED (PaymentService.require_stepup).
"""
import hashlib
import hmac
from pathlib import Path
import secrets
import threading

from .domain import DomainError, format_money
from .store import uid

CODE_LIFETIME_MS = 120_000
MAX_ATTEMPTS = 3
# After three wrong codes every money action pauses. 10 s keeps the demo moving; a bank would use minutes or hours.
SECURITY_PAUSE_MS = 10_000
_key_lock = threading.Lock()


def key_path(db_path):
    db_path = Path(db_path)
    return db_path.with_name(db_path.stem + '.token-key')


def token_key(path):
    """The simulated token's secret. Created once, 32 random bytes; never logged, never in the database."""
    path = Path(path)
    with _key_lock:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(secrets.token_bytes(32))
        key = path.read_bytes()
    if len(key) != 32:
        raise DomainError('STEPUP_KEY', 'The security token key is damaged. Delete it to create a new one.')
    return key


def derive_code(key, row):
    message = f"{row['id']}|{row['challenge_id']}|{row['amount_minor']}|{row['issued_ms']}".encode()
    return f'{int.from_bytes(hmac.new(key, message, hashlib.sha256).digest()[:8], "big") % 1_000_000:06d}'


class StepUpService:
    def __init__(self, store, key_file, clock):
        self.store, self.key_file, self.clock = store, key_file, clock

    def _authorized_challenge(self, user, session, challenge_id, payments):
        challenge = payments._challenge(user, session, challenge_id)
        if challenge['status'] != 'AUTHORIZED':
            raise DomainError('AUTH_STATE', 'Approve on your device first.')
        review = payments._current(user, session, challenge)
        if not payments.needs_stepup(review):
            raise DomainError('STEPUP_NOT_NEEDED', 'This amount does not need the extra security check.')
        return challenge, review

    def _expire(self, session, now):
        for row in self.store.db.execute("SELECT id FROM stepup_checks WHERE session_id=? AND status='ISSUED' AND expires_ms<=?", (session, now)).fetchall():
            self.store.db.execute("UPDATE stepup_checks SET status='EXPIRED' WHERE id=?", (row[0],))

    def view(self, row, now):
        return {'id': row['id'], 'challenge_id': row['challenge_id'], 'status': row['status'], 'amount_minor': row['amount_minor'],
                'expires_ms': row['expires_ms'], 'remaining_ms': max(0, row['expires_ms'] - now),
                'attempts_left': MAX_ATTEMPTS - row['attempts'], 'token_path': '/token'}

    def issue(self, user, session, challenge_id, payments):
        """Start (or restart) the second check for an authorized high-amount challenge. The code is not returned."""
        token_key(self.key_file)
        with self.store.transaction():
            challenge, review = self._authorized_challenge(user, session, challenge_id, payments)
            now = self.clock()
            self._expire(session, now)
            if self.store.db.execute("SELECT 1 FROM stepup_checks WHERE challenge_id=? AND status IN ('VERIFIED','FAILED')", (challenge_id,)).fetchone():
                raise DomainError('STEPUP_STATE', 'This approval already finished its security check. Review again for a new one.')
            if self.store.db.execute('SELECT COUNT(*) FROM stepup_checks WHERE challenge_id=?', (challenge_id,)).fetchone()[0] >= 5:
                raise DomainError('STEPUP_LIMIT', 'Too many new codes for this approval. Close the review and start again.')
            # One live code at a time per customer: a new code only after the previous one has run out.
            live = self.store.db.execute("SELECT * FROM stepup_checks WHERE user_id=? AND status='ISSUED' AND expires_ms>? "
                                         "ORDER BY expires_ms DESC LIMIT 1", (user, now)).fetchone()
            if live and live['challenge_id'] == challenge_id:
                return self.view(live, now)  # same approval: keep the code already on the token
            if live:
                wait = live['expires_ms'] - now
                self.store.audit(session, 'STEPUP_WAIT', {'challenge_id': challenge_id, 'wait_ms': wait})
                return {'status': 'WAIT', 'wait_ms': wait, 'challenge_id': challenge_id,
                        'message': f'Last action has been terminated, please wait {-(-wait // 1000)} s.'}
            self.store.db.execute("UPDATE stepup_checks SET status='REPLACED' WHERE challenge_id=? AND status='ISSUED'", (challenge_id,))
            check_id = uid('stepup')
            expires = min(now + CODE_LIFETIME_MS, challenge['expires_ms'])
            if expires <= now:
                raise DomainError('EXPIRED', 'Authorization expired. Review again.')
            self.store.db.execute("INSERT INTO stepup_checks VALUES (?,?,?,?,?,?,?,0,'ISSUED',NULL)",
                                  (check_id, challenge_id, user, session, review['total_minor'], now, expires))
            self.store.audit(session, 'STEPUP_ISSUED', {'stepup_id': check_id, 'challenge_id': challenge_id, 'amount_minor': review['total_minor']})
            row = self.store.db.execute('SELECT * FROM stepup_checks WHERE id=?', (check_id,)).fetchone()
            return self.view(row, now)

    def verify(self, user, session, stepup_id, code, payments):
        if type(code) is not str or not code.isascii() or not code.isdigit() or len(code) != 6:
            raise DomainError('STEPUP_CODE', 'Enter the 6 digits shown on your security token.')
        key = token_key(self.key_file)
        with self.store.transaction():
            row = self.store.db.execute('SELECT * FROM stepup_checks WHERE id=? AND user_id=? AND session_id=?', (stepup_id, user, session)).fetchone()
            if not row:
                raise DomainError('NOT_FOUND', 'Security check unavailable.')
            now = self.clock()
            if row['status'] == 'VERIFIED':
                return self.view(row, now)
            if row['status'] != 'ISSUED':
                raise DomainError('STEPUP_STATE', 'This code is no longer valid. Ask for a new code.')
            if now >= row['expires_ms']:
                self.store.db.execute("UPDATE stepup_checks SET status='EXPIRED' WHERE id=?", (stepup_id,))
                self.store.audit(session, 'STEPUP_EXPIRED', {'stepup_id': stepup_id})
                return {**self.view({**dict(row), 'status': 'EXPIRED'}, now), 'message': 'The code expired. Ask for a new code.'}
            # The approval itself must still be current: same draft, input and validation.
            self._authorized_challenge(user, session, row['challenge_id'], payments)
            if hmac.compare_digest(code, derive_code(key, row)):
                self.store.db.execute("UPDATE stepup_checks SET status='VERIFIED',verified_ms=? WHERE id=?", (now, stepup_id))
                self.store.audit(session, 'STEPUP_VERIFIED', {'stepup_id': stepup_id, 'challenge_id': row['challenge_id']})
                return self.view({**dict(row), 'status': 'VERIFIED'}, now)
            attempts = row['attempts'] + 1
            if attempts >= MAX_ATTEMPTS:
                self.store.db.execute("UPDATE stepup_checks SET status='FAILED',attempts=? WHERE id=?", (attempts, stepup_id))
                self.store.db.execute("UPDATE authorization_challenges SET status='INVALIDATED' WHERE id=? AND status IN ('ISSUED','AUTHORIZED')", (row['challenge_id'],))
                self.store.audit(session, 'STEPUP_FAILED', {'stepup_id': stepup_id, 'challenge_id': row['challenge_id'],
                                                            'paused_until_ms': now + SECURITY_PAUSE_MS})
                return {**self.view({**dict(row), 'status': 'FAILED', 'attempts': attempts}, now), 'paused_until_ms': now + SECURITY_PAUSE_MS,
                        'pause_ms': SECURITY_PAUSE_MS,
                        'message': f'Three wrong codes. This approval is cancelled for your safety and no money moved. Everything is paused for {SECURITY_PAUSE_MS // 1000} seconds.'}
            self.store.db.execute('UPDATE stepup_checks SET attempts=? WHERE id=?', (attempts, stepup_id))
            self.store.audit(session, 'STEPUP_WRONG_CODE', {'stepup_id': stepup_id, 'attempts': attempts})
            left = MAX_ATTEMPTS - attempts
            return {**self.view({**dict(row), 'attempts': attempts}, now),
                    'message': f"That code doesn't match. {left} attempt{'s' if left != 1 else ''} left."}

    def token(self, user):
        """What the simulated security token shows: the transaction to approve and its current code."""
        key = token_key(self.key_file)
        with self.store.read_transaction():
            now = self.clock()
            row = self.store.db.execute("SELECT s.*,c.payload_json FROM stepup_checks s JOIN authorization_challenges c ON c.id=s.challenge_id "
                                        "WHERE s.user_id=? AND s.status='ISSUED' AND s.expires_ms>? AND c.status='AUTHORIZED' "
                                        "ORDER BY s.issued_ms DESC,s.rowid DESC LIMIT 1", (user, now)).fetchone()
            if not row:
                return {'active': False}
            import json
            review = json.loads(row['payload_json'])['review']
            lines = ([f"{format_money(a['amount_minor'])} to {a['payee']['name']} (ending {a['payee']['ending']})" for a in review['actions']]
                     if review.get('plan') else [f"{format_money(review['amount_minor'])} to {review['payee']['name']} (ending {review['payee']['ending']})"])
            return {'active': True, 'code': derive_code(key, row), 'expires_ms': row['expires_ms'], 'now_ms': now,
                    'total': format_money(row['amount_minor']), 'lines': lines}


def pause_remaining(store, user, now):
    """Milliseconds left in the security pause that follows three wrong codes (0 when none)."""
    import json
    row = store.db.execute("SELECT a.details_json FROM audit_events a JOIN sessions s ON s.id=a.session_id "
                           "WHERE s.user_id=? AND a.event='STEPUP_FAILED' ORDER BY a.id DESC LIMIT 1", (user,)).fetchone()
    if not row:
        return 0
    until = json.loads(row[0]).get('paused_until_ms')
    return max(0, until - now) if type(until) is int else 0


def require_not_paused(store, user, now):
    left = pause_remaining(store, user, now)
    if left:
        raise DomainError('SECURITY_PAUSE', f'Too many wrong security codes. Everything is paused for {-(-left // 1000)} more seconds. No money moved.')
