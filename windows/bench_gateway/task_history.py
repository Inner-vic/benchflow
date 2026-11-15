"""Owner-only task views and reversible history visibility; never delete runs."""
import html
import json
import re
import time
from .core import GatewayError

TERMINAL={'SUCCEEDED','PARTIAL_SUCCESS','FAILED','CANCELLED','SIMULATED'}
NAMES={'DRAFT':'待补充材料','CHECKING':'检查材料中','READY':'待确认执行','NEEDS_INPUT':'待补充或修正材料',
       'QUEUED':'就绪排队','RUNNING':'执行中','POSTPROCESSING':'回放完成，评测中','NEEDS_REVIEW':'执行或恢复待核验',
       'SUCCEEDED':'已完成','PARTIAL_SUCCESS':'部分完成','FAILED':'失败，待处理','CANCELLED':'已取消',
       'SIMULATED':'模拟完成','NO_CHANGE':'无有效变更，未重复执行'}

def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS task_history_archives(
      owner TEXT NOT NULL REFERENCES users(id),kind TEXT NOT NULL CHECK(kind IN ('task','job')),
      object_id TEXT NOT NULL,archived_at REAL NOT NULL,reason TEXT NOT NULL,
      PRIMARY KEY(owner,kind,object_id))''')

def task_name(data,job=None,software=None):
    from .material_summary import label
    summary=((job or {}).get('payload') or {}).get('business_contract') or {}
    software=(summary.get('input_summary') or {}).get('software') or software or data.get('software_name') or summary.get('software_name') or label(data.get('package'))
    text=str(software)
    version=re.search(r'(SW\d+).*?-(\d+)(?:\.app(?:\.tar\.gz)?|$)',text,re.I)
    if version:text=version[1].upper()+' · 构建 '+version[2]
    goal='仅回放' if data.get('goal')=='replay' else '回放后评测'
    if job and job['payload'].get('action')=='replay':goal='回放后评测' if job.get('_evaluate_after',job['payload'].get('evaluate_after',False)) else '仅回放'
    if job and job['payload'].get('action')=='evaluate':goal='结果评测'
    text=html.escape(text.replace('\n',' ').replace('\r',' ')[:80])
    for char in '*~[]()#_`':text=text.replace(char,'&#'+str(ord(char))+';')
    return text+' · '+goal

def _rows(store,user):
    store.require_user(user)
    with store.connect() as c:
        schema(c)
        rows=[dict(r) for r in c.execute('''SELECT t.*,COALESCE(j.state,t.state) actual_state,
          a.archived_at FROM task_requests t LEFT JOIN jobs j ON j.id=t.job_id AND j.owner=t.owner
          LEFT JOIN task_history_archives a ON a.owner=t.owner AND a.kind='task' AND a.object_id=t.id
          WHERE t.owner=? ORDER BY t.created DESC,t.id DESC''',(user,))]
        focus=c.execute('SELECT task_id FROM task_focus WHERE owner=?',(user,)).fetchone()
    for row in rows:
        row['data']=json.loads(row['data']);row['current']=bool(focus and focus['task_id']==row['id'])
        if not row['job_id'] and row['prep_id'] and row['actual_state']!='NO_CHANGE':
            with store.connect() as c:p=c.execute('SELECT * FROM preparations WHERE id=? AND owner=?',(row['prep_id'],user)).fetchone()
            if p:
                from .preparation import fingerprint
                if p['fingerprint']==fingerprint(row['data']):row['actual_state']={'QUEUED':'CHECKING','RUNNING':'CHECKING','STATIC_PASSED':'READY','NEEDS_INPUT':'NEEDS_INPUT'}.get(p['state'],'DRAFT')
    return rows

def job_reference(store,user,jid):
    from .task_refs import reference
    ref=reference(store,user,job_id=jid)
    if ref:return ref
    job=store.job(user,jid)
    source=job['payload'].get('source_job')
    if source:
        # reference performs the source-owner check; never resolve other owners.
        return reference(store,user,job_id=source)
    return ''

def text(store,user,history=False):
    rows=_rows(store,user)
    with store.connect() as c:
        jobs={r['id']:store.unpack(r) for r in c.execute('SELECT * FROM jobs WHERE owner=?',(user,))}
        packages={}
        for asset in c.execute("SELECT * FROM assets WHERE owner=? AND kind='package' ORDER BY version DESC",(user,)):
            name=json.loads(asset['data']).get('filename') or asset['name']
            packages[asset['id']]=name;packages.setdefault(asset['name'],name)
        from .execution_controls import desired
        for job in jobs.values():
            if job['payload'].get('action')=='replay':job['_evaluate_after']=desired(c,job)
        hidden={r['object_id'] for r in c.execute("SELECT object_id FROM task_history_archives WHERE owner=? AND kind='job'",(user,))}
    visible=rows if history else [r for r in rows if not r['archived_at'] or r['current'] or (r['job_id'] and r['actual_state'] not in TERMINAL)]
    def line(row):
        from .task_refs import reference
        ref=reference(store,user,task_id=row['id'])
        return ('【当前】' if row['current'] else '')+ref+' · '+task_name(row['data'],jobs.get(row['job_id']),packages.get(row['data'].get('package')))+' · '+NAMES.get(row['actual_state'],row['actual_state'])+('（已归档）' if row['archived_at'] else '')
    lines=['本人历史任务：' if history else '本人任务：']
    if history:
        lines.extend(line(r) for r in visible[:30])
        if len(visible)>30:lines.append('显示最近30份历史材料；可按TJ编号查看指定任务。')
    else:
        current=[r for r in visible if r['current']]
        active=[r for r in visible if r['job_id'] and r['actual_state'] not in TERMINAL and not r['current']]
        active.sort(key=lambda r:({'RUNNING':0,'NEEDS_REVIEW':1,'POSTPROCESSING':2,'QUEUED':3}.get(r['actual_state'],4),r['created']))
        used={r['id'] for r in current+active}
        recent=[r for r in visible if r['id'] not in used][:3]
        for heading,items in [('当前材料' if current and not current[0]['job_id'] else '当前任务',current),('执行与排队',active),('最近记录',recent)]:
            if items:lines.append(heading+'：');lines.extend(line(r) for r in items)
    linked={r['job_id'] for r in rows if r['job_id']}
    legacy=[j for j in sorted(jobs.values(),key=lambda j:j['created'],reverse=True) if j['id'] not in linked and (history or j['id'] not in hidden or j['state'] not in TERMINAL)]
    # Active old jobs must remain visible even without a material task; terminal
    # historical runs are only shown when explicitly requesting history.
    legacy=legacy[:10] if history else [j for j in legacy if j['state'] not in TERMINAL]
    if legacy:
        lines.append('独立执行记录：')
        for job in legacy:
            ref=job_reference(store,user,job['id']) or job['id']
            lines.append(ref+' · '+('结果评测' if job['lane']=='postprocess' else '数据回放')+' · '+NAMES.get(job['state'],job['state'])+('（已归档）' if job['id'] in hidden else ''))
    if len(lines)==1:lines.append('暂无任务。可发送材料或打开当前配置。')
    if not history:lines.append('历史记录可发送“历史任务”查看；材料和结果目录保留。')
    return '\n'.join(lines)

def archive_history(store,user,reason='user requested test history cleanup'):
    """Archive only inactive records, retaining current draft, assets and receipts."""
    store.require_user(user)
    with store.connect() as c:
        schema(c);c.execute('BEGIN IMMEDIATE')
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'input_batches' in tables and c.execute("SELECT 1 FROM input_batches WHERE owner=? AND state='OPEN'",(user,)).fetchone():
            raise GatewayError('本人材料仍在接收，暂不归档历史记录。')
        focus=c.execute('SELECT task_id FROM task_focus WHERE owner=?',(user,)).fetchone()
        current=focus['task_id'] if focus else None
        archived=[];now=time.time()
        def safe_job(jid):
            job=c.execute('SELECT state FROM jobs WHERE id=? AND owner=?',(jid,user)).fetchone()
            if not job or job['state'] not in TERMINAL:return False
            if c.execute('SELECT 1 FROM outbox WHERE job=? AND sent=0',(jid,)).fetchone():return False
            if c.execute("SELECT 1 FROM jobs WHERE owner=? AND json_extract(payload,'$.source_job')=? AND state IN ('QUEUED','RUNNING','NEEDS_REVIEW','POSTPROCESSING')",(user,jid)).fetchone():return False
            return True
        for task in c.execute('SELECT * FROM task_requests WHERE owner=?',(user,)).fetchall():
            if task['id']==current:continue
            if task['job_id'] and not safe_job(task['job_id']):continue
            if task['prep_id']:
                prep=c.execute('SELECT state,result FROM preparations WHERE id=? AND owner=?',(task['prep_id'],user)).fetchone()
                if prep and (prep['state'] in ('QUEUED','RUNNING') or (json.loads(prep['result'] or '{}').get('adapter_binding') or {}).get('state')=='BINDING'):continue
            if 'material_confirmations' in tables and c.execute("SELECT 1 FROM material_confirmations WHERE task_id=? AND owner=? AND state='ACTIVE'",(task['id'],user)).fetchone():continue
            if 'automatic_tasks' in tables and c.execute("SELECT 1 FROM automatic_tasks WHERE task_id=? AND owner=? AND state IN ('WAITING','CHECKING')",(task['id'],user)).fetchone():continue
            if c.execute('INSERT OR IGNORE INTO task_history_archives VALUES(?,?,?,?,?)',(user,'task',task['id'],now,reason)).rowcount:archived.append(task['id'])
        protected=c.execute('SELECT job_id FROM task_requests WHERE owner=? AND id=?',(user,current)).fetchone() if current else None
        for row in c.execute('SELECT id FROM jobs WHERE owner=?',(user,)).fetchall():
            if protected and row['id']==protected['job_id'] or not safe_job(row['id']):continue
            if c.execute('INSERT OR IGNORE INTO task_history_archives VALUES(?,?,?,?,?)',(user,'job',row['id'],now,reason)).rowcount:archived.append(row['id'])
    return {'archived_records':len(archived),'record_ids':archived,'physical_files_deleted':0}

def restore_history(store,user):
    store.require_user(user)
    with store.connect() as c:
        schema(c)
        count=c.execute('DELETE FROM task_history_archives WHERE owner=?',(user,)).rowcount
    return count

def directories(store,user):
    """Resolve live DB mappings without renaming frozen workstation run paths."""
    store.require_user(user)
    with store.connect() as c:jobs=[store.unpack(r) for r in c.execute('SELECT * FROM jobs WHERE owner=? ORDER BY created DESC',(user,))]
    lines=['本人任务结果目录：'];paths={}
    for job in [j for j in jobs if (j.get('result') or {}).get('task')]:
        result=job.get('result') or {};path=result.get('task')
        if not path:continue
        ref=job_reference(store,user,job['id']) or job['id']
        key=(ref,str(path));kind='独立评测' if job['payload'].get('action')=='evaluate' else '回放任务'
        paths.setdefault(key,[])
        if kind not in paths[key]:paths[key].append(kind)
    for (ref,path),kinds in list(paths.items())[:10]:
        lines.append(ref+' · '+' / '.join(kinds)+'\n'+path)
    if len(lines)==1:lines.append('暂无已登记的结果目录；回放完成后自动记录。')
    return '\n'.join(lines)
