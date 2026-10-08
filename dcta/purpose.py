"""Transfer purpose: a label the user picks for a draft (Rent, Shopping, … or Others with a short reason).

It is record-keeping metadata shown on the draft, review and receipt. It is not part of the frozen payment
payload, the validator's input or the risk rules, and it can only be set by the owner while the draft is active.
"""
import unicodedata

from .domain import DomainError, MAX_BATCH_ACTIONS
from .store import now

DEFAULT = 'Transfer'  # what an unlabelled draft is: a plain transfer
PURPOSES = (DEFAULT, 'Family & friends', 'Rent', 'Security deposit', 'Shopping', 'Bills & utilities', 'Education',
            'Medical', 'Travel', 'Investment', 'Salary', 'Others')
MAX_NOTE = 80


def clean_note(note):
    if type(note) is not str:
        raise DomainError('INPUT', 'The reason must be text.')
    text = ' '.join(note.split())
    if any(unicodedata.category(ch).startswith('C') for ch in text):
        raise DomainError('INPUT', 'The reason contains characters that cannot be saved.')
    if len(text) > MAX_NOTE:
        raise DomainError('INPUT', f'Keep the reason within {MAX_NOTE} characters.')
    return text


# CV14: a sensible starting label by destination; shown until the customer picks one, never stored.
KIND_DEFAULTS = {'SHARES': 'Investment', 'BILL': 'Bills & utilities'}


def defaults_for(store, user, payee_ids):
    profiles = store.profiles(user)
    return [KIND_DEFAULTS.get((profiles.get(pid) or {}).get('kind'), DEFAULT) for pid in payee_ids]


def set_purpose(store, user, session, draft_id, category, note='', action=0):
    store.session(user, session)
    if type(action) is not int or not 0 <= action < MAX_BATCH_ACTIONS:
        raise DomainError('INPUT', 'Unknown action in this draft.')
    if category not in PURPOSES:
        raise DomainError('INPUT', 'Choose one of the listed transfer purposes.')
    note = clean_note(note)
    if category == 'Others' and not note:
        raise DomainError('INPUT', 'Please write a short reason for "Others".')
    if category != 'Others':
        note = ''
    with store.transaction():
        row = store.db.execute('SELECT d.id,d.state FROM transaction_drafts d JOIN sessions s ON s.id=d.session_id '
                               'WHERE d.id=? AND d.session_id=? AND s.user_id=?', (draft_id, session, user)).fetchone()
        if not row:
            raise DomainError('NOT_FOUND', 'Draft not found in this conversation.')
        if row['state'] != 'DRAFTED':
            raise DomainError('LOCKED', 'The purpose can no longer be changed for a finished or cancelled draft.')
        if action >= len(store.draft(user, draft_id)['payload']['actions']):
            raise DomainError('INPUT', 'Unknown action in this draft.')
        if action:
            store.db.execute('INSERT INTO action_purposes(draft_id,action_index,category,note,updated_at) VALUES (?,?,?,?,?) '
                             'ON CONFLICT(draft_id,action_index) DO UPDATE SET category=excluded.category,note=excluded.note,updated_at=excluded.updated_at',
                             (draft_id, action, category, note, now()))
            store.audit(session, 'TRANSFER_PURPOSE_SET', {'draft_id': draft_id, 'action': action, 'category': category, 'note_length': len(note)})
            return get_purposes(store, draft_id, action + 1)[action]
        store.db.execute('INSERT INTO transfer_purposes(draft_id,category,note,updated_at) VALUES (?,?,?,?) '
                         'ON CONFLICT(draft_id) DO UPDATE SET category=excluded.category,note=excluded.note,updated_at=excluded.updated_at',
                         (draft_id, category, note, now()))
        store.audit(session, 'TRANSFER_PURPOSE_SET', {'draft_id': draft_id, 'category': category, 'note_length': len(note)})
    return get_purpose(store, draft_id)


def get_purpose(store, draft_id):
    if not draft_id:
        return None
    row = store.db.execute('SELECT category,note FROM transfer_purposes WHERE draft_id=?', (draft_id,)).fetchone()
    return {'category': row['category'], 'note': row['note']} if row else None


def get_purposes(store, draft_id, count):
    """The chosen purpose of each action (None where the customer has not picked one)."""
    result = [get_purpose(store, draft_id)] + [None] * (max(count, 1) - 1)
    if draft_id and count > 1:
        for row in store.db.execute('SELECT action_index,category,note FROM action_purposes WHERE draft_id=? AND action_index<?', (draft_id, count)):
            result[row['action_index']] = {'category': row['category'], 'note': row['note']}
    return result


def label(purpose):
    if not purpose:
        return DEFAULT
    return f"Others · {purpose['note']}" if purpose['category'] == 'Others' else purpose['category']
