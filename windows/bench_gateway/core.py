from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path, PurePosixPath


class GatewayError(ValueError):
    pass


class EvaluationOutcomeUnknown(GatewayError):
    """An adapter was invoked, but no trustworthy completion receipt was obtained."""


class PreDispatchRejected(GatewayError):
    """A verified remote receipt proves that this task never touched hardware."""
    def __init__(self,message,rejection):
        self.rejection=dict(rejection)
        super().__init__('本次输入复核未通过，尚未刷包或回放。'+message+'；请重新检查材料后再提交。')


class PrecheckFailed(GatewayError):
    def __init__(self,diagnostics):
        self.diagnostics=diagnostics
        super().__init__('小场景预检未通过，全量回放未执行。'+diagnostics.get('summary','请检查软件包、依赖和预检日志。'))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def owner_key(user):
    if not re.fullmatch(r"ou_[A-Za-z0-9_-]{3,100}", user):
        raise GatewayError("需要真实的飞书 open_id")
    return "u_" + hashlib.sha256(user.encode()).hexdigest()[:24]


def name_check(name):
    if not isinstance(name, str) or not name or len(name) > 80 or any(c in name for c in "/\\\x00\r\n"):
        raise GatewayError("资产名称不能为空，且不能包含路径分隔符")
    return name


def within(root, path):
    root = Path(root)
    if root.is_symlink() or getattr(root, 'is_junction', lambda: False)():
        raise GatewayError("用户根目录不能是符号链接或目录联接")
    root, path = root.resolve(), Path(path).resolve()
    if path == root or root not in path.parents:
        raise GatewayError("路径超出本人资产目录")
    return path


