"""Model-independent acceptance assertions, using fresh SQLite files per scenario.

Call evaluate_cases with a real or test interpreter factory. The factory is not
allowed to silently fall back to the offline grammar on provider failures.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from .dialogue import Conversation
from .domain import DomainError
from .store import Store, bootstrap, connect


def load_cases(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def seed_variant(path, variant):
    db = connect(path)
    try:
        if variant == "two_accounts":
            db.execute("INSERT INTO accounts VALUES ('A2','U1','Current','SGD',200000,'ACTIVE')")
        elif variant == "unicode_payee":
            db.execute("INSERT INTO payees VALUES ('P5','U1','陈明','2211',1)")
        elif variant:
            raise ValueError("Unknown evaluation fixture")
    finally:
        db.close()


def check_step(response, snapshot, expected, previous_action):
    failures = []
    draft = snapshot["draft"]
    actual_fields = draft["payload"]["actions"][0] if draft else {}
    kinds = expected["kind"] if isinstance(expected["kind"], list) else [expected["kind"]]
    if response.get("kind") not in kinds:
        failures.append(f"kind: expected {kinds}, got {response.get('kind')}")
    for key, value in expected.get("fields", {}).items():
        if actual_fields.get(key) != value:
            failures.append(f"{key}: expected {value!r}, got {actual_fields.get(key)!r}")
    if "version" in expected and (not draft or draft["current_version"] != expected["version"]):
        failures.append("draft version differs")
    if expected.get("no_active_draft") and draft:
        failures.append("unexpected active draft")
    if "pending" in expected and snapshot["conversation"].get("pending") != expected["pending"]:
        failures.append("clarification field differs")
    if "candidate_ids" in expected and sorted(c["id"] for c in response.get("candidates", [])) != sorted(expected["candidate_ids"]):
        failures.append("clarification candidates differ")
    if expected.get("same_action") and (not previous_action or actual_fields.get("action_id") != previous_action):
        failures.append("correction changed action identity")
    return failures, actual_fields.get("action_id", previous_action)


def evaluate_cases(factory, cases, *, repeats=1, live=False, metadata=None, progress=None):
    if type(repeats) is not int or not 1 <= repeats <= 5:
        raise ValueError("Use 1-5 explicitly selected repetitions.")
    started = datetime.now(timezone.utc).isoformat()
    records = []
    for repetition in range(repeats):
        for case in cases:
            with TemporaryDirectory(prefix="dcta-eval-") as directory:
                path = Path(directory) / "eval.sqlite"
                bootstrap(path)
                seed_variant(path, case.get("fixture"))
                store = Store(path)
                try:
                    session = store.new_session()
                    app = Conversation(store, factory())
                    before = store.accounts("U1") + store.accounts("U2")
                    previous_action, steps = None, []
                    for index, step in enumerate(case["steps"]):
                        start = time.monotonic()
                        try:
                            response = app.turn("U1", session, step["text"], f"eval-{index}")
                        except DomainError as exc:
                            response = {"kind": "ERROR", "code": exc.code}
                        elapsed = time.monotonic() - start
                        snapshot = store.snapshot("U1", session)
                        failures, previous_action = check_step(response, snapshot, step["expect"], previous_action)
                        if before != store.accounts("U1") + store.accounts("U2"):
                            failures.append("SAFETY: account balances changed")
                        if store.db.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0] != 0:
                            failures.append("SAFETY: ledger entries created")
                        # Provider/network/schema failures cannot count as successful negative tests.
                        code = response.get("code", "")
                        if code.startswith("PROVIDER") or code in ("INVALID_JSON", "MODEL_CONTRACT", "CONTEXT_LIMIT"):
                            failures.append("technical failure cannot satisfy a semantic rejection")
                        steps.append({"input": step["text"], "expected": step["expect"], "response": response,
                                      "latency_seconds": round(elapsed, 4), "status": "FAIL" if failures else "PASS", "failures": failures,
                                      "provider": getattr(app.interpreter, "last_metadata", {})})
                    records.append({"case_id": case["id"], "specs": case["specs"], "repetition": repetition + 1,
                                    "adapter": app.interpreter.name, "status": "PASS" if all(s["status"] == "PASS" for s in steps) else "FAIL", "steps": steps})
                    if progress:
                        progress(records[-1])
                finally:
                    store.close()
    steps = [s for record in records for s in record["steps"]]
    financial = [s for s in steps if s["expected"].get("fields")]
    clarification = [s for s in steps if s["expected"].get("kind") == "CLARIFY"]
    def ratio(items):
        return {"passed": sum(s["status"] == "PASS" for s in items), "total": len(items)}
    return {"timestamp_utc": started, "mode": "LIVE_MODEL" if live else "OFFLINE_BASELINE",
            "metadata": metadata or {}, "corpus_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "repetitions": repeats, "cases": {"passed": sum(r["status"] == "PASS" for r in records), "total": len(records)},
            "steps": ratio(steps), "financial_steps": ratio(financial), "clarification_steps": ratio(clarification),
            "safety_failures": sum(any(f.startswith("SAFETY:") for f in s["failures"]) for s in steps),
            "scope_note": "Draft-only selected text scenarios. Not a 100-case coverage or banking accuracy claim.", "results": records}
