"""Button-only opt-in analysis, independent of replay/evaluation state and lock."""
import base64
import hashlib
import json
from pathlib import Path,PurePosixPath
import time
import uuid
from .core import GatewayError,within

STATES=('QUEUED','RUNNING','SUCCEEDED','NEEDS_INPUT','FAILED','NEEDS_REVIEW')
NAMES=('arrival-accuracy.xlsx','analysis_plan.json')

def schema(store):
    with store.connect() as c:c.execute('''CREATE TABLE IF NOT EXISTS arrival_analyses(
      id TEXT PRIMARY KEY,job TEXT NOT NULL UNIQUE REFERENCES jobs(id),owner TEXT NOT NULL,session TEXT NOT NULL,
      state TEXT NOT NULL,request TEXT NOT NULL,result TEXT NOT NULL DEFAULT '{}',
      reports TEXT NOT NULL DEFAULT '{}',created REAL NOT NULL,updated REAL NOT NULL)''')

def eligible(job):
    r=job.get('result') or {}
    return (job['payload'].get('action')=='replay' and job['state'] in ('SUCCEEDED','PARTIAL_SUCCESS','POSTPROCESSING')
            and not r.get('simulated') and r.get('state')=='PASS' and r.get('run_id')==job['id']
            and r.get('task')=='/mnt/data/bench-eval/tasks/'+job['id'])

def view(store,job):
    if not eligible(job):return {}
    with store.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='arrival_analyses'").fetchone():return {'state':'AVAILABLE'}
        row=c.execute('SELECT state,result,reports FROM arrival_analyses WHERE job=? AND owner=?',(job['id'],job['owner'])).fetchone()
    if not row:return {'state':'AVAILABLE'}
    result=json.loads(row['result']);state=row['state'];reports=json.loads(row['reports'])
    detail={'QUEUED':'等待计算','RUNNING':'计算中','SUCCEEDED':'已完成，报告交付中','FAILED':'计算未完成，回放结果保留',
            'NEEDS_REVIEW':'计算结果待核验，不会重复计算','NEEDS_INPUT':'输入不满足，回放结果保留'}[state]
    if result.get('code')=='MISSING_DISTANCE_CSV':detail='本次回放未生成距离CSV，无法计算；回放结果保留'
    elif result.get('code')=='MISSING_XCP':detail='缺少同场景XCP信号，无法计算；回放结果保留'
    elif result.get('code')=='TRUTH_ROOT_UNAVAILABLE':detail='原任务数据集目录不可读，无法计算'
    if state=='SUCCEEDED' and reports and all(x.get('message_id') for x in reports.values()):detail='已完成，报告已交付'
    elif state=='SUCCEEDED' and any(x.get('state')=='UNKNOWN' for x in reports.values()):detail='已完成，报告发送待核验；不会重复计算'
    return {'state':state,'detail':detail}

def touch(c,job):
    c.execute('UPDATE task_cards SET last_sent=0,next_attempt=0 WHERE job=?',(job,))

