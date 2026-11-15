"""Durable business notifications from scoped execution events, never raw tool output."""
import json
from pathlib import Path
import threading
import time

PREFIX='@@BENCH_PROGRESS@@'


def schema(store):
    with store.connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS progress_outbox(
          id INTEGER PRIMARY KEY,job TEXT NOT NULL REFERENCES jobs(id),event_key TEXT NOT NULL,
          session TEXT NOT NULL,text TEXT NOT NULL,sent INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at REAL NOT NULL DEFAULT 0,created REAL NOT NULL,
          UNIQUE(job,event_key));
        CREATE TABLE IF NOT EXISTS input_outbox(
          id INTEGER PRIMARY KEY,owner TEXT NOT NULL,session TEXT NOT NULL,event_key TEXT NOT NULL,
          text TEXT NOT NULL,sent INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at REAL NOT NULL DEFAULT 0,created REAL NOT NULL,UNIQUE(owner,event_key));
        ''')


def notify_input(store,user,session,key,text):
    from .wording import display_text
    text=display_text(text)
    schema(store)
    with store.connect() as c:c.execute('INSERT OR IGNORE INTO input_outbox(owner,session,event_key,text,created) VALUES(?,?,?,?,?)',(user,session,key,text,time.time()))


def notice(store,job,key,text):
    from .wording import display_text
    text=display_text(text)
    schema(store)
    with store.connect() as c:
        row=c.execute('SELECT state,session FROM jobs WHERE id=? AND owner=?',(job['id'],job['owner'])).fetchone()
        if row and row['state']=='RUNNING':
            c.execute('INSERT OR IGNORE INTO progress_outbox(job,event_key,session,text,created) VALUES(?,?,?,?,?)',
                      (job['id'],key,row['session'],'任务 '+job['id']+'：'+text,time.time()))


def stage(store,job,value):
    from .task_status import advance,STAGES
    if value in STAGES:advance(store,job,*STAGES[value])
    messages={'小场景预检中':'正在准备台架软件并进行本次小场景测试，通过后才进入正式回灌。',
        '预检失败，检查软件包及依赖':'小场景未通过，全量回灌尚未开始。正在进行一次有界诊断并核对台架恢复，暂时无需重复提交。',
        '正式回灌中':'小场景测试通过，开始正式数据回灌，共 '+str(len(job['payload'].get('scenes',[])))+' 个场景。',
        '回灌完成，保存本人装配包':'全量回灌与结果验收已完成，正在保存本人可复用软件装配。'}
    if value in messages:notice(store,job,'stage:'+value,messages[value])


def event(store,owner,parent,run,event):
    if event.get('schema_version')!=1 or event.get('run_id')!=run:return
    job=store.job(owner,parent)
    if job['state']!='RUNNING' or job['payload'].get('action')!='replay':return
    kind=event.get('kind');smoke=run==parent+'-smoke'
    if run not in (parent,parent+'-smoke'):return
    if kind in ('SOFTWARE_INSTALLED','SOFTWARE_READY') and smoke:
        from .task_status import advance
        advance(store,job,20,'小场景预检')
        text=('刷包完成，软件及依赖文件校验通过。' if kind=='SOFTWARE_INSTALLED' else '台架当前软件及依赖与本次输入一致，已跳过重复刷包。')+'接下来启动小场景测试。'
        notice(store,job,'software-ready',text)
    elif kind=='PHASE' and event.get('phase') in ('恢复正式服务','核验恢复','收集工作站预测'):
        if not smoke:
            from .task_status import advance,PHASES
            advance(store,job,*PHASES[event['phase']])
            notice(store,job,'phase:'+event['phase'],event['phase']+'中，完成后返回结果目录。')
    elif kind=='BATCH' and not smoke:
        done,total=event.get('completed'),event.get('total')
        if type(done) is not int or type(total) is not int or total!=len(job['payload'].get('scenes',[])) or not 0<=done<=total:return
        now=time.time()
        from .task_status import advance
        advance(store,job,30,'正式回灌')
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute("SELECT result FROM jobs WHERE id=? AND owner=? AND state='RUNNING'",(parent,owner)).fetchone()
            if not current:return
            result=json.loads(current['result'] or '{}');previous=result.get('execution_progress') or {}
            if done<previous.get('completed',0):return
            publish=done-previous.get('notified_completed',0)>=3 or now-previous.get('notified_at',now)>=120
            result['execution_progress']={'completed':done,'total':total,'observed_at':now,
                'notified_completed':done if publish else previous.get('notified_completed',0),
                'notified_at':now if publish else previous.get('notified_at',now)}
            c.execute("UPDATE jobs SET result=?,updated=? WHERE id=? AND owner=? AND state='RUNNING'",(json.dumps(result,ensure_ascii=False),now,parent,owner))
        if publish:notice(store,job,'batch:'+str(done)+':'+str(int(now//120)),f'正式回灌已完成 {done}/{total} 场景，正在按顺序继续执行。' if done<total else f'{done}/{total} 场景回放完成，等待恢复与产品验收。')


class Follow:
    def __init__(self,store,owner,run,path):
        self.store,self.owner,self.run,self.path=store,owner,run,Path(path)
        self.parent=run[:-6] if run.endswith('-smoke') else run
        self.offset=self.path.stat().st_size if self.path.exists() else 0
        self.pending=b'';self.stop=threading.Event();self.thread=None

    def poll(self):
        if not self.path.exists():return
        with self.path.open('rb') as f:f.seek(self.offset);chunk=f.read(256*1024);self.offset=f.tell()
        data=self.pending+chunk;lines=data.split(b'\n');self.pending=lines.pop()[-8192:]
        for raw in lines:
            if len(raw)>8192 or not raw.startswith(PREFIX.encode()):continue
            try:event(self.store,self.owner,self.parent,self.run,json.loads(raw[len(PREFIX):]))
            except Exception:continue  # Notification failure never retries or alters hardware work.

    def __enter__(self):
        def loop():
            while not self.stop.wait(.5):
                try:self.poll()
                except Exception:pass
        self.thread=threading.Thread(target=loop,daemon=True);self.thread.start();return self

    def __exit__(self,*args):
        self.stop.set();self.thread.join(timeout=2)
        try:self.poll()
        except Exception:pass


def deliver(store,sender,deadline=None,*,cards=False,unified=False):
    schema(store);count=0;now=time.time()
    with store.connect() as c:
        active=[store.unpack(row) for row in c.execute("SELECT * FROM jobs WHERE state='RUNNING'")]
    for job in active:
        value=(job.get('result') or {}).get('stage')
        with store.connect() as c:last=c.execute('SELECT MAX(created) FROM progress_outbox WHERE job=?',(job['id'],)).fetchone()[0]
        if not cards and value and now-(last if last is not None else job['updated'])>=90:
            execution=(job.get('result') or {}).get('execution_progress') or {}
            counts=('，已完成 '+str(execution['completed'])+'/'+str(execution['total'])+' 场景') if execution.get('total') else ''
            notice(store,job,'still-running:'+str(int(now//90)),'任务仍在执行，当前阶段：'+value+counts+'。尚未收到完成收据，无需重复提交。')
    with store.connect() as c:
        # Completed-result messages supersede undelivered execution chatter.
        c.execute("UPDATE progress_outbox SET sent=2 WHERE sent=0 AND job IN (SELECT id FROM jobs WHERE state NOT IN ('QUEUED','RUNNING'))")
        groups=[(table,[dict(r) for r in c.execute(f'SELECT * FROM {table} WHERE sent=0 AND next_attempt_at<=? ORDER BY id LIMIT 20',(now,))]) for table in ('input_outbox','progress_outbox')]
    for table,rows in groups:
        for row in rows:
            if deadline is not None and time.monotonic()>=deadline:return count
            with store.connect() as c:
                if not c.execute(f'SELECT 1 FROM {table} WHERE id=? AND sent=0',(row['id'],)).fetchone():continue
                if unified:
                    from .input_cards import route
                    routed=route(store,row,job=row.get('job') if table=='progress_outbox' else None)
                    if routed=='CARD':
                        c.execute(f'UPDATE {table} SET sent=2 WHERE id=? AND sent=0',(row['id'],));continue
                    if routed=='WAIT':continue
                if cards and table=='progress_outbox':
                    critical=row['event_key'] in ('stage:小场景预检中','software-ready','stage:正式回灌中','stage:预检失败，检查软件包及依赖')
                    if not critical:
                        c.execute('UPDATE progress_outbox SET sent=2 WHERE id=? AND sent=0',(row['id'],));continue
                    if row['event_key']=='stage:小场景预检中':
                        row['text']='任务 '+row['job']+'：已开始，正在准备台架软件，随后执行本次小场景预检。'
                if table=='input_outbox' and row['event_key'].startswith(('preparation:','preparation-wait:')):
                    parts=row['event_key'].split(':');pid=parts[1];phase=parts[2]
                    prep=c.execute('SELECT state,result FROM preparations WHERE id=? AND owner=?',(pid,row['owner'])).fetchone()
                    progress=(json.loads(prep['result'] or '{}').get('progress') or {}) if prep else {}
                    if not prep or prep['state'] not in ('RUNNING','STATIC_PASSED') or progress.get('phase')!=phase:
                        c.execute('UPDATE input_outbox SET sent=2 WHERE id=?',(row['id'],));continue
                if table=='progress_outbox' and not c.execute("SELECT 1 FROM jobs WHERE id=? AND state='RUNNING'",(row['job'],)).fetchone():continue
            try:ok=sender({**row,'attachment':None,'_outbox_table':table})
            except Exception:ok=False
            with store.connect() as c:c.execute(f'UPDATE {table} SET sent=?,attempts=attempts+1,next_attempt_at=? WHERE id=? AND sent=0',
                (int(bool(ok)),0 if ok else time.time()+min(60,2**min(row['attempts'],5)),row['id']))
            count+=int(bool(ok))
    return count
