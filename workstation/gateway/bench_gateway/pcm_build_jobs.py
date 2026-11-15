"""Independent software-build FIFO and immutable, real-user confirmations."""
import json
import time
import uuid
from .pcm_build_plan import checked
from .pcm_dual_build import digest, components


class BuildFailure(Exception):
    pass


class Uncertain(Exception):
    pass


class ReadPending(Exception):
    pass


class Jobs:
    def __init__(self, store):
        self.store = store
        with store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS pcm_build_jobs(
              id TEXT PRIMARY KEY,owner TEXT NOT NULL,session TEXT NOT NULL,
              draft_id TEXT NOT NULL,revision INTEGER NOT NULL,review_sha TEXT NOT NULL UNIQUE,
              review TEXT NOT NULL,confirmation_event TEXT NOT NULL UNIQUE,
              message_id TEXT NOT NULL,state TEXT NOT NULL,progress TEXT NOT NULL,
              modes TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',
              notify_pending INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS pcm_build_steps(
              job_id TEXT NOT NULL,step TEXT NOT NULL,state TEXT NOT NULL,
              intent TEXT NOT NULL,result TEXT,updated REAL NOT NULL,
              PRIMARY KEY(job_id,step));
            ''')

    @staticmethod
    def unpack(row):
        if row is None: return None
        d = dict(row);d['review'] = json.loads(d['review']);d['modes'] = json.loads(d['modes'])
        return d

    def get(self, jid):
        with self.store.connect() as c:
            return self.unpack(c.execute('SELECT * FROM pcm_build_jobs WHERE id=?', (jid,)).fetchone())

    def for_draft(self, owner, session, draft_id):
        with self.store.connect() as c:
            return self.unpack(c.execute('SELECT * FROM pcm_build_jobs WHERE owner=? AND session=? AND draft_id=? ORDER BY created DESC LIMIT 1',
                (owner, session, draft_id)).fetchone())

    def confirm(self, owner, session, data, expected_sha):
        """Only the HMAC-authenticated private-card handler may call this method."""
        value = data['value'];now = time.time()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM pcm_dual_drafts WHERE id=? AND owner=? AND session=?',
                            (value.get('task_id'), owner, session)).fetchone()
            if not row: raise ValueError('构建配置不属于本人会话。')
            report = json.loads(row['report'] or '{}');review = checked(report.get('build_review') or {})
            if (data.get('operator_id') != owner or data.get('chat_id') != session.split(':')[1]
                    or review['owner'] != owner or review['session'] != session
                    or review['nonce'] != value.get('nonce') or row['revision'] != value.get('revision')
                    or review['review_sha256'] != expected_sha or review['revision'] != row['revision']):
                raise ValueError('确认身份或最终参数已变化，请使用最新构建卡。')
            old = c.execute('SELECT * FROM pcm_build_jobs WHERE review_sha=?', (expected_sha,)).fetchone()
            if old:
                if (old['owner'], old['session'], old['message_id']) != (owner, session, data['message_id']):
                    raise ValueError('构建确认卡片不一致。')
                return self.unpack(old), False
            surface = c.execute('SELECT * FROM pcm_dual_surfaces WHERE owner=? AND session=?', (owner, session)).fetchone()
            opening = c.execute('SELECT state FROM pcm_dual_opens WHERE owner=?', (owner,)).fetchone()
            if (row['state'] != 'VALIDATED' or not surface or surface['draft_id'] != row['id']
                    or surface['message_id'] != data['message_id'] or (opening and opening['state'] == 'PENDING')):
                raise ValueError('原构建卡已更新，未提交。')
            if c.execute('SELECT 1 FROM pcm_build_jobs WHERE confirmation_event=?', (data['event_id'],)).fetchone():
                raise ValueError('确认事件已被其他构建使用。')
            text = report['effective_component_versions'];components(text, require_required=True)
            jid = 'PB-'+uuid.uuid4().hex[:20]
            c.execute('''INSERT INTO pcm_build_jobs(id,owner,session,draft_id,revision,review_sha,review,
              confirmation_event,message_id,state,progress,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (jid, owner, session, row['id'], row['revision'], expected_sha, json.dumps(review, ensure_ascii=False),
               data['event_id'], data['message_id'], 'QUEUED', '已确认，等待软件构建', now, now))
            c.execute("UPDATE pcm_dual_drafts SET state='CONFIRMED' WHERE id=?", (row['id'],))
            c.execute('INSERT INTO pcm_dual_defaults VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET component_versions=excluded.component_versions,confirmation=excluded.confirmation',
                      (owner, text, row['id']))
            return self.unpack(c.execute('SELECT * FROM pcm_build_jobs WHERE id=?', (jid,)).fetchone()), True

    def next(self, *, allow_new=True):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT * FROM pcm_build_jobs WHERE state='RUNNING' ORDER BY created,id LIMIT 1").fetchone()
            if row: return self.unpack(row)
            if not allow_new: return None
            row = c.execute("SELECT * FROM pcm_build_jobs WHERE state='QUEUED' ORDER BY created,id LIMIT 1").fetchone()
            if not row: return None
            c.execute("UPDATE pcm_build_jobs SET state='RUNNING',updated=? WHERE id=?", (time.time(), row['id']))
            return self.unpack(c.execute('SELECT * FROM pcm_build_jobs WHERE id=?', (row['id'],)).fetchone())

    def update(self, jid, *, state=None, progress=None, error=None, mode=None, result=None):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM pcm_build_jobs WHERE id=?', (jid,)).fetchone()
            if not row: raise ValueError('Unknown build')
            modes = json.loads(row['modes'])
            if mode: modes[mode] = result
            c.execute('UPDATE pcm_build_jobs SET state=?,progress=?,error=?,modes=?,notify_pending=1,updated=? WHERE id=?',
                (state or row['state'], progress or row['progress'], row['error'] if error is None else error,
                 json.dumps(modes, ensure_ascii=False), time.time(), jid))

    def deliveries(self):
        with self.store.connect() as c:
            return [self.unpack(r) for r in c.execute('SELECT * FROM pcm_build_jobs WHERE notify_pending=1 ORDER BY created LIMIT 20')]

    def delivered(self, job):
        with self.store.connect() as c:
            # Do not clear a newer stage's pending delivery.
            c.execute('UPDATE pcm_build_jobs SET notify_pending=0 WHERE id=? AND updated=?', (job['id'], job['updated']))

    def step(self, jid, key):
        with self.store.connect() as c:
            row = c.execute('SELECT * FROM pcm_build_steps WHERE job_id=? AND step=?', (jid, key)).fetchone()
            if not row: return None
            d = dict(row);d['intent'] = json.loads(d['intent']);d['result'] = json.loads(d['result']) if d['result'] else None
            return d

    def mutation(self, job, key, intent, perform, reconcile):
        """Persist intent before mutation. An ambiguous intent is never resubmitted."""
        row = self.step(job['id'], key)
        if row:
            if row['intent'] != intent: raise Uncertain('持久化派发身份不一致，已停止。')
            if row['state'] == 'DONE': return row['result']
            if row['state'] == 'FAILED': raise BuildFailure('该步骤已失败，不自动重新提交。')
            result = reconcile(row)
            if result is None: raise Uncertain('上次提交结果未确认，已停止重复派发，请管理员核对原平台记录。')
        else:
            with self.store.connect() as c:
                c.execute('INSERT INTO pcm_build_steps VALUES(?,?,?,?,NULL,?)',
                          (job['id'], key, 'INTENT', json.dumps(intent, sort_keys=True), time.time()))
            try:
                result = perform()
            except Exception as error:
                row = self.step(job['id'], key)
                try: result = reconcile(row)
                except Exception: result = None
                if result is None:
                    if isinstance(error, BuildFailure):
                        with self.store.connect() as c:
                            c.execute("UPDATE pcm_build_steps SET state='FAILED',updated=? WHERE job_id=? AND step=?",
                                      (time.time(), job['id'], key))
                        raise
                    raise Uncertain('平台提交结果未确认，已停止重复派发；请管理员核对原提交。') from None
        with self.store.connect() as c:
            c.execute("UPDATE pcm_build_steps SET state='DONE',result=?,updated=? WHERE job_id=? AND step=?",
                      (json.dumps(result, ensure_ascii=False), time.time(), job['id'], key))
        return result
