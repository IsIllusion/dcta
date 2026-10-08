"""Public conversation responses contain no database values or model prose."""
import re
import unicodedata

PUBLIC_MESSAGES = {
    'greeting': ('GREETING', "Hi! I can help you check your mock balance, prepare a transfer, or explain how this demo works. What would you like to do?"),
    'thanks': ('CHAT', "You're welcome! Let me know if you'd like help with your balance or a transfer."),
    'mic_test': ('CHAT', "Your message came through. You can ask a question or tell me what you'd like to do."),
    'help': ('HELP', "I can check your mock balance, show your past mock transfers, prepare up to five SGD actions to saved payees, and explain the review steps. All money here is simulated. What would you like to try?"),
    'smalltalk': ('CHAT', "I'm here to help. We can check your mock balance, prepare a transfer, or walk through the demo together."),
    'unrelated': ('GUIDANCE', "I can help with this banking demo: checking a mock balance, preparing a transfer, or explaining the steps. Which would be useful?"),
    'unclear': ('GUIDANCE', "I'm not sure what you'd like to do yet. You can ask a question, check your mock balance, or describe a transfer."),
    'restricted': ('RESTRICTED', "I can't provide internal records, credentials, or another user's information, or bypass authorization. I can explain this demo, show your own past transfers, or help with your mock balance and transfers."),
}

def normalized(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())

def local_route(text):
    """Narrow whole-message conveniences and defense in depth, not an injection firewall."""
    value = normalized(text)
    sensitive = r'\b(?:database|sql|sqlite|system\s+prompt|hidden\s+(?:prompt|instructions)|api\s*key|password|private\s+key|credentials|audit\s+logs?|all\s+users?|other\s+users?|another\s+user|developer\s+mode|admin(?:istrator)?|u2|a3)\b'
    if re.search(sensitive, value) or re.search(r'\bignore\b.{0,60}\b(?:instructions|rules|previous)\b', value):
        return 'restricted'
    plain = value.strip(' .!?,;:')
    if re.fullmatch(r'(?:hello|hi|hey)(?:[ ,.!]+(?:hello|hi|hey|there|dcta))*', plain):
        return 'greeting'
    if re.fullmatch(r'(?:good (?:morning|afternoon|evening)|thanks(?: a lot)?|thank you(?: very much)?)', plain):
        return 'greeting' if plain.startswith('good ') else 'thanks'
    if plain in ('test', 'testing', 'testing one two three', 'testing 1 2 3', 'mic test', 'microphone test', 'can you hear me', 'hello can you hear me'):
        return 'mic_test'
    if plain in ('help', 'help me', 'what can you do', 'what can i do here', 'how does this work', 'how do i use this', 'what is this demo'):
        return 'help'
    return None

def business_hint(text):
    # These words only select the existing strict interpreter; they grant no access.
    return bool(re.search(r'\b(?:transfers?|transactions?|history|receipts?|payments?|payees|contacts?|contact people|recipients|how much|saving|paid|sent|send|pay|balance|savings|account|payee|recipient|sgd|usd|dollars|amount|currency|cancel|confirm|yes|correct|make it|change|instead|use|ending|schedule|invest|bill)\b', normalized(text)))


def pending_payee_reply(text, pending):
    """A plain name answers the current payee question, never grants payment authority."""
    if pending not in ('payee', 'payee_query') or local_route(text) or business_hint(text):
        return None
    name = text.strip().removesuffix('.').strip()
    if not 1 <= len(name) <= 100:
        return None
    word = r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*"
    if not re.fullmatch(word + r'(?:\s+' + word + r'){0,4}', name):
        return None
    # Questions, negations and conjunctions need interpretation, not a literal lookup.
    reserved = {'i','me','my','we','you','your','it','this','that','the','a','an','to','from',
                'and','or','but','not','no','maybe','please','what','which','who','where','when',
                'why','how','is','are','can','could','would','should','do','does','did','know',
                'repeat','stop','idk','uh','um','hmm'}
    if any(part.casefold() in reserved for part in name.split()):
        return None
    return name


CURRENCY_REPLY = re.compile(r"(?:(?:in|use|it'?s|its|the currency is|currency)\s+)?(sgd|s\$|singapore dollars?|singapore)(?:\s+(?:please|pls))?", re.I)

