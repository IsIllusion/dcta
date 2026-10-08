"""Run with python -m dcta --demo, or python -m dcta --db work/demo.sqlite."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import uuid

from .dialogue import Conversation
from .domain import DomainError
from .store import Store, bootstrap

DEMO_TURNS = [
    "Transfer SGD 500 from savings to John.",
    "What is the available balance of my savings account?",
    "The one ending in 4521.",
    "No, make it SGD 50.",
    "Not John Tan. Send it to Mary instead.",
    "Cancel it.",
    "Yes.",
]


def run_demo():
    with TemporaryDirectory(prefix="dcta-demo-") as directory:
        db = Path(directory) / "demo.sqlite"
        bootstrap(db)
        store = Store(db)
        session = store.new_session()
        app = Conversation(store)
        events = []
        before = store.accounts("U1")
        for i, message in enumerate(DEMO_TURNS):
            events.append({"user": message, "response": app.turn("U1", session, message, f"demo-{i}")})
        snapshot = store.snapshot("U1", session)
        store.close()
        # Exercise restart using a new connection.
        store = Store(db)
        try:
            restored = store.snapshot("U1", session)
            return {"mode": "offline grammar; no LLM/ASR/authorization/execution", "turns": events,
                    "checks": {"restart_preserved_state": snapshot == restored,
                               "balances_unchanged": before == store.accounts("U1"),
                               "ledger_entries": store.db.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0],
                               "draft_versions": store.db.execute("SELECT COUNT(*) FROM draft_versions").fetchone()[0],
                               "cancelled_drafts": store.db.execute("SELECT COUNT(*) FROM transaction_drafts WHERE state='CANCELLED'").fetchone()[0]}}
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description="DCTA draft-only local foundation (mock data, offline grammar).")
    parser.add_argument("--demo", action="store_true", help="Run a disposable reproducible scenario.")
    parser.add_argument("--db", default="work/dcta.sqlite")
    parser.add_argument("--session", help="Resume the session ID printed by an earlier run.")
    parser.add_argument("--provider", choices=("offline", "deepseek"), default="offline")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--key-file", help="Local credential file; its contents are never printed.")
    args = parser.parse_args()
    if args.demo:
        if args.provider != "offline":
            parser.error("--demo is the fixed offline baseline; use run_eval.py for live acceptance.")
        print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
        return
    bootstrap(args.db)
    store = Store(args.db)
    try:
        # Fixed local mock identity. This CLI is not an authenticated network service.
        session = args.session or store.new_session("U1")
        store.session("U1", session)
        interpreter = None
        if args.provider == "deepseek":
            from .deepseek import DeepSeekCompletion, DeepSeekConfig
            from .structured import StructuredInterpreter
            interpreter = StructuredInterpreter(DeepSeekCompletion(DeepSeekConfig(model=args.model), key_file=args.key_file))
        app = Conversation(store, interpreter)
        print(f"DCTA | {app.interpreter.name} | DRAFT ONLY | MOCK DATA")
        print(f"Database: {Path(args.db).resolve()}\nSession: {session}")
        print("Use :state to inspect; exit, quit, :exit or :quit to exit. Resume with --session <ID>.")
        while True:
            try:
                message = input("You> ")
            except (EOFError, KeyboardInterrupt):
                break
            command = message.strip().casefold()
            if command in {"exit", "quit", ":exit", ":quit"}:
                break
            if command == ":state":
                print(json.dumps(store.snapshot("U1", session), ensure_ascii=False, indent=2))
                continue
            try:
                response = app.turn("U1", session, message, uuid.uuid4().hex)
                print(json.dumps(response, ensure_ascii=False, indent=2))
            except DomainError as exc:
                print(f"{exc.code}: {exc}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
