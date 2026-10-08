"""Host-bound tool dispatcher; model output cannot select user/session or SQL."""
from .domain import DomainError, FIELDS, strict_json
from .observability import observed

TOOL_FIELDS = {
    "list_accounts": set(), "get_account": {"account_id"}, "get_balance": {"account_id"},
    "find_payees": {"name_query"}, "get_payee": {"payee_id"}, "get_draft": {"draft_id"},
    "create_draft": {"proposal", "sources"},
    "update_draft": {"draft_id", "expected_version", "patch", "sources"},
    "cancel_draft": {"draft_id", "expected_version"},
    # Multi-action plans: the host resolves every object first; the plan is frozen as one version.
    "create_plan": {"proposals", "sources", "constraints"},
    "update_plan": {"draft_id", "expected_version", "proposals", "sources", "constraints"},
}


class AgentTools:
    def __init__(self, store, user_id, session_id, token, max_calls=8):
        self._store, self._user, self._session, self._token = store, user_id, session_id, token
        self._remaining = max_calls

    @observed('tool', tool=True)
    def call(self, name, arguments_json):
        if self._remaining <= 0:
            raise DomainError("TOOL_BUDGET", "Tool call budget exhausted.")
        self._remaining -= 1
        if type(name) is not str or name not in TOOL_FIELDS:
            raise DomainError("TOOL_DENIED", "Tool is not available.")
        args = strict_json(arguments_json)
        if type(args) is not dict or set(args) != TOOL_FIELDS[name]:
            raise DomainError("SCHEMA", "Unexpected or missing tool fields.")
        for key in ("account_id", "payee_id", "draft_id", "name_query"):
            if key in args and (type(args[key]) is not str or not 1 <= len(args[key]) <= 100):
                raise DomainError("SCHEMA", "Invalid tool identifier/query.")
        s, u, sid = self._store, self._user, self._session
        # A short deterministic transaction, with no model/network wait inside it.
        with s.transaction():
            s.assert_turn(u, sid, self._token["input_id"], self._token["revision"])
            if name == "list_accounts":
                result = s.accounts(u)
            elif name in ("get_account", "get_balance"):
                result = s.account(u, args["account_id"])
                if name == "get_balance":
                    result = {k: result[k] for k in ("id", "currency", "balance_minor")}
            elif name == "find_payees":
                result = s.find_payees(u, args["name_query"])
            elif name == "get_payee":
                result = s.payee(u, args["payee_id"])
            elif name == "create_draft":
                result = s.save_draft(u, sid, args["proposal"], args["sources"])
            elif name == "create_plan":
                result = s.save_plan(u, sid, args["proposals"], args["sources"], args["constraints"])
            else:
                draft = s.draft(u, args["draft_id"])
                if draft["session_id"] != sid:
                    raise DomainError("NOT_FOUND", "Draft unavailable in this conversation.")
                if name == "get_draft":
                    result = draft
                elif name == "update_plan":
                    result = s.save_plan(u, sid, args["proposals"], args["sources"], args["constraints"],
                                         draft_id=args["draft_id"], expected_version=args["expected_version"])
                elif name == "cancel_draft":
                    s.cancel(u, sid, args["draft_id"], args["expected_version"])
                    result = {"state": "CANCELLED"}
                else:
                    patch = args["patch"]
                    if type(patch) is not dict or not patch or not set(patch) <= FIELDS:
                        raise DomainError("SCHEMA", "Patch contains unsupported financial fields.")
                    if type(args["sources"]) is not dict or set(args["sources"]) != set(patch):
                        raise DomainError("EVIDENCE", "Each changed field requires a source reference.")
                    if draft["payload"].get("schema_version") != 1:
                        raise DomainError("SCHEMA", "A multi-action plan must be changed as a whole plan version.")
                    old = draft["payload"]["actions"][0]
                    fields = {k: old[k] for k in FIELDS}
                    fields.update(patch)
                    sources = {**draft["sources"], **args["sources"]}
                    result = s.save_draft(u, sid, fields, sources, draft_id=args["draft_id"], expected_version=args["expected_version"])
            s.audit(sid, "TOOL_RESULT", {"tool": name, "arguments": args, "result": result}, self._token["input_id"])
            return result
