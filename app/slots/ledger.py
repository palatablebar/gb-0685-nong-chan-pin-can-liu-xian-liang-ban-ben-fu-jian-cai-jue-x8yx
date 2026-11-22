import json
from datetime import datetime,timezone
from app.errors import DomainError
from app.store import connection
from app.slots.rules import normalize,build_ledger,version_id

DDL=[
'CREATE TABLE IF NOT EXISTS slot_applications (source_key TEXT PRIMARY KEY, batch TEXT NOT NULL, received_at TEXT NOT NULL, payload TEXT NOT NULL, ingested_at TEXT NOT NULL)',
'CREATE TABLE IF NOT EXISTS slot_ledger_versions (version_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, body TEXT NOT NULL, published_at TEXT NOT NULL)',
'CREATE TABLE IF NOT EXISTS slot_ledger_head (id INTEGER PRIMARY KEY CHECK(id=1), version_id TEXT NOT NULL)',
'CREATE TABLE IF NOT EXISTS slot_concessions (version_id TEXT NOT NULL, source_key TEXT NOT NULL, batch TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY (version_id, source_key, kind))']

def _init(db):
    for sql in DDL:db.execute(sql)

def _now():return datetime.now(timezone.utc).isoformat()

def ingest(applications):
    # 原始申请只追加：同源同内容幂等，同源异内容拒绝改写。
    if not isinstance(applications,list) or not applications:raise DomainError('PAYLOAD','申请列表为空')
    items=[(normalize(a),a) for a in applications]
    with connection() as db:
        _init(db)
        for norm,raw in items:
            payload=json.dumps(raw,sort_keys=True,ensure_ascii=False)
            row=db.execute('SELECT payload FROM slot_applications WHERE source_key=?',(norm['source_key'],)).fetchone()
            if row:
                if row['payload']!=payload:raise DomainError('CONFLICT','同源申请内容不一致，原始申请不可改写',409)
                continue
            db.execute('INSERT INTO slot_applications(source_key,batch,received_at,payload,ingested_at) VALUES (?,?,?,?,?)',
                (norm['source_key'],norm['batch'],norm['received_at'],payload,_now()))
    return [n['source_key'] for n,_ in items]

def applications():
    with connection() as db:
        _init(db)
        rows=db.execute('SELECT source_key,batch,received_at,payload,ingested_at FROM slot_applications ORDER BY received_at,source_key').fetchall()
    return [dict(r) for r in rows]

def _pool(db):
    return [normalize(json.loads(r['payload'])) for r in db.execute('SELECT payload FROM slot_applications').fetchall()]

def preview(slot_day,capacity):
    with connection() as db:
        _init(db);pool=_pool(db)
    if not pool:raise DomainError('EMPTY','无原始申请，无法形成承诺账')
    body=build_ledger(pool,slot_day,capacity);body['preview']=True
    return body

def _project(db,vid,body):
    # 让步记录随承诺账版本投影，同源版本重跑不重复入账。
    for c in body['concessions']:
        db.execute('INSERT OR IGNORE INTO slot_concessions(version_id,source_key,batch,kind,detail) VALUES (?,?,?,?,?)',
            (vid,c['source_key'],c['batch'],c['kind'],c['detail']))

def publish(slot_day,capacity):
    # 单事务发布：版本、让步投影、头指针要么全部落库，要么全部回滚；中断时旧版承诺账仍在发布位。
    with connection() as db:
        _init(db)
        pool=_pool(db)
        if not pool:raise DomainError('EMPTY','无原始申请，无法形成承诺账')
        body=build_ledger(pool,slot_day,capacity);vid=version_id(body)
        if not db.execute('SELECT 1 FROM slot_ledger_versions WHERE version_id=?',(vid,)).fetchone():
            seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 s FROM slot_ledger_versions').fetchone()['s']
            db.execute('INSERT INTO slot_ledger_versions(version_id,seq,body,published_at) VALUES (?,?,?,?)',
                (vid,seq,json.dumps(body,sort_keys=True,ensure_ascii=False),_now()))
            _project(db,vid,body)
        db.execute("INSERT INTO slot_ledger_head(id,version_id) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET version_id=excluded.version_id",(vid,))
    return published()

def published():
    with connection() as db:
        _init(db)
        head=db.execute('SELECT version_id FROM slot_ledger_head WHERE id=1').fetchone()
        if not head:raise DomainError('NO_LEDGER','承诺账尚未发布',404)
        row=db.execute('SELECT * FROM slot_ledger_versions WHERE version_id=?',(head['version_id'],)).fetchone()
        body=json.loads(row['body']);body.update(version_id=row['version_id'],seq=row['seq'],published_at=row['published_at'])
        return body

def versions():
    with connection() as db:
        _init(db)
        head=db.execute('SELECT version_id FROM slot_ledger_head WHERE id=1').fetchone()
        rows=db.execute('SELECT version_id,seq,published_at,body FROM slot_ledger_versions ORDER BY seq').fetchall()
    current=head['version_id'] if head else None
    return [{'version_id':r['version_id'],'seq':r['seq'],'published_at':r['published_at'],
        'slot_day':json.loads(r['body'])['slot_day'],'current':r['version_id']==current} for r in rows]

def concessions(version_id=None):
    with connection() as db:
        _init(db)
        if version_id is None:
            head=db.execute('SELECT version_id FROM slot_ledger_head WHERE id=1').fetchone()
            if not head:raise DomainError('NO_LEDGER','承诺账尚未发布',404)
            version_id=head['version_id']
        rows=db.execute('SELECT * FROM slot_concessions WHERE version_id=? ORDER BY source_key,kind',(version_id,)).fetchall()
    return [dict(r) for r in rows]

def release(batch):
    # 归档放行只认发布位上的承诺账版本。
    body=published()
    hit=body['release'].get(batch)
    if hit is None:raise DomainError('NOT_FOUND','批次不在已发布承诺账',404)
    return {'batch':batch,'release':hit,'conclusion':body['conclusions'][batch],
        'version_id':body['version_id'],'seq':body['seq']}
