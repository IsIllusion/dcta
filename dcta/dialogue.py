"""Limited offline English grammar for exercising the domain, NOT an LLM.

A future provider can implement Interpreter.interpret. Its result is untrusted and
must pass validate_plan. Provider timeouts/retries belong in that future adapter.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Protocol
import re

from .domain import DomainError, FIELDS, MAX_BATCH_ACTIONS, amount_to_minor, canonical, format_money, number_words, colloquial_amount, ONES, TENS
from .tools import AgentTools
from .interaction import PUBLIC_MESSAGES, local_route, pending_payee_reply, pending_field_reply, saved_bill_request
from .amounts import rule_of, resolve as resolve_amounts, BILL

ACTION_FIELDS = {"amount", "currency", "source_query", "payee_query"}
# keep_at_least: a minimum remaining balance for the plan's source account (deterministically enforced).
# target: which transfer of a multi-action plan a correction refers to (ordinal or recipient name).
PLAN_FIELDS = ACTION_FIELDS | {"keep_at_least", "target"}
KINDS = {"start", "patch", "cancel", "balance", "estimate", "explain", "unsupported", "select", "confirm", "clarify", "batch", "history", "payees"} | PUBLIC_MESSAGES.keys()


@dataclass
class Plan:
    kind: str
    fields: dict = field(default_factory=dict)
    selection: str = ""
    actions: list = field(default_factory=list)


class Interpreter(Protocol):
    name: str
    def interpret(self, text: str, context: dict) -> Plan: ...


def validate_plan(plan):
    if not isinstance(plan, Plan) or type(plan.kind) is not str or plan.kind not in KINDS or type(plan.fields) is not dict:
        raise DomainError("MODEL_CONTRACT", "Invalid interpretation contract.")
    if not set(plan.fields) <= PLAN_FIELDS or any(type(v) is not str or len(v) > 150 for v in plan.fields.values()):
        raise DomainError("MODEL_CONTRACT", "Unexpected interpretation fields.")
    if type(plan.selection) is not str or len(plan.selection) > 150:
        raise DomainError("MODEL_CONTRACT", "Invalid selection.")
    if plan.kind == "balance" and not set(plan.fields) <= {"source_query"}:
        raise DomainError("MODEL_CONTRACT", "A balance question may only name an account.")
    if plan.kind == "history" and not set(plan.fields) <= {"payee_query"}:
        raise DomainError("MODEL_CONTRACT", "History questions may only name a saved payee.")
    if plan.kind not in ("start", "patch", "estimate", "batch", "history", "balance") and plan.fields:
        raise DomainError("MODEL_CONTRACT", "This intent cannot modify fields.")
    if plan.kind == "estimate" and "payee_query" in plan.fields:
        raise DomainError("MODEL_CONTRACT", "Balance estimates do not look up recipients.")
    if plan.kind != "select" and plan.selection:
        raise DomainError("MODEL_CONTRACT", "Only a selection intent may select a candidate.")
    if type(plan.actions) is not list:
        raise DomainError("MODEL_CONTRACT", "Invalid action list.")
    if plan.kind == "batch":
        if not 2 <= len(plan.actions) <= MAX_BATCH_ACTIONS:
            raise DomainError("MODEL_CONTRACT", f"A multi-transfer request contains 2-{MAX_BATCH_ACTIONS} transfers.")
        for action in plan.actions:
            if type(action) is not dict or not set(action) <= ACTION_FIELDS or any(type(v) is not str or len(v) > 150 for v in action.values()):
                raise DomainError("MODEL_CONTRACT", "Unexpected transfer fields.")
        if not set(plan.fields) <= {"keep_at_least"}:
            raise DomainError("MODEL_CONTRACT", "A multi-transfer request may only add a minimum-balance constraint.")
    elif plan.actions:
        raise DomainError("MODEL_CONTRACT", "Only a multi-transfer intent may list actions.")
    if "target" in plan.fields and plan.kind != "patch":
        raise DomainError("MODEL_CONTRACT", "Only a correction may target one transfer.")
    if "keep_at_least" in plan.fields and plan.kind not in ("start", "patch", "batch"):
        raise DomainError("MODEL_CONTRACT", "Only a transfer request may add a minimum-balance constraint.")
    return plan


def money_fields(text):
    fields = {}
    text = text.strip()
    explicit = re.search(r"\b(SGD|USD|EUR|GBP)\b|S\$|Singapore dollars", text, re.I)
    if explicit:
        token = explicit.group().upper()
        fields["currency"] = "SGD" if token in ("SGD", "S$", "SINGAPORE DOLLARS") else token
        text = text[:explicit.start()] + text[explicit.end():]
    text = re.sub(r"\b(?:dollars|bucks)\b", "", text, flags=re.I).strip()
    if text.lower() not in ("", "money", "some money", "it"):
        try:
            fields["amount"] = colloquial_amount(text)
        except DomainError:
            fields["amount"] = text  # the host asks for an exact amount when it applies the field
    return fields


def destination_text(amount_minor, payee, profile):
    """How one frozen action reads to the customer, by destination kind (CV14 C)."""
    money = format_money(amount_minor)
    if profile and profile["kind"] == "SHARES":
        price = profile.get("mock_price_minor")
        shares = f", about {amount_minor / price:.2f} shares at the mock price {format_money(price)}" if price else ""
        return f'{money} into {payee["name"]} (investment account ending {payee["ending"]}{shares})'
    if profile and profile["kind"] == "BILL":
        return f'{money} to pay your {payee["name"]} (reference ending {payee["ending"]})'
    if profile and profile["kind"] == "OWN":
        return f'{money} to your {payee["name"]} (ending {payee["ending"]})'
    return f'{money} to {payee["name"]} (ending {payee["ending"]})'


class DemoInterpreter:
    name = "offline-grammar-v1 (not a language model)"

    def interpret(self, text, context):
        public = local_route(text)
        if public:
            return Plan(public)
        text = text.strip().rstrip(".!?")
        lower = text.lower()
        if re.fullmatch(r"(?:please )?cancel(?: it| the transfer| the draft)?", lower):
            return Plan("cancel")
        if re.match(r"^(?:do not|don't|never)\s+(?:send|transfer)", lower):
            return Plan("cancel")
        if re.fullmatch(r"(?:yes|correct|confirm(?: it)?|go ahead|execute(?: it)?)", lower):
            return Plan("confirm")
        match = re.fullmatch(r"what would my balance be if i (?:sent|transferred) (.+?)(?: from (?:my )?(.+?)(?: account)?)?(?: to [\w '\u2019-]+)?", text, re.I)
        if match:
            fields = money_fields(match[1])
            if match[2]:
                fields['source_query'] = match[2]
            return Plan('estimate', fields)
        if any(token in lower for token in ("how do", "what does", "my friend", "what would", "if i", "for example")):
            return Plan("explain")
        plan = self.commerce_plan(text)
        if plan:
            return plan
        if re.search(r"\b(?:tomorrow|next week|schedule|recurring|invest|buy|shares|credit card|bill|half|whatever|the rest|remaining)\b", lower):
            return Plan("unsupported")
        plan = self.transfer_plan(text)
        if plan:
            return plan
        if re.search(r"\b(?:and|then)\s+(?:pay|transfer|send|buy|invest)\b", lower):
            return Plan("unsupported")
        match = re.fullmatch(r"(?:please )?(?:show|list|see|view|what are|what were|give me)(?: me)?(?: all| my| all my| the)* (?:past |previous |completed )?(?:transactions|transfers|payments|receipts|history)(?: (?:between me and|with|to|for) (.+?))?", text, re.I)
        if match:
            return Plan("history", {"payee_query": match[1].strip()} if match[1] else {})
        if (re.search(r"\b(?:how many|who|list|show|which|what)\b.*\b(?:payees?|contacts?|recipients?|contact people)\b", lower)
                or re.search(r"\bwho (?:can|could) i (?:send|pay|transfer)", lower)) and not re.match(r"^(?:send|transfer|pay|use|change)\b", lower):
            return Plan("payees")
        match = re.fullmatch(r"(?:please )?(?:show|check|what(?:'s| is)|how much (?:money )?(?:do )?i have in)(?: me)?(?: my| the)? (savings?|[\w ]+?)(?: account)?(?: balance)?", text, re.I)
        if match and re.search(r"\b(?:savings?|balance|account)\b", lower):
            query = re.sub(r"\s*balance$", "", match[1].strip(), flags=re.I)
            return Plan("balance", {"source_query": query} if query and query.lower() not in ("balance", "money") else {})
        if "balance" in lower and not re.match(r"^(?:send|transfer)", lower):
            return Plan("balance")
        match = re.fullmatch(r"(?:not .+?\.\s*)?(?:send it to|change (?:the )?payee to)\s+(.+?)(?:\s+instead)?", text, re.I)
        if match:
            return Plan("patch", {"payee_query": match.group(1).strip()})
        match = re.fullmatch(r"(?:use|change (?:the )?source to)\s+(?:my )?(.+?)(?: account)?", text, re.I)
        if match:
            return Plan("patch", {"source_query": match.group(1).strip()})
        match = re.fullmatch(r"(?:(?:no|sorry)[, ]+)?change the (?:transfer|payment|one) to (.+?) to (.+)", text, re.I)
        if match:
            return Plan("patch", {**money_fields(match[2]), "target": match[1].strip()})
        match = re.fullmatch(r"(?:(?:no|sorry)[, ]+)?make the (first|second|third|1st|2nd|3rd) (?:one|transfer|payment)\s+(.+)", text, re.I)
        if match:
            return Plan("patch", {**money_fields(match[2]), "target": match[1].lower()})
        match = re.fullmatch(r"(?:and |also )?(?:leave|keep) at least (.+?)(?: in (?:my |the )?.+?)?", text, re.I)
        if match:
            return Plan("patch", {"keep_at_least": money_fields(match[1]).get("amount", match[1])})
        match = re.fullmatch(r"(?:(?:no|sorry)[, ]+)?(?:make it|change (?:it|the amount) to)\s+(.+)", text, re.I)
        if match:
            return Plan("patch", money_fields(match.group(1)))
        match = re.fullmatch(r"(?:please )?(?:transfer|send)\s+(.+)", text, re.I)
        if match:
            fields = self.transfer_fields(match.group(1))
            return Plan("start", fields) if fields is not None else Plan("unsupported")
        pending = context.get("pending")
        if pending == "amount":
            return Plan("patch", money_fields(text))
        if pending == "currency":
            return Plan("patch", money_fields(text))
        if pending in ("payee", "source"):
            return Plan("select", selection=text)
        if pending == "payee_query":
            return Plan("patch", {"payee_query": text})
        return Plan("unclear")


    BILLERS = {"singtel": "Singtel bill", "sp group": "SP Group bill", "sp": "SP Group bill"}

    def commerce_plan(self, text):
        """CV14 C offline grammar: DBS shares, saved billers, the joint account and host-calculated amounts.
        Every clause must be recognised; otherwise None and the older rules (including 'unsupported') apply."""
        lower = re.sub(r"\s+", " ", text.strip().rstrip(".!?").lower())
        if not re.search(r"\b(?:buy|invest|bill|joint|half|the rest|whatever is left|what is left)\b", lower):
            return None
        if re.search(r"\b(?:shares?|stocks?|invest)\b", lower) and not re.search(r"\bdbs\b", lower):
            return None  # other markets stay unsupported as a whole
        source = None
        pattern = r"\s+(?:from|using|with) (?:my |the )?(savings?)(?: account)?\b"
        found = re.search(pattern, lower)
        if found:
            source, lower = found.group(1), re.sub(pattern, "", lower).strip()
        lower = re.sub(r"^please\s+", "", lower)
        clauses = [c.strip() for c in re.split(r"\s*,?\s+(?:and then|and|then)\s+", lower) if c.strip()]
        # A numbered list ("1. transfer 100 sgd to john lim" on its own line): each line is one action; an
        # introduction such as "from my savings, all in sgd:" gives the account and the currency for every line.
        items = [re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".!?;,").lower() for m in re.finditer(r"(?m)^\s*\d+\s*[.)]\s*(.+?)\s*$", text)]
        list_sgd = False
        if len(items) >= 2:
            if len(items) > MAX_BATCH_ACTIONS:
                return Plan("unsupported")
            intro = re.sub(r"(?m)^\s*\d+\s*[.)].*$", "", text).lower()
            list_sgd = bool(re.search(r"\bsgd\b", intro))
            clauses = [re.sub(pattern, "", item).strip() for item in items]
        if not 1 <= len(clauses) <= MAX_BATCH_ACTIONS:
            return None
        def amount(value):
            token = rule_of(value)
            return {"amount": token} if token else money_fields(value)
        def payee(value):
            return re.sub(r"^(?:my|your|the)\s+", "", value.strip())
        actions, keep, new = [], None, False
        for clause in clauses:
            m = re.fullmatch(r"(?:buy|purchase)(?: some)? dbs(?: group)? (?:shares?|stocks?) with (.+)", clause) or \
                re.fullmatch(r"(?:buy|purchase)(?: some)? (?:shares?|stocks?) (?:of|in) dbs(?: group)? with (.+)", clause) or \
                re.fullmatch(r"(?:invest|put) (.+?) (?:in|into) (?:dbs(?: group)?(?: shares?)?)", clause)
            if m:
                actions.append({**amount(m.group(1)), "payee_query": "DBS shares"}); new = True; continue
            m = re.fullmatch(r"pay (?:my |the )?(singtel|sp group|sp)(?: bill)?(?: of (.+))?", clause) or \
                re.fullmatch(r"pay (?:my |the )?bill (?:for|of|from) (singtel|sp group|sp)()", clause)
            if m:
                actions.append({**(amount(m.group(2)) if m.group(2) else {"amount": BILL}), "payee_query": self.BILLERS[m.group(1)]}); new = True; continue
            m = re.fullmatch(r"(?:send|transfer|move) (whatever is left|what is left|the rest)(?: (?:above|over) (.+?))? to (.+)", clause)
            if m:
                actions.append({"amount": "REST", "payee_query": payee(m.group(3))}); new = True
                if m.group(2):
                    keep = money_fields(m.group(2))
                    if keep.get("currency", "SGD") != "SGD" or "amount" not in keep:
                        return Plan("unsupported")
                    keep = keep["amount"]
                continue
            m = re.fullmatch(r"(?:move|send|transfer|pay) (.+?) to (.+)", clause)
            if m:
                target = payee(m.group(2))
                actions.append({**amount(m.group(1)), "payee_query": target})
                new = new or "joint" in target or bool(rule_of(m.group(1)))
                continue
            return None
        if not new:
            return None
        if source:
            for action in actions:
                action["source_query"] = source
        if list_sgd:
            for action in actions:
                if not rule_of(action.get("amount")) and "currency" not in action:
                    action["currency"] = "SGD"
        extra = {"keep_at_least": keep} if keep is not None else {}
        if len(actions) == 1:
            return Plan("start", {**actions[0], **extra})
        return Plan("batch", extra, actions=actions)

    @staticmethod
    def transfer_fields(remaining):
        """Recognized word order: amount [from source] [to payee] [from source]."""
        fields = {}
        to = re.split(r"\s+to\s+", remaining, maxsplit=1, flags=re.I)
        before = to[0]
        if len(to) == 2:
            destination = re.split(r"\s+from\s+", to[1], maxsplit=1, flags=re.I)
            fields["payee_query"] = destination[0].strip()
            if len(destination) == 2:
                fields["source_query"] = destination[1].strip()
        source = re.split(r"\s+from\s+", before, maxsplit=1, flags=re.I)
        if len(source) == 2:
            if "source_query" in fields:
                return None
            fields["source_query"] = source[1].strip()
        fields.update(money_fields(source[0]))
        return fields

    def transfer_plan(self, text):
        """Offline multi-transfer and minimum-balance grammar. Returns None when not applicable."""
        match = re.fullmatch(r"(?:please )?(?:transfer|send)\s+(.+)", text, re.I)
        if not match:
            return None
        body, keep = match.group(1), None
        constraint = re.search(r"(?:[,;]\s*|\s+)(?:but\s+|and\s+)?(?:leave|keep)\s+at\s+least\s+(.+?)(?:\s+in\s+(?:my\s+|the\s+)?[\w ]+?)?$", body, re.I)
        if constraint:
            keep = money_fields(constraint.group(1))
            if keep.get("currency", "SGD") != "SGD" or "amount" not in keep:
                return Plan("unsupported")
            keep = keep["amount"]
            body = body[:constraint.start()]
        parts = [p for p in re.split(r"\s*(?:,\s*|\s+)(?:and|then)\s+(?:(?:also\s+)?(?:transfer|send)\s+)?", body, flags=re.I) if p.strip()]
        if len(parts) == 1 and keep is None:
            return None
        if len(parts) > MAX_BATCH_ACTIONS:
            return Plan("unsupported")
        actions = []
        for part in parts:
            fields = self.transfer_fields(part)
            if fields is None or "payee_query" not in fields:
                return Plan("unsupported")
            actions.append(fields)
        extra = {"keep_at_least": keep} if keep is not None else {}
        if len(actions) == 1:
            return Plan("start", {**actions[0], **extra})
        return Plan("batch", extra, actions=actions)


from .observability import observed, emit


class Conversation:
    def __init__(self, store, interpreter=None):
        self.store = store
        self.interpreter = interpreter or DemoInterpreter()

    def turn(self, user_id, session_id, text, turn_key):
        token = self.store.begin_turn(user_id, session_id, turn_key, text)
        return self.complete_turn(user_id, session_id, text, token)

    @observed('conversation', 'complete_turn')
    def complete_turn(self, user_id, session_id, text, token):
        """Complete a host-reserved turn, including an atomically claimed voice input."""
        if "cached" in token:
            emit('conversation', 'cached', {'reused_result': True})
            return token["cached"]
        state = deepcopy(token["state"])
        self._text = text  # Raw words of this turn: a model-proposed correction target must be grounded in them.
        try:
            # No write transaction is held while interpreting or waiting on a provider.
            emit('interpretation', 'start', {'engine': 'structured' if hasattr(self.interpreter, 'route') else 'offline'})
            public = local_route(text)
            payee_reply = pending_payee_reply(text, state.get('pending')) if not public else None
            accounts = self.store.accounts(user_id) if not public else []
            # Preserve all explicit fields in compact replies such as "SGD 85.40".
            field_reply = pending_field_reply(text, state.get('pending'), state.get('candidates'), accounts) if not public and payee_reply is None else None
            bill_fields = (saved_bill_request(text, self.store.profiles(user_id), accounts)
                           if not public and payee_reply is None and field_reply is None else None)
            if public:
                if hasattr(self.interpreter, 'record_local'):
                    self.interpreter.record_local(public)
            elif payee_reply is None and field_reply is None and bill_fields is None and hasattr(self.interpreter, 'route'):
                public = self.interpreter.route(text, state.get('pending'))
            if public:
                plan = validate_plan(Plan(public))
            elif bill_fields is not None:
                self.store.model_context(user_id, session_id, token)
                plan = validate_plan(Plan('start', bill_fields))
                if hasattr(self.interpreter, 'record_local'):
                    self.interpreter.record_local('saved_bill')
            elif field_reply is not None:
                self.store.model_context(user_id, session_id, token)
                kind, value = field_reply
                plan = validate_plan(Plan('patch', value) if kind == 'patch' else Plan('select', selection=value))
                if hasattr(self.interpreter, 'record_local'):
                    self.interpreter.record_local('field_reply')
            elif payee_reply is not None:
                # Keep the same evidence/history bounds as model-interpreted turns.
                self.store.model_context(user_id, session_id, token)
                plan = validate_plan(Plan('patch', {'payee_query': payee_reply}))
                if hasattr(self.interpreter, 'record_local'):
                    self.interpreter.record_local('payee_reply')
            else:
                context = self.store.model_context(user_id, session_id, token)
                plan = validate_plan(self.interpreter.interpret(text, context))
            emit('interpretation', 'end', {'intent': plan.kind, 'route': 'local' if public or payee_reply is not None or field_reply is not None or bill_fields is not None else 'interpreter'})
            with self.store.transaction():
                self.store.assert_turn(user_id, session_id, token["input_id"], token["revision"])
                self.store.audit(session_id, "INTERPRETATION_VALIDATED", {
                    "adapter": self.interpreter.name, "kind": plan.kind,
                    "fields": plan.fields, "selection": plan.selection}, token["input_id"])
                tools = AgentTools(self.store, user_id, session_id, token)
                response, state = self.apply(user_id, session_id, token, state, plan, tools)
                response["adapter"] = self.interpreter.name
                self.store.finish_turn(user_id, session_id, token, state, response)
                return response
        except (DomainError, TimeoutError, ConnectionError) as exc:
            code = exc.code if isinstance(exc, DomainError) else "PROVIDER_UNAVAILABLE"
            response = {"kind": "ERROR", "code": code,
                        "message": str(exc) if isinstance(exc, DomainError) else "Interpretation unavailable; no draft change was applied.",
                        "adapter": self.interpreter.name}
            if code == "AMOUNT_RULE":
                response.update(kind="CLARIFY")  # e.g. "nothing would be left": a question back, nothing drafted
            if code == "AMOUNT_VAGUE":
                # Approximate wording is a question back, not a failure: never guess the figure.
                response.update(kind="CLARIFY", message="That amount isn't exact, so nothing changed. Tell me the exact amount, for example "
                                "\u201cSend SGD 200 from savings to Mary\u201d, or for an open draft \u201cMake it SGD 200\u201d.")
            with self.store.transaction():
                session = self.store.session(user_id, session_id)
                if session["revision"] != token["revision"]:
                    response = {"kind": "ERROR", "code": "STALE_TURN", "message": "Newer input superseded this result.", "adapter": self.interpreter.name}
                    self.store.finish_stale_turn(user_id, session_id, token, response)
                    return response
                self.store.audit(session_id, "TURN_REJECTED", {"code": code}, token["input_id"])
                self.store.finish_turn(user_id, session_id, token, token["state"], response)
            return response

    def apply(self, user_id, session_id, token, state, plan, tools):
        if plan.kind in PUBLIC_MESSAGES:
            kind, message = PUBLIC_MESSAGES[plan.kind]
            return {'kind': kind, 'message': message}, state
        call = lambda name, **args: tools.call(name, canonical(args))
        if plan.kind == "cancel":
            active = self.store.active(user_id, session_id)
            if active:
                call("cancel_draft", draft_id=active["payload"]["draft_id"], expected_version=active["current_version"])
                return {"kind": "CANCELLED", "cancelled_draft": {"draft_id": active["payload"]["draft_id"], "version": active["current_version"]},
                        "message": f"Cancelled draft v{active['current_version']}: {self.describe_draft(user_id, active)}. No money moved."}, {"cancelled": True}
            elif self.store.db.execute('SELECT 1 FROM execution_records WHERE session_id=? LIMIT 1',(session_id,)).fetchone():
                return {"kind":"ALREADY_COMPLETED","message":"The mock transfer has already settled. Cancellation cannot reverse it; check the saved receipt."}, state
            if state.get("fields") or state.get("batch"):
                return {"kind": "CANCELLED", "message": "Cancelled the transfer you were still setting up (no draft had been created yet). No money moved."}, {"cancelled": True}
            return {"kind": "CANCELLED", "message": "There was no open transfer to cancel. Nothing changed and no money moved."}, {"cancelled": True}
        if plan.kind == "confirm":
            return {"kind": "INFO", "message": "Chat confirmation cannot authorize a payment. Use Review draft in the GUI for independent validation and explicit mock authorization."}, state
        if plan.kind == "balance":
            accounts = call("list_accounts")
            query = re.sub(r"^(?:my|the)\s+|\s+account$", "", plan.fields.get("source_query", ""), flags=re.I).strip().casefold().rstrip("s")
            chosen = [a for a in accounts if query and (a["name"].casefold().rstrip("s") == query or a["id"].casefold() == query)] if query else accounts
            note = "" if chosen or not query else f'No account is named "{plan.fields["source_query"]}". Your accounts: '
            shown = chosen or accounts
            return {"kind": "BALANCE", "message": note + "; ".join(f'{a["name"]}: {a["currency"]} {a["balance_minor"]//100:,}.{a["balance_minor"]%100:02d}' for a in shown), "accounts": shown}, state
        if plan.kind == "payees":
            payees = self.store.saved_payees(user_id)
            profiles = self.store.profiles(user_id)
            if not payees and not profiles:
                return {"kind": "PAYEES", "payees": [], "message": "You have no saved payees yet. Adding payees is not supported in this demo."}, state
            sections = ["Here are the destinations available."]
            groups = [("Transfers", payees, "account")]
            for kind, title, label in (("OWN", "Own accounts", "account"), ("SHARES", "Investment", "investment account"), ("BILL", "Bills", "reference")):
                groups.append((title, [p for p in profiles.values() if p['kind'] == kind], label))
            for title, destinations, label in groups:
                if destinations:
                    sections.append(title + "\n" + "\n".join(f'• {p["name"]} · {label} ending {p["ending"]}' for p in destinations))
            if payees:
                sections.append(f'Example request: “Send SGD 20 from savings to {payees[-1]["name"]}”.')
            return {"kind": "PAYEES", "payees": payees, "message": "\n\n".join(sections)}, state
        if plan.kind == "history":
            return self.history(user_id, plan.fields.get("payee_query")), state
        if plan.kind == "explain":
            return {"kind": "INFO", "message": "A transfer draft needs an explicit amount, currency, source account, and saved payee. Quoted or hypothetical requests do not create a draft."}, state
        if plan.kind == "estimate":
            # Query fields are intentionally separate from the collecting/active draft.
            fields = plan.fields
            if not fields.get('amount') or not fields.get('currency'):
                return {'kind':'INFO', 'message':'For a hypothetical balance, ask again with an exact amount and explicit SGD currency. Your transfer details have not changed.'}, state
            if fields['currency'] != 'SGD':
                return {'kind':'INFO', 'message':'Hypothetical balances currently support SGD only. No currency conversion or transfer was made.'}, state
            amount = amount_to_minor(fields['amount'])
            accounts = [a for a in call('list_accounts') if a['status']=='ACTIVE' and a['currency']=='SGD']
            query = fields.get('source_query', '').strip().casefold()
            if query:
                accounts = [a for a in accounts if query in (a['id'].casefold(), a['name'].casefold())]
            if len(accounts) != 1:
                return {'kind':'INFO', 'message':'Ask the hypothetical question again with the exact name of an active SGD account. No account was selected and no transfer was prepared.'}, state
            account = accounts[0]
            remaining = account['balance_minor'] - amount
            result = {'currency':'SGD', 'source_name':account['name'], 'balance_minor':account['balance_minor'],
                      'amount_minor':amount, 'fee_minor':0, 'remaining_minor':remaining}
            display = ('-' if remaining < 0 else '') + format_money(abs(remaining))
            note = ' This exceeds the available mock balance.' if remaining < 0 else ''
            return {'kind':'ESTIMATE', 'estimate':result, 'message':f"Hypothetical only: {account['name']} would have {display} after subtracting {format_money(amount)} from {format_money(account['balance_minor'])}, with the current mock fee of SGD 0.00.{note} This is not a payment approval. No transfer was created or changed."}, state
        if plan.kind == "unsupported":
            said = getattr(self, "_text", "").casefold()
            if re.search(r"\b(?:should i|recommend|advice|advise|best|good (?:stock|share|investment)|worth buying)\b", said) and re.search(r"\b(?:shares?|stocks?|invest\w*|funds?|buy)\b", said):
                return {"kind": "UNSUPPORTED", "message": "I can't give investment advice or recommend shares. If you decide yourself, I can prepare a mock purchase of DBS shares, for example “Invest SGD 200 from savings in DBS shares”. Nothing was drafted. Your draft hasn't changed."}, state
            if re.search(r"\b(?:shares?|stocks?|invest\w*|funds?)\b", said) and not re.search(r"\bdbs\b", said):
                return {"kind": "UNSUPPORTED", "message": "Only DBS shares are available in this demo, so nothing from this request was drafted. You can say, for example, “Buy DBS shares with SGD 100 from savings”. Your draft hasn't changed."}, state
            return {"kind": "UNSUPPORTED", "message": "I can prepare up to five immediate SGD actions: transfers to saved payees or your joint account, buying DBS shares, and paying your saved Singtel or SP Group bill (optionally keeping a minimum balance). This request needs something the demo doesn't support yet, so nothing from it was drafted. Your draft hasn't changed."}, state
        if plan.kind == "clarify":
            pending = state.get('pending')
            if pending == 'payee_query' and state.get('payee_query'):
                return self.missing_payee(state['payee_query']), state
            prompts = {
                'amount': 'What exact amount should the draft contain?',
                'currency': 'Which currency? This prototype supports SGD. Please say SGD explicitly.',
                'source': 'Choose an active SGD source account explicitly.',
                'payee': 'Choose the saved payee by exact name or account ending.',
                'payee_query': 'Which saved payee would you like to transfer to?',
            }
            index = state.get('pending_action')
            if index is not None and 'batch' in state and index < len(state['batch']):
                label = self._label(index, state['batch'][index])
                prompts.update(amount=f'What exact amount for {label}?', currency=f'Which currency for {label}? This prototype supports SGD. Please say SGD explicitly.',
                               source=f'Which account should {label} come from? Choose one of your SGD accounts.')
            if pending in prompts:
                response = {'kind': 'CLARIFY', 'message': prompts[pending]}
                if pending in ('source', 'payee'):
                    response['candidates'] = state.get('candidates', [])
                return response, state
            if not self.store.active(user_id, session_id):
                return {'kind': 'CLARIFY', 'message': 'To start a transfer, tell me the amount, currency, source account and saved payee.'}, state
            # A bare correction ("make it 10") on a draft with several transfers: ask which one, never guess.
            actions = (self.store.active(user_id, session_id) or {}).get("payload", {}).get("actions") or []
            said = getattr(self, "_text", "").casefold()
            if len(actions) > 1 and re.search(r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|hundred|thousand)\b", said):
                return {"kind": "CLARIFY", "message": "This draft has several transfers. Which one should change? Say the recipient's name or 'the first/second transfer'. Your draft hasn't changed."}, state
            return {"kind": "CLARIFY", "message": "I'd like to get this right. What would you like to change about the transfer: the amount, source account, or saved payee? Your draft hasn't changed."}, state
        active_plan = self.store.active(user_id, session_id)
        in_plan = "batch" in state or bool(active_plan and active_plan["payload"].get("schema_version") == 2)
        relative = bool(rule_of(plan.fields.get("amount"))) or any(rule_of(a.get("amount")) for a in plan.actions)
        wants_plan = plan.kind == "batch" or relative or ({"keep_at_least", "target"} & set(plan.fields) and plan.kind in ("start", "patch"))
        if wants_plan or (in_plan and plan.kind in ("patch", "select")):
            return self.apply_plan(user_id, session_id, token, state, plan, call, active_plan)
        if plan.kind == "start":
            existing = self.store.active(user_id, session_id)
            if existing:
                return {"kind": "CLARIFY", "message": f"You already have draft v{existing['current_version']}: {self.describe_draft(user_id, existing)}. Change it (for example “make it SGD 50”) or say “cancel” first; your new request was not applied."}, state
            state = {"fields": {}, "sources": {}}
        if plan.kind == "patch" and "fields" not in state:
            return {"kind": "CLARIFY", "message": "Start a new explicit transfer request first."}, state
        fields, sources = state.setdefault("fields", {}), state.setdefault("sources", {})
        input_id = token["input_id"]
        if plan.kind == "select":
            pending = state.get("pending")
            if pending not in ("source", "payee"):
                raise DomainError("SELECTION", "No current selection is pending.")
            candidates = state.get("candidates", [])
            selection = plan.selection.lower().strip()
            ending = re.fullmatch(r"(?:the one )?ending(?: in)?\s+(\d{4})", selection)
            matches = [c for c in candidates if selection in (c["name"].lower(), c["id"].lower()) or
                       (ending and c.get("ending") == ending.group(1))]
            if len(matches) != 1:
                return {"kind": "CLARIFY", "message": "Choose one of the displayed candidates by exact name or ending.", "candidates": candidates}, state
            key = "payee_id" if pending == "payee" else "source_account_id"
            fields[key], sources[key] = matches[0]["id"], input_id
        for key, value in plan.fields.items():
            if key == "amount":
                fields["amount_minor"] = amount_to_minor(value)
                sources["amount_minor"] = input_id
            elif key == "currency":
                if value.upper() != "SGD":
                    raise DomainError("CURRENCY", "Only SGD is supported. No currency conversion was applied.")
                fields["currency"], sources["currency"] = "SGD", input_id
            elif key == "payee_query":
                state["payee_query"] = value
                state["payee_query_input"] = input_id
                fields.pop("payee_id", None)
                sources.pop("payee_id", None)
            elif key == "source_query":
                state["source_query"] = re.sub(r"^(?:my|the)\s+|\s+account$", "", value, flags=re.I).strip()
                state["source_query_input"] = input_id
                fields.pop("source_account_id", None)
                sources.pop("source_account_id", None)
        if "amount_minor" not in fields:
            state["pending"] = "amount"
            return {"kind": "CLARIFY", "message": "What exact amount should the draft contain?"}, state
        if "currency" not in fields:
            state["pending"] = "currency"
            return {"kind": "CLARIFY", "message": "Which currency? This prototype supports SGD. Please say SGD explicitly."}, state
        if "source_account_id" not in fields:
            accounts = [a for a in call("list_accounts") if a["status"] == "ACTIVE" and a["currency"] == "SGD"]
            query = state.get("source_query", "").lower()
            matches = [a for a in accounts if query and query in (a["name"].lower(), a["id"].lower())]
            if len(matches) == 1:
                fields["source_account_id"] = matches[0]["id"]
                sources["source_account_id"] = state["source_query_input"]
            else:
                state["pending"], state["candidates"] = "source", accounts
                return {"kind": "CLARIFY", "message": "Which account should the money come from? Your SGD accounts: " + ", ".join(a["name"] for a in accounts) + ".", "candidates": accounts}, state
        if "payee_id" not in fields:
            query = state.get("payee_query")
            payees = call("find_payees", name_query=query) if query else []
            if len(payees) == 1:
                fields["payee_id"], sources["payee_id"] = payees[0]["id"], state["payee_query_input"]
            else:
                state["pending"], state["candidates"] = "payee", payees
                if not payees:
                    # Next reply must be able to initiate another lookup, not select an empty list.
                    state["pending"] = "payee_query"
                if query and not payees:
                    return self.missing_payee(query), state
                return {"kind": "CLARIFY", "message": "Which saved payee?" if not payees else f'More than one saved payee matches "{query}": ' + ", ".join(f'{p["name"]} (ending {p["ending"]})' for p in payees) + ". Which one?", "candidates": payees}, state
        active = self.store.active(user_id, session_id)
        if active:
            old = active["payload"]["actions"][0]
            patch = {k: fields[k] for k in FIELDS if fields[k] != old[k]}
            if patch:
                draft = call("update_draft", draft_id=active["payload"]["draft_id"], expected_version=active["current_version"],
                             patch=patch, sources={k: sources[k] for k in patch})
            else:
                draft = active
        else:
            draft = call("create_draft", proposal=fields, sources=sources)
        state.pop("pending", None)
        state.pop("candidates", None)
        state["draft_id"] = draft["payload"]["draft_id"]
        return {"kind": "DRAFT", "message": f'Draft v{draft["current_version"]}: {self.describe_draft(user_id, draft)}. Review it before anything is sent. No money moved.', "draft": draft}, state

    def describe_draft(self, user_id, draft):
        """Plain summary of every frozen transfer: amount, recipient, account ending and source."""
        parts, profiles = [], self.store.profiles(user_id)
        for action in draft["payload"]["actions"]:
            payee = self.store.payee(user_id, action["payee_id"]); source = self.store.account(user_id, action["source_account_id"])
            parts.append(destination_text(action["amount_minor"], payee, profiles.get(payee["id"])) + f' from {source["name"]}')
        keep = draft["payload"].get("constraints") or []
        kinds = {(profiles.get(a["payee_id"]) or {}).get("kind") for a in draft["payload"]["actions"]}
        noun = "transfers" if kinds <= {None, "OWN"} else "payments"
        text = parts[0] if len(parts) == 1 else f"{len(parts)} {noun} — " + "; ".join(parts)
        return text + (f", keeping at least {format_money(keep[0]['amount_minor'])}" if keep else "")

    # ---- Multi-action plans (CV13) -------------------------------------------------------------
    ORDINALS = {"first": 0, "1st": 0, "1": 0, "one": 0, "second": 1, "2nd": 1, "2": 1, "two": 1, "third": 2, "3rd": 2, "3": 2, "three": 2,
                "fourth": 3, "4th": 3, "4": 3, "four": 3, "fifth": 4, "5th": 4, "5": 4, "five": 4}

    @staticmethod
    def _slot_fields(slot, values, input_id):
        fields, sources = slot.setdefault("fields", {}), slot.setdefault("sources", {})
        for key, value in values.items():
            if key == "amount" and rule_of(value):
                # CV14 C: "half of it", "the rest", "my bill" -> the host calculates when the draft is made.
                slot["amount_rule"], slot["amount_rule_input"] = rule_of(value), input_id
                fields.pop("amount_minor", None); sources.pop("amount_minor", None)
                fields.setdefault("currency", "SGD"); sources.setdefault("currency", input_id)
            elif key == "amount":
                slot.pop("amount_rule", None); slot.pop("amount_rule_input", None)
                fields["amount_minor"], sources["amount_minor"] = amount_to_minor(value), input_id
            elif key == "currency":
                if value.upper() != "SGD":
                    raise DomainError("CURRENCY", "Only SGD is supported. No currency conversion was applied.")
                fields["currency"], sources["currency"] = "SGD", input_id
            elif key == "payee_query":
                slot["payee_query"], slot["payee_query_input"] = value, input_id
                slot.pop("payee_name", None)
                fields.pop("payee_id", None); sources.pop("payee_id", None)
            elif key == "source_query":
                slot["source_query"] = re.sub(r"^(?:my|the)\s+|\s+account$", "", value, flags=re.I).strip()
                slot["source_query_input"] = input_id
                fields.pop("source_account_id", None); sources.pop("source_account_id", None)

    @staticmethod
    def _label(index, slot):
        name = slot.get("payee_name") or slot.get("payee_query")
        return f"transfer {index + 1}" + (f" (to {name})" if name else "")

    def _target(self, slots, target):
        key = target.strip().casefold()
        key = re.sub(r"^(?:the\s+)?(?:transfer|payment|one)\s+(?:to\s+)?", "", key).strip()
        if key in self.ORDINALS:
            index = self.ORDINALS[key]
            return index if index < len(slots) else None
        matches = [i for i, slot in enumerate(slots)
                   if key and (key in (slot.get("payee_name") or "").casefold() or key == (slot.get("payee_query") or "").casefold())]
        return matches[0] if len(matches) == 1 else None

    ORDINAL_WORDS = {0: ("first", "1st", "transfer 1", "number 1", "#1"), 1: ("second", "2nd", "transfer 2", "number 2", "#2"),
                     2: ("third", "3rd", "transfer 3", "number 3", "#3")}

    def _grounded(self, target):
        """True only if the user actually said which transfer: the model may not pick one on its own."""
        text = " " + re.sub(r"\s+", " ", getattr(self, "_text", "").casefold()) + " "
        key = re.sub(r"^(?:the\s+)?(?:transfer|payment|one)\s+(?:to\s+)?", "", target.strip().casefold()).strip()
        word = lambda w: re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text)
        if key in self.ORDINALS:
            return any(word(w) for w in self.ORDINAL_WORDS[self.ORDINALS[key]])
        return bool(key) and (bool(word(key)) or any(len(part) >= 3 and word(part) for part in key.split()))

    def _slots_from(self, user_id, state, active):
        if active and active["payload"].get("schema_version") == 2:
            slots = []
            for action, sources in zip(active["payload"]["actions"], active["sources"]["actions"]):
                slots.append({"fields": {k: action[k] for k in FIELDS}, "sources": dict(sources)})
            state = {"batch": slots}
            for slot in slots:
                slot["payee_name"] = self.store.payee(user_id, slot["fields"]["payee_id"])["name"]
            constraints = active["payload"]["constraints"]
            if constraints:
                state["keep_at_least_minor"] = constraints[0]["amount_minor"]
                state["keep_input"] = active["sources"]["constraints"][0]
            return state
        if active:
            action = active["payload"]["actions"][0]
            return {"batch": [{"fields": {k: action[k] for k in FIELDS}, "sources": dict(active["sources"]),
                               "payee_name": self.store.payee(user_id, action["payee_id"])["name"]}]}
        if "fields" in state:
            slot = {"fields": dict(state["fields"]), "sources": dict(state.get("sources", {}))}
            for key in ("payee_query", "payee_query_input", "source_query", "source_query_input"):
                if key in state:
                    slot[key] = state[key]
            return {"batch": [slot]}
        return None

    def apply_plan(self, user_id, session_id, token, state, plan, call, active):
        """Collect, clarify and freeze an ordered plan of 1-3 transfers. The host resolves every object."""
        input_id = token["input_id"]
        fields = dict(plan.fields)
        keep = fields.pop("keep_at_least", None)
        target = fields.pop("target", None)
        if target is not None and not self._grounded(target):
            target = None  # e.g. "make it 10" with two transfers: ask instead of trusting a guessed target
        if plan.kind in ("batch", "start"):
            if active:
                return {"kind": "CLARIFY", "message": f"You already have draft v{active['current_version']}: {self.describe_draft(user_id, active)}. Change it or say “cancel” first; your new request was not applied."}, state
            raw = [dict(a) for a in plan.actions] if plan.kind == "batch" else [fields]
            # A currency or source stated once in the same sentence applies to its sibling transfers.
            for key in ("currency", "source_query"):
                values = {a[key] for a in raw if a.get(key)}
                if len(values) == 1:
                    for a in raw:
                        a.setdefault(key, next(iter(values)))
            state = {"batch": []}
            for a in raw:
                slot = {"fields": {}, "sources": {}}
                self._slot_fields(slot, a, input_id)
                state["batch"].append(slot)
        else:
            if "batch" not in state:
                converted = self._slots_from(user_id, state, active)
                if converted is None:
                    return {"kind": "CLARIFY", "message": "Start a new explicit transfer request first."}, state
                state = {**{k: v for k, v in state.items() if k not in ("fields", "sources")}, **converted}
            slots = state["batch"]
            pending, index = state.get("pending"), state.get("pending_action")
            if plan.kind == "select":
                if pending not in ("source", "payee") or index is None:
                    raise DomainError("SELECTION", "No current selection is pending.")
                candidates = state.get("candidates", [])
                selection = plan.selection.lower().strip()
                ending = re.fullmatch(r"(?:the one )?ending(?: in)?\s+(\d{4})", selection)
                matches = [c for c in candidates if selection in (c["name"].lower(), c["id"].lower()) or
                           (ending and c.get("ending") == ending.group(1))]
                if len(matches) != 1:
                    return {"kind": "CLARIFY", "message": "Choose one of the displayed candidates by exact name or ending.", "candidates": candidates}, state
                slot = slots[index]
                key = "payee_id" if pending == "payee" else "source_account_id"
                slot["fields"][key], slot["sources"][key] = matches[0]["id"], input_id
                if pending == "payee":
                    slot["payee_name"] = matches[0]["name"]
                else:
                    # An account chosen for one transfer also covers the others that named no account at all.
                    for other in slots:
                        if "source_account_id" not in other["fields"] and not other.get("source_query"):
                            other["fields"]["source_account_id"], other["sources"]["source_account_id"] = matches[0]["id"], input_id
            elif fields:
                if target is not None:
                    index = self._target(slots, target)
                    if index is None:
                        return {"kind": "CLARIFY", "message": "Which transfer should change? Say the recipient's name or 'the first/second transfer'. Your draft hasn't changed."}, state
                elif not (index is not None and pending in ("amount", "currency", "payee_query", "payee", "source")):
                    if len(slots) != 1:
                        return {"kind": "CLARIFY", "message": "This draft has several transfers. Which one should change? Say the recipient's name or 'the first/second transfer'. Your draft hasn't changed."}, state
                    index = 0
                self._slot_fields(slots[index], fields, input_id)
            elif target is not None and keep is None:
                return {"kind": "CLARIFY", "message": "What should change for that transfer? Your draft hasn't changed."}, state
        if keep is not None:
            state["keep_at_least_minor"], state["keep_input"] = amount_to_minor(keep) if keep.strip() not in ("0", "0.00") else 0, input_id
        state.pop("pending", None); state.pop("candidates", None); state.pop("pending_action", None)
        slots, accounts = state["batch"], None
        for i, slot in enumerate(slots):
            f, src = slot["fields"], slot["sources"]
            label = self._label(i, slot)
            if "amount_minor" not in f and not slot.get("amount_rule"):
                state["pending"], state["pending_action"] = "amount", i
                return {"kind": "CLARIFY", "message": f"What exact amount for {label}?"}, state
            if "currency" not in f:
                state["pending"], state["pending_action"] = "currency", i
                return {"kind": "CLARIFY", "message": f"Which currency for {label}? This prototype supports SGD. Please say SGD explicitly."}, state
            if "source_account_id" not in f:
                if accounts is None:
                    accounts = [a for a in call("list_accounts") if a["status"] == "ACTIVE" and a["currency"] == "SGD"]
                query = slot.get("source_query", "").lower()
                matches = [a for a in accounts if query and query in (a["name"].lower(), a["id"].lower())]
                if len(matches) == 1:
                    f["source_account_id"], src["source_account_id"] = matches[0]["id"], slot["source_query_input"]
                else:
                    state["pending"], state["pending_action"], state["candidates"] = "source", i, accounts
                    return {"kind": "CLARIFY", "message": f"Which account should {label} come from? Your SGD accounts: " + ", ".join(a["name"] for a in accounts) + ".", "candidates": accounts}, state
            if "payee_id" not in f:
                query = slot.get("payee_query")
                payees = call("find_payees", name_query=query) if query else []
                if len(payees) == 1:
                    f["payee_id"], src["payee_id"], slot["payee_name"] = payees[0]["id"], slot["payee_query_input"], payees[0]["name"]
                else:
                    state["pending"], state["pending_action"], state["candidates"] = ("payee" if payees else "payee_query"), i, payees
                    if query and not payees:
                        response = self.missing_payee(query)
                        response["message"] = f"For transfer {i + 1}: " + response["message"]
                        return response, state
                    return {"kind": "CLARIFY", "message": f"Which saved payee for transfer {i + 1}?" if not payees else f"More than one saved payee matches {label}: " + ", ".join(f'{p["name"]} (ending {p["ending"]})' for p in payees) + ". Which one?", "candidates": payees}, state
        constraints, constraint_sources = [], []
        if state.get("keep_at_least_minor") is not None:
            used = list(dict.fromkeys(slot["fields"]["source_account_id"] for slot in slots))
            if len(used) != 1:
                return {"kind": "CLARIFY", "message": "These transfers use different accounts. Say which account must keep the minimum balance, or remove the condition. Your draft hasn't changed."}, state
            if state["keep_at_least_minor"] > 0:
                constraints = [{"type": "MIN_BALANCE", "account_id": used[0], "amount_minor": state["keep_at_least_minor"]}]
                constraint_sources = [state["keep_input"]]
        notes = []
        if any(slot.get("amount_rule") for slot in slots):
            # CV14 C: the host works out relative amounts from its own data and freezes the result.
            items = [{"rule": slot.get("amount_rule"), "amount_minor": slot["fields"].get("amount_minor"),
                      "source_account_id": slot["fields"]["source_account_id"], "payee_id": slot["fields"]["payee_id"]} for slot in slots]
            balances = {a: self.store.account(user_id, a)["balance_minor"] for a in {item["source_account_id"] for item in items}}
            bills = {pid: p.get("due_minor") for pid, p in self.store.profiles(user_id).items() if p["kind"] == "BILL"}
            amounts, worked = resolve_amounts(items, balances, bills, constraints[0] if constraints else None)
            for slot, amount, note in zip(slots, amounts, worked):
                if slot.get("amount_rule"):
                    slot["fields"]["amount_minor"], slot["sources"]["amount_minor"] = amount, slot["amount_rule_input"]
                    notes.append(note)
        proposals = [{k: slot["fields"][k] for k in FIELDS} for slot in slots]
        sources = {"actions": [{k: slot["sources"][k] for k in FIELDS} for slot in slots], "constraints": constraint_sources}
        if active:
            same = ([{k: a[k] for k in FIELDS} for a in active["payload"]["actions"]] == proposals
                    and active["payload"].get("constraints", []) == constraints)
            draft = active if same else call("update_plan", draft_id=active["payload"]["draft_id"], expected_version=active["current_version"],
                                             proposals=proposals, sources=sources, constraints=constraints)
        else:
            draft = call("create_plan", proposals=proposals, sources=sources, constraints=constraints)
        state["draft_id"] = draft["payload"]["draft_id"]
        total = sum(p["amount_minor"] for p in proposals)
        summary = self.describe_draft(user_id, draft)
        if len(proposals) > 1:
            message = f"Draft v{draft['current_version']}: {summary}; total {format_money(total)}. They are authorized and executed together or not at all. No money moved."
        else:
            message = f"Draft v{draft['current_version']}: {summary}. Review it before anything is sent. No money moved."
        if notes:
            message += " Worked out by DCTA: " + "; ".join(notes) + ". These amounts are now fixed in the draft."
        return {"kind": "DRAFT", "message": message, "draft": draft}, state

    def history(self, user_id, query):
        """Read-only, owner-scoped list of the user's own completed mock transfers; never moves or changes anything."""
        from datetime import datetime, timezone
        import json
        payees = None
        if query:
            query = re.sub(r"^(?:my|the)\s+", "", query.strip(), flags=re.I)
            payees = {p["id"]: p for p in self.store.find_payees(user_id, query)}
            if not payees:
                return {"kind": "HISTORY", "items": [], "message": f'You have no saved payee matching "{query}", so there are no transfers to show. Nothing was changed.'}
        rows = self.store.db.execute("""SELECT e.id,e.created_ms,e.receipt_json FROM execution_records e WHERE e.user_id=?
            AND NOT EXISTS (SELECT 1 FROM hidden_conversations h WHERE h.session_id=e.session_id)
            ORDER BY e.created_ms DESC,e.rowid DESC LIMIT 500""", (user_id,)).fetchall()
        items = []
        for row in rows:
            receipt = json.loads(row["receipt_json"])
            for action in receipt.get("actions") or [{"payee": receipt["payee"], "amount_minor": receipt["amount_minor"], "source": receipt["source"]}]:
                if payees is None or action["payee"]["id"] in payees:
                    items.append({"execution_id": row["id"], "created_ms": row["created_ms"], "amount_minor": action["amount_minor"],
                                  "payee": {k: action["payee"][k] for k in ("id", "name", "ending")}, "source_name": action["source"]["name"]})
        who = "" if payees is None else " to " + " or ".join(f'{p["name"]} (ending {p["ending"]})' for p in payees.values())
        if not items:
            return {"kind": "HISTORY", "items": [], "message": f"You have no settled mock transfers{who} yet. Nothing was changed."}
        total = sum(i["amount_minor"] for i in items)
        lines = [f'• {datetime.fromtimestamp(i["created_ms"] / 1000, timezone.utc):%d %b %Y %H:%M} UTC — {format_money(i["amount_minor"])} to {i["payee"]["name"]} from {i["source_name"]} (receipt …{i["execution_id"][-8:]})' for i in items[:10]]
        more = f"\n…and {len(items) - 10} earlier. Open Transfers → Receipts for the full list." if len(items) > 10 else ""
        head = f"{len(items)} settled mock transfer{'s' if len(items) != 1 else ''}{who}, total {format_money(total)}:"
        return {"kind": "HISTORY", "items": items[:10], "total_minor": total, "count": len(items),
                "message": head + "\n" + "\n".join(lines) + more + "\nRead-only: nothing was changed."}

    @staticmethod
    def missing_payee(query):
        return {'kind': 'CLARIFY', 'code': 'PAYEE_NOT_FOUND', 'candidates': [],
                'message': f'I could not find a saved payee matching "{query}". Your other transfer details are kept. Enter a different saved payee name, or say "cancel". Adding new payees is not supported here. No transfer was made.'}
