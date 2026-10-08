"""Host-only mock authorization and atomic double-entry execution gateway.

No bank integration. Browser test keys demonstrate proof binding, not real identity.
"""
import base64
from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import json
import secrets
import sqlite3
from .domain import DomainError, canonical, digest, format_money, MAX_BATCH_ACTIONS
from .store import uid
from .access import GATEWAY_READS, read_allowed
from .validation import ControlStore, control_authorizer, ready_draft, voice_epoch, clock_ms


def gateway_authorizer(action,table,column,database,trigger):
    if action == sqlite3.SQLITE_READ and read_allowed(GATEWAY_READS, table, column, database):
        return sqlite3.SQLITE_OK
    if action==sqlite3.SQLITE_UPDATE and table=='accounts' and column=='balance_minor':
        return sqlite3.SQLITE_OK
    if action==sqlite3.SQLITE_INSERT and table in {'ledger_entries','ledger_links','ledger_action_links','execution_records','demo_resets','demo_topup_links'}:
        return sqlite3.SQLITE_OK
    return control_authorizer(action,table,column,database,trigger)


class GatewayStore(ControlStore):
    def __init__(self,path):
        super().__init__(path)
        self.db.set_authorizer(gateway_authorizer)


@dataclass(frozen=True)
class RiskPolicy:
    single_limit: int = 100000
    daily_limit: int = 150000
    extra_confirmation_at: int = 80000
    frequency_count: int = 3
    frequency_window_ms: int = 60000


def decode64(value,maximum):
    if type(value) is not str or len(value)>maximum*2:
        raise DomainError('PROOF','Invalid proof encoding.')
    try:
        data=base64.b64decode(value,validate=True)
    except Exception:
        raise DomainError('PROOF','Invalid proof encoding.') from None
    if not data or len(data)>maximum:
        raise DomainError('PROOF','Invalid proof size.')
    return data


DEMO_RESET_ENABLED=False  # DEMO ONLY: set False (and remove the Settings control) for a formal release.
RP_ID='localhost'  # WebAuthn relying-party ID. IP literals are not valid RP IDs, so the page must be opened on localhost.


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def decode_url(value,maximum):
    if type(value) is not str or not 1<=len(value)<=maximum*2 or not all(c.isalnum() or c in '-_' for c in value):
        raise DomainError('PROOF','Invalid WebAuthn encoding.')
    try:
        data=base64.urlsafe_b64decode(value+'='*(-len(value)%4))
    except Exception:
        raise DomainError('PROOF','Invalid WebAuthn encoding.') from None
    if not data or len(data)>maximum:
        raise DomainError('PROOF','Invalid WebAuthn size.')
    return data


def client_data(raw,kind,challenge,origin):
    try:
        data=json.loads(raw.decode('utf-8'))
    except Exception:
        raise DomainError('PROOF','Invalid WebAuthn client data.') from None
    if type(data) is not dict or data.get('type')!=kind:
        raise DomainError('PROOF','Unexpected WebAuthn ceremony type.')
    if data.get('challenge')!=challenge:
        raise DomainError('PROOF','WebAuthn challenge does not match this frozen transaction.')
    if data.get('origin')!=origin or data.get('crossOrigin') is True:
        raise DomainError('PROOF','WebAuthn origin does not match the local app.')


def authenticator_flags(data):
    """rpIdHash(32) | flags(1) | signCount(4) ... ; user presence AND user verification are required."""
    if len(data)<37 or data[:32]!=hashlib.sha256(RP_ID.encode()).digest():
        raise DomainError('PROOF','WebAuthn relying party does not match.')
    if data[32]&0x01!=0x01 or data[32]&0x04!=0x04:
        raise DomainError('PROOF','Device biometric/PIN verification (UV) is required.')
    return int.from_bytes(data[33:37],'big')


def public_key(spki):
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        key=load_der_public_key(spki)
        if not isinstance(key,ec.EllipticCurvePublicKey) or not isinstance(key.curve,ec.SECP256R1):
            raise ValueError()
        return key
    except Exception:
        raise DomainError('PROOF','Only a valid P-256 demo public key is accepted.') from None


def plan_projection(store,user,draft):
    """Deterministic review of a frozen multi-action plan; every object is re-read owner-scoped."""
    payload=draft['payload'];actions=[]
    for ordinal,action in enumerate(payload['actions'],1):
        account=store.account(user,action['source_account_id']);payee=store.payee(user,action['payee_id'])
        actions.append({'ordinal':ordinal,'action_id':action['action_id'],'source':{k:account[k] for k in ('id','name','currency')},
                        'payee':{k:payee[k] for k in ('id','name','ending')},'amount_minor':action['amount_minor'],
                        'fee_minor':action['fee_minor'],'currency':action['currency']})
    constraints=[{'type':c['type'],'account_id':c['account_id'],'account_name':store.account(user,c['account_id'])['name'],'amount_minor':c['amount_minor']} for c in payload['constraints']]
    total=sum(a['amount_minor']+a['fee_minor'] for a in actions)
    return {'draft_id':payload['draft_id'],'version':draft['current_version'],'content_hash':draft['content_hash'],'plan':True,
            'execution_policy':payload['execution_policy'],'actions':actions,'constraints':constraints,
            'source':actions[0]['source'],'payee':actions[0]['payee'],'amount_minor':total,'fee_minor':0,'total_minor':total,'currency':'SGD'}


def projection(store,user,draft):
    if draft['payload'].get('schema_version')==2:
        return plan_projection(store,user,draft)
    action=draft['payload']['actions'][0]
    account=store.account(user,action['source_account_id'])
    payee=store.payee(user,action['payee_id'])
    return {'draft_id':draft['payload']['draft_id'],'version':draft['current_version'],'content_hash':draft['content_hash'],
            'source':{k:account[k] for k in ('id','name','currency')},'payee':{k:payee[k] for k in ('id','name','ending')},
            'amount_minor':action['amount_minor'],'fee_minor':action['fee_minor'],'total_minor':action['amount_minor']+action['fee_minor'],'currency':action['currency']}


