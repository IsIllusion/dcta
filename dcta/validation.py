"""Independent semantic validation; adapters receive data, never database tools."""
import json
from pathlib import Path
import sqlite3
import time
from .domain import DomainError, canonical, strict_json, amount_to_minor, MAX_BATCH_ACTIONS
from .store import Store, runtime_authorizer, uid
from .access import CONTROL_READS, GATEWAY_READS, read_allowed
from .observability import observed
from .spoken import require_confirmed as require_spoken_confirmed
from .amounts import rule_of, resolve as resolve_amounts

CONTROL_TABLES = {'validation_results','demo_public_keys','authorization_challenges','authorization_proofs','execution_requests','webauthn_registrations','webauthn_credentials','stepup_checks'}


def control_authorizer(action, table, column, database, trigger):
    if action == sqlite3.SQLITE_READ and (read_allowed(CONTROL_READS, table, column, database) or read_allowed(GATEWAY_READS, table, column, database)):
        return sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE) and table in CONTROL_TABLES:
        return sqlite3.SQLITE_OK
    return runtime_authorizer(action,table,column,database,trigger)


class ControlStore(Store):
    """Host validation/authorization connection; still cannot write balances or ledger."""
    def __init__(self,path):
        super().__init__(path)
        self.db.set_authorizer(control_authorizer)


def clock_ms():
    return time.time_ns() // 1_000_000


def voice_epoch(store, session):
    return store.db.execute('SELECT COALESCE(MAX(rowid),0) FROM voice_inputs WHERE session_id=?',(session,)).fetchone()[0]


def ready_draft(store,user,session):
    state=store.session(user,session)
    draft=store.active(user,session)
    if draft is None or json.loads(state['state_json']).get('pending'):
        raise DomainError('REVIEW_NOT_READY','Finish or correct the draft before review.')
    # Read-only chatter cannot erase an unresolved correction or an interrupted turn.
    rows=store.context_rows(user,session)
    if not rows:
        raise DomainError('REVIEW_NOT_READY','Start an explicit transfer before review.')
    resolved=False
    for row in reversed(rows):
        kind=json.loads(row['response_json'])['kind'] if row['response_json'] else 'PENDING'
        if kind=='DRAFT':
            resolved=True
            break
        if kind in ('ERROR','CLARIFY','UNSUPPORTED','CANCELLED','PENDING','ALREADY_COMPLETED','RESTRICTED'):
            raise DomainError('REVIEW_NOT_READY','An earlier request is unresolved. Correct it explicitly or cancel this draft before review.')
    if not resolved:
        raise DomainError('CONTEXT_LIMIT','No resolved draft is available within the review history limit.')
    if store.db.execute("SELECT 1 FROM voice_inputs WHERE session_id=? AND status IN ('PROCESSING','READY')",(session,)).fetchone():
        raise DomainError('VOICE_PENDING','Use or discard the pending recording before review.')
    # CV14 D: a voice amount that is easily misheard (fifteen/fifty) must be confirmed first.
    require_spoken_confirmed(store,session,draft)
    return state,draft


def name_mentions(history, payees):
    """Deterministic string index: which saved payee names/endings literally appear in which raw input.
    Built only from the user's own words and the payee directory, never from the Business Agent's draft,
    so the validator stays independent. It says what was mentioned, not what was intended."""
    import re
    def candidates(fragment, kind):
        if kind == 'full_name':
            return [p['id'] for p in payees if p['name'].casefold() == fragment]
        if kind == 'partial_name':
            return [p['id'] for p in payees if fragment in p['name'].casefold().split()]
        return [p['id'] for p in payees if p['ending'] == fragment]

    found = []
    for row in history:
        text = row['text'].casefold()
        for payee in payees:
            name = payee['name'].casefold()
            word = lambda w: re.search(r'(?<!\w)' + re.escape(w) + r'(?!\w)', text)
            if word(name):
                match, fragment = 'full_name', name
            elif parts := [part for part in name.split() if len(part) >= 3 and word(part)]:
                match, fragment = 'partial_name', parts[0]
            elif payee['ending'] and word(payee['ending']):
                match, fragment = 'account_ending', payee['ending']
            else:
                continue
            # Identify the literal fragment and all directory matches for that fragment.
            # A mention is still not proof of intent (it can be quoted or negated).
            found.append({'input_id': row['input_id'], 'payee_id': payee['id'], 'match': match,
                          'matched_text': fragment, 'candidate_payee_ids': candidates(fragment, match)})
    return found


