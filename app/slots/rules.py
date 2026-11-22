import hashlib,json
from datetime import date,datetime,timezone
from app.errors import DomainError

RULE_VERSION='slots-2026.11.22-v1'
RISK={'PASS':0,'UNCERTAIN':1,'FAIL':2}
EVIDENCE=('verified','questionable')
REQUIRED=('batch','received_at','expires_at','risk_level','risk_evidence')

def _day(value,field):
    if not isinstance(value,str):raise DomainError('DATE',field+' 须为ISO日期')
    try:return date.fromisoformat(value).isoformat()
    except ValueError:raise DomainError('DATE',field+' 日期无效')

def _moment(value):
    if not isinstance(value,str):raise DomainError('DATE','received_at 须为ISO时间')
    try:t=datetime.fromisoformat(value)
    except ValueError:raise DomainError('DATE','received_at 时间无效')
    if t.tzinfo is None:raise DomainError('DATE','received_at 须带时区偏移')
    return t.astimezone(timezone.utc).isoformat()

def source_key(app):
    rid=app.get('request_id')
    if isinstance(rid,str) and rid.strip():return 'req:'+rid.strip()
    raw=json.dumps(app,sort_keys=True,ensure_ascii=False)
    return 'sha256:'+hashlib.sha256(raw.encode()).hexdigest()[:16]

def normalize(app):
    if not isinstance(app,dict):raise DomainError('PAYLOAD','申请必须是对象')
    missing=[k for k in REQUIRED if k not in app or app[k] in (None,'')]
    if missing:raise DomainError('MISSING','申请缺少必填项:'+','.join(missing))
    if app['risk_level'] not in RISK:raise DomainError('RISK','风险等级无效')
    if app['risk_evidence'] not in EVIDENCE:raise DomainError('EVIDENCE','证据状态无效')
    return {'source_key':source_key(app),'batch':str(app['batch']),
        'received_at':_moment(app['received_at']),
        'expires_at':_day(app['expires_at'],'expires_at'),
        'promise_due':_day(app['promise_due'],'promise_due') if app.get('promise_due') not in (None,'') else None,
        'risk_level':app['risk_level'],'risk_evidence':app['risk_evidence'],
        'note':str(app.get('note',''))}

def effective_risk(a):
    # 存疑证据不能全量采信，风险等级降一档参与排序。
    r=RISK[a['risk_level']]
    return r if a['risk_evidence']=='verified' else max(0,r-1)

def _group(a,slot_day):
    if a['risk_level']=='FAIL' and a['risk_evidence']=='verified':return 0  # S2 已核实严重风险，可插队
    if a['promise_due'] and a['promise_due']<=slot_day:return 1              # S1 承诺到期，履约优先
    return 2                                                                 # S3 普通排序

def rank(apps,slot_day):
    def key(a):
        g=_group(a,slot_day)
        second=a['promise_due'] if g==1 else -effective_risk(a)
        return (g,second,a['received_at'],a['source_key'])
    return sorted(apps,key=key)

def latest_per_batch(apps):
    # 同批次后到的申请视为补证更新，取代先前申请参与裁决；被取代者留档不删。
    by={}
    for a in apps:
        cur=by.get(a['batch'])
        if cur is None or (a['received_at'],a['source_key'])>(cur['received_at'],cur['source_key']):by[a['batch']]=a
    superseded=sorted(({'source_key':a['source_key'],'batch':a['batch'],'replaced_by':by[a['batch']]['source_key']}
        for a in apps if a['source_key']!=by[a['batch']]['source_key']),key=lambda s:s['source_key'])
    return list(by.values()),superseded

def _concessions(a,slot_day):
    out=[('NEXT_SLOT_PRIORITY','延期至下一可用席位，排第一优先')]
    if a['risk_evidence']=='questionable' and RISK[a['risk_level']]>=1:
        out.append(('EVIDENCE_REVIEW','风险证据存疑，下一席位前须补证核实'))
    if a['promise_due'] and a['promise_due']<=slot_day:
        out.append(('BREACH_NOTICE','出证承诺到期未履约，登记失约并通知委托方'))
    if (date.fromisoformat(a['expires_at'])-date.fromisoformat(slot_day)).days<=3:
        out.append(('EXPIRY_WATCH','临期管控：到期前加急通道，暂缓归档放行'))
    if a['risk_level']=='FAIL' and a['risk_evidence']=='verified':
        out.append(('EXPEDITE','已核实严重风险被容量挤占，启动加急复检'))
    return out

def build_ledger(apps,slot_day,capacity):
    slot_day=_day(slot_day,'slot_day')
    if isinstance(capacity,bool) or not isinstance(capacity,int) or capacity<1 or capacity>99:
        raise DomainError('CAPACITY','席位容量无效')
    pool,superseded=latest_per_batch(apps)
    ranked=rank(pool,slot_day);winners=ranked[:capacity]
    winner_keys={a['source_key'] for a in winners}
    top_group=_group(winners[0],slot_day) if winners else None
    decisions=[];occupancy=[];conclusions={};release={};concessions=[]
    for i,a in enumerate(ranked,1):
        win=a['source_key'] in winner_keys;g=_group(a,slot_day)
        if win:
            outcome='OCCUPIED'
            reason={0:'S2 已核实严重风险，越过承诺插队',1:'S1 出证承诺到期，履约优先',2:'S3 综合排序居首'}[g]
            occupancy.append({'slot':len(occupancy)+1,'batch':a['batch'],'source_key':a['source_key']})
            conclusions[a['batch']]='复检占位';release[a['batch']]='RELEASE'
        else:
            outcome='DEFERRED'
            if top_group==0:reason='S2 对方已核实严重风险插队'
            elif top_group==1:
                reason=('S1 对方承诺到期；自身风险证据存疑，未达插队门槛'
                    if a['risk_evidence']=='questionable' and RISK[a['risk_level']]>=1 else 'S1 对方承诺到期，履约优先')
            else:reason='S3 排序靠后'
            conclusions[a['batch']]='让步延期';release[a['batch']]='HOLD'
            for kind,detail in _concessions(a,slot_day):
                concessions.append({'source_key':a['source_key'],'batch':a['batch'],'kind':kind,'detail':detail})
        decisions.append({'source_key':a['source_key'],'batch':a['batch'],'rank':i,'outcome':outcome,'reason':reason,
            'risk_level':a['risk_level'],'risk_evidence':a['risk_evidence'],'promise_due':a['promise_due'],'received_at':a['received_at']})
    keys=sorted(a['source_key'] for a in apps)
    return {'rule_version':RULE_VERSION,'slot_day':slot_day,'capacity':capacity,
        'decisions':decisions,'occupancy':occupancy,'conclusions':conclusions,'release':release,
        'concessions':concessions,
        'inputs':{'count':len(apps),'application_keys':keys,'superseded':superseded,
            'digest':hashlib.sha256('|'.join(keys).encode()).hexdigest()[:16]}}

def version_id(body):
    return hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
