"""Owner-scoped presentation metadata; no transaction or evidence deletion."""
import sqlite3
import json
import unicodedata
import time
import secrets

from .domain import DomainError, canonical, digest
from .store import Store, runtime_authorizer, now
from .access import columns, read_allowed

HISTORY_READS = {
    'history_challenges': columns('id user_id session_id action snapshot issued_ms expires_ms status result_json'),
    'conversation_names': columns('session_id title revision updated_at'),
    'hidden_conversations': columns('session_id hidden_at'),
    'authorization_challenges': columns('session_id status expires_ms'),
}

def history_authorizer(action, table, column, database, trigger):
    if action == sqlite3.SQLITE_READ and read_allowed(HISTORY_READS, table, column, database):
        return sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE) and table == 'hidden_conversations' and database == 'main':
        return sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE) and table in ('conversation_names','history_challenges') and database == 'main':
        return sqlite3.SQLITE_OK
    return runtime_authorizer(action, table, column, database, trigger)


class HistoryStore(Store):
    """Only the host history endpoint can persist a hidden-conversation marker."""
    def __init__(self, path):
        super().__init__(path)
        self.db.set_authorizer(history_authorizer)


def require_visible(store, user_id, session):
    store.session(user_id, session)
    if store.db.execute('SELECT 1 FROM hidden_conversations WHERE session_id=?', (session,)).fetchone():
        raise DomainError('HISTORY_REMOVED', 'This conversation was removed from history. Start a new session.')


def _history_snapshot(store, user_id, session):
    current=store.session(user_id,session)
    audit=store.db.execute('SELECT COALESCE(MAX(id),0) FROM audit_events WHERE session_id=?',(session,)).fetchone()[0]
    hidden=store.db.execute('SELECT hidden_at FROM hidden_conversations WHERE session_id=?',(session,)).fetchone()
    return digest({'session':session,'user':user_id,'revision':current['revision'],'audit':audit,'hidden_at':hidden[0] if hidden else None})


def _history_available(store, session, action, clock_ms):
    hidden=bool(store.db.execute('SELECT 1 FROM hidden_conversations WHERE session_id=?',(session,)).fetchone())
    if hidden != (action=='RESTORE'):
        raise DomainError('HISTORY_CHANGED', 'This conversation changed. Close this dialog and refresh history before trying again.')
    # Preserve access to live reviews and pending recognition. Rechecked at commit.
    authorized=store.db.execute("SELECT 1 FROM authorization_challenges WHERE session_id=? AND status IN ('ISSUED','AUTHORIZED') AND expires_ms>? LIMIT 1",(session,clock_ms)).fetchone()
    recognizing=store.db.execute("SELECT 1 FROM voice_inputs WHERE session_id=? AND status='PROCESSING' LIMIT 1",(session,)).fetchone()
    if authorized or recognizing:
        raise DomainError('HISTORY_BUSY', 'Finish or resolve the pending request, recording or review before changing this conversation in history.')


def prepare_history_change(store, user_id, session, action):
    if action not in ('HIDE','RESTORE'):
        raise DomainError('INPUT','Unknown history action.')
    with store.transaction():
        store.session(user_id,session)
        clock_ms=int(time.time()*1000)
        _history_available(store,session,action,clock_ms)
        title=recent_sessions(store,user_id,removed=action=='RESTORE',exact_session=session)['sessions'][0]['title']
        store.db.execute("UPDATE history_challenges SET status='INVALIDATED' WHERE session_id=? AND user_id=? AND status='ISSUED'",(session,user_id))
        challenge=secrets.token_urlsafe(32)
        store.db.execute('INSERT INTO history_challenges VALUES (?,?,?,?,?,?,?,?,?)',
                         (challenge,user_id,session,action,_history_snapshot(store,user_id,session),clock_ms,clock_ms+120000,'ISSUED',None))
        return {'session_id':session,'challenge_id':challenge,'action':action,'title':title,'expires_ms':clock_ms+120000}


