"""Owner-scoped changes to future evaluation, separate from frozen material."""
import json
import time
from .core import GatewayError

def schema(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS execution_controls(
      job TEXT PRIMARY KEY REFERENCES jobs(id),owner TEXT NOT NULL,evaluate_after INTEGER NOT NULL,
      version INTEGER NOT NULL,updated REAL NOT NULL,session TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS execution_control_events(
      id INTEGER PRIMARY KEY,job TEXT NOT NULL REFERENCES jobs(id),owner TEXT NOT NULL,
      evaluate_after INTEGER NOT NULL,outcome TEXT NOT NULL,created REAL NOT NULL,session TEXT NOT NULL);''')

def desired(c,job):
    row=c.execute('SELECT evaluate_after FROM execution_controls WHERE job=? AND owner=?',(job['id'],job['owner'])).fetchone()
    return bool(row['evaluate_after']) if row else bool(job['payload'].get('evaluate_after'))

def set_goal(cfg,store,user,session,goal,target=''):
    from .tasks import Tasks
    from .task_refs import PUBLIC,reference
    tasks=Tasks(store);focus=tasks.current(user);enable=goal=='replay_and_evaluate'
    if goal not in ('replay','replay_and_evaluate'):raise GatewayError('执行目标只支持仅回放或回放后评测。')
    if target:
        if PUBLIC.fullmatch(target) or target.startswith('task-'):
            selected=tasks.get(user,target)
            if not selected['job_id']:
                if not focus or selected['id']!=focus['id']:
                    return {'state':'SELECT_DRAFT','text':'请先发送“切换任务 '+reference(store,user,task_id=selected['id'])+'”，再修改这份材料的执行目标。'}
                return {'state':'DRAFT'}
            job=store.job(user,selected['job_id'])
        else:job=store.job(user,target)
    else:
        active=[j for j in store.jobs(user) if j['payload'].get('action')=='replay' and j['state'] in ('QUEUED','RUNNING','POSTPROCESSING')]
        if len(active)>1 or active and focus and not focus['job_id']:
            return {'state':'AMBIGUOUS','text':'存在运行中任务和其他材料或任务，请带任务编号指定，例如“本次仅回放 TJ-日期-编号”；下一份材料使用“下次仅回放”。'}
        if focus and not focus['job_id']:return {'state':'DRAFT'}
        job=active[0] if active else store.job(user,focus['job_id']) if focus and focus['job_id'] else None
        if job is None:return {'state':'DRAFT'}
    if job['payload'].get('action')!='replay':return {'state':'UNSUPPORTED','text':'请指定回放任务编号；独立评测已运行后不能通过此选项中断。'}
    ref=reference(store,user,job_id=job['id']) or '本次任务'
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        job=store.unpack(c.execute('SELECT * FROM jobs WHERE id=? AND owner=?',(job['id'],user)).fetchone())
        result=job.get('result') or {};child=c.execute('SELECT * FROM jobs WHERE id=? AND owner=?',(result.get('evaluation_job',''),user)).fetchone()
        state=job['state'];current=desired(c,job)
        if state=='NEEDS_REVIEW' or result.get('evaluation_state')=='NEEDS_REVIEW':
            return {'state':'NEEDS_REVIEW','job':job['id'],'text':ref+' 的执行结果仍待核验，本指令未绕过核验或重新执行；请管理员先核对原任务收据。'}
        if state=='POSTPROCESSING' and not child:
            return {'state':'NEEDS_REVIEW','job':job['id'],'text':ref+'：评测交接记录待核验，目标未修改，请管理员核对；不会新建或重跑任务。'}
        if state not in ('QUEUED','RUNNING','POSTPROCESSING'):
            message=('回放已完成；需要追加评测可发送“评测任务 '+ref+'”，使用已有结果，不重新回放。' if enable and result.get('task') and not result.get('report') else
                     '任务已完成或停止，本指令不会新建任务或重新执行。下一次任务可注明“下次仅回放”或“下次回放后评测”。')
            return {'state':'CLOSED','job':job['id'],'text':ref+'：'+message}
        if child and child['state']!='QUEUED':
            c.execute('INSERT INTO execution_control_events(job,owner,evaluate_after,outcome,created,session) VALUES(?,?,?,?,?,?)',
                      (job['id'],user,int(enable),'TOO_LATE',time.time(),session))
            return {'state':'TOO_LATE','job':job['id'],'text':ref+'：评测已被派发或产生结果，已过修改截止点，本指令未取消或重启评测。回放结果保留。'}
        if current==enable:
            return {'state':'UNCHANGED','job':job['id'],'text':ref+' 当前已为'+('回放后评测。' if enable else '仅回放；后续不派发评测。')}
        if enable:
            from .business import capability_issues
            evaluator=job['payload'].get('baseline',{}).get('data',{}).get('evaluator')
            issues=capability_issues({'goal':'replay_and_evaluate'},evaluator)
            if not cfg.get('workstation_evaluation_enabled') or issues:
                return {'state':'UNAVAILABLE','job':job['id'],'text':ref+' 尚未绑定可用的评测入口，本次目标未修改；请管理员接入后对已有回放结果追加评测。'}
            if evaluator.get('kind')=='workstation-offline-v1':
                from .workstation_evaluation import validate_descriptor
                validate_descriptor(cfg,evaluator)
        now=time.time()
        c.execute('''INSERT INTO execution_controls VALUES(?,?,?,1,?,?) ON CONFLICT(job) DO UPDATE SET
          evaluate_after=excluded.evaluate_after,version=execution_controls.version+1,updated=excluded.updated,session=excluded.session''',
                  (job['id'],user,int(enable),now,session))
        c.execute('INSERT INTO execution_control_events(job,owner,evaluate_after,outcome,created,session) VALUES(?,?,?,?,?,?)',
                  (job['id'],user,int(enable),'APPLIED',now,session))
        if child and not enable:
            store._finish(c,store.unpack(child),'CANCELLED',{'task':result['task'],'control':'skip_evaluation','error':'本人已取消尚未派发的评测'})
        # A PATCH already in flight may acknowledge an older view. Keeping this
        # status timestamp newer makes the next worker tick refresh the card.
        c.execute('UPDATE jobs SET updated=? WHERE id=? AND owner=?',(now,job['id'],user))
        c.execute('UPDATE task_cards SET last_sent=0,next_attempt=0 WHERE job=?',(job['id'],))
    return {'state':'APPLIED','job':job['id'],'updated':now,'text':ref+' 已改为'+('回放后评测。' if enable else '仅回放；当前回放继续，结束后交付目录，不派发后续评测。')}

def present(cfg,store,result):
    """Suppress a direct reply only after this control reached its actual card."""
    if result.get('state')!='APPLIED' or not cfg.get('unified_status_cards'):return False
    from .task_cards import deliver
    deliver(cfg,store,deadline=time.monotonic()+6,job_id=result['job'])
    with store.connect() as c:card=c.execute('SELECT * FROM task_cards WHERE job=?',(result['job'],)).fetchone()
    return bool(card and card['message_id'] and not card['attempts'] and card['last_sent']>=result['updated'])
