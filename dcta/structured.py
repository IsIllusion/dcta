"""Provider-neutral structured interpretation contract. This module makes no API calls."""
from pathlib import Path
from typing import Protocol

from .dialogue import Plan, validate_plan
from .domain import DomainError, canonical, strict_json
from .interaction import PUBLIC_MESSAGES, business_hint

PROMPT_VERSION = "business-agent-v13"
CONTRACT_VERSION = "intent-plan-v3"
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "fields", "selection"],
    "properties": {
        "kind": {"type": "string", "enum": ["start", "patch", "cancel", "balance", "estimate", "explain", "unsupported", "select", "confirm", "clarify", "batch", "history", "payees", *PUBLIC_MESSAGES]},
        "fields": {"type": "object", "additionalProperties": False,
                   "properties": {key: {"type": "string", "maxLength": 150} for key in ("amount", "currency", "source_query", "payee_query", "keep_at_least", "target")}},
        "selection": {"type": "string", "maxLength": 150},
        "actions": {"type": "array", "maxItems": 5, "items": {"type": "object", "additionalProperties": False,
                    "properties": {key: {"type": "string", "maxLength": 150} for key in ("amount", "currency", "source_query", "payee_query")}}},
    },
}
READ_ONLY_KINDS = {"balance", "history", "payees"}  # public kinds stay strict: fields there signal confusion or injection
ROUTE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['route'],
                'properties': {'route': {'type': 'string', 'enum': ['business', *PUBLIC_MESSAGES]}}}


class Completion(Protocol):
    """Implement with the chosen provider; timeout and retry are transport duties."""
    name: str
    def complete(self, system: str, user: str, schema: dict) -> str: ...


def parse_plan(raw):
    result = strict_json(raw)
    if type(result) is not dict or set(result) not in ({"kind", "fields", "selection"}, {"kind", "fields", "selection", "actions"}):
        raise DomainError("MODEL_CONTRACT", "Model result must match the exact intent-plan schema.")
    # Read-only answers never change state, so stray model fields are dropped instead of failing the turn.
    allowed = {"balance": {"source_query"}, "history": {"payee_query"}}
    if result.get("kind") in READ_ONLY_KINDS and type(result.get("fields")) is dict:
        result = {**result, "fields": {k: v for k, v in result["fields"].items() if k in allowed.get(result["kind"], set())}, "selection": ""}
        result.pop("actions", None)
    return validate_plan(Plan(**result))


class StructuredInterpreter:
    def __init__(self, completion):
        self.completion = completion
        self.name = f"structured/{completion.name}/{PROMPT_VERSION}"
        self.system_prompt = Path(__file__).with_name("prompts").joinpath("business_agent.txt").read_text(encoding="utf-8")
        self.router_prompt = Path(__file__).with_name('prompts').joinpath('public_router.txt').read_text(encoding='utf-8')
        self._local = None
        self._stage = 'business'

    def record_local(self, kind):
        self._local = kind

    def route(self, text, pending):
        self._local = None
        self._stage = 'business'
        if hasattr(self.completion, 'begin_budget'):
            self.completion.begin_budget()
        if business_hint(text):
            return None
        self._stage = 'public-router'
        # No history, balances, candidates, draft, account identifiers or database tools.
        payload = {'current_user_input': text, 'pending_field': pending if pending in ('amount','currency','source','payee','payee_query') else None}
        result = strict_json(self.completion.complete(self.router_prompt, canonical(payload), ROUTE_SCHEMA))
        if type(result) is not dict or set(result) != {'route'} or type(result['route']) is not str or result['route'] not in ('business', *PUBLIC_MESSAGES):
            raise DomainError('MODEL_CONTRACT', 'Invalid public conversation route.')
        return None if result['route'] == 'business' else result['route']

    def interpret(self, text, context):
        self._local = None
        self._stage = 'business'
        # One bounded completion per user turn; no model-driven recursive repair loop.
        raw = self.completion.complete(self.system_prompt, canonical({
            "current_user_input": text, "untrusted_conversation_evidence": context,
        }), PLAN_SCHEMA)
        return parse_plan(raw)

    @property
    def last_metadata(self):
        if self._local:
            modes = {'payee_reply': 'LOCAL_PAYEE_QUERY', 'field_reply': 'LOCAL_FIELD_REPLY', 'saved_bill': 'LOCAL_SAVED_BILL'}
            return {'status': 'OK', 'mode': modes.get(self._local, 'LOCAL_PUBLIC_RESPONSE'), 'route': self._local, 'api_calls': 0}
        return {**dict(getattr(self.completion, "last_metadata", {})), 'stage': self._stage}
