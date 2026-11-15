"""RoadModel board phases use the existing workstation hardware FIFO.

No second dispatcher, hardware lock, TTL cleanup, or frame-resume implementation.
The original reviewed native controller retains all frame/checkpoint ownership.
"""
import hashlib
import json
import os
from pathlib import Path
import time

from .core import GatewayError,PreDispatchRejected,Store

ACTION='roadmodel_hardware'


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def modules():
    from . import roadmodel_plugin as backend
    # Use the installed plugin's public loader and its release-origin check.
    # The plugin no longer exports the historical load_backend interface.
    return backend,backend.backend('runtime'),backend.backend('active_execution'),backend.backend('two_link')


class HardwareQueue:
    def __init__(self,cfg):
        self.cfg=cfg
        self.store=Store(cfg['state_root'])

    def runtime(self):
        backend,runtime,_,_=modules()
        return runtime.Runtime(Path(self.cfg['state_root'])/'roadmodel',catalog_file=backend.ROOT/'configs/two_link_catalog.json')

    def request_path(self,task):
        if not isinstance(task,str) or not task.startswith('rm-') or not task[3:].isalnum():raise GatewayError('RoadModel任务身份无效')
        return Path(self.cfg['state_root'])/'roadmodel/runs'/task/'compile_alignment/queue-request.json'

    def submit(self,task,job,target):
        if job['kind']!='compile_alignment' or job.get('retained_result'):raise GatewayError('只有新的板端评测进入硬件队列')
        current=self.runtime().get(task['id'],task['owner'])
        inputs=json.loads(task['inputs']) if isinstance(task['inputs'],str) else task['inputs']
        if (current['state']!='RUNNING' or not current.get('event_id') or current['session']!=task['session']
                or current['inputs']!=inputs or current['event_id']!=task.get('event_id')):
            raise GatewayError('RoadModel材料确认或任务状态已变化，未派发台架')
        _,_,active,_=modules()
        if job.get('active_execution'):
            binding=active.verify(job['material_root'],job['active_execution'],job['kind'],job['version'],job['dataset_id'])
            if (Path(binding['execution_root'])/'compile/progress.json').exists():
                # Legacy/paused execution is read-only adoption, never a relaunch.
                detail=active.poll(binding)
                Path(target).mkdir(parents=True,exist_ok=False)
                (Path(target)/'native-attachment.json').write_text(canonical(detail),encoding='utf-8')
                return detail
        path=self.request_path(task['id'])
        if Path(target).resolve()!=path.parent.resolve():raise GatewayError('RoadModel队列材料目录不匹配')
        path.parent.mkdir(parents=True,exist_ok=True)
        for p in [path,*path.parents]:
            if p.is_symlink():raise GatewayError('RoadModel队列材料目录包含链接')
        document={'schema':1,'task':task['id'],'owner':task['owner'],'session':task['session'],
                  'event_id':current['event_id'],'inputs':inputs,'job':job}
        raw=canonical(document)
        if path.exists():
            if path.read_text(encoding='utf-8')!=raw:raise GatewayError('已冻结的RoadModel队列材料发生变化')
        else:
            fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        payload={'action':ACTION,'automatic':True,'roadmodel_task':task['id'],
                 'request_file':str(path),'request_sha256':sha(path)}
        queued,_=self.store.submit(task['owner'],task['session'],'roadmodel-board:'+task['id'],payload)
        return {'state':'QUEUED','mode':'shared_hardware_queue','owner':task['owner'],
                'hardware_job':queued['id'],'task':task['id'],'issues':['已进入统一台架队列，轮到后执行板端评测。']}

    def collect(self,old):
        job=self.store.job(old['owner'],old['hardware_job'])
        if job['payload'].get('action')!=ACTION or job['payload'].get('roadmodel_task')!=old['task']:
            raise GatewayError('RoadModel硬件队列身份不匹配')
        if job['state']=='SUCCEEDED':
            detail=(job.get('result') or {}).get('detail')
            if not isinstance(detail,dict):raise GatewayError('RoadModel硬件阶段缺少完成收据')
            return detail
        if job['state']=='FAILED' and (job.get('result') or {}).get('rejection',{}).get('code')=='ROADMODEL_PREDISPATCH_REJECTED':
            return {**old,'state':'BLOCKED','issues':['板端启动前检查未通过，尚未操作台架；请管理员核对执行绑定或后端环境。']}
        if job['state'] in ('QUEUED','RUNNING'):
            detail=(job.get('result') or {}).get('detail') or {}
            issues=detail.get('issues') or ['已轮到台架，准备执行板端评测。']
            if job['state']=='QUEUED':
                from .queue_status import QueueStatus
                rows=QueueStatus(self.store).snapshot();waiting=[r for r in rows if r['state']=='QUEUED']
                ahead=next((i for i,r in enumerate(waiting) if r['id']==job['id']),0)
                issues=['前方'+str(ahead)+'项等待。']
                occupant=QueueStatus(self.store).roster_text(old['owner'],job['id'],card=True)
                if occupant:issues.append(occupant)
                if any(r['state']=='NEEDS_REVIEW' for r in rows):issues.append('台架有待核验执行，核验完成前不派发。')
                from .availability import description
                resource=QueueStatus(self.store).resource()
                stamp=resource.get('checked_at')
                if type(stamp) in (int,float) and -30<=time.time()-stamp<=60 and resource.get('state') not in ('READY','RESOURCE_BUSY','UNKNOWN'):
                    issues.append(description(resource['state']))
            return {**old,**detail,'mode':'shared_hardware_queue','owner':old['owner'],
                    'task':old['task'],'hardware_job':old['hardware_job'],'state':job['state'],'issues':issues}
        return {**old,'state':'NEEDS_REVIEW','issues':['台架阶段'+job['state']+'；请核对原执行收据，不自动重跑。']}