class PaymentService:
    def __init__(self,store,clock=clock_ms,policy=None,fault=None):
        self.store,self.clock,self.policy,self.fault=store,clock,policy or RiskPolicy(),fault or (lambda _:None)

    def register_key(self,user,encoded):
        spki=decode64(encoded,256);public_key(spki)
        key_id='key_'+hashlib.sha256(user.encode()+spki).hexdigest()
        with self.store.transaction():
            old=self.store.db.execute('SELECT id FROM demo_public_keys WHERE id=? AND user_id=?',(key_id,user)).fetchone()
            if not old:
                if self.store.db.execute('SELECT COUNT(*) FROM demo_public_keys WHERE user_id=?',(user,)).fetchone()[0]>=1000:
                    raise DomainError('KEY_LIMIT','Demo key limit reached.')
                self.store.db.execute('INSERT INTO demo_public_keys VALUES (?,?,?,?)',(key_id,user,spki,self.clock()))
        return {'key_id':key_id,'mode':'MOCK_BROWSER_KEY_NOT_IDENTITY_VERIFICATION'}

    def begin_webauthn_registration(self,user):
        timestamp=self.clock();registration=uid('webauthn');challenge=b64url(secrets.token_bytes(32))
        with self.store.transaction():
            self.store.db.execute("INSERT INTO webauthn_registrations VALUES (?,?,?,?,?,'ISSUED')",(registration,user,challenge,timestamp,timestamp+120000))
        # The user handle is an opaque per-user value; no name, email or account data is sent to the authenticator.
        return {'id':registration,'challenge':challenge,'rp_id':RP_ID,'user_handle':b64url(hashlib.sha256(b'dcta-user:'+user.encode()).digest()[:16]),'expires_ms':timestamp+120000}

    def finish_webauthn_registration(self,user,registration,credential_id,spki,client_json,authenticator_data,origin):
        raw_client=decode_url(client_json,2048);auth=decode_url(authenticator_data,1024);decode_url(credential_id,1024)
        key=decode64(spki,256);public_key(key)
        with self.store.transaction():
            row=self.store.db.execute("SELECT * FROM webauthn_registrations WHERE id=? AND user_id=?",(registration,user)).fetchone()
            if not row or row['status']!='ISSUED' or not row['issued_ms']<=self.clock()<row['expires_ms']:
                raise DomainError('PROOF','Registration challenge expired or already used. Try again.')
            self.store.db.execute("UPDATE webauthn_registrations SET status='CONSUMED' WHERE id=?",(registration,))
        # Single use even when verification below fails: a registration challenge is never retried.
        with self.store.transaction():
            client_data(raw_client,'webauthn.create',row['challenge'],origin)
            count=authenticator_flags(auth)
            key_id='key_'+hashlib.sha256(user.encode()+key).hexdigest()
            if not self.store.db.execute('SELECT 1 FROM demo_public_keys WHERE id=?',(key_id,)).fetchone():
                self.store.db.execute('INSERT INTO demo_public_keys VALUES (?,?,?,?)',(key_id,user,key,self.clock()))
            if self.store.db.execute('SELECT 1 FROM webauthn_credentials WHERE key_id=? OR credential_id=?',(key_id,credential_id)).fetchone():
                raise DomainError('PROOF','This credential is already registered.')
            self.store.db.execute('INSERT INTO webauthn_credentials VALUES (?,?,?,?,?,?)',(key_id,user,credential_id,origin,count,self.clock()))
        return {'key_id':key_id,'credential_id':credential_id,'mode':'PLATFORM_AUTHENTICATOR_USER_VERIFIED'}

    def webauthn_known(self,user,key_id):
        if type(key_id) is not str or len(key_id)>100:raise DomainError('INPUT','Invalid key.')
        row=self.store.db.execute('SELECT credential_id FROM webauthn_credentials WHERE key_id=? AND user_id=?',(key_id,user)).fetchone()
        return {'known':bool(row)}

    def challenge(self,user,session,validation_id,key_id):
        from .history import require_visible
        rejected=None
        with self.store.transaction():
            # Serialize with hiding: a late review cannot make a hidden payment actionable.
            require_visible(self.store,user,session)
            state,draft=ready_draft(self.store,user,session)
            validation=self.store.db.execute('SELECT * FROM validation_results WHERE id=? AND session_id=?',(validation_id,session)).fetchone()
            key=self.store.db.execute('SELECT 1 FROM demo_public_keys WHERE id=? AND user_id=?',(key_id,user)).fetchone()
            epoch=voice_epoch(self.store,session)
            if not validation or validation['status']!='PASS' or not key:
                raise DomainError('VALIDATION_REQUIRED','A current independent PASS and an owned demo key are required.')
            if (validation['draft_id'],validation['version'],validation['content_hash'],validation['revision'],validation['voice_epoch'])!=(draft['payload']['draft_id'],draft['current_version'],draft['content_hash'],state['revision'],epoch):
                raise DomainError('STALE_REVIEW','Validation no longer matches the current draft and input.')
            # Preflight is advisory; exactly the same deterministic gates run again at execution.
            review=projection(self.store,user,draft)
            try:self.risk(user,review,self.clock())
            except DomainError as exc:
                self.record_block(user,draft,exc.code,'CONFIRMATION')
                rejected=exc
            if rejected is None:
                self.reconcile()
                timestamp=self.clock();challenge_id=uid('challenge')
                lifetime=300000 if self.needs_stepup(review) else 120000  # the second check needs time to read a code
                payload={'protocol':'dcta-mock-authorization-v1','user_id':user,'session_id':session,'draft_id':review['draft_id'],
                         'version':review['version'],'content_hash':review['content_hash'],'validation_id':validation_id,'key_id':key_id,
                         'revision':state['revision'],'voice_epoch':epoch,'challenge_id':challenge_id,'nonce':secrets.token_hex(24),
                         'issued_ms':timestamp,'expires_ms':timestamp+lifetime,'review':review}
                self.store.db.execute("UPDATE authorization_challenges SET status='INVALIDATED' WHERE session_id=? AND status IN ('ISSUED','AUTHORIZED')",(session,))
                self.store.db.execute("INSERT INTO authorization_challenges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ISSUED')",
                    (challenge_id,user,session,review['draft_id'],review['version'],review['content_hash'],validation_id,key_id,state['revision'],epoch,timestamp,timestamp+lifetime,canonical(payload)))
                self.store.audit(session,'CHALLENGE_ISSUED',{'challenge_id':challenge_id,'draft_id':review['draft_id'],'version':review['version']})
        # A refusal event commits without issuing an authorization challenge.
        if rejected is not None:raise rejected
        return {'id':challenge_id,'review':review,'expires_ms':payload['expires_ms'],'signing_text':canonical(payload),'key_id':key_id,'revision':state['revision'],'voice_epoch':epoch,
                'stepup_required':self.needs_stepup(review)}

    def eligibility(self,user,draft):
        """Read-only advisory rules; passing is never authorization."""
        if not draft or draft['state']!='DRAFTED' or draft['payload']['version']!=draft['current_version']:return None
        with self.store.read_transaction():
            self.store.session(user,draft['session_id'])
            try:self.risk(user,projection(self.store,user,draft),self.clock())
            except DomainError as exc:
                messages={
                    'SINGLE_LIMIT':f'This amount exceeds the mock single-transfer limit of {format_money(self.policy.single_limit)}. Change the amount or cancel the draft.',
                    'DAILY_LIMIT':'This transfer would exceed the mock daily limit. Change the amount, cancel, or review again on a later UTC day.',
                    'RATE_LIMIT':'Recent completed transfers reached the mock frequency limit. Wait, then refresh and review again.',
                    'INSUFFICIENT_FUNDS':'The source account has insufficient mock funds. Change the amount or source, or cancel the draft.',
                    'MIN_BALANCE':'These transfers would leave less than the minimum balance you asked to keep. Change an amount or the condition, or cancel. Nothing is sent partially.',
                    'KYC':'The mock user verification is not approved. This transfer cannot continue.',
                    'ACCOUNT':'The source account is unavailable for this transfer. Choose an eligible source or cancel.',
                    'NOT_FOUND':'An account or recipient is unavailable. Update the transfer details or cancel.',
                }
                return {'blocked':True,'code':exc.code,'message':messages.get(exc.code,'These transfer details cannot proceed. Update them or cancel the draft.')}
            if self.needs_stepup(projection(self.store,user,draft)):
                return {'blocked':False,'code':None,'stepup':True,
                        'message':f'Rules currently pass. From {format_money(self.policy.extra_confirmation_at)} an extra security check is needed: after your device approval, enter the 6-digit code from your security token.'}
        return {'blocked':False,'code':None,'message':'Rules currently pass. Independent validation and your confirmation are still required.'}

    def needs_stepup(self,review):
        """CV14 A: a single transfer or a combined plan from the step-up threshold needs the out-of-band code."""
        return review['total_minor']>=self.policy.extra_confirmation_at

    def require_stepup(self,challenge_id,review):
        """Deterministic gate at execution: no verified second check for this exact authorization, no money moves."""
        if not self.needs_stepup(review):return
        row=self.store.db.execute("SELECT 1 FROM stepup_checks WHERE challenge_id=? AND status='VERIFIED' AND amount_minor=?",(challenge_id,review['total_minor'])).fetchone()
        if not row:raise DomainError('STEPUP_REQUIRED','This amount needs the extra security check. Enter the code from your security token first.')

    def record_block(self,user,draft,code,phase):
        """Deduplicated version/reason/stage evidence, not an attempt counter."""
        session=draft['session_id'];self.store.session(user,session)
        details={'draft_id':draft['payload']['draft_id'],'version':draft['payload']['version'],
                 'content_hash':draft['content_hash'],'code':code,'phase':phase}
        with self.store.transaction():
            if not self.store.db.execute("SELECT 1 FROM audit_events WHERE session_id=? AND event='PAYMENT_RULE_BLOCKED' AND details_json=?",(session,canonical(details))).fetchone():
                self.store.audit(session,'PAYMENT_RULE_BLOCKED',details)

    def _challenge(self,user,session,challenge_id):
        self.store.session(user,session)
        row=self.store.db.execute('SELECT * FROM authorization_challenges WHERE id=? AND user_id=? AND session_id=?',(challenge_id,user,session)).fetchone()
        if not row:
            raise DomainError('NOT_FOUND','Authorization challenge unavailable.')
        return dict(row)

    def _verify(self,challenge,key_id,signature):
        if key_id!=challenge['key_id']:
            raise DomainError('PROOF','Signature key does not match the frozen challenge.')
        row=self.store.db.execute('SELECT spki FROM demo_public_keys WHERE id=? AND user_id=?',(key_id,challenge['user_id'])).fetchone()
        if not row:
            raise DomainError('PROOF','Signature key unavailable.')
        credential=self.store.db.execute('SELECT * FROM webauthn_credentials WHERE key_id=? AND user_id=?',(key_id,challenge['user_id'])).fetchone()
        if credential:
            return self._verify_webauthn(challenge,row['spki'],credential,signature)
        raw=decode64(signature,64)
        if len(raw)!=64:
            raise DomainError('PROOF','P-256 signatures must be exactly 64 bytes.')
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec,utils
        try:
            der=utils.encode_dss_signature(int.from_bytes(raw[:32],'big'),int.from_bytes(raw[32:],'big'))
            public_key(row['spki']).verify(der,challenge['payload_json'].encode('utf-8'),ec.ECDSA(hashes.SHA256()))
        except Exception:
            raise DomainError('PROOF','Signature verification failed.') from None

    def _verify_webauthn(self,challenge,spki,credential,proof):
        """Assertion over authenticatorData || SHA-256(clientDataJSON), whose challenge is SHA-256 of the frozen payload."""
        try:
            assertion=json.loads(proof) if type(proof) is str and len(proof)<=8000 else None
        except ValueError:
            assertion=None
        if type(assertion) is not dict or set(assertion)!={'credential_id','authenticator_data','client_data','signature'}:
            raise DomainError('PROOF','A device-verified WebAuthn assertion is required for this key.')
        if assertion['credential_id']!=credential['credential_id']:
            raise DomainError('PROOF','Assertion came from a different credential.')
        raw_client=decode_url(assertion['client_data'],2048);auth=decode_url(assertion['authenticator_data'],1024)
        signature=decode_url(assertion['signature'],80)
        client_data(raw_client,'webauthn.get',b64url(hashlib.sha256(challenge['payload_json'].encode('utf-8')).digest()),credential['origin'])
        count=authenticator_flags(auth)
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        try:
            public_key(spki).verify(signature,auth+hashlib.sha256(raw_client).digest(),ec.ECDSA(hashes.SHA256()))
        except Exception:
            raise DomainError('PROOF','WebAuthn signature verification failed.') from None
        return count

    def authorize_webauthn(self,user,session,challenge_id,key_id,assertion):
        if type(assertion) is not dict:raise DomainError('PROOF','Invalid WebAuthn assertion.')
        proof=canonical(assertion)
        challenge=self._challenge(user,session,challenge_id)
        count=self._verify(challenge,key_id,proof)
        if count is None:raise DomainError('PROOF','This key is not a platform authenticator credential.')
        with self.store.transaction():
            stored=self.store.db.execute('SELECT sign_count FROM webauthn_credentials WHERE key_id=?',(key_id,)).fetchone()[0]
            # A non-increasing non-zero counter can indicate a cloned authenticator.
            if (count or stored) and count<=stored:
                raise DomainError('PROOF','Authenticator counter did not increase. Authorization refused.')
            self.store.db.execute('UPDATE webauthn_credentials SET sign_count=? WHERE key_id=?',(count,key_id))
        return self.authorize(user,session,challenge_id,key_id,proof,verified=True)

    def _current(self,user,session,challenge):
        timestamp=self.clock()
        if timestamp<challenge['issued_ms']:
            raise DomainError('CLOCK_CHANGE','Clock moved backwards. Review again.')
        if timestamp>=challenge['expires_ms']:
            raise DomainError('EXPIRED','Authorization expired. Review again.')
        if challenge['status'] not in ('ISSUED','AUTHORIZED'):
            raise DomainError('AUTH_STATE','Authorization was invalidated or consumed.')
        state,draft=ready_draft(self.store,user,session)
        if (state['revision'],voice_epoch(self.store,session),draft['payload']['draft_id'],draft['current_version'],draft['content_hash'])!=(challenge['revision'],challenge['voice_epoch'],challenge['draft_id'],challenge['version'],challenge['content_hash']):
            raise DomainError('STALE_AUTHORIZATION','New input or draft version invalidated this authorization.')
        review=projection(self.store,user,draft)
        if review!=json.loads(challenge['payload_json'])['review']:
            raise DomainError('STALE_AUTHORIZATION','Displayed account or payee details changed.')
        row=self.store.db.execute('SELECT status FROM validation_results WHERE id=?',(challenge['validation_id'],)).fetchone()
        if not row or row[0]!='PASS':
            raise DomainError('VALIDATION_REQUIRED','The exact validation must still be PASS.')
        return review

    def authorize(self,user,session,challenge_id,key_id,signature,verified=False):
        challenge=self._challenge(user,session,challenge_id)
        if self._verify(challenge,key_id,signature) is not None and not verified:
            raise DomainError('PROOF','Use the device authenticator ceremony for this key.')
        self.fault('after_signature')
        with self.store.transaction():
            challenge=self._challenge(user,session,challenge_id)
            self._current(user,session,challenge)
            self.store.db.execute('INSERT OR IGNORE INTO authorization_proofs VALUES (?,?,?,?)',(challenge_id,key_id,signature,self.clock()))
            self.store.db.execute("UPDATE authorization_challenges SET status='AUTHORIZED' WHERE id=?",(challenge_id,))
            self.store.audit(session,'DEVICE_BIOMETRIC_VERIFIED' if verified else 'MOCK_PROOF_VERIFIED',{'challenge_id':challenge_id,'key_id':key_id})
        review=json.loads(challenge['payload_json'])['review']
        return {'status':'AUTHORIZED','challenge_id':challenge_id,'expires_ms':challenge['expires_ms'],'stepup_required':self.needs_stepup(review)}

    def risk(self,user,review,timestamp):
        if review.get('plan'):
            return self.risk_plan(user,review,timestamp)
        amount=review['total_minor'];rules=self.policy
        account=self.store.account(user,review['source']['id'])
        payee=self.store.payee(user,review['payee']['id'])
        kyc=self.store.db.execute('SELECT kyc_status FROM users WHERE id=?',(user,)).fetchone()
        if not kyc or kyc[0]!='VERIFIED': raise DomainError('KYC','Mock KYC is not verified.')
        if account['status']!='ACTIVE' or account['currency']!='SGD': raise DomainError('ACCOUNT','An active SGD account is required.')
        if review['currency']!='SGD' or review['fee_minor']!=0 or amount!=review['amount_minor']:
            raise DomainError('PAYLOAD','Unsupported payment fields or fee.')
        if type(amount) is not int or amount<=0: raise DomainError('AMOUNT','Invalid amount.')
        if amount>rules.single_limit: raise DomainError('SINGLE_LIMIT','Mock single-transfer limit exceeded.')
        spent,count=self.usage(user,timestamp)
        if spent+amount>rules.daily_limit: raise DomainError('DAILY_LIMIT','Mock daily limit exceeded.')
        if count>=rules.frequency_count: raise DomainError('RATE_LIMIT','Mock transfer frequency limit reached.')
        if account['balance_minor']<amount: raise DomainError('INSUFFICIENT_FUNDS','Current mock balance is insufficient.')

    def usage(self,user,timestamp):
        """Daily (UTC) spend and recent-transfer count. A demo reset starts both counters again; receipts are kept."""
        day_start=timestamp-timestamp%86_400_000  # Explicit demo policy: UTC calendar day.
        reset=self.store.db.execute('SELECT COALESCE(MAX(reset_ms),-1) FROM demo_resets WHERE user_id=?',(user,)).fetchone()[0]
        spent=self.store.db.execute('SELECT COALESCE(SUM(amount_minor),0) FROM execution_records WHERE user_id=? AND created_ms>=? AND created_ms>?',(user,day_start,reset)).fetchone()[0]
        count=self.store.db.execute('SELECT COUNT(*) FROM execution_records WHERE user_id=? AND created_ms>? AND created_ms>?',(user,timestamp-self.policy.frequency_window_ms,reset)).fetchone()[0]
        return spent,count

    def reset_demo(self,user):
        """DEMO ONLY. Restart the limit counters and top each active SGD account back up to its opening balance
        with balanced, linked ledger entries (from the mock clearing account). Receipts and audit history remain."""
        if not DEMO_RESET_ENABLED:raise DomainError('DISABLED','Demo reset is disabled in this build.')
        with self.store.transaction():
            self.reconcile()
            timestamp=self.clock();reset_id=uid('demo_reset');topups=[]
            rows=self.store.db.execute("SELECT a.id,a.name,a.balance_minor,o.balance_minor AS opening FROM accounts a JOIN opening_balances o ON o.account_id=a.id WHERE a.user_id=? AND a.status='ACTIVE' AND a.currency='SGD' ORDER BY a.id",(user,)).fetchall()
            for row in rows:
                if row['balance_minor']<row['opening']:
                    topups.append({'account_id':row['id'],'name':row['name'],'amount_minor':row['opening']-row['balance_minor'],'balance_after_minor':row['opening']})
            clearing=self.store.db.execute("SELECT balance_minor FROM accounts WHERE id='CLEARING_SGD'").fetchone()[0]
            if sum(t['amount_minor'] for t in topups)>clearing:raise DomainError('RECONCILIATION','Mock clearing funds are insufficient for a demo top-up.')
            self.store.db.execute('INSERT INTO demo_resets VALUES (?,?,?,?,?)',(reset_id,user,timestamp,canonical({'mode':'DEMO_ONLY','topups':topups}),datetime.now(timezone.utc).isoformat()))
            for t in topups:
                self.store.db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE id=? AND user_id=?',(t['amount_minor'],t['account_id'],user))
                self.store.db.execute("UPDATE accounts SET balance_minor=balance_minor-? WHERE id='CLEARING_SGD' AND balance_minor>=?",(t['amount_minor'],t['amount_minor']))
                if self.store.db.execute('SELECT changes()').fetchone()[0]!=1:raise DomainError('RECONCILIATION','Clearing balance changed.')
                for role,account,delta in (('TOPUP',t['account_id'],t['amount_minor']),('CLEARING','CLEARING_SGD',-t['amount_minor'])):
                    entry=uid('entry');self.store.db.execute('INSERT INTO ledger_entries VALUES (?,?,?,?)',(entry,account,delta,'SGD'))
                    self.store.db.execute('INSERT INTO demo_topup_links VALUES (?,?,?,?)',(entry,reset_id,t['account_id'],role))
            self.reconcile()
        return {'status':'RESET','mode':'DEMO_ONLY','reset_id':reset_id,'topups':topups,
                'message':'Demo limits restarted'+(' and balances restored: '+', '.join(f"{t['name']} +{format_money(t['amount_minor'])}" for t in topups) if topups else '; balances were already at their starting amounts')+'. Receipts and history are kept.'}

    def risk_plan(self,user,review,timestamp):
        """The same deterministic gates for a plan. Limits apply per transfer AND to the combined authorization,
        so splitting a payment into several actions cannot bypass a threshold."""
        rules=self.policy;actions=review['actions']
        kyc=self.store.db.execute('SELECT kyc_status FROM users WHERE id=?',(user,)).fetchone()
        if not kyc or kyc[0]!='VERIFIED': raise DomainError('KYC','Mock KYC is not verified.')
        if not 1<=len(actions)<=MAX_BATCH_ACTIONS or review['currency']!='SGD' or review['fee_minor']!=0: raise DomainError('PAYLOAD','Unsupported plan fields.')
        per_source={}
        for action in actions:
            account=self.store.account(user,action['source']['id']);self.store.payee(user,action['payee']['id'])
            if account['status']!='ACTIVE' or account['currency']!='SGD': raise DomainError('ACCOUNT','An active SGD account is required.')
            amount=action['amount_minor']
            if action['currency']!='SGD' or action['fee_minor']!=0: raise DomainError('PAYLOAD','Unsupported payment fields or fee.')
            if type(amount) is not int or amount<=0: raise DomainError('AMOUNT','Invalid amount.')
            if amount>rules.single_limit: raise DomainError('SINGLE_LIMIT','Mock single-transfer limit exceeded.')
            per_source[account['id']]=per_source.get(account['id'],0)+amount
        total=review['total_minor']
        if total!=sum(a['amount_minor'] for a in actions): raise DomainError('PAYLOAD','Plan total mismatch.')
        spent,count=self.usage(user,timestamp)
        if spent+total>rules.daily_limit: raise DomainError('DAILY_LIMIT','Mock daily limit exceeded.')
        if count>=rules.frequency_count: raise DomainError('RATE_LIMIT','Mock transfer frequency limit reached.')
        for account_id,needed in per_source.items():
            if self.store.account(user,account_id)['balance_minor']<needed: raise DomainError('INSUFFICIENT_FUNDS','Current mock balance is insufficient for the whole plan.')
        for constraint in review['constraints']:
            balance=self.store.account(user,constraint['account_id'])['balance_minor']
            if balance-per_source.get(constraint['account_id'],0)<constraint['amount_minor']:
                raise DomainError('MIN_BALANCE','The plan would breach the requested minimum balance.')

    def execute_plan(self,user,session,challenge_id,review,timestamp):
        """All-or-nothing: every debit/credit, receipt and audit commits in one SQLite transaction or none does."""
        execution_id=uid('execution');before={};results=[];entries=[]
        for action in review['actions']:
            source=action['source']['id'];amount=action['amount_minor']
            before.setdefault(source,self.store.account(user,source)['balance_minor'])
            self.store.db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE id=? AND balance_minor>=?',(amount,source,amount))
            if self.store.db.execute('SELECT changes()').fetchone()[0]!=1: raise DomainError('INSUFFICIENT_FUNDS','Balance changed.')
            if action['ordinal']==1:self.fault('after_debit')
            self.store.db.execute("UPDATE accounts SET balance_minor=balance_minor+? WHERE id='CLEARING_SGD'",(amount,))
            results.append({**action,'status':'SUCCEEDED','balance_after_minor':self.store.account(user,source)['balance_minor']})
            entries.append((action['ordinal'],source,amount))
        for constraint in review['constraints']:
            if self.store.account(user,constraint['account_id'])['balance_minor']<constraint['amount_minor']:
                raise DomainError('MIN_BALANCE','The plan would breach the requested minimum balance.')
        first=review['actions'][0]['source']['id'];total=review['total_minor']
        receipt={'status':'SUCCEEDED','mode':'MOCK_ONLY','plan':True,'execution_policy':'ALL_OR_NOTHING','execution_id':execution_id,
                 'draft_id':review['draft_id'],'version':review['version'],'content_hash':review['content_hash'],'actions':results,
                 'constraints':review['constraints'],'source':review['source'],'payee':review['payee'],'currency':'SGD','amount_minor':total,
                 'fee_minor':0,'balance_before_minor':before[first],'balance_after_minor':self.store.account(user,first)['balance_minor'],'created_ms':timestamp}
        self.store.db.execute('INSERT INTO execution_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (execution_id,user,session,review['draft_id'],review['version'],review['content_hash'],challenge_id,total,first,review['payee']['id'],timestamp,canonical(receipt)))
        for ordinal,source,amount in entries:
            for role,account,delta in [('DEBIT',source,-amount),('CREDIT','CLEARING_SGD',amount)]:
                entry=uid('entry');self.store.db.execute('INSERT INTO ledger_entries VALUES (?,?,?,?)',(entry,account,delta,'SGD'))
                self.store.db.execute('INSERT INTO ledger_action_links VALUES (?,?,?,?)',(entry,execution_id,ordinal,role))
        self.fault('after_ledger')
        self.store.db.execute("UPDATE authorization_challenges SET status='CONSUMED' WHERE id=?",(challenge_id,))
        self.store.db.execute("UPDATE transaction_drafts SET state='SUCCEEDED' WHERE id=?",(review['draft_id'],))
        self.store.db.execute('UPDATE sessions SET state_json=? WHERE id=?',(canonical({'last_execution_id':execution_id}),session))
        self.store.audit(session,'MOCK_EXECUTION_SUCCEEDED',{'execution_id':execution_id,'draft_id':review['draft_id'],'version':review['version'],'amount_minor':total,'actions':len(results)})
        self.fault('after_audit')
        return receipt

    def reconcile(self):
        rows=self.store.db.execute('SELECT a.id,a.balance_minor,o.balance_minor AS opening,COALESCE(SUM(l.delta_minor),0) AS movement FROM accounts a LEFT JOIN opening_balances o ON o.account_id=a.id LEFT JOIN ledger_entries l ON l.account_id=a.id GROUP BY a.id').fetchall()
        if any(r['opening'] is None or r['balance_minor']!=r['opening']+r['movement'] for r in rows):
            raise DomainError('RECONCILIATION','Mock ledger and balances disagree. Execution stopped; no automatic repair.')
        if self.store.db.execute('SELECT 1 FROM ledger_entries l LEFT JOIN ledger_links x ON x.entry_id=l.id LEFT JOIN ledger_action_links y ON y.entry_id=l.id LEFT JOIN demo_topup_links z ON z.entry_id=l.id WHERE x.entry_id IS NULL AND y.entry_id IS NULL AND z.entry_id IS NULL LIMIT 1').fetchone():
            raise DomainError('RECONCILIATION','An unlinked ledger entry was found.')
        # Every demo top-up is a balanced pair: +X to the account, -X from mock clearing.
        for pair in self.store.db.execute("SELECT z.reset_id,z.account_id,SUM(l.delta_minor) AS net,COUNT(*) AS n,SUM(CASE WHEN z.role='TOPUP' AND l.account_id=z.account_id AND l.delta_minor>0 THEN 1 WHEN z.role='CLEARING' AND l.account_id='CLEARING_SGD' AND l.delta_minor<0 THEN 1 ELSE 0 END) AS ok FROM demo_topup_links z JOIN ledger_entries l ON l.id=z.entry_id GROUP BY z.reset_id,z.account_id"):
            if pair['net']!=0 or pair['n']!=2 or pair['ok']!=2:
                raise DomainError('RECONCILIATION','An unbalanced demo top-up was found.')
        for execution in self.store.db.execute('SELECT * FROM execution_records'):
            receipt=json.loads(execution['receipt_json'])
            if receipt.get('plan'):
                entries=self.store.db.execute('SELECT l.*,y.role,y.ordinal FROM ledger_entries l JOIN ledger_action_links y ON y.entry_id=l.id WHERE y.execution_id=?',(execution['id'],)).fetchall()
                expected=set()
                for action in receipt.get('actions',[]):
                    expected|={(action['ordinal'],'DEBIT',action['source']['id'],-action['amount_minor'],'SGD'),(action['ordinal'],'CREDIT','CLEARING_SGD',action['amount_minor'],'SGD')}
                legacy=self.store.db.execute('SELECT 1 FROM ledger_links WHERE execution_id=? LIMIT 1',(execution['id'],)).fetchone()
                if (legacy or not expected or len(entries)!=len(expected) or {(e['ordinal'],e['role'],e['account_id'],e['delta_minor'],e['currency']) for e in entries}!=expected
                        or sum(a['amount_minor'] for a in receipt['actions'])!=execution['amount_minor']):
                    raise DomainError('RECONCILIATION','An incomplete or unbalanced plan execution was found.')
                if receipt.get('execution_id')!=execution['id'] or receipt.get('amount_minor')!=execution['amount_minor'] or receipt.get('status')!='SUCCEEDED':
                    raise DomainError('RECONCILIATION','Receipt integrity check failed.')
                continue
            entries=self.store.db.execute('SELECT l.*,x.role FROM ledger_entries l JOIN ledger_links x ON x.entry_id=l.id WHERE x.execution_id=?',(execution['id'],)).fetchall()
            expected={('DEBIT',execution['source_account_id'],-execution['amount_minor'],'SGD'),('CREDIT','CLEARING_SGD',execution['amount_minor'],'SGD')}
            if len(entries)!=2 or {(e['role'],e['account_id'],e['delta_minor'],e['currency']) for e in entries}!=expected:
                raise DomainError('RECONCILIATION','An incomplete or unbalanced execution was found.')
            receipt=json.loads(execution['receipt_json'])
            if receipt.get('execution_id')!=execution['id'] or receipt.get('amount_minor')!=execution['amount_minor'] or receipt.get('status')!='SUCCEEDED':
                raise DomainError('RECONCILIATION','Receipt integrity check failed.')

    def receipt(self,user,session,execution_id):
        self.store.session(user,session)
        row=self.store.db.execute('SELECT receipt_json FROM execution_records WHERE id=? AND user_id=? AND session_id=?',(execution_id,user,session)).fetchone()
        if not row: raise DomainError('NOT_FOUND','Receipt unavailable.')
        return json.loads(row[0])

    def status(self,user,session,key):
        with self.store.read_transaction():
            return self._status(user,session,key)

    def _status(self,user,session,key):
        self.store.session(user,session)
        row=self.store.db.execute('SELECT * FROM execution_requests WHERE user_id=? AND idempotency_key=? AND session_id=?',(user,key,session)).fetchone()
        if not row: return {'status':'UNKNOWN','message':'No saved result yet. Check the same request again; do not create a replacement transfer.'}
        if row['status']=='SUCCEEDED': return {'status':'SUCCEEDED','receipt':self.receipt(user,session,row['execution_id'])}
        if row['status']=='PENDING':
            challenge=self._challenge(user,session,row['challenge_id'])
            existing=self.store.db.execute('SELECT id FROM execution_records WHERE draft_id=? AND version=?',(challenge['draft_id'],challenge['version'])).fetchone()
            if existing:
                return {'status':'SUCCEEDED','receipt':self.receipt(user,session,existing[0])}
            try:
                self._current(user,session,challenge)
            except DomainError as exc:
                # An observational status; never send or mutate money from a GET request.
                return {'status':'REVIEW_REQUIRED','error_code':exc.code,'request_id':row['id']}
        return {'status':row['status'],'error_code':row['error_code'],'request_id':row['id']}

    def recoverable(self,user,session):
        with self.store.read_transaction():
            return self._recoverable(user,session)

    def _recoverable(self,user,session):
        self.store.session(user,session)
        result=[]
        for row in self.store.db.execute("SELECT idempotency_key,challenge_id FROM execution_requests WHERE user_id=? AND session_id=? AND status='PENDING' ORDER BY created_ms DESC,rowid DESC LIMIT 100",(user,session)):
            state=self.status(user,session,row['idempotency_key'])
            result.append({'session':session,'key':row['idempotency_key'],'challenge_id':row['challenge_id'],**state})
        return result

    def cancel_review(self,user,session,challenge_id):
        with self.store.transaction():
            challenge=self._challenge(user,session,challenge_id)
            if challenge['status'] in ('ISSUED','AUTHORIZED'):
                self.store.db.execute("UPDATE authorization_challenges SET status='INVALIDATED' WHERE id=?",(challenge_id,))
                self.store.audit(session,'REVIEW_CLOSED',{'challenge_id':challenge_id})
        return {'status':'INVALIDATED' if challenge['status'] in ('ISSUED','AUTHORIZED') else challenge['status']}

    def execute(self,user,session,challenge_id,key):
        if type(key) is not str or not 1<=len(key)<=100: raise DomainError('INPUT','A bounded idempotency key is required.')
        try:
            challenge=self._challenge(user,session,challenge_id)
            proof=self.store.db.execute('SELECT * FROM authorization_proofs WHERE challenge_id=?',(challenge_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            # Reads before the write transaction can meet a concurrent commit's lock: a safe, retryable BUSY.
            if 'locked' in str(exc).lower():raise DomainError('BUSY','Database busy; retry the same request key.') from exc
            raise
        if not proof: raise DomainError('AUTHORIZATION_REQUIRED','A verified independent demo signature is required.')
        self._verify(challenge,proof['key_id'],proof['signature'])
        request_hash=digest({'user_id':user,'session_id':session,'challenge_id':challenge_id,'payload':challenge['payload_json']})
        with self.store.transaction():
            old=self.store.db.execute('SELECT * FROM execution_requests WHERE user_id=? AND idempotency_key=?',(user,key)).fetchone()
            if old:
                if old['request_hash']!=request_hash: raise DomainError('IDEMPOTENCY_CONFLICT','Request key belongs to different frozen content.')
                if old['status']=='SUCCEEDED': return self.receipt(user,session,old['execution_id'])
                if old['status']=='REJECTED': raise DomainError(old['error_code'],'This request was rejected. Review again before a new attempt.')
            else:
                self.store.db.execute("INSERT INTO execution_requests VALUES (?,?,?,?,?,?,'PENDING',NULL,NULL,?)",(uid('request'),user,session,key,request_hash,challenge_id,self.clock()))
        self.fault('after_reservation')
        try:
            with self.store.transaction():
                # Recheck after obtaining the write lock. Different keys cannot bypass draft/version uniqueness.
                existing=self.store.db.execute('SELECT id FROM execution_records WHERE draft_id=? AND version=?',(challenge['draft_id'],challenge['version'])).fetchone()
                if existing:
                    receipt=self.receipt(user,session,existing['id'])
                else:
                    challenge=self._challenge(user,session,challenge_id)
                    if challenge['status']!='AUTHORIZED': raise DomainError('AUTH_STATE','Authorization is not ready for execution.')
                    review=self._current(user,session,challenge)
                    timestamp=self.clock()
                    self.reconcile();self.risk(user,review,timestamp);self.require_stepup(challenge_id,review)
                    if review.get('plan'):
                        receipt=self.execute_plan(user,session,challenge_id,review,timestamp)
                    else:
                        amount=review['total_minor'];source=review['source']['id'];execution_id=uid('execution')
                        before=self.store.account(user,source)['balance_minor']
                        self.store.db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE id=? AND balance_minor>=?',(amount,source,amount))
                        if self.store.db.execute('SELECT changes()').fetchone()[0]!=1: raise DomainError('INSUFFICIENT_FUNDS','Balance changed.')
                        self.fault('after_debit')
                        self.store.db.execute("UPDATE accounts SET balance_minor=balance_minor+? WHERE id='CLEARING_SGD'",(amount,))
                        receipt={'status':'SUCCEEDED','mode':'MOCK_ONLY','execution_id':execution_id,'draft_id':review['draft_id'],'version':review['version'],
                                 'content_hash':review['content_hash'],'source':review['source'],'payee':review['payee'],'currency':'SGD','amount_minor':amount,
                                 'fee_minor':0,'balance_before_minor':before,'balance_after_minor':before-amount,'created_ms':timestamp}
                        self.store.db.execute('INSERT INTO execution_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            (execution_id,user,session,review['draft_id'],review['version'],review['content_hash'],challenge_id,amount,source,review['payee']['id'],timestamp,canonical(receipt)))
                        for role,account,delta in [('DEBIT',source,-amount),('CREDIT','CLEARING_SGD',amount)]:
                            entry=uid('entry');self.store.db.execute('INSERT INTO ledger_entries VALUES (?,?,?,?)',(entry,account,delta,'SGD'))
                            self.store.db.execute('INSERT INTO ledger_links VALUES (?,?,?)',(entry,execution_id,role))
                        self.fault('after_ledger')
                        self.store.db.execute("UPDATE authorization_challenges SET status='CONSUMED' WHERE id=?",(challenge_id,))
                        self.store.db.execute("UPDATE transaction_drafts SET state='SUCCEEDED' WHERE id=?",(review['draft_id'],))
                        self.store.db.execute('UPDATE sessions SET state_json=? WHERE id=?',(canonical({'last_execution_id':execution_id}),session))
                        self.store.audit(session,'MOCK_EXECUTION_SUCCEEDED',{'execution_id':execution_id,'draft_id':review['draft_id'],'version':review['version'],'amount_minor':amount})
                        self.fault('after_audit')
                    self.reconcile()
                self.store.db.execute("UPDATE execution_requests SET status='SUCCEEDED',execution_id=? WHERE user_id=? AND idempotency_key=?",(receipt['execution_id'],user,key))
                self.fault('before_commit')
        except Exception as exc:
            code=exc.code if isinstance(exc,DomainError) else 'EXECUTION_ERROR'
            if code!='BUSY':
                with self.store.transaction():
                    # A concurrent successful request must never be overwritten by a losing result.
                    self.store.db.execute("UPDATE execution_requests SET status='REJECTED',error_code=? WHERE user_id=? AND idempotency_key=? AND status='PENDING'",(code,user,key))
            if isinstance(exc,DomainError): raise
            raise DomainError('EXECUTION_ERROR','Execution failed before commit. Check the saved request status.') from exc
        self.fault('after_commit')
        return receipt
