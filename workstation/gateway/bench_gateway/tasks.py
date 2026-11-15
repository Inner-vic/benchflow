"""Owner-scoped task registration from the first material draft onward."""
import json
import re
import time
import uuid
from .core import GatewayError

TASK_ID=re.compile(r'task-\d{8}-[a-f0-9]{10}\Z')


class Tasks:
    def __init__(self,store):
        self.store=store
        with store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS task_requests(
              id TEXT PRIMARY KEY,owner TEXT NOT NULL REFERENCES users(id),session TEXT NOT NULL,
              data TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,job_id TEXT,
              prep_id TEXT,legacy INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS task_focus(owner TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES task_requests(id));
            CREATE TABLE IF NOT EXISTS task_attempts(owner TEXT NOT NULL,task_id TEXT NOT NULL,job_id TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(owner,job_id));
            CREATE INDEX IF NOT EXISTS task_owner_created ON task_requests(owner,created);
            CREATE TABLE IF NOT EXISTS task_lineage(
              task_id TEXT PRIMARY KEY REFERENCES task_requests(id),owner TEXT NOT NULL,
              base_task_id TEXT,base_job_id TEXT,change_kind TEXT NOT NULL,
              force_run INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS input_batches(
              token TEXT PRIMARY KEY,owner TEXT NOT NULL,state TEXT NOT NULL,
              created REAL NOT NULL,updated REAL NOT NULL,claim TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS input_batch_owner_state ON input_batches(owner,state);
            CREATE TABLE IF NOT EXISTS task_explicit_fields(task_id TEXT NOT NULL,owner TEXT NOT NULL,field TEXT NOT NULL,
              PRIMARY KEY(task_id,field));
            ''')
            from .task_refs import schema
            schema(c)
            from .task_history import schema as history_schema
            history_schema(c)

    def current(self,user):
        self.store.require_user(user)
        with self.store.connect() as c:
            row=c.execute('SELECT t.* FROM task_focus f JOIN task_requests t ON t.id=f.task_id WHERE f.owner=? AND t.owner=?',(user,user)).fetchone()
        return self.store.unpack(row) if row else None

    def get(self,user,tid):
        self.store.require_user(user)
        with self.store.connect() as c:
            from .task_refs import PUBLIC,resolve
            if PUBLIC.fullmatch(tid):tid=resolve(c,user,tid)
            row=c.execute('SELECT * FROM task_requests WHERE owner=? AND id=?',(user,tid)).fetchone()
        if not row:raise GatewayError('本人没有这个任务，请发送“我的任务”查看。')
        return self.store.unpack(row)

    def sync(self,c,user,data,new=False,session=''):
        row=c.execute('SELECT t.* FROM task_focus f JOIN task_requests t ON t.id=f.task_id WHERE f.owner=? AND t.owner=?',(user,user)).fetchone()
        now=time.time();encoded=json.dumps(data,ensure_ascii=False)
        if row and not new:
            if row['job_id']:
                raise GatewayError('当前任务已提交，材料已冻结；新材料需自动归入下一次任务。')
            tid=row['id']
            from .confirmations import invalidate
            invalidate(c,user,tid,'material revision updated')
            c.execute("UPDATE task_requests SET data=?,revision=revision+1,state='DRAFT',updated=? WHERE owner=? AND id=?",(encoded,now,user,tid))
        else:
            tid='task-'+time.strftime('%Y%m%d-')+uuid.uuid4().hex[:10]
            legacy=int(not new and bool(c.execute('SELECT 1 FROM material_drafts WHERE owner=?',(user,)).fetchone()))
            c.execute('INSERT INTO task_requests VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (tid,user,session,encoded,1,'DRAFT',None,None,legacy,now,now))
            c.execute('INSERT INTO task_focus VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET task_id=excluded.task_id',(user,tid))
        from .task_refs import allocate
        created=row['created'] if row and not new else now
        allocate(c,user,tid,created)
        return tid

    def ensure(self,user,session=''):
        current=self.current(user)
        if current:return current
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT data FROM material_drafts WHERE owner=?',(user,)).fetchone()
            from .incremental import DEFAULTS
            data=json.loads(row['data']) if row else dict(DEFAULTS,dataset=getattr(self.store,'default_dataset_path',DEFAULTS['dataset']))
            self.sync(c,user,data,session=session)
            c.execute('INSERT OR IGNORE INTO material_drafts VALUES(?,?,?)',(user,json.dumps(data,ensure_ascii=False),time.time()))
        return self.current(user)

    def select(self,user,tid):
        task=self.get(user,tid)
        tid=task['id']
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT INTO task_focus VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET task_id=excluded.task_id',(user,tid))
            c.execute('INSERT INTO material_drafts VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET data=excluded.data,updated=excluded.updated',
                      (user,json.dumps(task['data'],ensure_ascii=False),time.time()))
        return task

    def update(self,user,tid,values):
        from .intake import validate,CLEAR_YAML
        values=validate(values)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM task_requests WHERE owner=? AND id=?',(user,tid)).fetchone()
            if not row:raise GatewayError('本人没有这个任务')
            if row['job_id']:
                raise GatewayError('这份材料所属任务已提交，原材料保持冻结；请重新发送附件，系统会准备下一次任务。')
            data=json.loads(row['data']);data.update({k:v for k,v in values.items() if v})
            original=json.loads(row['data'])
            if original.get('assembly_ref') and any(values.get(k) and values[k]!=original.get(k) for k in ('package','camera_so','lidar_so','m100_so')):
                raise GatewayError('已验证装配的依赖保持冻结，更换 SO 请建立新材料任务。')
            release=bool(values.get('package') and values['package']!=original.get('package'))
            if release:
                data.pop('m100_so',None);data['m100_mode']='build_if_missing'
                if not values.get('yaml_request'):data.pop('yaml_request',None)
                if not values.get('build_reference'):data.pop('build_reference',None)
                if values.get('m100_so') and values['m100_so']!=original.get('m100_so'):data['m100_so']=values['m100_so']
            if values.get('yaml_request')==CLEAR_YAML:data.pop('yaml_request',None)
            if values.get('m100_so') and (not release or values['m100_so']!=original.get('m100_so')):data['m100_mode']='provided'
            if (any(values.get(k) and values[k]!=original.get(k) for k in ('camera_so','lidar_so'))
                    and original.get('m100_mode')=='build_if_missing' and not values.get('m100_so')):
                data.pop('m100_so',None);data['m100_mode']='build_if_missing'
            c.executemany('INSERT OR IGNORE INTO task_explicit_fields VALUES(?,?,?)',[(tid,user,k) for k,v in values.items() if v])
            encoded=json.dumps(data,ensure_ascii=False);now=time.time()
            from .confirmations import invalidate
            invalidate(c,user,tid,'material revision updated')
            c.execute("UPDATE task_requests SET data=?,revision=revision+1,state='DRAFT',updated=? WHERE owner=? AND id=?",(encoded,now,user,tid))
            focus=c.execute('SELECT task_id FROM task_focus WHERE owner=?',(user,)).fetchone()
            if focus and focus['task_id']==tid:
                c.execute('UPDATE material_drafts SET data=?,updated=? WHERE owner=?',(encoded,now,user))
        return self.get(user,tid)

    def link(self,user,job):
        task=self.current(user)
        if not task:return
        with self.store.connect() as c:
            c.execute('INSERT OR IGNORE INTO task_attempts VALUES(?,?,?,?)',(user,task['id'],job['id'],time.time()))
            c.execute('UPDATE task_requests SET job_id=?,state=?,updated=? WHERE owner=? AND id=? AND (job_id IS NULL OR job_id=?)',
                      (job['id'],job['state'],time.time(),user,task['id'],job['id']))

    def text(self,user):
        from .task_history import text
        return text(self.store,user)

    def command(self,user,text):
        value=text.strip()
        if value in ('我的任务','查看我的任务'):return self.text(user)
        if value in ('历史任务','查看历史任务'):
            from .task_history import text as history_text
            return history_text(self.store,user,history=True)
        if value in ('任务目录','查看任务目录'):
            from .task_history import directories
            return directories(self.store,user)
        if value=='恢复历史任务展示':
            from .task_history import restore_history
            count=restore_history(self.store,user)
            return '已恢复本人历史记录展示（'+str(count)+'项）；未提交或重跑任务。\n'+self.text(user)
        identifier=r'(task-\d{8}-[a-f0-9]{10}|TJ-\d{8}-\d{4,})'
        match=re.fullmatch(r'(?:切换任务|继续补充任务)\s+'+identifier,value,re.I)
        if match:
            task=self.select(user,match.group(1))
            from .task_refs import reference
            return '当前任务已切换为 '+reference(self.store,user,task_id=task['id'])+'。\n'+self.text(user)
        match=re.fullmatch(r'(?:查看任务|任务状态)\s+'+identifier,value,re.I)
        if match:
            from .task_refs import reference
            task=self.get(user,match[1]);prefix='任务：'+reference(self.store,user,task_id=task['id'])+'\n'
            if task['job_id']:
                from .core import format_result
                job=self.store.job(user,task['job_id'])
                return prefix+format_result(job,job['state'],job.get('result') or {})
            from .business import render_review
            return prefix+render_review(task['data'])
        return None