def enqueue(store,user,job):
    store.require_user(user)
    if job['owner']!=user or not eligible(job):raise GatewayError('仅能对本人已完成回放并交付结果的任务计算到点精度。')
    data=(job['payload'].get('assets') or {}).get('dataset') or {}
    if data.get('owner') not in (user,'shared'):raise GatewayError('原任务数据源不属于本人或已授权共享源。')
    truth=data.get('data',{}).get('remote_path','')
    if not truth.startswith('/') or '..' in PurePosixPath(truth).parts:raise GatewayError('原任务没有有效的数据集路径。')
    schema(store);now=time.time();aid='arr-'+uuid.uuid4().hex[:24]
    req={'run_id':job['id'],'source_task':job['result']['task'],'truth_root':truth,'attempt':aid}
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT * FROM jobs WHERE id=? AND owner=?',(job['id'],user)).fetchone()
        if not current or not eligible(store.unpack(current)):raise GatewayError('原回放任务状态已改变，本次未提交分析。')
        c.execute('INSERT OR IGNORE INTO arrival_analyses(id,job,owner,session,state,request,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                  (aid,job['id'],user,job['session'],'QUEUED',json.dumps(req),now,now))
        row=c.execute('SELECT * FROM arrival_analyses WHERE job=? AND owner=?',(job['id'],user)).fetchone()
        touch(c,job['id'])
    return dict(row)

def run_remote(cfg,row):
    from .transfer import SSH
    code=(Path(cfg['project_root'])/'bench_gateway/arrival_remote.py').read_text(encoding='utf-8')
    encoded=base64.b64encode(row['request'].encode()).decode()
    return json.loads(SSH(cfg).python(code,(encoded,),timeout=660))

def tick(cfg,store,runner=None):
    schema(store)
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM arrival_analyses WHERE state='RUNNING'").fetchone():return None
        row=c.execute("SELECT * FROM arrival_analyses WHERE state='QUEUED' ORDER BY created,id LIMIT 1").fetchone()
        if not row:return None
        row=dict(row);c.execute("UPDATE arrival_analyses SET state='RUNNING',updated=? WHERE id=?",(time.time(),row['id']));touch(c,row['job'])
    result={'state':'NEEDS_REVIEW','code':'REMOTE_OUTCOME_UNKNOWN'};reports={}
    try:
        job=store.job(row['owner'],row['job'])
        if not eligible(job) or job['session']!=row['session']:raise GatewayError('Original replay changed')
        if runner is None and (cfg.get('mode')!='live' or not cfg.get('live_enabled')):raise GatewayError('Live analysis is disabled')
        result=(runner or run_remote)(cfg,row)
        if not isinstance(result,dict) or result.get('state') not in STATES:raise GatewayError('Invalid analysis result')
        if result['state']=='SUCCEEDED':
            req=json.loads(row['request']);expected=req['source_task']+'/evaluations/arrival-accuracy/'+req['attempt']
            if result.get('output')!=expected or set(result.get('files',{}))!=set(NAMES):raise GatewayError('Report identity differs')
            directory=within(store.user_root(row['owner']),store.user_root(row['owner'])/'arrival-reports'/row['id']);directory.mkdir(parents=True,exist_ok=True)
            for name in NAMES:
                f=result['files'][name];raw=base64.b64decode(f['data'],validate=True)
                if not 0<len(raw)<=4*1024*1024 or len(raw)!=f['bytes'] or hashlib.sha256(raw).hexdigest()!=f['sha256']:raise GatewayError('Report digest differs')
                path=within(directory,directory/name)
                if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=f['sha256']:raise GatewayError('Existing report differs')
                if not path.exists():path.write_bytes(raw)
                reports[name]={'path':str(path),'sha256':f['sha256'],'state':'PENDING'}
            result=dict(result);result.pop('files')
    except Exception as exc:result={'state':'NEEDS_REVIEW','code':'REMOTE_OUTCOME_UNKNOWN','error_type':type(exc).__name__};reports={}
    with store.connect() as c:
        c.execute("UPDATE arrival_analyses SET state=?,result=?,reports=?,updated=? WHERE id=? AND state='RUNNING'",
                  (result['state'],json.dumps(result),json.dumps(reports),time.time(),row['id']));touch(c,row['job'])
    return row['id']

def recover(store):
    schema(store)
    with store.connect() as c:
        rows=c.execute("SELECT job FROM arrival_analyses WHERE state='RUNNING'").fetchall()
        for row in rows:touch(c,row['job'])
        c.execute("UPDATE arrival_analyses SET state='NEEDS_REVIEW',result=?,updated=? WHERE state='RUNNING'",
                  (json.dumps({'code':'INTERRUPTED'}),time.time()))
        for row in c.execute("SELECT id,job,reports FROM arrival_analyses WHERE state='SUCCEEDED'").fetchall():
            reports=json.loads(row['reports'])
            if any(item.get('state')=='SENDING' for item in reports.values()):
                for item in reports.values():
                    if item.get('state')=='SENDING':item['state']='UNKNOWN'
                c.execute('UPDATE arrival_analyses SET reports=?,updated=? WHERE id=?',(json.dumps(reports),time.time(),row['id']));touch(c,row['job'])

def deliver(cfg,store,*,deadline=None,client=None):
    schema(store)
    from .feishu_files import FeishuFiles
    client=client or FeishuFiles(cfg);count=0
    def budget():
        left=8 if deadline is None else deadline-time.monotonic()
        if left<1:raise TimeoutError('Notification budget')
        return min(8,left)
    with store.connect() as c:rows=[dict(r) for r in c.execute('''SELECT a.* FROM arrival_analyses a
        WHERE a.state='SUCCEEDED' AND EXISTS(SELECT 1 FROM json_each(a.reports) r
          WHERE json_extract(r.value,'$.state') NOT IN ('SENT','SENDING','UNKNOWN')
          AND COALESCE(json_extract(r.value,'$.next_attempt'),0)<=?)
        ORDER BY a.created LIMIT 10''',(time.time(),))]
    for row in rows:
        reports=json.loads(row['reports'])
        for name,data in reports.items():
            if data.get('message_id') or data['state'] in ('SENDING','UNKNOWN') or data.get('next_attempt',0)>time.time():continue
            try:
                budget();job=store.job(row['owner'],row['job'])
                if job['session']!=row['session'] or not eligible(job):continue
                with store.connect() as c:card=c.execute('SELECT message_id FROM task_cards WHERE job=?',(row['job'],)).fetchone()
                if not card or not card['message_id']:continue
                mid=card['message_id'];actual=client.message(mid,timeout=budget())
                if actual['chat_id']!=row['session'].split(':')[1] or actual['sender']['id']!=cfg['feishu_app_id']:raise GatewayError('Original card identity differs')
                path=within(store.user_root(row['owner']),data['path'])
                if hashlib.sha256(path.read_bytes()).hexdigest()!=data['sha256']:raise GatewayError('Local report changed')
                if not data.get('file_key'):
                    data['file_key']=client.upload_report(path,timeout=budget())
                    with store.connect() as c:c.execute('UPDATE arrival_analyses SET reports=? WHERE id=?',(json.dumps(reports),row['id']))
                budget();data['state']='SENDING'
                with store.connect() as c:c.execute('UPDATE arrival_analyses SET reports=? WHERE id=?',(json.dumps(reports),row['id']))
                key=hashlib.sha256((row['id']+':'+name).encode()).hexdigest()[:40]
                data['message_id']=client.reply_report(mid,data['file_key'],key,timeout=budget());data['state']='SENT';count+=1
            except Exception:
                if data['state']=='SENDING':data['state']='UNKNOWN'
                data['attempts']=data.get('attempts',0)+1
                data['next_attempt']=time.time()+min(300,5*2**min(data['attempts'],6))
            finally:
                with store.connect() as c:c.execute('UPDATE arrival_analyses SET reports=?,updated=? WHERE id=?',(json.dumps(reports),time.time(),row['id']));touch(c,row['job'])
    return count
