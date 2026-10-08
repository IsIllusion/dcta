PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, kyc_status TEXT NOT NULL CHECK(kyc_status IN ('VERIFIED','BLOCKED'))
) STRICT;
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN ('SGD','USD')),
  balance_minor INTEGER NOT NULL CHECK(balance_minor >= 0),
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','FROZEN','CLOSED'))
) STRICT;
CREATE TABLE IF NOT EXISTS payees (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
  ending TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1))
) STRICT;
-- CV14 C: saved destinations that are not people. MOCK: a joint account, a DBS share holding bought by SGD
-- amount at a fixed mock price, and billers with a saved bill amount. Seeded by the host, read-only at runtime.
CREATE TABLE IF NOT EXISTS payee_profiles (
  payee_id TEXT PRIMARY KEY REFERENCES payees(id),
  kind TEXT NOT NULL CHECK(kind IN ('OWN','SHARES','BILL')),
  detail_json TEXT NOT NULL CHECK(json_valid(detail_json))
) STRICT;
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
  revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
  state_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(state_json))
) STRICT;
-- Host UI preference only; deleting history never deletes financial evidence.
CREATE TABLE IF NOT EXISTS hidden_conversations (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id), hidden_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS user_inputs (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  turn_key TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal > 0),
  raw_text TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(session_id,turn_key), UNIQUE(session_id,ordinal)
) STRICT;
CREATE TABLE IF NOT EXISTS input_results (
  input_id TEXT PRIMARY KEY REFERENCES user_inputs(id),
  response_json TEXT NOT NULL CHECK(json_valid(response_json))
) STRICT;
CREATE TABLE IF NOT EXISTS transaction_drafts (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  current_version INTEGER NOT NULL CHECK(current_version > 0),
  state TEXT NOT NULL CHECK(state IN ('DRAFTED','CANCELLED','SUCCEEDED'))
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS one_active_draft_per_session
  ON transaction_drafts(session_id) WHERE state='DRAFTED';
CREATE TABLE IF NOT EXISTS draft_versions (
  draft_id TEXT NOT NULL REFERENCES transaction_drafts(id), version INTEGER NOT NULL CHECK(version > 0),
  action_id TEXT NOT NULL, source_account_id TEXT NOT NULL REFERENCES accounts(id),
  payee_id TEXT NOT NULL REFERENCES payees(id),
  amount_minor INTEGER NOT NULL CHECK(amount_minor > 0 AND amount_minor <= 100000000),
  currency TEXT NOT NULL CHECK(currency='SGD'), fee_minor INTEGER NOT NULL CHECK(fee_minor=0),
  content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
  sources_json TEXT NOT NULL CHECK(json_valid(sources_json)), created_at TEXT NOT NULL,
  PRIMARY KEY(draft_id,version)
) STRICT;
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  input_id TEXT REFERENCES user_inputs(id), event TEXT NOT NULL,
  details_json TEXT NOT NULL CHECK(json_valid(details_json)), created_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS ledger_entries (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
  delta_minor INTEGER NOT NULL, currency TEXT NOT NULL CHECK(currency='SGD')
) STRICT;
CREATE TRIGGER IF NOT EXISTS immutable_versions_update BEFORE UPDATE ON draft_versions
  BEGIN SELECT RAISE(ABORT,'draft versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_versions_delete BEFORE DELETE ON draft_versions
  BEGIN SELECT RAISE(ABORT,'draft versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_inputs_update BEFORE UPDATE ON user_inputs
  BEGIN SELECT RAISE(ABORT,'inputs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_inputs_delete BEFORE DELETE ON user_inputs
  BEGIN SELECT RAISE(ABORT,'inputs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_update BEFORE UPDATE ON audit_events
  BEGIN SELECT RAISE(ABORT,'audit is append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_delete BEFORE DELETE ON audit_events
  BEGIN SELECT RAISE(ABORT,'audit is append only'); END;
CREATE TABLE IF NOT EXISTS voice_inputs (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  request_key TEXT NOT NULL, base_revision INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PROCESSING','READY','CANCELLED','FAILED','SUBMITTED')),
  mime TEXT NOT NULL, audio BLOB NOT NULL CHECK(length(audio) <= 6291456),
  audio_sha256 TEXT NOT NULL, transcript TEXT, asr_json TEXT CHECK(json_valid(asr_json)),
  error_code TEXT, input_id TEXT REFERENCES user_inputs(id), created_at TEXT NOT NULL,
  UNIQUE(session_id,request_key)
) STRICT;
-- CV13 retention: raw audio may be erased (never altered) only after a recorded purge; hash and transcript remain.
CREATE TABLE IF NOT EXISTS purged_audio (
  voice_id TEXT PRIMARY KEY REFERENCES voice_inputs(id), audio_sha256 TEXT NOT NULL, bytes INTEGER NOT NULL CHECK(bytes>0),
  purged_at TEXT NOT NULL, policy TEXT NOT NULL
) STRICT;
CREATE TRIGGER IF NOT EXISTS immutable_purge_update BEFORE UPDATE ON purged_audio
  BEGIN SELECT RAISE(ABORT,'purge records are append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_purge_delete BEFORE DELETE ON purged_audio
  BEGIN SELECT RAISE(ABORT,'purge records are append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_voice_evidence BEFORE UPDATE ON voice_inputs
WHEN NEW.id IS NOT OLD.id OR NEW.session_id IS NOT OLD.session_id OR NEW.request_key IS NOT OLD.request_key
  OR (NEW.audio IS NOT OLD.audio AND NOT (length(NEW.audio)=0 AND EXISTS(SELECT 1 FROM purged_audio p WHERE p.voice_id=OLD.id)))
  OR NEW.audio_sha256 IS NOT OLD.audio_sha256 OR NEW.mime IS NOT OLD.mime
  OR NEW.base_revision IS NOT OLD.base_revision OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.transcript IS NOT NULL AND (NEW.transcript IS NOT OLD.transcript OR NEW.asr_json IS NOT OLD.asr_json))
  OR (OLD.input_id IS NOT NULL AND NEW.input_id IS NOT OLD.input_id)
BEGIN SELECT RAISE(ABORT,'voice evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_voice_delete BEFORE DELETE ON voice_inputs
BEGIN SELECT RAISE(ABORT,'voice evidence is immutable'); END;
CREATE TABLE IF NOT EXISTS opening_balances (
  account_id TEXT PRIMARY KEY REFERENCES accounts(id), balance_minor INTEGER NOT NULL CHECK(balance_minor>=0)
) STRICT;
CREATE TABLE IF NOT EXISTS validation_results (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), draft_id TEXT NOT NULL,
  version INTEGER NOT NULL, content_hash TEXT NOT NULL, revision INTEGER NOT NULL, voice_epoch INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PROCESSING','PASS','DISCREPANCY','NEEDS_CLARIFICATION','ERROR','STALE')),
  result_json TEXT CHECK(json_valid(result_json)), adapter TEXT NOT NULL, created_ms INTEGER NOT NULL,
  FOREIGN KEY(draft_id,version) REFERENCES draft_versions(draft_id,version)
) STRICT;
CREATE TABLE IF NOT EXISTS demo_public_keys (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), spki BLOB NOT NULL, created_ms INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS authorization_challenges (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), session_id TEXT NOT NULL REFERENCES sessions(id),
  draft_id TEXT NOT NULL, version INTEGER NOT NULL, content_hash TEXT NOT NULL,
  validation_id TEXT NOT NULL REFERENCES validation_results(id), key_id TEXT NOT NULL REFERENCES demo_public_keys(id),
  revision INTEGER NOT NULL, voice_epoch INTEGER NOT NULL, issued_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  status TEXT NOT NULL CHECK(status IN ('ISSUED','AUTHORIZED','INVALIDATED','CONSUMED')),
  FOREIGN KEY(draft_id,version) REFERENCES draft_versions(draft_id,version)
) STRICT;
CREATE TABLE IF NOT EXISTS authorization_proofs (
  challenge_id TEXT PRIMARY KEY REFERENCES authorization_challenges(id), key_id TEXT NOT NULL REFERENCES demo_public_keys(id),
  signature TEXT NOT NULL, verified_ms INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS execution_requests (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), session_id TEXT NOT NULL REFERENCES sessions(id),
  idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, challenge_id TEXT NOT NULL REFERENCES authorization_challenges(id),
  status TEXT NOT NULL CHECK(status IN ('PENDING','SUCCEEDED','REJECTED')),
  execution_id TEXT, error_code TEXT, created_ms INTEGER NOT NULL,
  UNIQUE(user_id,idempotency_key)
) STRICT;
CREATE TABLE IF NOT EXISTS execution_records (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), session_id TEXT NOT NULL REFERENCES sessions(id),
  draft_id TEXT NOT NULL, version INTEGER NOT NULL, content_hash TEXT NOT NULL,
  challenge_id TEXT NOT NULL UNIQUE REFERENCES authorization_challenges(id),
  amount_minor INTEGER NOT NULL CHECK(amount_minor>0), source_account_id TEXT NOT NULL REFERENCES accounts(id),
  payee_id TEXT NOT NULL REFERENCES payees(id), created_ms INTEGER NOT NULL,
  receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)), UNIQUE(draft_id,version),
  FOREIGN KEY(draft_id,version) REFERENCES draft_versions(draft_id,version)
) STRICT;
CREATE TABLE IF NOT EXISTS ledger_links (
  entry_id TEXT PRIMARY KEY REFERENCES ledger_entries(id), execution_id TEXT NOT NULL REFERENCES execution_records(id),
  role TEXT NOT NULL CHECK(role IN ('DEBIT','CREDIT')), UNIQUE(execution_id,role)
) STRICT;
CREATE TRIGGER IF NOT EXISTS immutable_ledger_update BEFORE UPDATE ON ledger_entries
BEGIN SELECT RAISE(ABORT,'ledger is append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_ledger_delete BEFORE DELETE ON ledger_entries
BEGIN SELECT RAISE(ABORT,'ledger is append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_execution_update BEFORE UPDATE ON execution_records
BEGIN SELECT RAISE(ABORT,'receipt is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_execution_delete BEFORE DELETE ON execution_records
BEGIN SELECT RAISE(ABORT,'receipt is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_validation BEFORE UPDATE ON validation_results
WHEN OLD.status!='PROCESSING' OR NEW.id IS NOT OLD.id OR NEW.draft_id IS NOT OLD.draft_id
 OR NEW.version IS NOT OLD.version OR NEW.content_hash IS NOT OLD.content_hash OR NEW.revision IS NOT OLD.revision
 OR NEW.voice_epoch IS NOT OLD.voice_epoch OR NEW.session_id IS NOT OLD.session_id OR NEW.adapter IS NOT OLD.adapter OR NEW.created_ms IS NOT OLD.created_ms
BEGIN SELECT RAISE(ABORT,'validation binding is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_challenge BEFORE UPDATE ON authorization_challenges
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id OR NEW.session_id IS NOT OLD.session_id
 OR NEW.draft_id IS NOT OLD.draft_id OR NEW.version IS NOT OLD.version OR NEW.content_hash IS NOT OLD.content_hash
 OR NEW.validation_id IS NOT OLD.validation_id OR NEW.key_id IS NOT OLD.key_id OR NEW.revision IS NOT OLD.revision
 OR NEW.voice_epoch IS NOT OLD.voice_epoch OR NEW.issued_ms IS NOT OLD.issued_ms OR NEW.expires_ms IS NOT OLD.expires_ms OR NEW.payload_json IS NOT OLD.payload_json
BEGIN SELECT RAISE(ABORT,'authorization binding is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_proof_update BEFORE UPDATE ON authorization_proofs
BEGIN SELECT RAISE(ABORT,'proof is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_proof_delete BEFORE DELETE ON authorization_proofs
BEGIN SELECT RAISE(ABORT,'proof is immutable'); END;
CREATE TABLE IF NOT EXISTS conversation_names (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 80),
  revision INTEGER NOT NULL CHECK(revision > 0),
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS history_challenges (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
  session_id TEXT NOT NULL REFERENCES sessions(id), action TEXT NOT NULL CHECK(action IN ('HIDE','RESTORE')),
  snapshot TEXT NOT NULL CHECK(length(snapshot)=64), issued_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL CHECK(expires_ms>issued_ms),
  status TEXT NOT NULL CHECK(status IN ('ISSUED','CONSUMED','INVALIDATED')),
  result_json TEXT CHECK(json_valid(result_json)), CHECK((status='CONSUMED')=(result_json IS NOT NULL))
) STRICT;
CREATE INDEX IF NOT EXISTS history_challenges_session ON history_challenges(session_id,user_id,status);
CREATE TRIGGER IF NOT EXISTS immutable_history_challenge BEFORE UPDATE ON history_challenges
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id OR NEW.session_id IS NOT OLD.session_id
 OR NEW.action IS NOT OLD.action OR NEW.snapshot IS NOT OLD.snapshot OR NEW.issued_ms IS NOT OLD.issued_ms
 OR NEW.expires_ms IS NOT OLD.expires_ms OR OLD.status IN ('CONSUMED','INVALIDATED')
BEGIN SELECT RAISE(ABORT,'history confirmation is immutable'); END;
-- CV13: multi-action plans. draft_versions keeps action 1 (with its foreign keys) for every draft;
-- a schema-2 plan additionally freezes the full ordered action list and constraints here.
CREATE TABLE IF NOT EXISTS draft_version_plans (
  draft_id TEXT NOT NULL, version INTEGER NOT NULL,
  plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
  PRIMARY KEY(draft_id,version),
  FOREIGN KEY(draft_id,version) REFERENCES draft_versions(draft_id,version)
) STRICT;
CREATE TRIGGER IF NOT EXISTS immutable_plans_update BEFORE UPDATE ON draft_version_plans
  BEGIN SELECT RAISE(ABORT,'draft plans are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_plans_delete BEFORE DELETE ON draft_version_plans
  BEGIN SELECT RAISE(ABORT,'draft plans are immutable'); END;
-- Per-action double entries of an all-or-nothing plan execution.
CREATE TABLE IF NOT EXISTS ledger_action_links (
  entry_id TEXT PRIMARY KEY REFERENCES ledger_entries(id), execution_id TEXT NOT NULL REFERENCES execution_records(id),
  ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 5),
  role TEXT NOT NULL CHECK(role IN ('DEBIT','CREDIT')), UNIQUE(execution_id,ordinal,role)
) STRICT;
CREATE TRIGGER IF NOT EXISTS immutable_action_links_update BEFORE UPDATE ON ledger_action_links
BEGIN SELECT RAISE(ABORT,'ledger is append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_action_links_delete BEFORE DELETE ON ledger_action_links
BEGIN SELECT RAISE(ABORT,'ledger is append only'); END;
-- CV13: platform-authenticator (WebAuthn) credentials. The P-256 public key is also registered in
-- demo_public_keys so the existing challenge/proof binding applies unchanged.
CREATE TABLE IF NOT EXISTS webauthn_registrations (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), challenge TEXT NOT NULL,
  issued_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL CHECK(expires_ms>issued_ms),
  status TEXT NOT NULL CHECK(status IN ('ISSUED','CONSUMED'))
) STRICT;
CREATE TABLE IF NOT EXISTS webauthn_credentials (
  key_id TEXT PRIMARY KEY REFERENCES demo_public_keys(id), user_id TEXT NOT NULL REFERENCES users(id),
  credential_id TEXT NOT NULL UNIQUE, origin TEXT NOT NULL, sign_count INTEGER NOT NULL CHECK(sign_count>=0),
  created_ms INTEGER NOT NULL
) STRICT;
CREATE TRIGGER IF NOT EXISTS immutable_webauthn_credential BEFORE UPDATE ON webauthn_credentials
WHEN NEW.key_id IS NOT OLD.key_id OR NEW.user_id IS NOT OLD.user_id OR NEW.credential_id IS NOT OLD.credential_id
 OR NEW.origin IS NOT OLD.origin OR NEW.created_ms IS NOT OLD.created_ms OR NEW.sign_count < OLD.sign_count
BEGIN SELECT RAISE(ABORT,'credential binding is immutable'); END;
-- DEMO ONLY (remove for a formal release): reset spending-limit counters and top balances back up.
-- Append-only; balances are restored through balanced ledger entries so reconciliation still holds.
CREATE TABLE IF NOT EXISTS demo_resets (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), reset_ms INTEGER NOT NULL,
  details_json TEXT NOT NULL CHECK(json_valid(details_json)), created_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS demo_topup_links (
  entry_id TEXT PRIMARY KEY REFERENCES ledger_entries(id), reset_id TEXT NOT NULL REFERENCES demo_resets(id),
  account_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('TOPUP','CLEARING')), UNIQUE(reset_id,account_id,role)
) STRICT;
CREATE TRIGGER IF NOT EXISTS immutable_demo_resets BEFORE UPDATE ON demo_resets BEGIN SELECT RAISE(ABORT,'demo resets are append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_demo_resets_delete BEFORE DELETE ON demo_resets BEGIN SELECT RAISE(ABORT,'demo resets are append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_demo_topups BEFORE UPDATE ON demo_topup_links BEGIN SELECT RAISE(ABORT,'ledger is append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_demo_topups_delete BEFORE DELETE ON demo_topup_links BEGIN SELECT RAISE(ABORT,'ledger is append only'); END;
-- Transfer purpose (a user-chosen label such as Rent; "Others" carries a short reason). Presentation and audit
-- metadata only: never part of the frozen payload, the validator input or risk rules. Editable only while DRAFTED.
CREATE TABLE IF NOT EXISTS transfer_purposes (
  draft_id TEXT PRIMARY KEY REFERENCES transaction_drafts(id),
  category TEXT NOT NULL CHECK(category IN ('Transfer','Family & friends','Rent','Security deposit','Shopping','Bills & utilities','Education','Medical','Travel','Investment','Salary','Others')),
  note TEXT NOT NULL CHECK(length(note)<=80 AND (category='Others')=(length(note)>0)),
  updated_at TEXT NOT NULL
) STRICT;
CREATE TRIGGER IF NOT EXISTS purpose_insert_only_while_drafted BEFORE INSERT ON transfer_purposes
WHEN (SELECT state FROM transaction_drafts WHERE id=NEW.draft_id) IS NOT 'DRAFTED'
BEGIN SELECT RAISE(ABORT,'purpose is locked once the draft is finished'); END;
CREATE TRIGGER IF NOT EXISTS purpose_update_only_while_drafted BEFORE UPDATE ON transfer_purposes
WHEN (SELECT state FROM transaction_drafts WHERE id=OLD.draft_id) IS NOT 'DRAFTED' OR NEW.draft_id<>OLD.draft_id
BEGIN SELECT RAISE(ABORT,'purpose is locked once the draft is finished'); END;
CREATE TRIGGER IF NOT EXISTS purpose_no_delete BEFORE DELETE ON transfer_purposes
BEGIN SELECT RAISE(ABORT,'purposes are not deleted'); END;
-- CV14: one purpose per action of a multi-action plan. Action 0 keeps using transfer_purposes (unchanged);
-- actions 1 to 4 are stored here with the same rules.
CREATE TABLE IF NOT EXISTS action_purposes (
  draft_id TEXT NOT NULL REFERENCES transaction_drafts(id), action_index INTEGER NOT NULL CHECK(action_index BETWEEN 1 AND 4),
  category TEXT NOT NULL CHECK(category IN ('Transfer','Family & friends','Rent','Security deposit','Shopping','Bills & utilities','Education','Medical','Travel','Investment','Salary','Others')),
  note TEXT NOT NULL CHECK(length(note)<=80 AND (category='Others')=(length(note)>0)),
  updated_at TEXT NOT NULL, PRIMARY KEY(draft_id,action_index)
) STRICT;
CREATE TRIGGER IF NOT EXISTS action_purpose_insert_only_while_drafted BEFORE INSERT ON action_purposes
WHEN (SELECT state FROM transaction_drafts WHERE id=NEW.draft_id) IS NOT 'DRAFTED'
BEGIN SELECT RAISE(ABORT,'purpose is locked once the draft is finished'); END;
CREATE TRIGGER IF NOT EXISTS action_purpose_update_only_while_drafted BEFORE UPDATE ON action_purposes
WHEN (SELECT state FROM transaction_drafts WHERE id=OLD.draft_id) IS NOT 'DRAFTED' OR NEW.draft_id<>OLD.draft_id OR NEW.action_index<>OLD.action_index
BEGIN SELECT RAISE(ABORT,'purpose is locked once the draft is finished'); END;
CREATE TRIGGER IF NOT EXISTS action_purpose_no_delete BEFORE DELETE ON action_purposes
BEGIN SELECT RAISE(ABORT,'purposes are not deleted'); END;
-- CV14 A: out-of-band second check for SGD 800-1,000. The 6-digit code is never stored: the host and the
-- simulated security token derive it from a key kept outside the database (work/<db>.token-key).
CREATE TABLE IF NOT EXISTS stepup_checks (
  id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL REFERENCES authorization_challenges(id),
  user_id TEXT NOT NULL REFERENCES users(id), session_id TEXT NOT NULL REFERENCES sessions(id),
  amount_minor INTEGER NOT NULL CHECK(amount_minor>0), issued_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL CHECK(expires_ms>issued_ms),
  attempts INTEGER NOT NULL CHECK(attempts BETWEEN 0 AND 3),
  status TEXT NOT NULL CHECK(status IN ('ISSUED','VERIFIED','FAILED','EXPIRED','REPLACED')),
  verified_ms INTEGER
) STRICT;
CREATE INDEX IF NOT EXISTS stepup_by_challenge ON stepup_checks(challenge_id,status);
CREATE TRIGGER IF NOT EXISTS stepup_immutable BEFORE UPDATE ON stepup_checks
WHEN NEW.id IS NOT OLD.id OR NEW.challenge_id IS NOT OLD.challenge_id OR NEW.user_id IS NOT OLD.user_id
 OR NEW.session_id IS NOT OLD.session_id OR NEW.amount_minor IS NOT OLD.amount_minor OR NEW.issued_ms IS NOT OLD.issued_ms
 OR NEW.expires_ms IS NOT OLD.expires_ms OR OLD.status<>'ISSUED' OR NEW.attempts<OLD.attempts
BEGIN SELECT RAISE(ABORT,'step-up checks only move forward from ISSUED'); END;
CREATE TRIGGER IF NOT EXISTS stepup_no_delete BEFORE DELETE ON stepup_checks
BEGIN SELECT RAISE(ABORT,'step-up checks are not deleted'); END;
PRAGMA user_version = 6;
