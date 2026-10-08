"""Host-calculated amounts (CV14 C): "half of it", "the rest" / "whatever is left", "my bill".

The language models never do arithmetic. They only mark an amount with a rule token; the host works out
the number from its own data (balances, the saved biller's amount due) when the draft is made, shows the
working, and freezes the result in the draft. The independent validator marks the same tokens from the raw
words, and the host resolves them again with the same function, so both sides must arrive at the same cents.
"""
import re

from .domain import DomainError, format_money

HALF, REST, BILL = 'HALF_OF_PREVIOUS', 'REST', 'BILL_DUE'
RULES = (HALF, REST, BILL)
_SYNONYMS = {
    HALF: r'half|half of (?:it|that|this|the amount|the money|the transfer|the first|the previous)|half the amount|half of action 1|half of the first (?:one|transfer|amount)|50\s*%|fifty percent',
    REST: r'rest|the rest|what(?:ever)?(?:\'s| is)? left(?: over)?|the remaining(?: balance| money| amount)?|remaining|remainder|the remainder|all the rest|everything (?:left|else)',
    BILL: r'bill|the bill|bill due|amount due|the amount due|the full bill|full bill|bill amount|the bill amount|my bill|what i owe',
}


def rule_of(value):
    """The rule token for a relative amount, or None for an ordinary (explicit) amount."""
    if type(value) is not str:
        return None
    text = re.sub(r'\s+', ' ', value.strip().casefold()).rstrip('.')
    if text.upper() in RULES:
        return text.upper()
    for token, pattern in _SYNONYMS.items():
        if re.fullmatch(f'(?:{pattern})', text):
            return token
    return None


def resolve(items, balances, bills, keep=None):
    """Work out every rule amount in order. items: dicts with source_account_id, payee_id and either
    amount_minor or rule. keep: {'account_id', 'amount_minor'} minimum balance, or None.
    Returns (amounts, notes); notes explain each calculated amount in plain words."""
    amounts, notes = [None] * len(items), [None] * len(items)
    rests = [i for i, item in enumerate(items) if item.get('rule') == REST]
    if len(rests) > 1:
        raise DomainError('AMOUNT_RULE', 'Only one transfer can take "the rest". Say an exact amount for the others.')
    for i, item in enumerate(items):
        rule = item.get('rule')
        if rule is None:
            amounts[i] = item['amount_minor']
        elif rule == BILL:
            due = bills.get(item.get('payee_id'))
            if not due:
                raise DomainError('AMOUNT_RULE', 'That payee has no bill amount on file. Say the exact amount instead.')
            amounts[i], notes[i] = due, f'the bill due, {format_money(due)}'
        elif rule == HALF:
            if i == 0 or amounts[i - 1] is None:
                raise DomainError('AMOUNT_RULE', '"Half of it" needs an amount before it. Say the exact amount instead.')
            amounts[i] = amounts[i - 1] // 2
            exact = amounts[i - 1] % 2 == 0
            notes[i] = f'half of {format_money(amounts[i - 1])} = {format_money(amounts[i])}' + ('' if exact else ' (rounded down to the cent)')
    for i in rests:
        source = items[i].get('source_account_id')
        if source not in balances:
            raise DomainError('AMOUNT_RULE', 'Choose the account first, then I can work out what is left.')
        others = sum(a for j, a in enumerate(amounts) if j != i and items[j].get('source_account_id') == source)
        floor = keep['amount_minor'] if keep and keep.get('account_id') == source else 0
        amounts[i] = balances[source] - others - floor
        parts = [format_money(balances[source])] + [f'− {format_money(a)}' for j, a in enumerate(amounts) if j != i and items[j].get('source_account_id') == source]
        if floor:
            parts.append(f'− {format_money(floor)} to keep')
        if amounts[i] <= 0:
            raise DomainError('AMOUNT_RULE', f'Nothing would be left: {" ".join(parts)} leaves {format_money(max(amounts[i], 0))}. Nothing was drafted.')
        notes[i] = f'what is left: {" ".join(parts)} = {format_money(amounts[i])}'
    return amounts, notes
