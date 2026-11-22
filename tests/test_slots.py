import json
from pathlib import Path
import pytest
from app.errors import DomainError

SAMPLE=json.loads((Path(__file__).resolve().parents[1]/'sample_data'/'slot_applications.json').read_text(encoding='utf-8'))
DAY=SAMPLE['slot_day']

@pytest.fixture
def workbench(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'slots.db'))
    from app.slots import ledger
    return ledger

def _ingest(wb):return wb.ingest(list(SAMPLE['applications']))

def _supplement():
    patch=dict(SAMPLE['applications'][1])
    patch.update(request_id='REQ-2026-1122-02B',risk_evidence='verified',received_at='2026-11-22T09:30:00+08:00')
    return patch

def test_commitment_beats_questionable_risk(workbench):
    _ingest(workbench);body=workbench.publish(DAY,1)
    assert body['occupancy']==[{'slot':1,'batch':'B901','source_key':'req:REQ-2026-1122-01'}]
    assert body['conclusions']=={'B901':'复检占位','B902':'让步延期'}
    assert body['release']=={'B901':'RELEASE','B902':'HOLD'}
    defer=[d for d in body['decisions'] if d['batch']=='B902'][0]
    assert defer['outcome']=='DEFERRED' and '存疑' in defer['reason']
    kinds={c['kind'] for c in body['concessions'] if c['batch']=='B902'}
    assert kinds=={'NEXT_SLOT_PRIORITY','EVIDENCE_REVIEW','EXPIRY_WATCH'}

def test_verified_evidence_preempts_commitment(workbench):
    _ingest(workbench);first=workbench.publish(DAY,1)
    workbench.ingest([_supplement()]);second=workbench.publish(DAY,1)
    assert second['version_id']!=first['version_id'] and second['seq']==2
    assert second['occupancy'][0]['batch']=='B902'
    assert second['release']=={'B901':'HOLD','B902':'RELEASE'}
    kinds={c['kind'] for c in second['concessions'] if c['batch']=='B901'}
    assert 'BREACH_NOTICE' in kinds and 'NEXT_SLOT_PRIORITY' in kinds
    assert second['inputs']['superseded']==[{'source_key':'req:REQ-2026-1122-02','batch':'B902','replaced_by':'req:REQ-2026-1122-02B'}]
    assert len(workbench.versions())==2 and len(workbench.applications())==3

def test_same_source_rerun_no_double_occupancy(workbench):
    _ingest(workbench);first=workbench.publish(DAY,1)
    _ingest(workbench);second=workbench.publish(DAY,1)
    assert first['version_id']==second['version_id'] and second['seq']==1
    assert len(workbench.versions())==1 and len(workbench.applications())==2
    assert len(second['occupancy'])==1
    assert len(workbench.concessions())==len(second['concessions'])

def test_interrupted_publish_keeps_old_ledger(workbench,monkeypatch):
    _ingest(workbench);first=workbench.publish(DAY,1)
    workbench.ingest([_supplement()])
    original=workbench._project
    def boom(db,vid,body):raise RuntimeError('power loss')
    monkeypatch.setattr(workbench,'_project',boom)
    with pytest.raises(RuntimeError):workbench.publish(DAY,1)
    assert workbench.published()['version_id']==first['version_id']
    assert len(workbench.versions())==1 and workbench.concessions(first['version_id'])
    assert len(workbench.applications())==3
    monkeypatch.setattr(workbench,'_project',original)
    second=workbench.publish(DAY,1)
    assert workbench.published()['version_id']==second['version_id']!=first['version_id']

def test_release_gate_follows_published_head(workbench):
    _ingest(workbench);workbench.publish(DAY,1)
    assert workbench.release('B901')['release']=='RELEASE'
    assert workbench.release('B902')['release']=='HOLD'
    workbench.ingest([_supplement()]);workbench.publish(DAY,1)
    assert workbench.release('B901')['release']=='HOLD'
    assert workbench.release('B902')['release']=='RELEASE'
    with pytest.raises(DomainError):workbench.release('B999')

def test_raw_applications_and_concessions_not_lost(workbench):
    _ingest(workbench);first=workbench.publish(DAY,1)
    stored={a['source_key']:json.loads(a['payload']) for a in workbench.applications()}
    for app in SAMPLE['applications']:
        key='req:'+app['request_id']
        assert stored[key]==app
    workbench.ingest([_supplement()]);workbench.publish(DAY,1)
    assert len(workbench.applications())==3
    assert workbench.concessions(first['version_id'])==workbench.concessions(first['version_id'])
    assert len(workbench.concessions(first['version_id']))==len(first['concessions'])

def test_preview_does_not_publish(workbench):
    _ingest(workbench);body=workbench.preview(DAY,1)
    assert body['preview'] is True and body['occupancy'][0]['batch']=='B901'
    with pytest.raises(DomainError):workbench.published()

def test_invalid_application_rejected(workbench):
    bad=dict(SAMPLE['applications'][0]);bad['risk_evidence']='maybe'
    with pytest.raises(DomainError):workbench.ingest([bad])
    naive=dict(SAMPLE['applications'][0]);naive['received_at']='2026-11-22 08:58'
    with pytest.raises(DomainError):workbench.ingest([naive])
    _ingest(workbench)
    tampered=dict(SAMPLE['applications'][0]);tampered['note']='篡改'
    with pytest.raises(DomainError):workbench.ingest([tampered])
    assert len(workbench.applications())==2

def test_publish_without_applications_rejected(workbench):
    with pytest.raises(DomainError):workbench.publish(DAY,1)

def test_api_flow_and_idempotent_rerun(client):
    payload={'applications':SAMPLE['applications']}
    a=client.post('/api/slots/applications',json=payload).get_json()
    b=client.post('/api/slots/applications',json=payload).get_json()
    assert a['total']==b['total']==2
    p1=client.post('/api/slots/publish',json={'slot_day':DAY,'capacity':1}).get_json()
    p2=client.post('/api/slots/publish',json={'slot_day':DAY,'capacity':1}).get_json()
    assert p1['version_id']==p2['version_id'] and p2['seq']==1 and len(p2['occupancy'])==1
    assert len(client.get('/api/slots/ledger/versions').get_json())==1
    r=client.get('/api/slots/release/B902');assert r.get_json()['release']=='HOLD'
    assert client.get('/api/slots/release/B999').status_code==404
    assert client.get('/slots').status_code==200

def test_api_conflict_and_validation(client):
    assert client.post('/api/slots/applications',json={'applications':SAMPLE['applications']}).status_code==200
    tampered=dict(SAMPLE['applications'][0]);tampered['note']='篡改'
    r=client.post('/api/slots/applications',json={'applications':[tampered]})
    assert r.status_code==409 and r.get_json()['code']=='CONFLICT'
    assert client.post('/api/slots/publish',json={'capacity':1}).status_code==422
