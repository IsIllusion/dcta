"""Strict contracts and exact money conversion; no network or database access."""
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import re

MAX_MINOR = 100_000_000  # Representation bound, NOT a payment/risk limit.
FIELDS = {"source_account_id", "payee_id", "amount_minor", "currency"}


class DomainError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def strict_json(raw):
    if not isinstance(raw, str) or len(raw) > 16_000:
        raise DomainError("INVALID_JSON", "Expected bounded JSON text.")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("Non-finite number")
    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        stack=[(result,1)]
        while stack:
            item,depth=stack.pop()
            if depth>32:raise ValueError('Excessive JSON nesting')
            if isinstance(item,dict):stack.extend((value,depth+1) for value in item.values())
            elif isinstance(item,list):stack.extend((value,depth+1) for value in item)
        canonical(result).encode('utf-8')  # Reject escaped lone surrogates before DB/HTTP encoding.
        return result
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise DomainError("INVALID_JSON", "Malformed or ambiguous JSON.") from exc


ONES = dict(zip("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(1, 20)))
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
# Words that make an amount approximate. "a couple of hundred" is never guessed; the user is asked instead.
VAGUE = re.compile(r"\b(?:couple|few|several|some|around|about|roughly|approximately|approx|maybe|ish|or so|hundreds|thousands|odd)\b|~")
MONEY_WORDS = re.compile(r"\s*\b(?:bucks?|dollars?)\b\s*$")


def number_words(text):
    """Deliberately narrow: 1..99, X hundred, X thousand; reject 'one twenty'."""
    words = text.lower().replace("-", " ").strip()
    if words in ONES:
        return str(ONES[words])
    if words in TENS:
        return str(TENS[words])
    pieces = words.split()
    if len(pieces) == 2 and pieces[0] in TENS and pieces[1] in ONES and ONES[pieces[1]] < 10:
        return str(TENS[pieces[0]] + ONES[pieces[1]])
    if len(pieces) == 2 and pieces[0] in ONES and ONES[pieces[0]] < 10 and pieces[1] in ("hundred", "thousand"):
        return str(ONES[pieces[0]] * (100 if pieces[1] == "hundred" else 1000))
    return text


def _scaled(number, factor):
    """number (digits or narrow words) x factor, exact to the cent or None."""
    digits = number_words(number)
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,3})?", digits):
        return None
    value = Decimal(digits) * factor
    return None if value != value.quantize(Decimal("0.01")) else format(value.normalize(), "f")


def colloquial_amount(value):
    """Host-side reading of everyday amounts (CV14 D). Returns plain decimal text, or the input unchanged.

    'a grand' / 'two grand' / '1.5 grand' -> 1000 / 2000 / 1500; '5k' / '1.5k' -> 5000 / 1500;
    'a hundred' / 'a thousand' -> 100 / 1000; '50 bucks' / 'fifty bucks' -> 50 (currency is NOT implied).
    Approximate wording raises AMOUNT_VAGUE so the host asks for an exact figure instead of guessing.
    """
    text = re.sub(r"\s+", " ", value.strip().lower())
    if VAGUE.search(text):
        raise DomainError("AMOUNT_VAGUE", "I need an exact amount, not an approximate one.")
    text = MONEY_WORDS.sub("", text).strip()
    text = re.sub(r"^(?:s\$|\$)\s*", "", text)
    match = re.fullmatch(r"(?:(a|one|[0-9]+(?:\.[0-9]{1,3})?|[a-z]+(?: [a-z]+)?) )?grand", text)
    if match:
        return _scaled("1" if match[1] in (None, "a", "one") else match[1], 1000) or value
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]{1,3})?) ?k", text)
    if match:
        return _scaled(match[1], 1000) or value
    if text in ("a hundred", "one hundred"):
        return "100"
    if text in ("a thousand", "one thousand"):
        return "1000"
    converted = number_words(text)
    return converted if converted != text or re.fullmatch(r"[0-9,]+(?:\.[0-9]+)?", text) else value


