"""SQLite draft repository. Bootstrap has fixture access; runtime cannot write money."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import sqlite3
import uuid

from .domain import DomainError, FIELDS, Proposal, canonical, digest, payload_for, plan_payload_for, parse_constraints, is_plan, strict_json, MAX_BATCH_ACTIONS
from .access import CORE_READS, read_decision

CONTEXT_MAX_INPUTS = 120
CONTEXT_MAX_CHARS = 96000


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    db = sqlite3.connect(path, timeout=0.5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        db.close()
        raise RuntimeError("Foreign keys must be enabled.")
    return db


def _upgrade_purpose_choices(db, schema):
    """A purpose table created before 'Transfer' was a choice is rebuilt with the new CHECK, keeping every row."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='transfer_purposes'").fetchone()
    if not row or "'Transfer'" in row[0]:
        return
    for name in ('purpose_insert_only_while_drafted', 'purpose_update_only_while_drafted', 'purpose_no_delete'):
        db.execute(f'DROP TRIGGER IF EXISTS {name}')
    db.execute('ALTER TABLE transfer_purposes RENAME TO transfer_purposes_old')
    db.executescript(schema)
    # Rows for finished drafts must be copied as they are, so the insert guard is recreated only afterwards.
    db.execute('DROP TRIGGER IF EXISTS purpose_insert_only_while_drafted')
    db.execute('INSERT INTO transfer_purposes SELECT draft_id,category,note,updated_at FROM transfer_purposes_old')
    db.execute('DROP TABLE transfer_purposes_old')
    db.executescript(schema)


def _upgrade_action_limits(db, schema):
    """CV14.6: plans grew from 3 to 5 actions. Tables created with the old CHECK ranges are rebuilt with the new
    ones in one transaction, keeping every row (ledger links stay append-only; purposes keep their values).
    Any failure rolls back and leaves the database exactly as it was."""
    tables = {'ledger_action_links': ('BETWEEN 1 AND 3', ('immutable_action_links_update', 'immutable_action_links_delete')),
              'action_purposes': ('BETWEEN 1 AND 2', ('action_purpose_insert_only_while_drafted', 'action_purpose_update_only_while_drafted',
                                                      'action_purpose_no_delete'))}
    old = {name: spec for name, spec in tables.items()
           if (row := db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()) and spec[0] in row[0]}
    if not old:
        return
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        for name, (_, triggers) in old.items():
            create = re.search(rf'CREATE TABLE IF NOT EXISTS {name} \(.*?\) STRICT;', schema, re.S).group(0)
            for trigger in triggers:
                db.execute(f'DROP TRIGGER IF EXISTS {trigger}')
            db.execute(f'ALTER TABLE {name} RENAME TO {name}_old')
            db.execute(create)
            db.execute(f'INSERT INTO {name} SELECT * FROM {name}_old')
            db.execute(f'DROP TABLE {name}_old')
            if db.execute(f'PRAGMA foreign_key_check({name})').fetchone():
                raise RuntimeError(f'Migration foreign-key check failed for {name}.')
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')
    db.executescript(schema)  # recreates the triggers dropped above


