"""Trusted intake changes advance once through preparation and guarded admission."""
import json
import time

from .core import GatewayError
from .tasks import Tasks
from .preparation import PreparationQueue, fingerprint
from .execution_identity import EquivalentExecution
from .incremental import intake_blocked


class AutoFlow:
    def __init__(self,cfg,store):
        self.cfg,self.store=cfg,store
        self.tasks=Tasks(store)
        from .confirmations import configure
        configure(cfg,store)
        with store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS automatic_tasks(
              task_id TEXT PRIMARY KEY REFERENCES task_requests(id),owner TEXT NOT NULL,session TEXT NOT NULL,
              revision INTEGER NOT NULL,fingerprint TEXT NOT NULL,state TEXT NOT NULL,not_before REAL NOT NULL,
              preparation_id TEXT,job_id TEXT,error TEXT NOT NULL DEFAULT '',updated REAL NOT NULL)''')

    def arm(self,user,session,task=None):
        if not self.cfg.get('auto_execute_on_complete'):return
        from .service import identity
        if identity(session)!=user:raise GatewayError('自动提交会话归属不一致')
        task=task or self.tasks.current(user)
        if not task or task['job_id']:return
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT t.* FROM task_requests t JOIN task_focus f ON f.task_id=t.id WHERE t.owner=? AND f.owner=?',(user,user)).fetchone()
            if not current or current['id']!=task['id'] or current['revision']!=task['revision'] or current['job_id']:return
            from .confirmations import enabled,active,invalidate
            if enabled(self.cfg):
                confirmation=active(c,user,dict(current),session)
                previous=c.execute('SELECT state FROM automatic_tasks WHERE task_id=?',(task['id'],)).fetchone()
                if confirmation and previous and previous['state'] in ('WAITING','CHECKING'):return
                invalidate(c,user,task['id'],'material intake changed or confirmation absent')
                now=time.time()
                c.execute('''INSERT INTO automatic_tasks(task_id,owner,session,revision,fingerprint,state,not_before,updated)
                  VALUES(?,?,?,?,?,'AWAITING_CONFIRMATION',?,?) ON CONFLICT(task_id) DO UPDATE SET session=excluded.session,
                  revision=excluded.revision,fingerprint=excluded.fingerprint,state='AWAITING_CONFIRMATION',
                  not_before=excluded.not_before,preparation_id=NULL,job_id=NULL,error='',updated=excluded.updated''',
                  (task['id'],user,session,task['revision'],fingerprint(task['data']),now,now))
            else:
                now=time.time();settle=max(0,min(30,float(self.cfg.get('intake_settle_seconds',15))))
                previous=c.execute('SELECT state FROM automatic_tasks WHERE owner=? ORDER BY updated DESC LIMIT 1',(user,)).fetchone()
                initial='PAUSED' if previous and previous['state']=='PAUSED' else 'WAITING'
                c.execute('''INSERT INTO automatic_tasks(task_id,owner,session,revision,fingerprint,state,not_before,updated)
                  VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET session=excluded.session,
                  revision=excluded.revision,fingerprint=excluded.fingerprint,
                  state=CASE WHEN automatic_tasks.state='PAUSED' THEN 'PAUSED' ELSE 'WAITING' END,
                  not_before=excluded.not_before,preparation_id=NULL,job_id=NULL,error='',updated=excluded.updated''',
                  (task['id'],user,session,task['revision'],fingerprint(task['data']),initial,now+settle,now))
        from .input_cards import ensure
        ensure(self.cfg,self.store,user,session,task)

    def control(self,user,session,pause):
        task=self.tasks.current(user) or self.tasks.ensure(user,session)
        previous=bool(task['job_id'])
        if previous:
            from .intake import Intake
            Intake(self.store).update(user,{},incoming_kind='control')
            task=self.tasks.current(user)
        self.arm(user,session,task)
        with self.store.connect() as c:
            from .confirmations import enabled,invalidate
            if enabled(self.cfg):invalidate(c,user,task['id'],'paused or returned to material review')
            if not pause:c.execute("UPDATE input_batches SET state='RESOLVED',updated=? WHERE owner=? AND state='ABORTED'",(time.time(),user))
            c.execute('UPDATE automatic_tasks SET state=?,not_before=?,updated=? WHERE owner=? AND task_id=?',
                      ('PAUSED' if pause else 'AWAITING_CONFIRMATION' if enabled(self.cfg) else 'WAITING',time.time(),time.time(),user,task['id']))
        prefix='已建立下一次材料草稿，原任务保持不变。' if previous else ''
        if enabled(self.cfg):return prefix+('已暂缓，材料保留；后续仍需点击“确认执行”。' if pause else '已回到材料确认卡，请核对后点击“确认执行”。')
        return prefix+('已暂停当前材料的自动提交。' if pause else '已恢复自动处理；材料齐备后自动检查并排队。')

    def receipt(self,user):
        task=self.tasks.current(user)
        if not task or task['job_id']:return ''
        if self.cfg.get('require_material_confirmation'):return '材料已登记，请核对卡片并点击“确认执行”；补充文件只更新材料，不会自动开始。'
        with self.store.connect() as c:
            row=c.execute('SELECT state FROM automatic_tasks WHERE task_id=? AND owner=?',(task['id'],user)).fetchone()
            if row and row['state']=='PAUSED':return '自动提交已暂停。'
            if row and row['state']=='AWAITING_MATERIAL':return '执行目标已保存，等待后续材料或明确开始，尚未提交台架。'
            if row and row['state']=='NO_CHANGE':return '执行输入无有效变更，未重复提交回放。'
            if c.execute("SELECT 1 FROM input_batches WHERE owner=? AND state='ABORTED' LIMIT 1",(user,)).fetchone():return '上次材料接收未完成，自动提交已暂停。请重新发送未成功的文件，或核对后发送“检查材料”。'
            if self.pending_attachments(c,task['id'],user):return ''
        from .business import missing_inputs,LABELS
        missing=[LABELS[i['field']] for i in missing_inputs(task['data']) if not (i['code']=='M100_BUILD_REQUIRED' and self.cfg.get('m100_build_enabled'))]
        return '待补充：'+'、'.join(missing)+'。' if missing else '材料齐备，将自动检查并排队。'

    def update(self,row,state,**values):
        allowed={'preparation_id','job_id','error'}
        assert set(values)<=allowed
        with self.store.connect() as c:
            return c.execute('UPDATE automatic_tasks SET state=?,updated=?'+''.join(','+k+'=?' for k in values)+
                      ' WHERE task_id=? AND owner=? AND revision=? AND fingerprint=? AND state=?',
                      (state,time.time(),*values.values(),row['task_id'],row['owner'],row['revision'],row['fingerprint'],row['state'])).rowcount

    def pending_attachments(self,c,task_id,user):
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_attachments'").fetchone():return False
        return bool(c.execute("SELECT 1 FROM task_attachments WHERE owner=? AND task_id=? AND state NOT IN ('ASSOCIATED','SUPERSEDED','DUPLICATE') LIMIT 1",(user,task_id)).fetchone())

    def tick(self):
        if not self.cfg.get('auto_execute_on_complete'):return
        queue=PreparationQueue(self.cfg,self.store)
        with self.store.connect() as c:
            rows=[dict(r) for r in c.execute("SELECT * FROM automatic_tasks WHERE state IN ('WAITING','CHECKING') AND not_before<=? ORDER BY updated LIMIT 20",(time.time(),))]
        for row in rows:
            user=row['owner'];task=self.tasks.get(user,row['task_id']);focus=self.tasks.current(user)
            if task['job_id']:
                self.update(row,'SUBMITTED',job_id=task['job_id']);continue
            if not focus or focus['id']!=task['id']:
                self.update(row,'INACTIVE');continue
            if task['revision']!=row['revision'] or fingerprint(task['data'])!=row['fingerprint']:
                # A message may have committed before its facade armed this revision.
                # Never submit or silently arm that new content from the worker.
                continue
            from .confirmations import enabled,active
            if enabled(self.cfg):
                with self.store.connect() as c:confirmation=active(c,user,task,row['session'])
                if not confirmation:
                    self.update(row,'AWAITING_CONFIRMATION');continue
            from .business import missing_inputs,capability_issues
            missing=[i for i in missing_inputs(task['data']) if not (i['code']=='M100_BUILD_REQUIRED' and self.cfg.get('m100_build_enabled'))]
            with self.store.connect() as c:attachments=self.pending_attachments(c,task['id'],user) or intake_blocked(c,user)
            if missing or attachments:continue
            blocked=capability_issues(task['data'],self.cfg.get('default_evaluator'))
            if blocked:
                self.block(row,'；'.join(i['message']+' '+i['next_action'] for i in blocked));continue
            prep=queue.latest(user)
            if row['state']=='WAITING':
                # A fresh confirmation/intake may retry a failed binding. Keep
                # its historical receipt, but do not reuse that failed result.
                binding=(prep.get('result') or {}).get('adapter_binding') if prep else None
                binding_failed=bool(prep and prep['state']=='STATIC_PASSED' and
                    isinstance(binding,dict) and binding.get('state')=='NEEDS_INPUT')
                from .bindings import preparation_needs_rebind
                binding_outdated=bool(prep and prep['state']=='STATIC_PASSED' and preparation_needs_rebind(self.cfg,self.store,user,prep))
                if not prep or prep['fingerprint']!=row['fingerprint'] or prep['state']=='NEEDS_INPUT' or binding_failed or binding_outdated:
                    queue.submit(user,row['session'],expected_task=row);prep=queue.latest(user)
                self.update(row,'CHECKING',preparation_id=prep['id']);continue
            if not prep or prep['id']!=row['preparation_id'] or prep['fingerprint']!=row['fingerprint']:
                self.block(row,'材料检查记录已变化，请重新检查当前材料。');continue
            result=prep.get('result') or {};binding=result.get('adapter_binding') or {}
            if prep['state']=='NEEDS_INPUT':
                self.update(row,'BLOCKED',error='材料检查未通过');continue  # Existing preparation delivery lists exact missing inputs.
            if prep['state']!='STATIC_PASSED' or not isinstance(binding,dict) or binding.get('state')=='BINDING':continue
            if binding.get('state')!='READY_FOR_GATE':
                self.block(row,binding.get('message') or '回放配置未就绪，请管理员核验。');continue
            from .service import Service
            guard={'task_id':task['id'],'revision':row['revision'],'fingerprint':row['fingerprint'],'preparation_id':prep['id']}
            try:
                reply=Service(self.cfg).handle(row['session'],'自动提交当前已核验材料',{'action':'replay'},
                    request_id='automatic:'+task['id'],expected_task=guard)
            except EquivalentExecution as exc:
                if exc.state in ('FAILED','NEEDS_REVIEW'):
                    self.block(row,'相同输入的任务 '+exc.job_id+' '+('待核验，未重复派发。' if exc.state=='NEEDS_REVIEW' else '已失败；请修正材料或明确发送“重新回放”。'))
                elif self.update(row,'NO_CHANGE',job_id=exc.job_id):
                    with self.store.connect() as c:c.execute("UPDATE task_requests SET state='NO_CHANGE' WHERE id=? AND owner=? AND revision=? AND job_id IS NULL",(task['id'],user,row['revision']))
                    from .progress import notify_input
                    notify_input(self.store,user,row['session'],'no-change:'+task['id']+':'+str(row['revision']),
                                 '执行输入无有效变更，未重复提交回放。\n关联任务：'+exc.job_id)
                continue
            except GatewayError as exc:
                current=self.tasks.get(user,task['id'])
                if current['revision']!=row['revision'] or current['job_id']:continue
                with self.store.connect() as c:latest=c.execute('SELECT state FROM automatic_tasks WHERE task_id=?',(task['id'],)).fetchone()
                if latest and latest['state']=='PAUSED':continue
                with self.store.connect() as c:
                    if intake_blocked(c,user):continue
                self.block(row,str(exc));continue
            current=self.tasks.get(user,task['id']);assert current['job_id']
            self.update(row,'SUBMITTED',job_id=current['job_id'])
            from .progress import notify_input
            prefix='【模拟模式，未执行台架】\n' if self.cfg.get('mode')=='simulate' or not self.cfg.get('live_enabled') else ''
            notify_input(self.store,user,row['session'],'auto-submitted:'+task['id'],
                         prefix+'材料检查通过，已自动进入执行队列。\n任务：'+current['job_id'])

    def block(self,row,message):
        if not self.update(row,'BLOCKED',error=str(message)[:1200]):return
        if self.cfg.get('require_material_confirmation'):
            from .confirmations import invalidate
            with self.store.connect() as c:invalidate(c,row['owner'],row['task_id'],'preparation or submission failed')
        from .progress import notify_input
        notify_input(self.store,row['owner'],row['session'],'auto-blocked:'+row['task_id']+':'+str(row['revision']),
                     '自动处理已停止：'+str(message)[:600]+'\n已收材料保留。')
