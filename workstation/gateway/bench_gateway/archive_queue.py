"""A workstation-only archive lane, committed with replay delivery, never hardware."""
import hashlib
import json
import time

from .core import GatewayError


def identity(payload):
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def enqueue(connection,job,result):
    payload=job['payload'];baseline=payload.get('baseline',{}).get('data',{});digest=baseline.get('binding',{}).get('input_digest')
    smoke=result.get('smoke') or {}
    if (payload.get('action')!='replay' or payload.get('retained_result') or result.get('simulated')
            or baseline.get('kind')!='candidate-v1' or result.get('state')!='PASS'
            or result.get('run_id')!=job['id'] or not digest or result.get('input_digest')!=digest
            or smoke.get('state')!='PASS' or smoke.get('run_id')!=job['id']+'-smoke' or smoke.get('input_digest')!=digest
            or not result.get('task')):
        raise GatewayError('后台归档必须属于本次真实回放和小场景成功收据。')
    now=time.time()
    connection.execute('INSERT OR IGNORE INTO archive_jobs(job,owner,state,payload_sha256,created,updated) VALUES(?,?,?,?,?,?)',
                       (job['id'],job['owner'],'QUEUED',identity(payload),now,now))


def tick(cfg,store,executor=None):
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM archive_jobs WHERE state='RUNNING'").fetchone():return None
        row=c.execute("SELECT * FROM archive_jobs WHERE state='QUEUED' ORDER BY created,job LIMIT 1").fetchone()
        if not row:return None
        row=dict(row)
        c.execute("UPDATE archive_jobs SET state='RUNNING',updated=? WHERE job=?",(time.time(),row['job']))
    outcome='NEEDS_REVIEW';code='ARCHIVE_UNCONFIRMED'
    try:
        job=store.job(row['owner'],row['job'])
        if identity(job['payload'])!=row['payload_sha256']:raise GatewayError('归档任务的冻结输入变化')
        from .assembly_recovery import recover_assembly
        recovered=recover_assembly(cfg,store,row['owner'],row['job'],executor)
        if recovered['state']=='ASSEMBLY_RECOVERED':outcome,code='DONE',None
        else:code=recovered.get('error_code') or code
    except Exception:pass  # Replay delivery and the next hardware job remain unaffected.
    with store.connect() as c:
        c.execute("UPDATE archive_jobs SET state=?,error_code=?,updated=? WHERE job=? AND state='RUNNING'",
                  (outcome,code,time.time(),row['job']))
        original=c.execute('SELECT * FROM jobs WHERE id=? AND owner=?',(row['job'],row['owner'])).fetchone()
        if original:
            original=store.unpack(original);result=original.get('result') or {}
            # Merge only metadata; evaluation may have completed concurrently.
            result['assembly_pending']=False
            result['archive_state']=outcome
            if outcome!='DONE' and (result.get('assembly') or {}).get('state')!='SAVED':
                result['assembly']={'state':'NEEDS_REVIEW'}
            c.execute('UPDATE jobs SET result=?,updated=? WHERE id=? AND owner=?',
                      (json.dumps(result,ensure_ascii=False),time.time(),row['job'],row['owner']))
    return row['job']


def recover(store):
    """An interrupted archive is uncertain; never automatically start another."""
    with store.connect() as c:
        rows=c.execute("SELECT job,owner FROM archive_jobs WHERE state='RUNNING'").fetchall()
        for row in rows:
            original=c.execute('SELECT result FROM jobs WHERE id=? AND owner=?',(row['job'],row['owner'])).fetchone()
            if original:
                result=json.loads(original['result'] or '{}');result.update(assembly_pending=False,archive_state='NEEDS_REVIEW')
                if (result.get('assembly') or {}).get('state')!='SAVED':result['assembly']={'state':'NEEDS_REVIEW'}
                c.execute('UPDATE jobs SET result=?,updated=? WHERE id=?',(json.dumps(result,ensure_ascii=False),time.time(),row['job']))
        c.execute("UPDATE archive_jobs SET state='NEEDS_REVIEW',error_code='INTERRUPTED',updated=? WHERE state='RUNNING'",(time.time(),))
