"""Stable, lossless display references for existing execution UUIDs."""
import re

ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'


def reference_number(execution_id):
    if not isinstance(execution_id, str) or not re.fullmatch(r'execution_[0-9a-f]{32}', execution_id):
        return ''
    value = int(execution_id[10:], 16)
    encoded = ''
    for _ in range(26):
        encoded = ALPHABET[value & 31] + encoded
        value >>= 5
    return 'DCTA-' + '-'.join((encoded[:6], encoded[6:12], encoded[12:18], encoded[18:]))


def execution_from_reference(reference):
    """Decode a complete reference for an owner-scoped lookup; never an authorization."""
    compact = re.sub(r'[-\s]', '', reference).upper()
    if not compact.startswith('DCTA'):
        return None
    encoded = compact[4:]
    if len(encoded) != 26 or any(c not in ALPHABET for c in encoded):
        return None
    value = 0
    for char in encoded:
        value = (value << 5) | ALPHABET.index(char)
    if value >= 1 << 128:
        return None
    return 'execution_' + format(value, '032x')