class UnavailableValidator:
    name='unavailable-validator'
    def validate(self,evidence):
        raise DomainError('VALIDATOR_UNAVAILABLE','A live independent validator is required.')


class IndependentValidator:
    def __init__(self,completion):
        self.completion=completion
        self.name='independent/'+completion.name+'/validator-v13'
    def validate(self,evidence):
        prompt=Path(__file__).with_name('prompts').joinpath('validator.txt').read_text(encoding='utf-8')
        # Reconstruct without seeing the proposed answer. Exact comparison stays in the host.
        unanchored={k:evidence[k] for k in ('raw_history','accounts','payees','name_mentions') if k in evidence}
        return self.completion.complete(prompt,canonical(unanchored),{})
    @property
    def last_metadata(self):
        return dict(self.completion.last_metadata)


# Compatibility for historical evaluation scripts and archived test results.
DeepSeekValidator = IndependentValidator


def parse_result(raw,evidence):
    result=strict_json(raw)
    if type(result) is not dict or set(result)!={'decision','expected','action_count','sources','reason'}:
        raise DomainError('VALIDATOR_CONTRACT','Invalid validator response.')
    if result['decision'] not in ('PASS','DISCREPANCY','NEEDS_CLARIFICATION','ERROR') or type(result['action_count']) is not int:
        raise DomainError('VALIDATOR_CONTRACT','Invalid validator decision.')
    if type(result['reason']) is not str or len(result['reason'])>800 or type(result['sources']) is not dict:
        raise DomainError('VALIDATOR_CONTRACT','Invalid validator evidence.')
    if result['decision']=='PASS':
        expected=result['expected'];fields={'amount','currency','source_account_id','payee_id'}
        ids={row['input_id'] for row in evidence['raw_history']}
        def reconstruct(item,sources):
            if type(item) is not dict or set(item)!=fields or any(type(v) is not str or len(v)>100 for v in item.values()):
                raise DomainError('VALIDATOR_CONTRACT','Validator must reconstruct exact fields.')
            if type(sources) is not dict or set(sources)!=fields or any(type(v) is not str or v not in ids for v in sources.values()):
                raise DomainError('VALIDATOR_CONTRACT','Validator sources must belong to this conversation.')
            amount={'rule':rule_of(item['amount'])} if rule_of(item['amount']) else {'amount_minor':amount_to_minor(item['amount'])}
            return {**amount,'currency':item['currency'],'source_account_id':item['source_account_id'],'payee_id':item['payee_id']}
        if type(expected) is dict and 'actions' in expected:
            # Plan contract: the ordered list of every transfer plus an optional minimum balance.
            if set(expected)!={'actions','keep_at_least'} or type(expected['actions']) is not list or not 1<=len(expected['actions'])<=MAX_BATCH_ACTIONS:
                raise DomainError('VALIDATOR_CONTRACT','Validator must reconstruct the ordered transfer list.')
            src=result['sources']
            if set(src)!={'actions','keep_at_least'} or type(src['actions']) is not list or len(src['actions'])!=len(expected['actions']):
                raise DomainError('VALIDATOR_CONTRACT','Validator sources must cover every transfer.')
            rebuilt=[reconstruct(item,source) for item,source in zip(expected['actions'],src['actions'])]
            keep=expected['keep_at_least']
            if keep is None:
                keep_minor=None
            else:
                if type(keep) is not str or type(src['keep_at_least']) is not str or src['keep_at_least'] not in ids:
                    raise DomainError('VALIDATOR_CONTRACT','A minimum balance needs its own source.')
                keep_minor=amount_to_minor(keep) if keep.strip() not in ('0','0.00') else None
        else:
            rebuilt=[reconstruct(expected,result['sources'])];keep_minor=None
        if any('rule' in r for r in rebuilt):
            # CV14 C: the validator marks "half"/"the rest"/"the bill"; the host resolves them from its own data.
            basis=evidence.get('amount_basis') or {}
            rest=[r for r in rebuilt if r.get('rule')=='REST']
            keep={'account_id':(rest or rebuilt)[0]['source_account_id'],'amount_minor':keep_minor} if keep_minor else None
            try:
                amounts,_=resolve_amounts(rebuilt,basis.get('balances',{}),basis.get('bills',{}),keep)
            except DomainError:
                amounts=[None]*len(rebuilt)
            for r,a in zip(rebuilt,amounts):
                r.pop('rule',None);r['amount_minor']=a
        draft=evidence['exact_draft'];actions=draft['actions']
        frozen_keep=[c['amount_minor'] for c in draft.get('constraints',[])]
        matches=(result['action_count']==len(rebuilt)==len(actions)
                 and all(a.get('type')=='TRANSFER' and a.get('fee_minor')==0 and all(a.get(k)==v for k,v in r.items()) for a,r in zip(actions,rebuilt))
                 and frozen_keep==([keep_minor] if keep_minor is not None else []))
        if not matches:
            result['decision']='DISCREPANCY'
            result['reason']='Independent reconstruction differs from the frozen draft.'
    return result