def remote_within(root, path):
    root, path = PurePosixPath(root), PurePosixPath(path)
    if not path.is_absolute() or ".." in path.parts or path == root or root not in path.parents:
        raise GatewayError("远程路径必须位于本人资产目录下")
    return str(path)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "gateway.sqlite3"
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS assets(
              id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL,
              name TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, created REAL NOT NULL,
              UNIQUE(owner,kind,name,version));
            CREATE TABLE IF NOT EXISTS baselines(
              id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
              data TEXT NOT NULL, created REAL NOT NULL, UNIQUE(owner,name));
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users(id), session TEXT NOT NULL,
              request_key TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
              result TEXT, created REAL NOT NULL, updated REAL NOT NULL,
              UNIQUE(owner,request_key));
            CREATE TABLE IF NOT EXISTS outbox(
              id INTEGER PRIMARY KEY, job TEXT NOT NULL REFERENCES jobs(id), session TEXT NOT NULL,
              text TEXT NOT NULL, attachment TEXT, sent INTEGER NOT NULL DEFAULT 0,
              attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(job));
            CREATE TABLE IF NOT EXISTS material_drafts(owner TEXT PRIMARY KEY REFERENCES users(id),data TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS resource_status(id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS queue_outbox(
              job TEXT PRIMARY KEY REFERENCES jobs(id), session TEXT NOT NULL, text TEXT NOT NULL,
              version INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS messages(
              id INTEGER PRIMARY KEY, owner TEXT NOT NULL, session TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS archive_jobs(
              job TEXT PRIMARY KEY REFERENCES jobs(id),owner TEXT NOT NULL,state TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,error_code TEXT);
            CREATE TABLE IF NOT EXISTS task_progress(
              job TEXT PRIMARY KEY REFERENCES jobs(id),rank INTEGER NOT NULL,label TEXT NOT NULL,
              started REAL NOT NULL,observed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS task_cards(
              job TEXT PRIMARY KEY REFERENCES jobs(id),message_id TEXT,sent_digest TEXT NOT NULL DEFAULT '',
              last_sent REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
              lease_token TEXT,lease_until REAL NOT NULL DEFAULT 0,
              fallback_at REAL NOT NULL DEFAULT 0,fallback_digest TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS execution_inputs(
              job TEXT PRIMARY KEY REFERENCES jobs(id),owner TEXT NOT NULL,
              byte_digest TEXT NOT NULL,semantic_digest TEXT,created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS execution_owner_digest ON execution_inputs(owner,byte_digest);
            ''')
            from .execution_controls import schema as control_schema
            control_schema(c)
            # Existing deployments keep queued notifications across schema upgrades.
            c.execute('BEGIN IMMEDIATE')
            for table, columns in {
                'outbox': {'version': 'INTEGER NOT NULL DEFAULT 1',
                           'text_sent': 'INTEGER NOT NULL DEFAULT 0',
                           'next_attempt_at': 'REAL NOT NULL DEFAULT 0'},
                'queue_outbox': {'next_attempt_at': 'REAL NOT NULL DEFAULT 0'},
                'jobs': {'lane': "TEXT NOT NULL DEFAULT 'hardware'"},
                'messages': {'task_ref': 'TEXT'},
            }.items():
                present={row['name'] for row in c.execute(f'PRAGMA table_info({table})')}
                for name, declaration in columns.items():
                    if name not in present:c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
            c.execute("UPDATE jobs SET lane='postprocess' WHERE json_extract(payload,'$.action')='evaluate' AND lane!='postprocess'")
            c.execute("DELETE FROM queue_outbox WHERE job IN (SELECT id FROM jobs WHERE lane='postprocess')")
            c.execute('CREATE INDEX IF NOT EXISTS jobs_lane_state ON jobs(lane,state,created,id)')

    @contextlib.contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    def allow(self, user, label=""):
        owner_key(user)
        with self.connect() as c:
            c.execute("INSERT INTO users VALUES(?,?,1) ON CONFLICT(id) DO UPDATE SET label=excluded.label,enabled=1", (user, label or user))
        for part in ("packages", "dependencies", "datasets", "inbox", "reports"):
            (self.user_root(user) / part).mkdir(parents=True, exist_ok=True)

    def require_user(self, user):
        owner_key(user)
        with self.connect() as c:
            row = c.execute("SELECT * FROM users WHERE id=? AND enabled=1", (user,)).fetchone()
        if row is None:
            raise GatewayError(f"尚未开通使用权限。你的飞书用户 ID：{user}；请管理员在本机开通。")
        return dict(row)

    def provision(self,user):
        """Called only for identities supplied by the authenticated Feishu transport."""
        owner_key(user)
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO users(id,label,enabled) VALUES(?,?,1)',(user,user))
        self.require_user(user)
        for part in ('packages','dependencies','datasets','inbox','reports'):
            (self.user_root(user)/part).mkdir(parents=True,exist_ok=True)

    def user_root(self, user):
        return self.root / "users" / owner_key(user)

    def remote_root(self, user, base="/mnt/data/bench-gateway/users"):
        return str(PurePosixPath(base) / owner_key(user))

    def add_asset(self, user, kind, name, data):
        self.require_user(user)
        if kind not in ("package", "dependency", "dataset"):
            raise GatewayError("仅登记软件包、依赖文件和数据集；不建立配置资产库")
        name_check(name)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT * FROM assets WHERE owner=? AND kind=? AND name=? ORDER BY version DESC LIMIT 1", (user, kind, name)).fetchone()
            if old and json.loads(old["data"]) == data:
                return self.unpack(old)
            ver = old["version"] + 1 if old else 1
            aid = "a-" + uuid.uuid4().hex[:20]
            c.execute("INSERT INTO assets VALUES(?,?,?,?,?,?,?)", (aid, user, kind, name, ver, json.dumps(data, ensure_ascii=False), time.time()))
            return self.unpack(c.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone())

    @staticmethod
    def unpack(row):
        item = dict(row)
        for field in ("data", "payload", "result"):
            if field in item and item[field] is not None:
                item[field] = json.loads(item[field])
        return item

    def asset(self, user, kind, name):
        self.require_user(user)
        with self.connect() as c:
            row = c.execute("SELECT * FROM assets WHERE owner=? AND kind=? AND name=? ORDER BY version DESC LIMIT 1", (user, kind, name)).fetchone()
        if not row:
            raise GatewayError(f"本人目录未登记 {kind}：{name}。不会查找其他用户目录。")
        return self.unpack(row)

    def list_assets(self, user):
        self.require_user(user)
        with self.connect() as c:
            return [self.unpack(r) for r in c.execute("SELECT * FROM assets WHERE owner=? ORDER BY kind,name,version", (user,))]

    def import_file(self, user, kind, name, source, target=None):
        self.require_user(user)
        if kind not in ("package", "dependency"):
            raise GatewayError("数据集请通过本人工作站目录登记")
        source = Path(source).resolve(strict=True)
        users_root=(self.root/'users').resolve()
        if users_root in source.parents and self.user_root(user).resolve() not in source.parents:
            raise GatewayError('不能从其他用户目录导入文件')
        if not source.is_file():
            raise GatewayError("需要普通文件")
        h = digest(source)
        part = "packages" if kind == "package" else "dependencies"
        dest = self.user_root(user) / part / h / source.name
        within(self.user_root(user), dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            tmp = dest.with_name(dest.name + ".partial-" + uuid.uuid4().hex[:8])
            try:
                shutil.copyfile(source, tmp)
                if digest(tmp) != h:
                    raise GatewayError("复制后文件身份不一致")
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
        elif digest(dest) != h:
            raise GatewayError("已存资产发生变化")
        data={"local_path": str(dest), "sha256": h, "size": dest.stat().st_size}
        if target is not None:data['target']=target
        return self.add_asset(user, kind, name, data)

    def baseline(self, user, name):
        self.require_user(user)
        with self.connect() as c:
            row = c.execute("SELECT * FROM baselines WHERE owner=? AND name=?", (user, name)).fetchone()
        if not row:
            raise GatewayError("该组合尚未绑定回放适配器，请管理员核对适配记录")
        return self.unpack(row)

    def add_baseline(self, user, name, data):
        self.require_user(user)
        name_check(name)
        bid = "b-" + uuid.uuid4().hex[:20]
        with self.connect() as c:
            try:
                c.execute("INSERT INTO baselines VALUES(?,?,?,?,?)", (bid, user, name, json.dumps(data, ensure_ascii=False), time.time()))
            except sqlite3.IntegrityError as exc:
                raise GatewayError("基线名称已存在；更新请登记新名称") from exc
        return self.baseline(user, name)

    def submit(self, user, session, request_key, payload, *, expected_task=None):
        self.require_user(user)
        now = time.time()
        jid = "gw-" + time.strftime("%Y%m%d-") + uuid.uuid4().hex[:14]
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT * FROM jobs WHERE owner=? AND request_key=?", (user, request_key)).fetchone()
            if old:
                return self.unpack(old), True
            from .confirmations import required_in,active as active_confirmation
            confirmation_required=payload.get('action')=='replay' and required_in(c)
            confirmation=None
            if confirmation_required and not expected_task:raise GatewayError('本次材料需要在卡片点击“确认执行”，本次未入队。')
            if expected_task:
                from .preparation import fingerprint
                from .incremental import intake_blocked
                if intake_blocked(c,user):raise GatewayError('材料消息正在处理或曾中断，本次未入队。')
                task=c.execute('SELECT t.* FROM task_requests t JOIN task_focus f ON f.task_id=t.id WHERE f.owner=? AND t.owner=?',(user,user)).fetchone()
                if confirmation_required:
                    confirmation=active_confirmation(c,user,dict(task) if task else {},session)
                    if not confirmation:raise GatewayError('材料尚未确认或确认后已变化，请重新核对卡片并确认；本次未入队。')
                auto=c.execute('SELECT * FROM automatic_tasks WHERE task_id=? AND owner=?',(expected_task['task_id'],user)).fetchone()
                prep=c.execute('SELECT * FROM preparations WHERE id=? AND owner=?',(expected_task['preparation_id'],user)).fetchone()
                if (not task or task['id']!=expected_task['task_id'] or task['revision']!=expected_task['revision'] or task['job_id']
                        or fingerprint(json.loads(task['data']))!=expected_task['fingerprint'] or task['prep_id']!=expected_task['preparation_id']
                        or not auto or auto['state']!='CHECKING' or auto['revision']!=task['revision'] or auto['session']!=session
                        or auto['fingerprint']!=expected_task['fingerprint'] or auto['not_before']>now
                        or not prep or prep['fingerprint']!=auto['fingerprint'] or prep['state']!='STATIC_PASSED'
                        or (json.loads(prep['result'] or '{}').get('adapter_binding') or {}).get('state')!='READY_FOR_GATE'):
                    raise GatewayError('材料或自动提交状态已变化，本次未入队。')
                if (c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_attachments'").fetchone()
                        and c.execute("SELECT 1 FROM task_attachments WHERE owner=? AND task_id=? AND state NOT IN ('ASSOCIATED','SUPERSEDED','DUPLICATE') LIMIT 1",(user,task['id'])).fetchone()):
                    raise GatewayError('附件尚未完成保存或关联，本次未入队。')
                execution=expected_task.get('execution_identity')
                if execution:
                    from .execution_identity import EquivalentExecution
                    lineage=c.execute('SELECT * FROM task_lineage WHERE task_id=? AND owner=?',(task['id'],user)).fetchone()
                    base_job=lineage['base_job_id'] if lineage else None;force=bool(lineage and lineage['force_run'])
                    equivalent=c.execute('''SELECT j.id,j.state FROM execution_inputs e JOIN jobs j ON j.id=e.job AND j.owner=e.owner
                      WHERE e.owner=? AND (e.byte_digest=? OR (? IS NOT NULL AND e.semantic_digest=?))
                      AND (j.state IN ('QUEUED','RUNNING','NEEDS_REVIEW','POSTPROCESSING') OR
                           (j.id=? AND j.state IN ('SUCCEEDED','PARTIAL_SUCCESS','FAILED')))
                      ORDER BY CASE WHEN j.state='NEEDS_REVIEW' THEN 0 ELSE 1 END,j.created DESC LIMIT 1''',
                      (user,execution['byte_digest'],execution['semantic_digest'],execution['semantic_digest'],base_job)).fetchone()
                    if equivalent and (not force or equivalent['state']=='NEEDS_REVIEW'):
                        raise EquivalentExecution(equivalent['id'],equivalent['state'])
            if payload['action']=='evaluate':
                old=c.execute("SELECT * FROM jobs WHERE owner=? AND lane='postprocess' AND json_extract(payload,'$.source_job')=? AND state IN ('QUEUED','RUNNING','NEEDS_REVIEW') ORDER BY created LIMIT 1",(user,payload['source_job'])).fetchone()
                if old:return self.unpack(old),True
            lane='postprocess' if payload['action']=='evaluate' else 'hardware'
            c.execute("INSERT INTO jobs(id,owner,session,request_key,state,payload,result,created,updated,lane) VALUES(?,?,?,?,?,?,?,?,?,?)", (jid, user, session, request_key, "QUEUED", json.dumps(payload, ensure_ascii=False), None, now, now,lane))
            if payload['action']=='replay':c.execute('INSERT OR IGNORE INTO task_cards(job) VALUES(?)',(jid,))
            if expected_task:
                if confirmation:
                    changed=c.execute("UPDATE material_confirmations SET state='CONSUMED',job_id=? WHERE id=? AND state='ACTIVE'",(jid,confirmation['id'])).rowcount
                    if not changed:raise GatewayError('本次确认已使用或失效，未重复入队。')
                c.execute('INSERT INTO task_attempts VALUES(?,?,?,?)',(user,task['id'],jid,now))
                c.execute("UPDATE task_requests SET job_id=?,state='QUEUED',updated=? WHERE owner=? AND id=?",(jid,now,user,task['id']))
                if expected_task.get('execution_identity'):
                    value=expected_task['execution_identity']
                    c.execute('INSERT INTO execution_inputs VALUES(?,?,?,?,?)',(jid,user,value['byte_digest'],value['semantic_digest'],now))
            return self.unpack(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()), False

    def job(self, user, jid):
        self.require_user(user)
        with self.connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE owner=? AND id=?", (user, jid)).fetchone()
        if not row:
            raise GatewayError("本人没有这个任务")
        return self.unpack(row)

    def jobs(self, user):
        self.require_user(user)
        with self.connect() as c:
            return [self.unpack(r) for r in c.execute("SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 30", (user,))]

    def history(self,user,session):
        self.require_user(user)
        from .tasks import Tasks
        task=Tasks(self).current(user)
        with self.connect() as c:
            if task:
                rows=c.execute('SELECT role,content FROM messages WHERE owner=? AND session=? AND (task_ref=? OR (task_ref IS NULL AND ?=1)) ORDER BY id DESC LIMIT 12',(user,session,task['id'],task['legacy'])).fetchall()
            else:rows=c.execute('SELECT role,content FROM messages WHERE owner=? AND session=? ORDER BY id DESC LIMIT 12',(user,session)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def message(self,user,session,role,content):
        self.require_user(user)
        from .tasks import Tasks
        task=Tasks(self).current(user)
        with self.connect() as c:
            c.execute('INSERT INTO messages(owner,session,role,content,task_ref) VALUES(?,?,?,?,?)',(user,session,role,content[:12000],task['id'] if task else None))

    def cancel(self, user, jid):
        self.job(user, jid)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT * FROM jobs WHERE id=? AND owner=? AND state='QUEUED'",(jid,user)).fetchone()
            if not row:raise GatewayError("只能取消尚未派发的任务；聊天停止不等于台架安全停止")
            self._finish(c,self.unpack(row),'CANCELLED',{'error':'尚未执行的任务已取消'})

    def claim(self,lane='hardware'):
        if lane not in ('hardware','postprocess'):raise GatewayError('未知调度队列')
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # Unknown outcomes also block new hardware mutations until reconciled.
            if c.execute("SELECT 1 FROM jobs WHERE lane=? AND (state='RUNNING' OR (lane='hardware' AND state='NEEDS_REVIEW'))",(lane,)).fetchone():
                return None
            row = c.execute("SELECT * FROM jobs WHERE lane=? AND state='QUEUED' ORDER BY created,id LIMIT 1",(lane,)).fetchone()
            if not row:
                return None
            self.require_user(row["owner"])
            c.execute("UPDATE jobs SET state='RUNNING',updated=? WHERE id=?", (time.time(), row["id"]))
            job=self.unpack(c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            self._update_evaluation_parent(c,job,'RUNNING',{})
            return job

    def complete_replay(self,job,result,recovered=False,expected_snapshot=None):
        """Commit the replay receipt and its evaluation handoff together, before releasing FIFO."""
        if job['payload']['action']!='replay' or not result.get('task') or result.get('simulated'):
            raise GatewayError('需要已完成的真实回放收据才能安排后处理')
        now=time.time();eid=job['id']+'-eval'
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT * FROM jobs WHERE id=? AND owner=?',(job['id'],job['owner'])).fetchone()
            expected='NEEDS_REVIEW' if recovered else 'RUNNING'
            if not current or current['state']!=expected:raise GatewayError('回放任务状态已变化，不能重复交接评测')
            if expected_snapshot is not None and self.unpack(current)!=expected_snapshot:
                raise GatewayError('核验期间任务或收据发生变化，未覆盖当前任务。')
            if recovered and (result.get('state')!='PASS' or result.get('run_id')!=job['id']):raise GatewayError('续交付收据与源回放任务不匹配')
            from .execution_controls import desired
            if not desired(c,job):
                saved={**(job.get('result') or {}),**result};saved.pop('error',None)
                if c.execute('SELECT 1 FROM execution_controls WHERE job=?',(job['id'],)).fetchone():
                    saved.update(evaluation_state='SKIPPED',execution_goal='replay')
                self._finish(c,job,'SUCCEEDED',saved)
                return
            payload={'action':'evaluate','baseline':job['payload']['baseline'],'source_job':job['id'],
                     'source_task':result['task'],'source_run':result.get('run_id',job['id']),
                     'source_assets':job['payload'].get('assets') or {},'automatic':True}
            if job['payload'].get('business_contract'):payload['business_contract']=job['payload']['business_contract']
            if c.execute('SELECT 1 FROM jobs WHERE id=?',(eid,)).fetchone():raise GatewayError('该回放任务已有评测交接记录，请核对原评测任务，不会覆盖或重复提交')
            c.execute("INSERT INTO jobs(id,owner,session,request_key,state,payload,result,created,updated,lane) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (eid,job['owner'],job['session'],'automatic-evaluation:'+job['id'],'QUEUED',json.dumps(payload,ensure_ascii=False),None,now,now,'postprocess'))
            saved={**(job.get('result') or {}),**result,'evaluation_job':eid,'evaluation_state':'QUEUED'}
            saved.pop('error',None)
            self._finish(c,job,'POSTPROCESSING',saved)

    def _update_evaluation_parent(self,c,job,state,result):
        if not job['payload'].get('automatic') or job['payload']['action']!='evaluate':return
        row=c.execute('SELECT * FROM jobs WHERE id=? AND owner=?',(job['payload']['source_job'],job['owner'])).fetchone()
        if not row:return
        parent=self.unpack(row);saved=parent.get('result') or {}
        if saved.get('evaluation_job')!=job['id']:return
        merged={**saved,'evaluation_state':state}
        if state=='RUNNING':
            c.execute('UPDATE jobs SET result=?,updated=? WHERE id=?',(json.dumps(merged,ensure_ascii=False),time.time(),parent['id']))
            return
        if state=='CANCELLED' and result.get('control')=='skip_evaluation':
            merged.update(evaluation_state='SKIPPED',execution_goal='replay');merged.pop('error',None)
            parent_state='SUCCEEDED'
        elif state=='SUCCEEDED':
            merged.update(evaluation=result,report=result['report'],local_report=result['local_report'])
            merged.pop('error',None)
            parent_state='SUCCEEDED'
        elif state=='PARTIAL_SUCCESS' and result.get('report') and result.get('local_report'):
            merged.update(evaluation=result,report=result['report'],local_report=result['local_report'],
                          warnings=result.get('warnings',[]),evaluation_state='PARTIAL')
            merged['error']='回放完成；评测报告包含缺项，已保留原始 TXT，请核对任务有效性与 GT 覆盖。'
            parent_state='PARTIAL_SUCCESS'
        else:
            merged['error']='回放成功；'+('评测结果待核验，不会自动重跑：' if state=='NEEDS_REVIEW' else '评测未完成：')+result.get('error',state)
            parent_state='PARTIAL_SUCCESS'
        self._finish(c,parent,parent_state,merged)

    def finish(self, job, state, result):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._finish(c,job,state,result)

    def _finish(self,c,job,state,result):
        if result.get('assembly_pending') and state in ('SUCCEEDED','POSTPROCESSING'):
            from .archive_queue import enqueue
            enqueue(c,job,result)
        c.execute("UPDATE jobs SET state=?,result=?,updated=? WHERE id=? AND owner=?", (state, json.dumps(result, ensure_ascii=False), time.time(), job["id"],job['owner']))
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_requests'").fetchone():
            c.execute('UPDATE task_requests SET state=?,updated=? WHERE owner=? AND job_id=?',(state,time.time(),job['owner'],job['id']))
        if state not in ("QUEUED", "RUNNING"):
            c.execute("DELETE FROM queue_outbox WHERE job=?",(job["id"],))
            if not job['payload'].get('automatic'):
                text=format_result(job,state,result)
                c.execute("INSERT INTO outbox(job,session,text,attachment) VALUES(?,?,?,?) ON CONFLICT(job) DO UPDATE SET text=excluded.text,attachment=excluded.attachment,sent=0,attempts=0,version=outbox.version+1,text_sent=0,next_attempt_at=0", (job["id"], job["session"], text, result.get("local_report")))
            self._update_evaluation_parent(c,job,state,result)

    def stage(self,job,stage,**details):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute("SELECT result FROM jobs WHERE id=? AND owner=?",(job['id'],job['owner'])).fetchone()
            result={**(json.loads(current['result'] or '{}') if current else (job.get('result') or {})),**details,'stage':stage}
            c.execute("UPDATE jobs SET result=?,updated=? WHERE id=? AND state='RUNNING'",(json.dumps(result,ensure_ascii=False),time.time(),job['id']))
        job['result']=result
        try:
            from .progress import stage as publish_stage
            publish_stage(self,job,stage)
        except Exception:pass


def diagnosis_text(result):
    diagnostic=result.get('diagnostics') or {}
    if not isinstance(diagnostic,dict) or not diagnostic:return ''
    lines=[]
    if diagnostic.get('category'):lines.append('诊断分类：'+str(diagnostic['category']))
    if diagnostic.get('summary'):lines.append('日志判断：'+str(diagnostic['summary'])[:600])
    for item in diagnostic.get('evidence',[])[:3]:
        if isinstance(item,dict) and item.get('excerpt'):
            lines.append('证据：'+str(item['excerpt'])[:400]+('（'+str(item['path'])+'）' if item.get('path') else ''))
    if not diagnostic.get('evidence'):
        for path in diagnostic.get('logs',[])[:3]:
            lines.append('已检查日志：'+str(path)[:600])
    if diagnostic.get('next_action'):lines.append('下一步：'+str(diagnostic['next_action'])[:800])
    elif diagnostic.get('logs'):lines.append('请核对本任务日志并补充匹配材料；本轮已停止。')
    return '\n'+'\n'.join(lines) if lines else ''


def format_result(job, state, result):
    evaluation=job['payload']['action']=='evaluate'
    assembly=result.get('assembly') or {}
    archive_text=('\n本人成功装配存档：'+str(assembly.get('name',''))+' · '+str(assembly['id'])+
                  '；下次可发送“复用装配 '+str(assembly['id'])+'”。') if assembly.get('state')=='SAVED' else (
                  '\n'+result['assembly_archive_warning'] if result.get('assembly_archive_warning') else
                  '\n本人装配存档：后台归档中；结果已交付，不占用台架队列。' if result.get('assembly_pending') else '')
    if state=='POSTPROCESSING':
        status='执行中' if result.get('evaluation_state')=='RUNNING' else '等待执行'
        return f"回放完成，评测{status}，后续评测不占用台架队列。任务：{job['id']}\n工作站：{result.get('workstation','ubuntu-workstation')}\n结果目录：{result.get('task','')}\n评测任务：{result.get('evaluation_job','')}"+archive_text
    if state=='RUNNING':
        stage=result.get('stage','评测中（不占用台架队列）' if evaluation else '等待派发')
        return f"任务 {job['id']} 执行中：{stage}。"
    if state == "SIMULATED":
        if job['payload']['action']=='deployment_check':
            return f"【部署联通验证】后台入口、串行队列和完成通知已就绪。\n任务：{job['id']}\n随附 TXT 仅用于验证文件交付。本次未刷包、未做小场景预检、未回放或评测。"
        return f"模拟验证完成（未派发台架任务）。任务：{job['id']}\n{json.dumps(result, ensure_ascii=False)}"
    if state=='NEEDS_REVIEW':
        if evaluation:return f"评测任务 {job['id']}：结果待核验，不会自动重跑；不阻塞台架队列。\n源回放任务：{job['payload'].get('source_job','')}\n回放结果：{job['payload'].get('source_task','')}\n"+result.get('error','请核对评测收据，无需重新回放')
        return f"任务 {job['id']}：待核验，已暂停自动派发。\n"+result.get('error','执行结果尚未确认')+diagnosis_text(result)+"\n若台架与工作站断连，请联系现场同事（联系人待指定）检查物理连接、供电和网络。执行中断连后即使连接恢复，也需核验 Runner 和恢复收据，不自动重跑。"
    if state == "SUCCEEDED":
        if job['payload'].get('retained_result'):
            return f"已有回放结果已登记。任务：{job['id']}\n工作站结果目录：{result.get('task','')}\n本次未重新刷包或回放，可对本任务提交评测。"
        prefix = "评测完成" if job["payload"]["action"] == "evaluate" else ("回放及评测完成" if result.get('report') else "回放完成")
        source=f"\n源回放任务：{job['payload'].get('source_job','')}" if evaluation else ''
        return f"{prefix}。任务：{job['id']}"+source+f"\n工作站：{result.get('workstation','ubuntu-workstation')}\n结果目录：{result.get('task', '')}" + (f"\n原始 TXT：{result['report']}" if result.get("report") else "")+archive_text
    if state=='PARTIAL_SUCCESS' and result.get('report'):
        title='评测生成部分结果' if evaluation else '回放完成，评测生成部分结果'
        warnings='；'.join(str(v) for v in result.get('warnings',[])[:8])
        return f"{title}。任务：{job['id']}\n结果目录：{result.get('task','')}\n原始 TXT：{result['report']}\n"+(warnings or result.get('error') or '部分任务缺少有效样本或真值，不能视为完整评测通过。')+archive_text
    return f"任务 {job['id']}：{state}\n{result.get('error','请核对执行收据')}"+diagnosis_text(result) + (f"\n已保留回放结果：{result['task']}" if result.get("task") else "")+archive_text


@contextlib.contextmanager
def worker_lock(root):
    """OS-released lock: a crash does not turn a live worker into a second worker."""
    p = Path(root) / "worker.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+b") as f:
        f.seek(0)
        if os.fstat(f.fileno()).st_size == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise GatewayError("已有调度进程在运行") from exc
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
