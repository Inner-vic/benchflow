"""Owner-bound confirmation of one material revision, before preparation/admission.

The persisted policy is read by the final SQLite admission transaction. Nothing
in this module dispatches hardware, downloads materials or sends a message.
"""
import json
import time
import uuid

from .core import GatewayError

POLICY='require_material_confirmation'


def enabled(cfg):
    return bool(cfg and cfg.get(POLICY))


def _exists(c,name):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())


def schema(store):
    with store.connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS gateway_policies(
          name TEXT PRIMARY KEY,value TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS material_confirmations(
          id TEXT PRIMARY KEY,owner TEXT NOT NULL REFERENCES users(id),
          task_id TEXT NOT NULL REFERENCES task_requests(id),revision INTEGER NOT NULL,
          fingerprint TEXT NOT NULL,session TEXT NOT NULL,snapshot TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('ACTIVE','INVALIDATED','CONSUMED')),
          created REAL NOT NULL,job_id TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS material_confirmation_active_task
          ON material_confirmations(owner,task_id) WHERE state='ACTIVE';
        ''')


def configure(cfg,store):
    """An omitted flag must not clear an already persisted admission rule."""
    if POLICY not in (cfg or {}):return
    schema(store)
    with store.connect() as c:
        c.execute('INSERT INTO gateway_policies(name,value,updated) VALUES(?,?,?) '
                  'ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated=excluded.updated',
                  (POLICY,'1' if enabled(cfg) else '0',time.time()))


def required_in(c):
    if not _exists(c,'gateway_policies'):return False
    row=c.execute('SELECT value FROM gateway_policies WHERE name=?',(POLICY,)).fetchone()
    return bool(row and str(row['value']).lower() in ('1','true'))


def _data(task):
    data=task['data']
    return json.loads(data) if isinstance(data,str) else data


def active(c,user,task,session=None):
    """Return a raw record dict only when its complete snapshot still matches.

    ``snapshot`` remains JSON text, matching SQLite. ``task.data`` may be JSON
    text (a SQL row) or a dict (Tasks.get's public result).
    """
    if not task or not _exists(c,'material_confirmations'):return None
    task=dict(task)
    if task.get('owner')!=user:return None
    try:
        from .preparation import fingerprint
        data=_data(task)
        if not isinstance(data,dict):return None
        fp=fingerprint(data)
        row=c.execute("SELECT * FROM material_confirmations WHERE owner=? AND task_id=? AND state='ACTIVE'",
                      (user,task['id'])).fetchone()
        if (not row or row['revision']!=task['revision'] or row['fingerprint']!=fp
                or session is not None and row['session']!=session):return None
        snapshot=json.loads(row['snapshot'])
        if not isinstance(snapshot,dict) or fingerprint(snapshot)!=fp:return None
        return dict(row)
    except (KeyError,TypeError,ValueError):return None


def invalidate(c,user,tid,reason=''):
    # The state is the durable security boundary. The caller owns explanatory
    # card feedback; reasons are deliberately not mixed into material snapshots.
    if not _exists(c,'material_confirmations'):return 0
    return c.execute("UPDATE material_confirmations SET state='INVALIDATED' "
                     "WHERE owner=? AND task_id=? AND state='ACTIVE'",(user,tid)).rowcount


def confirm(cfg,store,user,session,tid,revision,exclude_batch=None):
    """Confirm exactly the currently displayed material revision.

    The callback's own input batch may be excluded. All other unfinished or
    interrupted material messages remain barriers. Explicit confirmation can
    resume a paused draft, but no unfinished attachment is silently discarded.
    """
    if not enabled(cfg):raise GatewayError('材料确认功能尚未启用。')
    if not cfg.get('auto_execute_on_complete'):raise GatewayError('入口调度尚未启用，本次未确认执行。')
    from .service import identity
    if identity(session)!=user:raise GatewayError('确认操作的本人私聊身份不一致。')
    if type(revision) is not int:raise GatewayError('卡片版本无效，请使用最新材料卡。')
    store.require_user(user)
    schema(store)
    from .tasks import Tasks
    tasks=Tasks(store);task=tasks.get(user,tid);tid=task['id']
    # AutoFlow.arm owns its schema/row initialization. Do not call control(),
    # which could recursively interpret an old resume command as confirmation.
    from .autoflow import AutoFlow
    AutoFlow(cfg,store).arm(user,session,task)
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not required_in(c):raise GatewayError('材料确认规则尚未配置，本次未提交。')
        current=c.execute('SELECT t.* FROM task_requests t JOIN task_focus f '
                          'ON f.task_id=t.id AND f.owner=t.owner WHERE t.owner=?',(user,)).fetchone()
        if not current or current['id']!=tid:raise GatewayError('当前材料已切换，请使用当前材料卡确认。')
        if current['job_id']:raise GatewayError('本次材料已提交，不会重复执行。')
        if current['revision']!=revision:raise GatewayError('材料已更新，旧卡确认未生效，请核对最新材料后重新确认。')
        if current['session'] and current['session']!=session:raise GatewayError('材料不属于当前本人私聊。')
        if (_exists(c,'card_ui') and c.execute('SELECT 1 FROM card_ui WHERE task_id=? AND owner=? AND editing=1',
                                              (tid,user)).fetchone()):
            raise GatewayError('材料仍在编辑，请先保存或取消编辑，再确认执行。')
        if (_exists(c,'input_batches') and c.execute("SELECT 1 FROM input_batches WHERE owner=? "
                "AND state IN ('OPEN','ABORTED') AND (? IS NULL OR token!=?) LIMIT 1",
                (user,exclude_batch,exclude_batch)).fetchone()):
            raise GatewayError('材料仍在接收或上次接收未完成，请补齐后再确认。')
        if (_exists(c,'task_attachments') and c.execute("SELECT 1 FROM task_attachments WHERE owner=? AND task_id=? "
                "AND state NOT IN ('ASSOCIATED','SUPERSEDED','DUPLICATE') LIMIT 1",(user,tid)).fetchone()):
            raise GatewayError('附件尚未保存并关联完成，请等待或补齐后再确认。')
        from .card_ui import upload_waits
        if upload_waits(c,user,tid):raise GatewayError('仍有已声明等待的附件，请上传或取消等待项后再确认。')
        draft=_data(current)
        from .business import missing_inputs,LABELS
        missing=[i for i in missing_inputs(draft) if not (
            i['code']=='M100_BUILD_REQUIRED' and cfg.get('m100_build_enabled'))]
        if missing:raise GatewayError('尚缺材料：'+'、'.join(LABELS.get(i['field'],i['field']) for i in missing)+'。补齐后再确认。')
        from .preparation import fingerprint
        fp=fingerprint(draft);now=time.time()
        record=active(c,user,current,session)
        if not record:
            invalidate(c,user,tid,'MATERIAL_REVISION_CHANGED')
            cid='confirm-'+uuid.uuid4().hex[:24]
            c.execute('INSERT INTO material_confirmations VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (cid,user,tid,revision,fp,session,json.dumps(draft,ensure_ascii=False),'ACTIVE',now,None))
            record=dict(c.execute('SELECT * FROM material_confirmations WHERE id=?',(cid,)).fetchone())
        auto=c.execute('SELECT * FROM automatic_tasks WHERE owner=? AND task_id=?',(user,tid)).fetchone()
        if (not auto or auto['revision']!=revision or auto['fingerprint']!=fp or auto['session']!=session):
            raise GatewayError('材料调度记录已变化，请重新核对最新卡片。')
        # A duplicate click must not reset a check that is already in progress.
        if auto['state']!='CHECKING':
            c.execute("UPDATE automatic_tasks SET state='WAITING',not_before=?,error='',updated=? "
                      'WHERE task_id=? AND owner=?',(now,now,tid,user))
        return record