def confirm_history_change(store, user_id, session, action, challenge_id):
    if action not in ('HIDE','RESTORE') or type(challenge_id) is not str or not 1<=len(challenge_id)<=100:
        raise DomainError('HISTORY_CONFIRMATION','Open a fresh confirmation before changing history.')
    with store.transaction():
        snapshot=_history_snapshot(store,user_id,session)
        challenge=store.db.execute('SELECT * FROM history_challenges WHERE id=? AND user_id=? AND session_id=? AND action=?',
                                   (challenge_id,user_id,session,action)).fetchone()
        if not challenge:
            raise DomainError('HISTORY_CONFIRMATION','The confirmation does not match this conversation and action.')
        if challenge['status']=='CONSUMED':
            saved=json.loads(challenge['result_json'])
            if snapshot==saved['snapshot']:return saved['result']
        clock_ms=int(time.time()*1000)
        if challenge['status']!='ISSUED' or snapshot!=challenge['snapshot']:
            raise DomainError('HISTORY_CHANGED','This conversation changed. Close this dialog and confirm its latest state again.')
        if not challenge['issued_ms']<=clock_ms<challenge['expires_ms']:
            raise DomainError('HISTORY_EXPIRED','This confirmation expired. Close this dialog and open it again.')
        _history_available(store,session,action,clock_ms)
        if action=='HIDE':
            store.db.execute('INSERT INTO hidden_conversations VALUES (?,?)',(session,now()))
        else:
            store.db.execute('DELETE FROM hidden_conversations WHERE session_id=?',(session,))
        store.audit(session,'CONVERSATION_HIDDEN' if action=='HIDE' else 'CONVERSATION_RESTORED',
                    {'actor':user_id,'scope':'history_visibility','challenge_id':challenge_id,
                     'interaction_evidence_retained':True,'transactions_retained':True})
        result={'session_id':session,'removed':action=='HIDE','restored':action=='RESTORE'}
        saved={'result':result,'snapshot':_history_snapshot(store,user_id,session)}
        store.db.execute("UPDATE history_challenges SET status='CONSUMED',result_json=? WHERE id=?",(canonical(saved),challenge_id))
        return result


def delete_conversation(store, user_id, session, challenge_id=None):
    return confirm_history_change(store,user_id,session,'HIDE',challenge_id)


def conversation_summary(row):
    """Summarize host outcomes, never copy names, amounts or model prose."""
    kinds = set(json.loads(row['kinds'])) - {None}
    state = row['draft_state']
    if state == 'SUCCEEDED':
        title, preview = 'Settled transfer', 'Transfer reviewed and settled.'
    elif state == 'CANCELLED':
        title, preview = 'Cancelled transfer', 'Transfer request cancelled.'
    elif state == 'DRAFTED':
        title = 'Transfer corrections' if row['draft_version'] > 1 else 'Transfer preparation'
        preview = 'Transfer details revised for review.' if row['draft_version'] > 1 else 'Transfer prepared for review.'
    elif 'CLARIFY' in kinds:
        title, preview = 'Transfer clarification', 'Discussed missing or ambiguous transfer details.'
        if 'CANCELLED' in kinds:
            preview = 'Clarified transfer details; request cancelled.'
    elif 'ESTIMATE' in kinds:
        title, preview = 'Balance estimate', 'Explored a hypothetical balance; no payment from the estimate.'
    elif 'BALANCE' in kinds:
        title, preview = 'Balance enquiry', 'Checked the available account balance.'
    elif 'CANCELLED' in kinds:
        title, preview = 'Cancelled request', 'A request was cancelled.'
    elif 'INFO' in kinds:
        title, preview = 'Transfer guidance', 'Discussed transfer preparation and review.'
    elif kinds & {'HELP', 'GREETING', 'CHAT'}:
        title, preview = 'Getting started', 'Greetings and help using the assistant.'
    elif kinds & {'GUIDANCE', 'UNSUPPORTED', 'RESTRICTED'}:
        title, preview = 'Questions and guidance', 'Discussed supported requests and boundaries.'
    elif 'ERROR' in kinds:
        title, preview = 'Request needs attention', 'A request could not be completed.'
    elif row['revision'] and row['last_kind'] is None:
        title, preview = 'Awaiting a response', 'A message was saved without a final response.'
    elif row['revision']:
        title, preview = 'Conversation', 'Saved messages; open to view the conversation.'
    elif row['voice_status']:
        title, preview = 'Voice conversation', 'Recording saved; no message sent yet.'
    else:
        title, preview = 'New conversation', 'No messages yet.'
    if 'BALANCE' in kinds and title != 'Balance enquiry':
        preview += ' Also checked the balance.'
    return title, preview