def execute(cfg,store,queued):
    """Only the existing single hardware worker may invoke this function."""
    dispatch={'entered':False}
    try:return _execute(cfg,store,queued,dispatch)
    except Exception:
        previous=queued.get('result') or {}
        if dispatch['entered'] or previous.get('dispatch_started') or previous.get('detail'):raise
        raise PreDispatchRejected('RoadModel板端启动前检查未通过',
            {'schema_version':1,'code':'ROADMODEL_PREDISPATCH_REJECTED','run_id':queued['id'],
             'hardware_actions':'NOT_RUN','no_new_dispatch':True}) from None


def _execute(cfg,store,queued,dispatch):
    current=store.job(queued['owner'],queued['id'])
    if current['state']!='RUNNING' or current['payload']!=queued['payload']:
        raise GatewayError('RoadModel资源任务尚未被统一调度领取')
    queue=HardwareQueue(cfg);payload=queued['payload'];path=queue.request_path(payload.get('roadmodel_task'))
    if any(p.is_symlink() for p in [path,*path.parents]):raise GatewayError('RoadModel队列材料目录包含链接')
    if payload.get('request_file')!=str(path) or not path.is_file() or path.is_symlink() or sha(path)!=payload.get('request_sha256'):
        raise GatewayError('RoadModel队列材料身份未通过，未启动新的执行')
    document=json.loads(path.read_text(encoding='utf-8'))
    if (document.get('schema')!=1 or document.get('owner')!=queued['owner'] or document.get('session')!=queued['session']
            or document.get('task')!=payload['roadmodel_task']):raise GatewayError('RoadModel队列归属不匹配')
    rt=queue.runtime();task=rt.get(document['task'],queued['owner'])
    if task['state']!='RUNNING':
        return {'detail':{'state':'NEEDS_REVIEW','mode':'shared_no_dispatch','issues':['任务已暂停或结束，未启动板端执行。']},'hardware_actions':'NOT_RUN'}
    if task['inputs']!=document['inputs'] or task['event_id']!=document['event_id']:
        raise GatewayError('RoadModel确认材料变化，未启动新的执行')
    backend,runtime,active,two_link=modules()
    resolved=two_link.resolve_pair(document['inputs'],document['task'],catalog_file=rt.catalog_file)
    specs=[j for j in resolved['jobs'] if j['kind']=='compile_alignment']
    if len(specs)!=1 or not specs[0]['ready'] or canonical(specs[0])!=canonical(document['job']):
        raise GatewayError('RoadModel执行绑定变化，未启动新的执行')
    if (Path(cfg['project_root'])/'runtime/service/drain.json').exists():return {'state':'RESOURCE_BUSY'}
    target=path.parent/'execution'
    # The frozen native adapter arbitrates existing dispatch receipts and frames.
    dispatch['entered']=True
    queued['result']={**(queued.get('result') or {}),'dispatch_started':True}
    with store.connect() as c:
        c.execute("UPDATE jobs SET result=?,updated=? WHERE id=? AND owner=? AND state='RUNNING'",
            (json.dumps(queued['result']),time.time(),queued['id'],queued['owner']))
    detail=runtime.execute_atomic(document['job'],target)
    def progress(detail):
        queued['result']={'detail':detail,'dispatch_started':True}
        with store.connect() as c:
            c.execute("UPDATE jobs SET result=?,updated=? WHERE id=? AND owner=? AND state='RUNNING'",
                (json.dumps(queued['result'],ensure_ascii=False),time.time(),queued['id'],queued['owner']))
    while detail.get('mode')=='active_native_execution':
        progress(detail)
        if detail.get('native_finished'):
            if detail.get('completed_frames')!=detail.get('expected_frames'):
                raise GatewayError('原生板端阶段完成范围不一致，需核验原收据')
            break  # Report generation/CPU/cloud work does not hold the FIFO.
        if detail.get('state')!='RUNNING':raise GatewayError('原生板端执行结果需核验，不自动重跑')
        time.sleep(max(.1,min(5,float(cfg.get('poll_seconds',3)))))
        detail=active.poll(detail['binding'])
    if detail.get('state')=='NEEDS_REVIEW' and not detail.get('native_finished'):
        raise GatewayError('板端执行结果未知，保留原任务与检查点，不自动重跑')
    return {'detail':detail,'project':'roadmodel','resource_phase_complete':True}
