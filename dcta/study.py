"""User-study mode (evaluation only). Records WHEN a participant started and finished each protocol task.

Privacy: no free text, no transcripts, no names. A row holds a participant code (P1, P2 …), a task number,
the conversation id, a server timestamp and, at the end, two counters (key presses, pointer presses) that
show whether a task was done keyboard-only. Rows go to work/study/<database name>.jsonl, beside the study
database, so they never mix with the ledger. `ux_metrics.py --study` joins them with the conversation data.
Set STUDY_ENABLED = False to remove the feature from a formal release.
"""
import json
from pathlib import Path
import re
import threading
import time

from .domain import DomainError

STUDY_ENABLED = False
PARTICIPANT = re.compile(r'P[0-9]{1,3}')
EVENTS = ('start', 'done', 'gave_up')
MAX_COUNT = 1_000_000
# Mirrors submission/ACCESSIBILITY_PROTOCOL.md. `goal` says what success looks like, so metrics can score it.
TASKS = (
    {'id': 1, 'title': 'Typed transfer with two Johns', 'prompt': 'Send SGD 50 from your savings account to John Lim (account ending 8892).', 'goal': 'receipt'},
    {'id': 2, 'title': 'Correct the amount', 'prompt': 'Start a transfer of SGD 50 from savings to Mary. Then say you meant SGD 500, and complete the transfer.', 'goal': 'receipt'},
    {'id': 3, 'title': 'High amount with security code', 'prompt': 'Send SGD 900 from savings to John Tan. Complete the extra security check with the code from the security token.', 'goal': 'receipt'},
    {'id': 4, 'title': 'Voice request', 'prompt': 'Use the microphone to send SGD 25 from savings to Mary. Check the transcript before using it.', 'goal': 'receipt'},
    {'id': 5, 'title': 'Keyboard only', 'prompt': 'Without using the mouse or touch, send SGD 30 from savings to John Tan.', 'goal': 'receipt'},
)
_lock = threading.Lock()


def display_path(db_path):
    """The study database as the facilitator would type it in PowerShell from the project folder."""
    path = Path(db_path).resolve()
    try:
        path = path.relative_to(Path(__file__).resolve().parents[1])
    except ValueError:
        pass
    return str(path).replace('/', '\\')


def log_path(db_path):
    db_path = Path(db_path)
    return db_path.parent / 'study' / f'{db_path.stem}.jsonl'


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise DomainError('INPUT', 'Counters must be small non-negative integers.')
    return value


def record(store, db_path, data):
    """Validate one study event and append it. The conversation must belong to the local user."""
    if not STUDY_ENABLED:
        raise DomainError('DISABLED', 'Study mode is disabled in this build.')
    participant, task, event = data.get('participant'), data.get('task'), data.get('event')
    if type(participant) is not str or not PARTICIPANT.fullmatch(participant):
        raise DomainError('INPUT', 'Participant code must look like P1, P2 …')
    if type(task) is not int or not 1 <= task <= len(TASKS):
        raise DomainError('INPUT', 'Unknown study task.')
    if event not in EVENTS:
        raise DomainError('INPUT', 'Unknown study event.')
    store.session('U1', data.get('session'))
    row = {'ms': int(time.time() * 1000), 'participant': participant, 'task': task, 'event': event, 'session': data['session']}
    if event != 'start':
        row.update(keys=_count(data.get('keys')), pointer=_count(data.get('pointer')))
    path = log_path(db_path)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as output:
            output.write(json.dumps(row, separators=(',', ':')) + '\n')
    return {'recorded': event, 'task': task}


def read_events(db_path):
    path = log_path(db_path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('event') in EVENTS and PARTICIPANT.fullmatch(str(row.get('participant', ''))):
            rows.append(row)
    return rows