_NUMBER = r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
_CURRENCY = r"SGD|USD|EUR|GBP|AUD|HKD|CNY|S\$|Singapore dollars?"


def money_reply(answer):
    """Read a whole, literal amount reply; never extract numbers from questions or mixed requests."""
    value = re.sub(r"^(?:it is|it's|the amount is|amount(?: is)?)\s+", "", answer, flags=re.I)
    value = re.sub(r"\s+(?:please|pls)$", "", value, flags=re.I)
    match = re.fullmatch(rf"(?P<currency>{_CURRENCY})\s*(?P<amount>{_NUMBER})", value, re.I)
    if not match:
        match = re.fullmatch(rf"(?P<amount>{_NUMBER})\s*(?P<currency>{_CURRENCY})", value, re.I)
    if match:
        currency = match['currency'].upper()
        if currency == 'S$' or currency.startswith('SINGAPORE DOLLAR'):
            currency = 'SGD'
        return {'amount': match['amount'], 'currency': currency}
    if re.fullmatch(_NUMBER, value):
        return {'amount': value}
    return None


def named_source(text, accounts):
    key = re.sub(r'^(?:my|the)\s+|\s+account$', '', normalized(text)).strip()
    matches = [a for a in accounts if a['status'] == 'ACTIVE' and a['currency'] == 'SGD'
               and key in (normalized(a['name']), normalized(a['name']).removesuffix('s'), normalized(a['id']))]
    return matches[0]['name'] if len(matches) == 1 else None


def saved_bill_request(text, profiles, accounts):
    """Recognize one complete saved-bill instruction using owned, active records.

    Only a literal whole-message match takes this path. Quotes, amounts, negations,
    extra actions and schedules remain with the interpreter. The host resolves BILL_DUE.
    """
    value = normalized(text).rstrip('.!').strip()
    matches = []
    for profile in profiles.values():
        if profile.get('kind') != 'BILL' or type(profile.get('due_minor')) is not int or profile['due_minor'] <= 0:
            continue
        name = normalized(profile['name'])
        aliases = {name, name.removesuffix(' bill')}
        bill = '(?:' + '|'.join(re.escape(a) for a in sorted(aliases) if a) + ')'
        match = re.fullmatch(r'(?:please )?pay (?:my |the )?' + bill + r'(?: (?:from|with|using) (.+?))?(?: please)?', value)
        if not match:
            continue
        fields = {'amount': 'BILL_DUE', 'payee_query': profile['name']}
        if match[1]:
            source = named_source(match[1], accounts)
            if source is None:
                continue
            fields['source_query'] = source
        matches.append(fields)
    return matches[0] if len(matches) == 1 else None


def pending_field_reply(text, pending, candidates=(), accounts=()):
    """Apply every explicit field in a short answer to a collecting transfer.
    Returns ('patch', fields) or ('select', name), else None. Never authorizes payment.
    """
    answer = re.sub(r"\s+", " ", (text or "").strip()).rstrip(".!").strip()
    if not answer or len(answer) > 120:
        return None
    if pending in ('amount', 'currency', 'source', 'payee', 'payee_query'):
        fields = money_reply(answer)
        combined = re.fullmatch(r'(.+?)\s+(?:from|using|with)\s+(.+)', answer, re.I)
        if fields is None and combined:
            supplied = money_reply(combined[1])
            source = named_source(combined[2], accounts)
            if supplied and source:
                fields = {**supplied, 'source_query': source}
        if fields and (pending in ('amount', 'currency') or 'currency' in fields):
            return ('patch', fields)
    if pending in ('amount', 'currency') and CURRENCY_REPLY.fullmatch(answer):
        return ('patch', {'currency': 'SGD'})
    if pending == 'source':
        key = re.sub(r"^(?:my|the|use|from)\s+|\s+account$", "", answer, flags=re.I).strip().casefold()
        for c in candidates or ():
            name = str(c.get('name', ''))
            if key and key in (name.casefold(), name.casefold().rstrip('s'), str(c.get('id', '')).casefold()):
                return ('select', name)
    return None
