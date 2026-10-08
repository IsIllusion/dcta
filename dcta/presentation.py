"""Owner-scoped differences between immutable draft versions for explicit review."""
from .domain import format_money


def draft_changes(store, user, draft):
    if not draft or draft['current_version'] < 2:
        return None
    previous = store.draft(user, draft['payload']['draft_id'], draft['current_version'] - 1)
    return compare_payloads(store, user, previous['payload'], draft['payload'])


def compare_payloads(store, user, old, new):
    """Field differences for single transfers; per-transfer and constraint differences for plans."""
    if len(old['actions']) == 1 and len(new['actions']) == 1 and not old.get('constraints') and not new.get('constraints'):
        return _single(store, user, old, new)
    changes = []
    for index in range(max(len(old['actions']), len(new['actions']))):
        prefix = f'Transfer {index + 1} '
        if index >= len(old['actions']):
            changes.append({'label': prefix + 'added', 'before': '—', 'after': _describe(store, user, new['actions'][index])})
        elif index >= len(new['actions']):
            changes.append({'label': prefix + 'removed', 'before': _describe(store, user, old['actions'][index]), 'after': '—'})
        else:
            for item in _single(store, user, {'actions': [old['actions'][index]], 'version': old['version']},
                                {'actions': [new['actions'][index]], 'version': new['version']})['changes']:
                changes.append({**item, 'label': prefix + item['label'].lower()})
    keep = lambda payload: format_money(payload['constraints'][0]['amount_minor']) if payload.get('constraints') else 'None'
    if keep(old) != keep(new):
        changes.append({'label': 'Minimum balance', 'before': keep(old), 'after': keep(new)})
    return {'from_version': old['version'], 'to_version': new['version'], 'changes': changes}


def _describe(store, user, action):
    row = store.db.execute('SELECT name FROM payees WHERE user_id=? AND id=?', (user, action['payee_id'])).fetchone()
    return f"{format_money(action['amount_minor'])} to {row[0] if row else 'payee unavailable'}"


def _single(store, user, old, new):
    before, after = old['actions'][0], new['actions'][0]
    changes = []
    def display(field, value):
        if field in ('amount_minor', 'fee_minor'):
            return format_money(value)
        if field == 'source_account_id':
            row = store.db.execute('SELECT name FROM accounts WHERE user_id=? AND id=?', (user,value)).fetchone()
            return f'{row[0]} · {value}' if row else 'Account unavailable'
        if field == 'payee_id':
            row = store.db.execute('SELECT name,ending FROM payees WHERE user_id=? AND id=?', (user,value)).fetchone()
            return f'{row[0]} · ending {row[1]}' if row else 'Payee unavailable'
        return value
    for field, label in (('amount_minor','Amount'), ('source_account_id','From'), ('payee_id','To'), ('currency','Currency'), ('fee_minor','Fee')):
        if before[field] != after[field]:
            changes.append({'label':label, 'before':display(field,before[field]), 'after':display(field,after[field])})
    return {'from_version':old['version'], 'to_version':new['version'], 'changes':changes}
