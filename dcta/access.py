"""Explicit host read contracts. Row ownership remains enforced by host queries."""
import sqlite3

def columns(value):
    return set(value.split()) | {'', 'ROWID'}

CORE_READS = {name: columns(value) for name, value in {
    'users': 'id kyc_status',
    'accounts': 'id user_id name currency balance_minor status',
    'payees': 'id user_id name ending active',
    'payee_profiles': 'payee_id kind detail_json',
    'sessions': 'id user_id revision state_json',
    'user_inputs': 'id session_id turn_key ordinal raw_text created_at',
    'input_results': 'input_id response_json',
    'transaction_drafts': 'id session_id current_version state',
    'draft_versions': 'draft_id version action_id source_account_id payee_id amount_minor currency fee_minor content_hash sources_json created_at',
    'audit_events': 'id session_id input_id event details_json created_at',
    'voice_inputs': 'id session_id request_key base_revision status mime audio audio_sha256 transcript asr_json error_code input_id created_at',
    # Own-history answers: receipts are read owner-scoped by host code, never by the model.
    'execution_records': 'id session_id user_id amount_minor created_ms receipt_json',
    'hidden_conversations': 'session_id',
    'ledger_entries': '',
    'draft_version_plans': 'draft_id version plan_json',
    'purged_audio': 'voice_id',
    'transfer_purposes': 'draft_id category note updated_at',
    'action_purposes': 'draft_id action_index category note updated_at',
    'demo_resets': 'user_id reset_ms',  # demo-only limit counter restart  # read by the voice-evidence trigger
}.items()}
CONTROL_READS = {name: columns(value) for name, value in {
    'hidden_conversations': 'session_id',
    'validation_results': 'id session_id draft_id version content_hash revision voice_epoch status result_json adapter created_ms',
    'demo_public_keys': 'id user_id spki created_ms',
    'authorization_challenges': 'id user_id session_id draft_id version content_hash validation_id key_id revision voice_epoch issued_ms expires_ms payload_json status',
    'authorization_proofs': 'challenge_id key_id signature verified_ms',
    'execution_requests': 'id user_id session_id idempotency_key request_hash challenge_id status execution_id error_code created_ms',
    'webauthn_registrations': 'id user_id challenge issued_ms expires_ms status',
    'webauthn_credentials': 'key_id user_id credential_id origin sign_count created_ms',
    'stepup_checks': 'id challenge_id user_id session_id amount_minor issued_ms expires_ms attempts status verified_ms',
}.items()}
GATEWAY_READS = {name: columns(value) for name, value in {
    'opening_balances': 'account_id balance_minor',
    'ledger_entries': 'id account_id delta_minor currency',
    'ledger_links': 'entry_id execution_id role',
    'ledger_action_links': 'entry_id execution_id ordinal role',
    'demo_resets': 'id user_id reset_ms details_json created_at',
    'demo_topup_links': 'entry_id reset_id account_id role',
    'execution_records': 'id user_id session_id draft_id version content_hash challenge_id amount_minor source_account_id payee_id created_ms receipt_json',
}.items()}

def read_allowed(mapping, table, column, database):
    # SQLite reports database=None for COUNT(*) with no referenced column.
    return (database == 'main' or (database is None and column == '')) and table in mapping and column in mapping[table]

def read_decision(mapping, table, column, database):
    return sqlite3.SQLITE_OK if read_allowed(mapping, table, column, database) else sqlite3.SQLITE_DENY