def rename_conversation(store, user_id, session, title, expected_version):
    if type(title) is not str or not 1 <= len(title.strip()) <= 80:
        raise DomainError('TITLE', 'Use a conversation name between 1 and 80 characters.')
    # Permit international names and emoji, but not control/bidi spoofing or invalid Unicode.
    if any(unicodedata.category(c) in ('Cc','Cs','Cf') and c != '\u200d' for c in title):
        raise DomainError('TITLE', 'Use a single-line name without control characters.')
    if type(expected_version) is not int or not 0 <= expected_version < 2**31:
        raise DomainError('INPUT', 'A valid name version is required.')
    title=title.strip()
    with store.transaction():
        require_visible(store,user_id,session)
        old=store.db.execute('SELECT title,revision FROM conversation_names WHERE session_id=?',(session,)).fetchone()
        version=old['revision'] if old else 0
        # A retry after a lost reply is harmless; never overwrite a newer different name.
        if old and old['title']==title and expected_version in (version,version-1):
            return {'session_id':session,'title':title,'title_version':version}
        if expected_version != version:
            raise DomainError('TITLE_CONFLICT', 'The name changed in another window. Close and reopen Rename conversation before saving again.')
        store.db.execute('INSERT INTO conversation_names VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET title=excluded.title,revision=excluded.revision,updated_at=excluded.updated_at',
                         (session,title,version+1,now()))
        store.audit(session,'CONVERSATION_RENAMED',{'actor':user_id,'from_title':old['title'] if old else None,'to_title':title,'title_version':version+1})
        return {'session_id':session,'title':title,'title_version':version+1}


def recent_sessions(store, user_id, query='', limit=100, *, removed=False, exact_session=None):
    if type(query) is not str or len(query) > 80 or type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError('INPUT', 'Search must be within 80 characters.')
    try:
        query.encode('utf-8')
    except UnicodeError:
        raise DomainError('INPUT', 'Search must contain valid Unicode.') from None
    query = query.strip()
    # User identity belongs to the host; search is a literal substring, never SQL/LIKE syntax.
    if type(removed) is not bool:raise DomainError('INPUT','Invalid history view.')
    where = """s.user_id=? AND """+('' if removed else 'NOT ')+"""EXISTS (SELECT 1 FROM hidden_conversations h WHERE h.session_id=s.id)
        AND (?='' OR instr(lower(s.id),lower(?))>0 OR EXISTS (
        SELECT 1 FROM conversation_names n WHERE n.session_id=s.id AND instr(lower(n.title),lower(?))>0) OR EXISTS (
        SELECT 1 FROM user_inputs i WHERE i.session_id=s.id AND instr(lower(i.raw_text),lower(?))>0))"""
    args = (user_id, query, query, query, query)
    if exact_session is not None:
        store.session(user_id,exact_session)
        where+=' AND s.id=?';args+= (exact_session,)
    with store.read_transaction():
        total = store.db.execute('SELECT COUNT(*) FROM sessions s WHERE '+where, args).fetchone()[0]
        rows = store.db.execute("""
            SELECT s.id,s.revision,
              (SELECT h.hidden_at FROM hidden_conversations h WHERE h.session_id=s.id) AS hidden_at,
              (SELECT n.title FROM conversation_names n WHERE n.session_id=s.id) AS custom_title,
              COALESCE((SELECT n.revision FROM conversation_names n WHERE n.session_id=s.id),0) AS title_version,
              (SELECT json_group_array(DISTINCT json_extract(r.response_json,'$.kind')) FROM user_inputs i
                 LEFT JOIN input_results r ON r.input_id=i.id WHERE i.session_id=s.id) AS kinds,
              (SELECT json_extract(r.response_json,'$.kind') FROM user_inputs i LEFT JOIN input_results r ON r.input_id=i.id
                 WHERE i.session_id=s.id ORDER BY i.ordinal DESC LIMIT 1) AS last_kind,
              (SELECT a.id FROM audit_events a WHERE a.session_id=s.id ORDER BY a.id DESC LIMIT 1) AS last_event,
              (SELECT a.created_at FROM audit_events a WHERE a.session_id=s.id ORDER BY a.id DESC LIMIT 1) AS last_activity,
              (SELECT d.state FROM transaction_drafts d WHERE d.session_id=s.id ORDER BY d.rowid DESC LIMIT 1) AS draft_state,
              (SELECT d.current_version FROM transaction_drafts d WHERE d.session_id=s.id ORDER BY d.rowid DESC LIMIT 1) AS draft_version,
              (SELECT v.status FROM voice_inputs v WHERE v.session_id=s.id ORDER BY v.rowid DESC LIMIT 1) AS voice_status
            FROM sessions s WHERE """+where+" ORDER BY COALESCE(last_event,0) DESC,s.rowid DESC LIMIT ?", (*args,limit)).fetchall()
        result = []
        for row in rows:
            item = {k: row[k] for k in ('id','revision','last_activity','last_kind','draft_state','draft_version','voice_status')}
            item['title'], item['preview'] = conversation_summary(row)
            if row['custom_title'] is not None:
                item['title']=row['custom_title']
            item['title_version']=row['title_version']
            if removed:item['removed_at']=row['hidden_at']
            # Search matches original text, but list previews reveal no amounts or names.
            result.append(item)
        return {'sessions': result, 'total': total, 'limit': limit, 'has_more': total > len(result)}
