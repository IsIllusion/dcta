"""Read-only host projections for owned saved drafts and immutable versions."""
from .domain import DomainError, format_money, strict_json
from .history import require_visible
from .payments import PaymentService
from .references import execution_from_reference


def _text(value,maximum=100):
    if type(value) is not str or not 1<=len(value)<=maximum:
        raise DomainError('INPUT','A bounded draft identifier is required.')
    try:value.encode('utf-8')
    except UnicodeError:raise DomainError('INPUT','Use valid Unicode.') from None


from .purpose import get_purpose, get_purposes, defaults_for


def list_drafts(store,user,query='',status='DRAFTED',limit=100,offset=0):
    if type(query) is not str or len(query)>80 or type(status) is not str or status not in ('DRAFTED','CANCELLED','SUCCEEDED','ALL') or type(limit) is not int or not 1<=limit<=100:
        raise DomainError('INPUT','Use a search within 80 characters and a supported draft status.')
    try:query.encode('utf-8')
    except UnicodeError:raise DomainError('INPUT','Search must contain valid Unicode.') from None
    _offset(offset)
    query=query.strip()
    execution_query=execution_from_reference(query) or query
    # Every object join is owner-scoped. This lists saved transactions, never a payee directory.
    joins="""FROM transaction_drafts d JOIN sessions s ON s.id=d.session_id
        JOIN draft_versions v ON v.draft_id=d.id AND v.version=d.current_version
        JOIN payees p ON p.id=v.payee_id AND p.user_id=s.user_id
        JOIN accounts a ON a.id=v.source_account_id AND a.user_id=s.user_id
        LEFT JOIN conversation_names n ON n.session_id=s.id
        WHERE s.user_id=? AND NOT EXISTS (SELECT 1 FROM hidden_conversations h WHERE h.session_id=s.id)
        AND (?='ALL' OR d.state=?) AND (?='' OR instr(lower(p.name),lower(?))>0
        OR instr(lower(COALESCE(n.title,'')),lower(?))>0 OR instr(lower(d.id),lower(?))>0
        OR EXISTS (SELECT 1 FROM execution_records e WHERE e.user_id=s.user_id AND e.draft_id=d.id AND e.id=?)
        OR instr('SGD '||CAST(v.amount_minor/100 AS TEXT)||'.'||printf('%02d',v.amount_minor%100),?)>0)"""
    args=(user,status,status,query,query,query,query,execution_query,query.upper())
    with store.read_transaction():
        total=store.db.execute('SELECT COUNT(*) '+joins,args).fetchone()[0]
        rows=store.db.execute("""SELECT d.id,d.session_id,d.state,d.current_version AS version,
            v.amount_minor,v.currency,v.created_at AS updated_at,p.name AS payee_name,
            COALESCE(n.title,'Conversation '||substr(s.id,-8)) AS session_title """+joins+
            ' ORDER BY v.created_at DESC,d.rowid DESC LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
        items=[]
        for row in rows:
            item=dict(row);draft=store.draft(user,item['id'])
            item['eligibility']=PaymentService(store).eligibility(user,draft) if item['state']=='DRAFTED' else None
            actions=draft['payload']['actions'];item['action_count']=len(actions)
            cats=_categories(store,user,item['id'],[a['payee_id'] for a in actions]);item.update(cats[0])
            if item['state']=='SUCCEEDED':
                # One Transactions list: a settled entry opens its receipt and shows when it settled.
                done=store.db.execute("SELECT id,created_ms FROM execution_records WHERE user_id=? AND json_extract(receipt_json,'$.draft_id')=? ORDER BY created_ms DESC LIMIT 1",(user,item['id'])).fetchone()
                if done:item['execution_id'],item['settled_ms']=done['id'],done['created_ms']
            if len(actions)>1:
                item['amount_minor']=draft['payload']['total_minor'];item['payee_name']+=f' + {len(actions)-1} more'
                # CV14: the Transfers panel shows every action of a plan as its own entry.
                item['actions']=[{'amount_minor':a['amount_minor'],'payee_name':store.payee(user,a['payee_id'])['name'],**c} for a,c in zip(actions,cats)]
            items.append(item)
        return {'drafts':items,'total':total,'limit':limit,'offset':offset,'has_more':total>offset+len(rows)}


def _offset(offset):
    if type(offset) is not int or not 0<=offset<=1000000:
        raise DomainError('INPUT','Use a bounded nonnegative page offset.')


def list_receipts(store,user,query='',limit=100,offset=0):
    if type(query) is not str or len(query)>80 or type(limit) is not int or not 1<=limit<=100:
        raise DomainError('INPUT','Use a search within 80 characters.')
    try:query.encode('utf-8')
    except UnicodeError:raise DomainError('INPUT','Search must contain valid Unicode.') from None
    _offset(offset)
    query=query.strip()
    query=execution_from_reference(query) or query
    # Recipient labels come from the frozen receipt, not today's payee directory.
    joins="""FROM execution_records e JOIN sessions s ON s.id=e.session_id AND s.user_id=e.user_id
        LEFT JOIN conversation_names n ON n.session_id=s.id
        WHERE e.user_id=? AND NOT EXISTS (SELECT 1 FROM hidden_conversations h WHERE h.session_id=s.id)
        AND (?='' OR instr(lower(json_extract(e.receipt_json,'$.payee.name')),lower(?))>0
        OR instr(lower(COALESCE(n.title,'')),lower(?))>0 OR instr(lower(e.id),lower(?))>0
        OR instr('SGD '||CAST(e.amount_minor/100 AS TEXT)||'.'||printf('%02d',e.amount_minor%100),?)>0)"""
    args=(user,query,query,query,query,query.upper())
    with store.read_transaction():
        total=store.db.execute('SELECT COUNT(*) '+joins,args).fetchone()[0]
        rows=store.db.execute("""SELECT e.id,e.session_id,e.draft_id,e.version,e.amount_minor,e.created_ms,
            json_extract(e.receipt_json,'$.payee.name') AS payee_name,COALESCE(json_array_length(e.receipt_json,'$.actions'),1) AS action_count,
            json_extract(e.receipt_json,'$.actions') AS actions_json,json_extract(e.receipt_json,'$.payee.id') AS first_payee_id,
            COALESCE(n.title,'Conversation '||substr(s.id,-8)) AS session_title """+joins+
            ' ORDER BY e.created_ms DESC,e.rowid DESC LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
        receipts=[dict(row) for row in rows]
        for item in receipts:
            actions=strict_json(item.pop('actions_json')) if item.get('actions_json') else None
            payee_ids=[a['payee']['id'] for a in actions] if actions else [item['first_payee_id']]
            cats=_categories(store,user,item['draft_id'],payee_ids);item.update(cats[0]);item.pop('first_payee_id',None)
            if item['action_count']>1:
                item['payee_name']+=f" + {item['action_count']-1} more"
                item['actions']=[{'amount_minor':a['amount_minor'],'payee_name':a['payee']['name'],**c} for a,c in zip(actions,cats)]
        return {'receipts':receipts,'total':total,'limit':limit,'offset':offset,'has_more':total>offset+len(rows)}


def receipt_detail(store,user,execution_id):
    _text(execution_id)
    with store.read_transaction():
        row=store.db.execute('SELECT session_id,receipt_json FROM execution_records WHERE id=? AND user_id=?',(execution_id,user)).fetchone()
        if not row:raise DomainError('NOT_FOUND','Saved receipt not found.')
        require_visible(store,user,row['session_id'])
        receipt=strict_json(row['receipt_json'])
        return {**{key:receipt[key] for key in ('execution_id','draft_id','version','content_hash','amount_minor','fee_minor','currency','created_ms','balance_after_minor')},
                'source':{key:receipt['source'][key] for key in ('id','name')},
                'payee':{key:receipt['payee'][key] for key in ('id','name','ending')},
                'session_id':row['session_id'],'status':'SUCCEEDED','mode':'MOCK_ONLY','read_only':True,'purpose':get_purpose(store,receipt['draft_id']),
                **_purposes(store,user,receipt['draft_id'],[a['payee']['id'] for a in receipt['actions']] if receipt.get('plan') else [receipt['payee']['id']]),
                **({'actions':[{'ordinal':a['ordinal'],'amount_minor':a['amount_minor'],'source':{k:a['source'][k] for k in ('id','name')},
                                'payee':{k:a['payee'][k] for k in ('id','name','ending')},'balance_after_minor':a['balance_after_minor']} for a in receipt['actions']],
                    'constraints':receipt.get('constraints',[])} if receipt.get('plan') else {})}


def _projection(store,user,draft):
    action=draft['payload']['actions'][0]
    source=store.db.execute('SELECT id,name FROM accounts WHERE id=? AND user_id=?',(action['source_account_id'],user)).fetchone()
    payee=store.db.execute('SELECT id,name,ending FROM payees WHERE id=? AND user_id=?',(action['payee_id'],user)).fetchone()
    if not source or not payee:
        raise DomainError('INTEGRITY','Saved draft objects are unavailable for this user.')
    view={**{field:action[field] for field in ('amount_minor','currency','fee_minor')},
            'total_minor':action['amount_minor']+action['fee_minor'],'source':dict(source),'payee':dict(payee)}
    payload=draft['payload']
    if payload.get('schema_version')==2:
        actions=[]
        for ordinal,item in enumerate(payload['actions'],1):
            src=store.db.execute('SELECT id,name FROM accounts WHERE id=? AND user_id=?',(item['source_account_id'],user)).fetchone()
            dst=store.db.execute('SELECT id,name,ending FROM payees WHERE id=? AND user_id=?',(item['payee_id'],user)).fetchone()
            if not src or not dst:raise DomainError('INTEGRITY','Saved draft objects are unavailable for this user.')
            actions.append({'ordinal':ordinal,'amount_minor':item['amount_minor'],'fee_minor':item['fee_minor'],'source':dict(src),'payee':dict(dst)})
        view.update({'actions':actions,'constraints':payload['constraints'],'amount_minor':payload['total_minor'],'total_minor':payload['total_minor']})
    return view


def draft_detail(store,user,draft_id,version=None,compare=None):
    _text(draft_id)
    for value in (version,compare):
        if value is not None and (type(value) is not int or not 1<=value<2**31):
            raise DomainError('INPUT','Use a positive saved version number.')
    with store.read_transaction():
        draft=store.draft(user,draft_id,version)
        require_visible(store,user,draft['session_id'])
        selected=draft['payload']['version']
        if compare is not None and compare>=selected:
            raise DomainError('INPUT','Compare with an earlier saved version.')
        if compare is None and selected>1:compare=selected-1
        view=_projection(store,user,draft)
        changed=None
        if compare is not None and (draft['payload'].get('schema_version')==2 or store.draft(user,draft_id,compare)['payload'].get('schema_version')==2):
            from .presentation import compare_payloads
            changed=compare_payloads(store,user,store.draft(user,draft_id,compare)['payload'],draft['payload'])
            compare=None
        if compare is not None:
            before=_projection(store,user,store.draft(user,draft_id,compare))
            changes=[]
            for field,label in (('amount_minor','Amount'),('source','From'),('payee','To'),('currency','Currency'),('fee_minor','Fee')):
                old,new=before[field],view[field]
                different=old['id']!=new['id'] if field in ('source','payee') else old!=new
                if not different:continue
                def display(value):
                    if field in ('amount_minor','fee_minor'):return format_money(value)
                    if field=='source':return value['name']+' · '+value['id']
                    if field=='payee':return value['name']+' · ending '+value['ending']
                    return value
                changes.append({'label':label,'before':display(old),'after':display(new)})
            changed={'from_version':compare,'to_version':selected,'changes':changes}
        named=store.db.execute('SELECT title FROM conversation_names WHERE session_id=?',(draft['session_id'],)).fetchone()
        created=store.db.execute('SELECT created_at FROM draft_versions WHERE draft_id=? AND version=?',(draft_id,selected)).fetchone()[0]
        return {**view,'draft_id':draft_id,'session_id':draft['session_id'],'session_title':named[0] if named else 'Conversation '+draft['session_id'][-8:],
                'state':draft['state'],'version':selected,'current_version':draft['current_version'],
                'created_at':created,'content_hash':draft['content_hash'],'changes':changed,'read_only':True,
                'eligibility':PaymentService(store).eligibility(user,draft),'purpose':get_purpose(store,draft_id),
                **_purposes(store,user,draft_id,[a['payee_id'] for a in draft['payload']['actions']])}


def _purposes(store,user,draft_id,payee_ids):
    profiles=store.profiles(user)
    return {'purposes':get_purposes(store,draft_id,len(payee_ids)),'purpose_defaults':defaults_for(store,user,payee_ids),
            'kinds':[(profiles.get(pid) or {}).get('kind','PERSON') for pid in payee_ids]}


def _categories(store,user,draft_id,payee_ids):
    """Per action: destination kind and the purpose to show (chosen, else the default) for the Transfers list."""
    info=_purposes(store,user,draft_id,payee_ids)
    return [{'kind':k,'purpose':(p or {}).get('category') or d} for k,p,d in zip(info['kinds'],info['purposes'],info['purpose_defaults'])]
