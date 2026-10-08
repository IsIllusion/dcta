"""Spoken-amount check (CV14 D): catch "fifteen" vs "fifty" before review.

Speech recognition most often confuses the -teen and -ty numbers (13-19 vs 30-90) and their
hundreds/thousands. When a draft amount came from a VOICE input and has that shape, the host asks
the user to confirm the exact figure once. Review and authorization stay locked until they do.
The confirmation is an append-only audit event bound to (draft, version, action, amount), so any
new draft version asks again. Typed amounts are never questioned.
"""
from .domain import DomainError, format_money

EVENT = 'SPOKEN_AMOUNT_CONFIRMED'
ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
TENS = {2: 'twenty', 3: 'thirty', 4: 'forty', 5: 'fifty', 6: 'sixty', 7: 'seventy', 8: 'eighty', 9: 'ninety'}
DIGITS = 'zero one two three four five six seven eight nine'.split()


def confusable(amount_minor):
    """True for whole amounts whose significant digits are 13-19 or 3-9 followed by zeros: 15, 150, 1,500, 50, 500, 5,000."""
    if type(amount_minor) is not int or amount_minor <= 0 or amount_minor % 100:
        return False
    digits = str(amount_minor // 100)
    core = digits.rstrip('0')
    zeros = len(digits) - len(core)
    return (core.isdigit() and 13 <= int(core) <= 19) or (len(core) == 1 and core in '3456789' and zeros >= 1)


def words(number):
    """English words for 0 <= number < 1,000,000 (enough for every mock limit)."""
    if number < 20:
        return ONES[number]
    if number < 100:
        return TENS[number // 10] + ('' if number % 10 == 0 else '-' + ONES[number % 10])
    if number < 1000:
        rest = number % 100
        return ONES[number // 100] + ' hundred' + ('' if not rest else ' and ' + words(rest))
    rest = number % 1000
    return words(number // 1000) + ' thousand' + ('' if not rest else (' and ' if rest < 100 else ' ') + words(rest))


def spoken_form(amount_minor):
    """'SGD 50.00' is read back as 'fifty (five-zero)' so the two readings cannot be mistaken."""
    major = amount_minor // 100
    return f'{words(major)} ({"-".join(DIGITS[int(d)] for d in str(major))})'


def _voice_inputs(store, session_id):
    return {row[0] for row in store.db.execute(
        'SELECT input_id FROM voice_inputs WHERE session_id=? AND input_id IS NOT NULL', (session_id,))}


def _confirmed(store, session_id, draft_id, version):
    import json
    done = set()
    for (details,) in store.db.execute('SELECT details_json FROM audit_events WHERE session_id=? AND event=?', (session_id, EVENT)):
        item = json.loads(details)
        if item.get('draft_id') == draft_id and item.get('version') == version:
            done.add((item.get('action'), item.get('amount_minor')))
    return done


def pending_checks(store, session_id, draft):
    """Voice-sourced, easily confused amounts in the current draft version that the user has not confirmed yet."""
    if not draft:
        return []
    payload, sources = draft['payload'], draft['sources']
    actions = payload.get('actions') or []
    amount_sources = [item.get('amount_minor') for item in sources['actions']] if 'actions' in sources else [sources.get('amount_minor')]
    voice = _voice_inputs(store, session_id)
    if not voice:
        return []
    done = _confirmed(store, session_id, payload['draft_id'], draft['current_version'])
    checks = []
    for index, action in enumerate(actions):
        source = amount_sources[index] if index < len(amount_sources) else None
        amount = action.get('amount_minor')
        if source in voice and confusable(amount) and (index, amount) not in done:
            checks.append({'action': index, 'amount_minor': amount, 'display': format_money(amount), 'spoken': spoken_form(amount),
                           'message': f'I heard {format_money(amount)}: {spoken_form(amount)}. Is that right?'})
    return checks


def require_confirmed(store, session_id, draft):
    checks = pending_checks(store, session_id, draft)
    if checks:
        raise DomainError('SPOKEN_AMOUNT', f"Confirm the spoken amount first: {checks[0]['message']}")


def confirm(store, user_id, session_id, draft_id, version, action):
    """Record that the user confirmed one spoken amount of the CURRENT draft version."""
    if type(draft_id) is not str or type(version) is not int or type(action) is not int:
        raise DomainError('INPUT', 'Invalid confirmation.')
    with store.transaction():
        draft = store.active(user_id, session_id)
        if not draft or draft['payload']['draft_id'] != draft_id or draft['current_version'] != version:
            raise DomainError('STALE_REVIEW', 'The draft changed. Check the new amount.')
        match = [c for c in pending_checks(store, session_id, draft) if c['action'] == action]
        if not match:
            return {'confirmed': False, 'checks': pending_checks(store, session_id, draft)}
        store.audit(session_id, EVENT, {'draft_id': draft_id, 'version': version, 'action': action, 'amount_minor': match[0]['amount_minor']})
        return {'confirmed': True, 'checks': pending_checks(store, session_id, draft)}
