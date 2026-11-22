import json
from pathlib import Path
from flask import Blueprint,request,jsonify,render_template
from app.errors import DomainError
from app.config import MODULES
from app.slots import ledger
from app.slots.rules import RULE_VERSION

bp=Blueprint('slots',__name__)

def _sample():
    return json.loads((Path(__file__).resolve().parents[2]/'sample_data'/'slot_applications.json').read_text(encoding='utf-8'))

def _slot_params(data):
    slot_day=(data or {}).get('slot_day') or request.args.get('slot_day')
    capacity=(data or {}).get('capacity') or request.args.get('capacity') or 1
    if not slot_day:raise DomainError('SLOT_DAY','请提供席位日期')
    try:capacity=int(capacity)
    except (TypeError,ValueError):raise DomainError('CAPACITY','席位容量无效')
    return slot_day,capacity

@bp.get('/slots')
def screen():
    try:state=ledger.published()
    except DomainError:state=None
    return render_template('slots.html',sample=_sample(),rule_version=RULE_VERSION,modules=MODULES,
        ledger=state,versions=ledger.versions(),applications=ledger.applications())

@bp.post('/api/slots/applications')
def submit():
    data=request.get_json(silent=True)
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    apps=data.get('applications',data.get('application'))
    if isinstance(apps,dict):apps=[apps]
    if not isinstance(apps,list) or not apps:raise DomainError('PAYLOAD','缺少申请')
    keys=ledger.ingest(apps)
    return jsonify(ingested=len(keys),source_keys=keys,total=len(ledger.applications()))

@bp.get('/api/slots/applications')
def list_applications():return jsonify(ledger.applications())

@bp.get('/api/slots/preview')
def peek():
    slot_day,capacity=_slot_params(None)
    return jsonify(ledger.preview(slot_day,capacity))

@bp.post('/api/slots/publish')
def release():
    slot_day,capacity=_slot_params(request.get_json(silent=True) or {})
    return jsonify(ledger.publish(slot_day,capacity))

@bp.get('/api/slots/ledger')
def current():return jsonify(ledger.published())

@bp.get('/api/slots/ledger/versions')
def history():return jsonify(ledger.versions())

@bp.get('/api/slots/release/<batch>')
def gate(batch):return jsonify(ledger.release(batch))