class ValidationService:
    def __init__(self,store,adapter=None,clock=clock_ms):
        self.store,self.adapter,self.clock=store,adapter or UnavailableValidator(),clock

    @observed('validation', 'independent_validation')
    def run(self,user,session,draft_id,version,content_hash):
        with self.store.transaction():
            state,draft=ready_draft(self.store,user,session)
            if type(version) is not int or (draft_id,version,content_hash)!=(draft['payload']['draft_id'],draft['current_version'],draft['content_hash']):
                raise DomainError('STALE_REVIEW','Draft changed. Reload before review.')
            rows=self.store.context_rows(user,session)
            evidence={'raw_history':[{'input_id':row['id'],'text':row['raw_text']} for row in rows],
                      'exact_draft':draft['payload'],'field_sources':draft['sources'],
                      'accounts':[{'id':a['id'],'name':a['name'],'currency':a['currency']} for a in self.store.accounts(user)],
                      'payees':[dict(p) for p in self.store.db.execute("SELECT p.id,p.name,p.ending,COALESCE(f.kind,'PERSON') AS kind FROM payees p LEFT JOIN payee_profiles f ON f.payee_id=p.id WHERE p.user_id=? AND p.active=1",(user,))]}
            # Host-only data for resolving relative amounts; never sent to the model (see DeepSeekValidator.validate).
            evidence['amount_basis']={'balances':{a['id']:a['balance_minor'] for a in self.store.accounts(user)},
                                      'bills':{pid:p.get('due_minor') for pid,p in self.store.profiles(user).items() if p['kind']=='BILL'}}
            evidence['name_mentions']=name_mentions(evidence['raw_history'],evidence['payees'])
            validation_id=uid('validation');epoch=voice_epoch(self.store,session)
            self.store.db.execute("INSERT INTO validation_results VALUES (?,?,?,?,?,?,?,'PROCESSING',NULL,?,?)",
                                  (validation_id,session,draft_id,version,content_hash,state['revision'],epoch,self.adapter.name,self.clock()))
            self.store.audit(session,'VALIDATION_STARTED',{'validation_id':validation_id,'draft_id':draft_id,'version':version})
        # Model I/O is outside all write transactions. No main-agent summaries are provided.
        try:
            result=parse_result(self.adapter.validate(evidence),evidence)
        except Exception as exc:
            result={'decision':'ERROR','reason':'Independent validation failed. No authorization is available.',
                    'code':exc.code if isinstance(exc,DomainError) else 'VALIDATOR_UNAVAILABLE'}
            if result['code']=='PROVIDER_INCOMPLETE':
                finish=getattr(self.adapter,'last_metadata',{}).get('finish_reason')
                result['reason']=('The independent check reached its response length limit. No authorization was created.'
                                  if finish=='length' else 'The independent check returned an incomplete response. No authorization was created.')
        with self.store.transaction():
            try:
                current,active=ready_draft(self.store,user,session)
                valid=(current['revision']==state['revision'] and voice_epoch(self.store,session)==epoch
                       and active['payload']['draft_id']==draft_id and active['current_version']==version and active['content_hash']==content_hash)
            except DomainError:
                valid=False
            if not valid:
                result={'decision':'STALE','reason':'New input or draft state superseded this validation.'}
            metadata=getattr(self.adapter,'last_metadata',{})
            stored={'result':result,'provider':metadata}
            self.store.db.execute('UPDATE validation_results SET status=?,result_json=? WHERE id=?',
                                  (result['decision'],canonical(stored),validation_id))
            self.store.audit(session,'VALIDATION_COMPLETED',{'validation_id':validation_id,'status':result['decision']})
        return {'id':validation_id,'draft_id':draft_id,'version':version,'content_hash':content_hash,**result}
