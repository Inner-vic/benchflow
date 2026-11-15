"""Stable public references, separate from immutable runner IDs and paths."""
import datetime
import re
from .core import GatewayError

PUBLIC=re.compile(r'TJ-\d{8}-\d{4,}\Z',re.I)

def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS task_references(
      task_id TEXT PRIMARY KEY REFERENCES task_requests(id),owner TEXT NOT NULL,
      reference TEXT NOT NULL UNIQUE,day TEXT NOT NULL,sequence INTEGER NOT NULL,
      UNIQUE(day,sequence))''')

def allocate(c,user,tid,created):
    row=c.execute('SELECT reference FROM task_references WHERE task_id=? AND owner=?',(tid,user)).fetchone()
    if row:return row['reference']
    day=datetime.datetime.fromtimestamp(created,datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y%m%d')
    seq=c.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM task_references WHERE day=?',(day,)).fetchone()[0]
    ref='TJ-'+day+'-'+str(seq).zfill(4)
    c.execute('INSERT INTO task_references VALUES(?,?,?,?,?)',(tid,user,ref,day,seq))
    return ref

def reference(store,user,*,task_id=None,job_id=None):
    store.require_user(user)
    with store.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_requests'").fetchone():return ''
        schema(c);c.execute('BEGIN IMMEDIATE')
        if task_id:
            row=c.execute('SELECT id,created FROM task_requests WHERE id=? AND owner=?',(task_id,user)).fetchone()
        else:
            row=c.execute('SELECT id,created FROM task_requests WHERE job_id=? AND owner=?',(job_id,user)).fetchone()
        return allocate(c,user,row['id'],row['created']) if row else ''

def resolve(c,user,ref):
    row=c.execute('SELECT task_id FROM task_references WHERE reference=? AND owner=?',(ref.upper(),user)).fetchone()
    if not row:raise GatewayError('本人没有这个任务编号，请发送“我的任务”查看。')
    return row['task_id']
