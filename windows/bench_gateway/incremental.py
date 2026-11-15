"""Resolve private base versions and explicit submission intent for incremental work."""
import json
import re
import time
import uuid
from pathlib import Path

DEFAULTS={'goal':'replay_and_evaluate','m100_mode':'build_if_missing','lpai_target':'none',
          'dataset':'共享18场景','scene_scope':'全部','replay_mode':'stream'}
SO_TARGETS={'camera_so':'lib/libxcamera.so','lidar_so':'lib/libxlidar.so','m100_so':'lib/liburoad_m100.so'}


def frozen_inputs(connection,user,focus,previous):
    data=dict(previous)
    if not focus or not focus['job_id']:return data
    row=connection.execute('SELECT payload FROM jobs WHERE id=? AND owner=?',(focus['job_id'],user)).fetchone()
    if not row:return data
    payload=json.loads(row['payload']);assets=payload.get('assets') or {}
    if payload.get('action')!='replay' or not isinstance(assets,dict):return data
    package=assets.get('package')
    dataset=assets.get('dataset')
    if isinstance(dataset,dict) and dataset.get('owner')==user and dataset.get('data',{}).get('cloud_source'):
        data['dataset']=dataset['id']  # Reuse the exact imported snapshot; changed scope resolves the original cloud root.
    if isinstance(package,dict) and package.get('owner')==user and package.get('id'):data['package']=package['id']
    for field,target in SO_TARGETS.items():
        asset=next((a for a in (assets.get('dependencies') or []) if isinstance(a,dict) and a.get('owner')==user and a.get('data',{}).get('target')==target),None)
        if asset and asset.get('id'):data[field]=asset['id']
    pid=((payload.get('baseline') or {}).get('data') or {}).get('preparation_id')
    prep=connection.execute('SELECT result FROM preparations WHERE id=? AND owner=?',(pid,user)).fetchone() if pid else None
    outputs=(json.loads(prep['result'] or '{}').get('yaml_preparation') or {}).get('outputs') or [] if prep else []
    if outputs and all(isinstance(x.get('path'),str) and isinstance(x.get('target'),str) for x in outputs):
        data['yaml_request']='；'.join('用工作站文件 '+x['path']+' 替换 APP 内 '+x['target'] for x in outputs)
    if data.get('m100_so') and data.get('m100_mode')!='build_if_missing':data['m100_mode']='provided'
    return data


def latest_private_base(connection,user):
    rows=connection.execute("SELECT t.* FROM task_requests t JOIN jobs j ON j.id=t.job_id AND j.owner=t.owner WHERE t.owner=? AND j.state IN ('SUCCEEDED','PARTIAL_SUCCESS','POSTPROCESSING') AND json_extract(j.payload,'$.action')='replay' ORDER BY t.created DESC LIMIT 1",(user,)).fetchall()
    return rows[0] if rows else None


def hold_requested(text):
    """Fail closed on an explicit hold; file captions never silently lose it."""
    value=''.join(str(text).split()).lower()
    return bool(re.search(r'(?:暂不|先不|不要|别|不必|不能|不)(?:自动)?(?:执行|运行|(?:回放|回灌)|提交|启动)|(?:只|仅)(?:保存|登记|上传|检查)|先(?:保存|登记)|稍后(?:再)?(?:执行|运行|(?:回放|回灌))|(?:等|待)我.*(?:再|后)(?:执行|运行|(?:回放|回灌))|(?:还要|还需|还有).*(?:补充|上传|发送|配置|文件|材料)|稍后.*(?:补充|上传|发送)',value))


def explicit_check(text):
    return bool(re.search(r'(?:先|只|仅)(?:做)?(?:检查|核验)',str(text)))


class InputBatch:
    """An open private intake turn blocks admission, not ongoing bench work."""
    def __init__(self,store,user,claim='message'):
        self.store,self.user,self.token,self.claim=store,user,uuid.uuid4().hex,claim;self.finished=False
        with store.connect() as c:
            c.execute('INSERT INTO input_batches VALUES(?,?,?,?,?,?)',(self.token,user,'OPEN',time.time(),time.time(),claim))

    def finish(self,success,material_changed=False,resolve_all=False):
        if self.finished:return
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE input_batches SET state=?,updated=? WHERE token=? AND owner=? AND state=\'OPEN\'',
                      ('COMPLETE' if success else 'ABORTED',time.time(),self.token,self.user))
            if success and material_changed:
                c.execute("UPDATE input_batches SET state='RESOLVED',updated=? WHERE owner=? AND state='ABORTED' AND (claim=? OR ?=1)",(time.time(),self.user,self.claim,int(resolve_all)))
        self.finished=True


def intake_blocked(connection,user):
    from .card_ui import blocks
    if blocks(connection,user):return True
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='input_batches'").fetchone():return False
    # A crashed intake cannot silently release an unknown partial message.
    connection.execute("UPDATE input_batches SET state='ABORTED',updated=? WHERE owner=? AND state='OPEN' AND updated<?",(time.time(),user,time.time()-900))
    return bool(connection.execute("SELECT 1 FROM input_batches WHERE owner=? AND state IN ('OPEN','ABORTED') LIMIT 1",(user,)).fetchone())


def input_claim(text,trailer):
    if trailer not in text:return 'message'
    raw=text.rsplit(trailer,1)[1]
    names=[re.sub(r'\(\d+\)(?=\.)','',Path(x).name.lower()) for x in raw.rstrip(')').split(', ')]
    return 'files:'+','.join(sorted(names))
