"""Compare effective private replay inputs, excluding paths of generated candidates."""
import hashlib
import json
import re
import time

from .core import GatewayError


class EquivalentExecution(GatewayError):
    def __init__(self,job_id,state):
        self.job_id,self.state=job_id,state
        super().__init__('执行输入与任务 '+job_id+' 一致。')


def sha(value):return isinstance(value,str) and bool(re.fullmatch(r'[a-f0-9]{64}',value))


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def identity(store,user,payload):
    assets=payload.get('assets') or {};package=assets.get('package') or {};dataset=assets.get('dataset') or {}
    if payload.get('action')!='replay' or package.get('owner')!=user or not sha(package.get('data',{}).get('sha256')):
        raise GatewayError('缺少可核验的软件身份。')
    dependencies={}
    for asset in assets.get('dependencies') or []:
        data=asset.get('data') or {}
        if asset.get('owner')!=user or not sha(data.get('sha256')) or not data.get('target'):raise GatewayError('依赖身份不完整。')
        if data['target'] in dependencies:raise GatewayError('依赖目标重复。')
        dependencies[data['target']]=data['sha256']
    if len(dependencies)!=3 or not sha(dataset.get('data',{}).get('sha256')):raise GatewayError('执行材料身份不完整。')
    baseline=payload.get('baseline') or {};description=baseline.get('data') or {}
    pid=description.get('preparation_id');preparation=None
    if pid:
        with store.connect() as c:row=c.execute('SELECT result FROM preparations WHERE id=? AND owner=?',(pid,user)).fetchone()
        if row:preparation=json.loads(row['result'] or '{}')
    if not preparation:raise GatewayError('缺少本次配置准备证据。')
    yaml=preparation.get('yaml_preparation') or {};outputs=yaml.get('outputs') or []
    if yaml and yaml.get('status') not in ('PREPARED','NOT_REQUIRED'):raise GatewayError('YAML 候选尚未核验。')
    byte_changes={};semantic_changes={};semantic=True
    for item in outputs:
        if not item.get('target') or not sha(item.get('sha256')):raise GatewayError('YAML 内容身份不完整。')
        if item.get('before_sha256')!=item['sha256']:byte_changes[item['target']]=item['sha256']
        before,after=item.get('before_semantic_sha256'),item.get('semantic_sha256')
        if sha(before) and sha(after):
            if before!=after:semantic_changes[item['target']]=after
        else:semantic=False
    common={'schema':1,'owner':user,'package':package['data']['sha256'],'dependencies':dependencies,
            'dataset':{'id':dataset.get('id'),'version':dataset.get('version'),'sha256':dataset['data']['sha256']},
            'scenes':payload.get('scenes'),'evaluate':bool(payload.get('evaluate_after')),
            'delivery':((payload.get('business_contract') or {}).get('delivery') or {}).get('lpai_target','none'),
            'protocol':{k:(description.get('execution_policy') or {}).get(k)
                        for k in ('protocol','fixed_configuration','required_output_types')}}
    mode=(description.get('execution_policy') or {}).get('replay_mode','stream')
    if mode not in ('stream','frame_by_frame'):raise GatewayError('回放方式无效。')
    if mode!='stream':common['protocol']['replay_mode']=mode
    return {'byte_digest':digest({**common,'yaml':byte_changes}),
            'semantic_digest':digest({**common,'yaml':semantic_changes}) if semantic else None}


def cache_base(store,user,job_id):
    if not job_id:return
    with store.connect() as c:
        if c.execute('SELECT 1 FROM execution_inputs WHERE job=? AND owner=?',(job_id,user)).fetchone():return
    try:
        job=store.job(user,job_id);value=identity(store,user,job['payload'])
    except (GatewayError,KeyError,TypeError,AttributeError):return
    with store.connect() as c:
        c.execute('INSERT OR IGNORE INTO execution_inputs VALUES(?,?,?,?,?)',
                  (job_id,user,value['byte_digest'],value['semantic_digest'],time.time()))
