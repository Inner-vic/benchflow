"""Bridge the legacy dual-chain board unit to PCM's single hardware FIFO.

RoadModel owns business records and native checkpoints. This adapter owns no
hardware lock and never restarts inference from a completion counter.
"""
import json
import time
from pathlib import Path

from .roadmodel_queue import HardwareQueue

MODE='shared_hardware_queue'

def _rows(rt):
    with rt.connect() as c:
        return [dict(r) for r in c.execute("SELECT c.task,c.kind,c.detail,t.owner FROM chains c JOIN tasks t ON t.id=c.task WHERE c.state='RUNNING' AND t.state='RUNNING'")]

def _save(rt,row,detail):
    state=detail['state']
    if state=='QUEUED':state='RUNNING'
    if state not in ('RUNNING','SUCCEEDED','PARTIAL','FAILED','BLOCKED','NEEDS_REVIEW'):state='NEEDS_REVIEW'
    with rt.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        changed=c.execute("UPDATE chains SET state=?,detail=? WHERE task=? AND kind=? AND state='RUNNING' AND detail=? AND EXISTS(SELECT 1 FROM tasks WHERE id=? AND state='RUNNING')",
            (state,json.dumps(detail,ensure_ascii=False),row['task'],row['kind'],row['detail'],row['task'])).rowcount
        if not changed:return
        states=[r[0] for r in c.execute('SELECT state FROM chains WHERE task=?',(row['task'],))]
        if 'NEEDS_REVIEW' in states:final='NEEDS_REVIEW'
        elif any(s in ('RUNNING','PENDING','QUEUED') for s in states):final='RUNNING'
        elif all(s=='SUCCEEDED' for s in states):final='SUCCEEDED'
        else:final='PARTIAL' if 'SUCCEEDED' in states else 'BLOCKED'
        c.execute("UPDATE tasks SET state=?,updated=? WHERE id=? AND state='RUNNING'",(final,time.time(),row['task']))

def refresh(cfg,rt,queue=None):
    queue=queue or HardwareQueue(cfg)
    for row in _rows(rt):
        old=json.loads(row['detail'])
        if old.get('mode')!=MODE:continue
        # collect verifies the hardware Job's owner, business task and action.
        try:detail=queue.collect(old)
        except Exception:
            detail={**old,'state':'NEEDS_REVIEW','issues':['统一台架任务身份或执行结果待核验；不重新推理。']}
        _save(rt,row,detail)

def recover(cfg,rt,queue=None):
    queue=queue or HardwareQueue(cfg)
    # Valid queued units survive a Bot restart. Started/unknown board units
    # still pass through the gateway worker's RUNNING -> NEEDS_REVIEW barrier.
    for row in _rows(rt):
        old=json.loads(row['detail'])
        if old.get('mode')=='active_native_execution':continue
        if old.get('mode')==MODE:
            try:
                detail=queue.collect(old)
                if detail.get('state')=='QUEUED':continue
            except Exception:pass
        _save(rt,row,{**old,'state':'NEEDS_REVIEW','issues':['调度曾中断，请核对原执行；不会自动重跑。']})
    with rt.connect() as c:
        c.execute("UPDATE tasks SET state='NEEDS_REVIEW',notice='执行状态待核验，不自动重跑' WHERE state='RUNNING' AND NOT EXISTS(SELECT 1 FROM chains WHERE chains.task=tasks.id AND state='RUNNING')")

def tick(cfg,rt,queue=None,native=None):
    queue=queue or HardwareQueue(cfg)
    if native is None:
        from .roadmodel_plugin import backend
        native=backend('runtime').execute_atomic
    refresh(cfg,rt,queue)
    def execute(job,target):
        # Retained results and model accuracy are CPU/cloud work, no board claim.
        if job['kind']!='compile_alignment' or job.get('retained_result'):
            return native(job,target)
        tid=Path(target).parent.name
        with rt.connect() as c:
            row=c.execute("SELECT owner FROM tasks WHERE id=? AND state='RUNNING'",(tid,)).fetchone()
        if not row:raise ValueError('RoadModel task is not active')
        task=rt.get(tid,row['owner'])
        detail=queue.submit(task,job,target)
        return {**detail,'state':'RUNNING' if detail.get('state')=='QUEUED' else detail['state']}
    return rt.tick(executor=execute)