def bootstrap(path):
    """Idempotent schema/seed. Never reset an existing database."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6):
            raise RuntimeError("Unsupported database version; explicit migration required.")
        if version in (1, 2):
            # SQLite CHECK expansion requires a table rebuild. Preserve all IDs and incoming references.
            db.execute('PRAGMA foreign_keys=OFF')
            # Triggers that read transaction_drafts would block its rebuild; schema.sql recreates them below.
            for name in ('purpose_insert_only_while_drafted', 'purpose_update_only_while_drafted',
                         'action_purpose_insert_only_while_drafted', 'action_purpose_update_only_while_drafted'):
                db.execute(f'DROP TRIGGER IF EXISTS {name}')
            try:
                db.execute('BEGIN IMMEDIATE')
                db.execute("CREATE TABLE transaction_drafts_cv3 (id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id),current_version INTEGER NOT NULL CHECK(current_version>0),state TEXT NOT NULL CHECK(state IN ('DRAFTED','CANCELLED','SUCCEEDED'))) STRICT")
                db.execute('INSERT INTO transaction_drafts_cv3 SELECT * FROM transaction_drafts')
                db.execute('DROP TABLE transaction_drafts')
                db.execute('ALTER TABLE transaction_drafts_cv3 RENAME TO transaction_drafts')
                if db.execute('PRAGMA foreign_key_check').fetchone():
                    raise RuntimeError('Migration foreign-key check failed.')
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.execute('PRAGMA foreign_keys=ON')
        if version in (1, 2, 3, 4, 5):
            # CV13: the voice-evidence trigger gains one narrow exception (recorded audio purge). Recreated below.
            db.execute("DROP TRIGGER IF EXISTS immutable_voice_evidence")
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        db.executescript(schema)
        _upgrade_purpose_choices(db, schema)
        _upgrade_action_limits(db, schema)
        db.execute("BEGIN IMMEDIATE")
        db.executemany("INSERT OR IGNORE INTO users VALUES (?,?)", [("U1", "VERIFIED"), ("U2", "VERIFIED"), ("MOCK_BANK", "VERIFIED")])
        db.executemany("INSERT OR IGNORE INTO accounts VALUES (?,?,?,?,?,?)", [
            ("A1", "U1", "Savings", "SGD", 400000, "ACTIVE"),
            ("A3", "U2", "Savings", "SGD", 250000, "ACTIVE"),
            ("CLEARING_SGD", "MOCK_BANK", "Mock outbound clearing", "SGD", 0, "ACTIVE")])
        db.execute('INSERT OR IGNORE INTO opening_balances SELECT id,balance_minor FROM accounts')
        db.executemany("INSERT OR IGNORE INTO payees VALUES (?,?,?,?,?)", [
            ("P1", "U1", "John Tan", "4521", 1), ("P2", "U1", "John Lim", "8892", 1),
            ("P3", "U1", "Mary", "7710", 1), ("P4", "U2", "Private Payee", "1100", 1),
            # CV14 C (mock): own joint account, DBS shares, two billers.
            ("P5", "U1", "Joint account", "3310", 1), ("P6", "U1", "DBS shares", "2604", 1),
            ("P7", "U1", "Singtel bill", "4410", 1), ("P8", "U1", "SP Group bill", "7021", 1)])
        db.executemany("INSERT OR IGNORE INTO payee_profiles VALUES (?,?,?)", [
            ("P5", "OWN", '{"label":"your joint account"}'),
            ("P6", "SHARES", '{"label":"DBS Group Holdings shares (SGX: D05), mock","symbol":"D05","mock_price_minor":3850}'),
            ("P7", "BILL", '{"label":"Singtel mobile bill","reference":"8812-4410","due_minor":8540}'),
            ("P8", "BILL", '{"label":"SP Group utilities bill","reference":"5530-7021","due_minor":12370}')])
        db.commit()
    finally:
        db.close()


def runtime_authorizer(action, table, column, database, trigger):
    # Defense within the local process, NOT an OS sandbox against arbitrary Python.
    if action == sqlite3.SQLITE_READ:
        return read_decision(CORE_READS, table, column, database)
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA):
        return sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
        writable = {"sessions", "user_inputs", "input_results", "transaction_drafts", "draft_versions", "draft_version_plans", "audit_events", "voice_inputs", "transfer_purposes", "action_purposes"}
        if table not in writable:
            return sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TRIGGER,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VTABLE,
                  sqlite3.SQLITE_DROP_VTABLE):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class Store:
    def __init__(self, path):
        self.db = connect(path)
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.OperationalError as exc:
            # A concurrent commit can briefly lock the schema; report BUSY instead of crashing the request.
            self.db.close()
            if "locked" in str(exc).lower():
                raise DomainError("BUSY", "Database busy; retry the same request.") from exc
            raise
        if version != 6:
            self.db.close()
            raise RuntimeError("Run bootstrap first.")
        self.db.set_authorizer(runtime_authorizer)

    def close(self):
        self.db.close()

    @contextmanager
    def read_transaction(self):
        """One consistent snapshot across all GUI/status queries; no write lock."""
        if self.db.in_transaction:
            yield
            return
        try:
            self.db.execute('BEGIN')
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            yield
            return
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.commit()
        except sqlite3.OperationalError as exc:
            self.db.rollback()
            if "locked" in str(exc).lower():
                raise DomainError("BUSY", "Database busy; retry the same turn key.") from exc
            raise
        except BaseException:
            self.db.rollback()
            raise

    def new_session(self, user_id="U1"):
        # This parameter belongs to a trusted host, never to model/tool arguments.
        if not self.db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("NOT_FOUND", "User unavailable.")
        sid = uid("session")
        with self.transaction():
            self.db.execute("INSERT INTO sessions(id,user_id) VALUES (?,?)", (sid, user_id))
        return sid

    def session(self, user_id, session_id):
        if type(session_id) is not str or not 1 <= len(session_id) <= 100:
            raise DomainError('INPUT','A bounded session identifier is required.')
        row = self.db.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Session unavailable.")
        return dict(row)

    def audit(self, session_id, event, details, input_id=None):
        self.db.execute("INSERT INTO audit_events(session_id,input_id,event,details_json,created_at) VALUES (?,?,?,?,?)",
                        (session_id, input_id, event, canonical(details), now()))

    def accounts(self, user_id):
        return [dict(r) for r in self.db.execute("SELECT id,user_id,name,currency,balance_minor,status FROM accounts WHERE user_id=? ORDER BY id", (user_id,))]

    def account(self, user_id, account_id):
        row = self.db.execute("SELECT id,user_id,name,currency,balance_minor,status FROM accounts WHERE id=? AND user_id=?", (account_id, user_id)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Account unavailable.")
        return dict(row)

    def payee(self, user_id, payee_id):
        row = self.db.execute("SELECT id,user_id,name,ending,active FROM payees WHERE id=? AND user_id=? AND active=1", (payee_id, user_id)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Payee unavailable.")
        return dict(row)

    def saved_payees(self, user_id):
        """The user's own active saved payees who are people (name and account ending only), for an explicit request."""
        return [dict(r) for r in self.db.execute("SELECT p.id,p.name,p.ending FROM payees p WHERE p.user_id=? AND p.active=1 "
                                                 "AND NOT EXISTS (SELECT 1 FROM payee_profiles f WHERE f.payee_id=p.id) ORDER BY p.name,p.id", (user_id,))]

    def profiles(self, user_id):
        """CV14 C: kind and details of the user's non-person destinations (joint account, shares, billers)."""
        rows = self.db.execute("SELECT f.payee_id,f.kind,f.detail_json,p.name,p.ending FROM payee_profiles f JOIN payees p ON p.id=f.payee_id "
                               "WHERE p.user_id=? AND p.active=1 ORDER BY p.id", (user_id,))
        return {r["payee_id"]: {"kind": r["kind"], "name": r["name"], "ending": r["ending"], **json.loads(r["detail_json"])} for r in rows}

    def find_payees(self, user_id, query):
        if type(query) is not str or not 1 <= len(query.strip()) <= 100:
            raise DomainError("SCHEMA", "A bounded payee query is required.")
        # instr treats quotes, SQL, %, and _ as literal search data.
        return [dict(r) for r in self.db.execute(
            "SELECT id,user_id,name,ending,active FROM payees WHERE user_id=? AND active=1 AND instr(lower(name),lower(?))>0 ORDER BY id",
            (user_id, query.strip()))]

    def begin_turn(self, user_id, session_id, turn_key, text):
        if type(text) is not str or not text.strip() or len(text) > 2000:
            raise DomainError("INPUT", "Enter 1-2000 characters.")
        if type(turn_key) is not str or not 1 <= len(turn_key) <= 100:
            raise DomainError("INPUT", "A bounded turn key is required.")
        try:
            text.encode('utf-8');turn_key.encode('utf-8')
        except UnicodeError:
            raise DomainError('INPUT','Input must contain valid Unicode text.') from None
        with self.transaction():
            session = self.session(user_id, session_id)
            previous = self.db.execute("SELECT * FROM user_inputs WHERE session_id=? AND turn_key=?", (session_id, turn_key)).fetchone()
            if previous:
                if previous["raw_text"] != text:
                    raise DomainError("CONFLICT", "Turn key already belongs to different input.")
                result = self.db.execute("SELECT response_json FROM input_results WHERE input_id=?", (previous["id"],)).fetchone()
                if result:
                    return {"cached": json.loads(result[0])}
                raise DomainError("PENDING", "Turn has no recorded result; inspect state and submit a new explicit turn.")
            revision = session["revision"] + 1
            input_id = uid("input")
            self.db.execute("INSERT INTO user_inputs VALUES (?,?,?,?,?,?)", (input_id, session_id, turn_key, revision, text, now()))
            self.db.execute("UPDATE sessions SET revision=? WHERE id=?", (revision, session_id))
            self.db.execute("UPDATE voice_inputs SET status='CANCELLED' WHERE session_id=? AND status IN ('PROCESSING','READY')", (session_id,))
            self.audit(session_id, "INPUT_RECORDED", {"revision": revision}, input_id)
            return {"input_id": input_id, "revision": revision, "state": json.loads(session["state_json"])}

    def assert_turn(self, user_id, session_id, input_id, revision):
        session = self.session(user_id, session_id)
        row = self.db.execute("SELECT ordinal FROM user_inputs WHERE id=? AND session_id=?", (input_id, session_id)).fetchone()
        if row is None or row["ordinal"] != revision or session["revision"] != revision:
            raise DomainError("STALE_TURN", "A newer turn exists; this result cannot modify the conversation.")
        if self.db.execute("SELECT 1 FROM input_results WHERE input_id=?", (input_id,)).fetchone():
            raise DomainError("TURN_COMPLETE", "This turn is already complete.")

    def finish_turn(self, user_id, session_id, token, state, response):
        self.assert_turn(user_id, session_id, token["input_id"], token["revision"])
        self.db.execute("UPDATE sessions SET state_json=? WHERE id=?", (canonical(state), session_id))
        self.db.execute("INSERT INTO input_results VALUES (?,?)", (token["input_id"], canonical(response)))
        self.audit(session_id, "TURN_COMPLETED", {"kind": response["kind"]}, token["input_id"])

    def finish_stale_turn(self, user_id, session_id, token, response):
        """Record a late result without changing newer conversation state."""
        session = self.session(user_id, session_id)
        row = self.db.execute("SELECT ordinal FROM user_inputs WHERE id=? AND session_id=?",
                              (token["input_id"], session_id)).fetchone()
        if row is None or row["ordinal"] != token["revision"] or session["revision"] <= token["revision"]:
            raise DomainError("STATE", "Only a superseded turn may be completed as stale.")
        self.db.execute("INSERT OR IGNORE INTO input_results VALUES (?,?)", (token["input_id"], canonical(response)))
        self.audit(session_id, "STALE_RESULT_REJECTED", {"revision": token["revision"]}, token["input_id"])

    def context_rows(self, user_id, session_id, through_revision=None):
        """Complete current-transaction evidence, bounded without silently truncating it.

        A committed execution and its audit event are atomic. Input audit order,
        rather than wall-clock timestamps or model guesses, identifies that boundary.
        Existing databases therefore gain this scope without rewriting any history.
        """
        current = self.session(user_id, session_id)
        end = current['revision'] if through_revision is None else through_revision
        after = self.db.execute("""SELECT COALESCE(MAX(i.ordinal),0)
            FROM user_inputs i JOIN audit_events a ON a.input_id=i.id AND a.session_id=i.session_id
            WHERE i.session_id=? AND a.event='INPUT_RECORDED'
              AND a.id < (SELECT MAX(id) FROM audit_events
                          WHERE session_id=? AND event='MOCK_EXECUTION_SUCCEEDED')""", (session_id, session_id)).fetchone()[0]
        rows = self.db.execute("""SELECT i.id,i.ordinal,i.raw_text,r.response_json
            FROM user_inputs i LEFT JOIN input_results r ON r.input_id=i.id
            WHERE i.session_id=? AND i.ordinal>? AND i.ordinal<=?
            ORDER BY i.ordinal LIMIT ?""", (session_id, after, end, CONTEXT_MAX_INPUTS + 1)).fetchall()
        if len(rows) > CONTEXT_MAX_INPUTS or sum(len(r['raw_text']) for r in rows) > CONTEXT_MAX_CHARS:
            raise DomainError('CONTEXT_LIMIT', 'This unfinished request exceeds the conversation context limit. Start a new session with its complete details; no history was silently dropped.')
        return rows

    def model_context(self, user_id, session_id, token):
        """Build bounded evidence from database truth; never from a model summary."""
        self.assert_turn(user_id, session_id, token["input_id"], token["revision"])
        rows = self.context_rows(user_id, session_id, token['revision'])
        history = []
        for row in rows:
            if row['id'] == token['input_id']:
                continue  # Sent separately as current_user_input; still counts against the shared budget.
            response = json.loads(row["response_json"]) if row["response_json"] else {}
            history.append({"input_id": row["id"], "user_text": row["raw_text"],
                            "host_response": {k: response[k] for k in ("kind", "code") if k in response}})
        state = token['state']
        projected = {k: state[k] for k in ('pending', 'cancelled') if k in state}
        projected['fields'] = {k: v for k, v in state.get('fields', {}).items() if k in FIELDS}
        projected['candidates'] = [{k: c[k] for k in ('id', 'name', 'ending', 'currency') if k in c} for c in state.get('candidates', [])]
        if 'batch' in state:
            # Collected plan fields only; queries are the user's own words from this conversation.
            projected['plan'] = {'actions': [{**{k: v for k, v in slot.get('fields', {}).items() if k in FIELDS},
                                              **({'payee_query': slot['payee_query']} if slot.get('payee_query') else {})}
                                             for slot in state['batch']],
                                 'keep_at_least_minor': state.get('keep_at_least_minor')}
            if 'pending_action' in state:
                projected['pending_action'] = state['pending_action'] + 1
        draft = self.active(user_id, session_id)
        public_draft = {'payload': {'actions': [{k: v for k, v in a.items() if k in FIELDS} for a in draft['payload']['actions']],
                                    **({'constraints': draft['payload']['constraints']} if 'constraints' in draft['payload'] else {})}} if draft else None
        return {**projected, "_evidence": {
            "history": history, "active_draft": public_draft,
            "revision": token["revision"], "current_input_id": token["input_id"]}}

    def _evidence(self, session_id, sources):
        if type(sources) is not dict or set(sources) != FIELDS:
            raise DomainError("EVIDENCE", "Every financial field needs an input reference.")
        for value in sources.values():
            if type(value) is not str or not self.db.execute(
                    "SELECT 1 FROM user_inputs WHERE id=? AND session_id=?", (value, session_id)).fetchone():
                raise DomainError("EVIDENCE", "Evidence must belong to the current conversation.")

    def _objects(self, user_id, proposal):
        account = self.account(user_id, proposal.source_account_id)
        self.payee(user_id, proposal.payee_id)
        if account["status"] != "ACTIVE" or account["currency"] != proposal.currency:
            raise DomainError("ACCOUNT", "Choose an active SGD account.")

    def active(self, user_id, session_id):
        self.session(user_id, session_id)
        row = self.db.execute("SELECT id FROM transaction_drafts WHERE session_id=? AND state='DRAFTED'", (session_id,)).fetchone()
        return self.draft(user_id, row["id"]) if row else None

    def draft(self, user_id, draft_id, version=None):
        row = self.db.execute("SELECT d.* FROM transaction_drafts d JOIN sessions s ON s.id=d.session_id WHERE d.id=? AND s.user_id=?",
                              (draft_id, user_id)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Draft unavailable.")
        if version is None:
            version = row["current_version"]
        v = self.db.execute("SELECT * FROM draft_versions WHERE draft_id=? AND version=?", (draft_id, version)).fetchone()
        if v is None:
            raise DomainError("NOT_FOUND", "Version unavailable.")
        proposal = Proposal.parse({field: v[field] for field in FIELDS})
        plan = self.db.execute("SELECT plan_json FROM draft_version_plans WHERE draft_id=? AND version=?", (draft_id, version)).fetchone()
        if plan:
            stored = strict_json(plan["plan_json"])
            if type(stored) is not dict or set(stored) != {"actions", "constraints"} or type(stored["actions"]) is not list:
                raise DomainError("INTEGRITY", "Draft plan is malformed.")
            actions = []
            for item in stored["actions"]:
                if type(item) is not dict or set(item) != FIELDS | {"action_id"}:
                    raise DomainError("INTEGRITY", "Draft plan is malformed.")
                actions.append((item["action_id"], Proposal.parse({k: item[k] for k in FIELDS})))
            if not actions or actions[0][0] != v["action_id"] or actions[0][1] != proposal:
                raise DomainError("INTEGRITY", "Draft plan does not match its first action.")
            payload = plan_payload_for(draft_id, version, actions, stored["constraints"])
        else:
            payload = payload_for(draft_id, version, v["action_id"], proposal)
        if digest(payload) != v["content_hash"]:
            raise DomainError("INTEGRITY", "Draft hash mismatch.")
        return {"payload": payload, "state": row["state"], "current_version": row["current_version"],
                "session_id": row["session_id"], "content_hash": v["content_hash"], "sources": json.loads(v["sources_json"])}

    def save_draft(self, user_id, session_id, fields, sources, *, draft_id=None, expected_version=None):
        proposal = Proposal.parse(fields)
        with self.transaction():
            self.session(user_id, session_id)
            self._objects(user_id, proposal)
            self._evidence(session_id, sources)
            if draft_id is None:
                if expected_version is not None or self.active(user_id, session_id):
                    raise DomainError("CONFLICT", "An active draft already exists; update or cancel it explicitly.")
                draft_id, action_id, version = uid("draft"), uid("action"), 1
                self.db.execute("INSERT INTO transaction_drafts VALUES (?,?,?,'DRAFTED')", (draft_id, session_id, version))
            else:
                old = self.draft(user_id, draft_id)
                if old["session_id"] != session_id or old["state"] != "DRAFTED":
                    raise DomainError("STATE", "Only this session's active draft may be changed.")
                if type(expected_version) is not int or expected_version != old["current_version"]:
                    raise DomainError("CONFLICT", "Draft changed; reload before updating.")
                action_id = old["payload"]["actions"][0]["action_id"]
                version = expected_version + 1
                self.db.execute("UPDATE transaction_drafts SET current_version=? WHERE id=?", (version, draft_id))
            payload = payload_for(draft_id, version, action_id, proposal)
            self.db.execute("INSERT INTO draft_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (draft_id, version, action_id, proposal.source_account_id, proposal.payee_id, proposal.amount_minor,
                 proposal.currency, 0, digest(payload), canonical(sources), now()))
            self.audit(session_id, "DRAFT_VERSION_CREATED", {"draft_id": draft_id, "version": version, "hash": digest(payload)})
            return self.draft(user_id, draft_id)

    def _plan_evidence(self, session_id, sources, count, constraints):
        if (type(sources) is not dict or set(sources) != {"actions", "constraints"} or type(sources["actions"]) is not list
                or len(sources["actions"]) != count or type(sources["constraints"]) is not list or len(sources["constraints"]) != len(constraints)):
            raise DomainError("EVIDENCE", "Every action field and constraint needs an input reference.")
        for item in sources["actions"]:
            self._evidence(session_id, item)
        for value in sources["constraints"]:
            if type(value) is not str or not self.db.execute("SELECT 1 FROM user_inputs WHERE id=? AND session_id=?", (value, session_id)).fetchone():
                raise DomainError("EVIDENCE", "Evidence must belong to the current conversation.")

    def save_plan(self, user_id, session_id, proposals, sources, constraints, *, draft_id=None, expected_version=None):
        """Freeze an ordered multi-action plan. Any change to any action, order or constraint is a new version."""
        if type(proposals) is not list or not 1 <= len(proposals) <= MAX_BATCH_ACTIONS:
            raise DomainError("SCHEMA", f"A plan contains 1-{MAX_BATCH_ACTIONS} transfers.")
        parsed = [Proposal.parse(item) for item in proposals]
        constraints = parse_constraints(constraints)
        if not is_plan(parsed, constraints):
            return self.save_draft(user_id, session_id, proposals[0], sources["actions"][0] if type(sources) is dict and type(sources.get("actions")) is list and sources["actions"] else sources,
                                   draft_id=draft_id, expected_version=expected_version)
        with self.transaction():
            self.session(user_id, session_id)
            for proposal in parsed:
                self._objects(user_id, proposal)
            used = {p.source_account_id for p in parsed}
            for item in constraints:
                if item["account_id"] not in used:
                    raise DomainError("SCHEMA", "A minimum-balance constraint must apply to a source account in this plan.")
                self.account(user_id, item["account_id"])
            self._plan_evidence(session_id, sources, len(parsed), constraints)
            if draft_id is None:
                if expected_version is not None or self.active(user_id, session_id):
                    raise DomainError("CONFLICT", "An active draft already exists; update or cancel it explicitly.")
                draft_id, version = uid("draft"), 1
                action_ids = [uid("action") for _ in parsed]
                self.db.execute("INSERT INTO transaction_drafts VALUES (?,?,?,'DRAFTED')", (draft_id, session_id, version))
            else:
                old = self.draft(user_id, draft_id)
                if old["session_id"] != session_id or old["state"] != "DRAFTED":
                    raise DomainError("STATE", "Only this session's active draft may be changed.")
                if type(expected_version) is not int or expected_version != old["current_version"]:
                    raise DomainError("CONFLICT", "Draft changed; reload before updating.")
                previous = [a["action_id"] for a in old["payload"]["actions"]]
                # Stable action IDs by position; added positions get new IDs.
                action_ids = previous[:len(parsed)] + [uid("action") for _ in parsed[len(previous):]]
                version = expected_version + 1
                self.db.execute("UPDATE transaction_drafts SET current_version=? WHERE id=?", (version, draft_id))
            actions = list(zip(action_ids, parsed))
            payload = plan_payload_for(draft_id, version, actions, constraints)
            first = parsed[0]
            self.db.execute("INSERT INTO draft_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (draft_id, version, action_ids[0], first.source_account_id, first.payee_id, first.amount_minor,
                 first.currency, 0, digest(payload), canonical(sources), now()))
            self.db.execute("INSERT INTO draft_version_plans VALUES (?,?,?)", (draft_id, version, canonical({
                "actions": [{"action_id": a, **p.fields()} for a, p in actions], "constraints": constraints})))
            self.audit(session_id, "DRAFT_VERSION_CREATED", {"draft_id": draft_id, "version": version, "hash": digest(payload), "actions": len(parsed)})
            return self.draft(user_id, draft_id)

    def cancel(self, user_id, session_id, draft_id, expected_version):
        with self.transaction():
            old = self.draft(user_id, draft_id)
            if old["session_id"] != session_id or old["state"] != "DRAFTED":
                raise DomainError("STATE", "Draft is not active in this conversation.")
            if type(expected_version) is not int or expected_version != old["current_version"]:
                raise DomainError("CONFLICT", "Draft changed; reload before cancellation.")
            self.db.execute("UPDATE transaction_drafts SET state='CANCELLED' WHERE id=?", (draft_id,))
            self.audit(session_id, "DRAFT_CANCELLED", {"draft_id": draft_id, "version": expected_version})

    def snapshot(self, user_id, session_id):
        session = self.session(user_id, session_id)
        return {"session_id": session_id, "revision": session["revision"], "conversation": json.loads(session["state_json"]),
                "draft": self.active(user_id, session_id), "accounts": self.accounts(user_id),
                "inputs": [dict(r) for r in self.db.execute("SELECT * FROM user_inputs WHERE session_id=? ORDER BY ordinal", (session_id,))],
                "audit": [dict(r) for r in self.db.execute("SELECT * FROM audit_events WHERE session_id=? ORDER BY id", (session_id,))]}