def amount_to_minor(value):
    """Explicit major-unit decimal text only; no rounding, floats, or scientific notation.
    Everyday forms ('a grand', '5k', '50 bucks') are converted by colloquial_amount first."""
    if type(value) is not str or len(value) > 32:
        raise DomainError("AMOUNT", "Use an explicit decimal amount with at most two decimal places.")
    value = value.strip()
    if not re.fullmatch(r"(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?", value):
        value = colloquial_amount(value)
    if not re.fullmatch(r"(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?", value):
        raise DomainError("AMOUNT", "Use an explicit positive amount with at most two decimal places.")
    minor = int(Decimal(value.replace(",", "")) * 100)
    validate_minor(minor)
    return minor


def validate_minor(value):
    if type(value) is not int or not 0 < value <= MAX_MINOR:
        raise DomainError("AMOUNT", "Amount must be a positive bounded integer in minor units.")


def format_money(minor):
    return f"SGD {minor // 100:,}.{minor % 100:02d}"


@dataclass(frozen=True)
class Proposal:
    source_account_id: str
    payee_id: str
    amount_minor: int
    currency: str

    @classmethod
    def parse(cls, value):
        if type(value) is not dict or set(value) != FIELDS:
            raise DomainError("SCHEMA", "Exactly one transfer with the four permitted financial fields is required.")
        validate_minor(value["amount_minor"])
        if value["currency"] != "SGD":
            raise DomainError("CURRENCY", "Only SGD is supported.")
        for field in ("source_account_id", "payee_id"):
            if type(value[field]) is not str or not 1 <= len(value[field]) <= 100:
                raise DomainError("SCHEMA", "Invalid object identifier.")
        return cls(**value)

    def fields(self):
        return {key: getattr(self, key) for key in sorted(FIELDS)}


def payload_for(draft_id, version, action_id, proposal):
    return {"schema_version": 1, "draft_id": draft_id, "version": version,
            "actions": [{"action_id": action_id, "type": "TRANSFER", **proposal.fields(), "fee_minor": 0}]}


MAX_BATCH_ACTIONS = 5


def parse_constraints(value):
    """Execution-relevant plan constraints. Only a per-account minimum remaining balance exists."""
    if type(value) is not list or len(value) > MAX_BATCH_ACTIONS:
        raise DomainError("SCHEMA", "Invalid plan constraints.")
    seen, result = set(), []
    for item in value:
        if type(item) is not dict or set(item) != {"type", "account_id", "amount_minor"} or item["type"] != "MIN_BALANCE":
            raise DomainError("SCHEMA", "Only a minimum-remaining-balance constraint is supported.")
        if type(item["account_id"]) is not str or not 1 <= len(item["account_id"]) <= 100 or item["account_id"] in seen:
            raise DomainError("SCHEMA", "Each constraint needs one distinct account.")
        amount = item["amount_minor"]
        if type(amount) is not int or not 0 <= amount <= MAX_MINOR:
            raise DomainError("AMOUNT", "Minimum balance must be a bounded non-negative integer in minor units.")
        seen.add(item["account_id"])
        result.append({"type": "MIN_BALANCE", "account_id": item["account_id"], "amount_minor": amount})
    return result


def is_plan(proposals, constraints):
    """Legacy schema-1 payloads stay byte-identical; anything richer is an explicit schema-2 plan."""
    return len(proposals) != 1 or bool(constraints)


def plan_payload_for(draft_id, version, actions, constraints):
    """actions: [(action_id, Proposal)] in the user's order. The whole list is authorized as one unit."""
    if not 1 <= len(actions) <= MAX_BATCH_ACTIONS:
        raise DomainError("SCHEMA", f"A plan contains 1-{MAX_BATCH_ACTIONS} transfers.")
    if len({action_id for action_id, _ in actions}) != len(actions):
        raise DomainError("SCHEMA", "Action identifiers must be unique.")
    body = [{"action_id": action_id, "type": "TRANSFER", **proposal.fields(), "fee_minor": 0} for action_id, proposal in actions]
    return {"schema_version": 2, "draft_id": draft_id, "version": version, "actions": body,
            "constraints": parse_constraints(constraints), "execution_policy": "ALL_OR_NOTHING",
            "total_minor": sum(item["amount_minor"] for item in body)}


def digest(payload):
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
