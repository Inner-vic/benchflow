"""Workstation-persisted bounded AI requests; this module never runs an AI model."""
import contextlib,json,sqlite3,time,uuid,hashlib,socket
from pathlib import Path
from .core import GatewayError
from .client_errors import MESSAGES,ParserFailure

KINDS={'intent','yaml'}

@contextlib.contextmanager
def broker_guard(cfg):
    """Linux process-lifetime guard, separate from every hardware/dataset lock.

    Abstract sockets vanish on exit. A second laptop cannot consume prompts or
    take over the active parser. Never release another process's guard.
    """
    name=hashlib.sha256(str(Path(cfg['state_root']).resolve()).encode()).hexdigest()[:32]
    guard=socket.socket(socket.AF_UNIX)
    try:
        guard.bind('\0bench-gateway-ai-'+name)
        yield
    finally:guard.close()

@contextlib.contextmanager
def connection(cfg):
    root=Path(cfg['state_root']);root.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(str(root/'ai-requests.sqlite3'),timeout=15)
    try:
        c.row_factory=sqlite3.Row
        c.execute('''CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,kind TEXT NOT NULL,prompt TEXT NOT NULL,
          created REAL NOT NULL,deadline REAL NOT NULL,state TEXT NOT NULL,result TEXT,error TEXT,claim TEXT)''')
        c.execute('CREATE TABLE IF NOT EXISTS client(id INTEGER PRIMARY KEY CHECK(id=1),heartbeat REAL NOT NULL,busy_until REAL NOT NULL)')
        c.commit()
        with c:yield c
    finally:c.close()


def touch(cfg,busy_until=0):
    with connection(cfg) as c:
        c.execute('INSERT OR REPLACE INTO client VALUES(1,?,?)',(time.time(),busy_until))


def health(cfg):
    with connection(cfg) as c:
        row=c.execute('SELECT * FROM client WHERE id=1').fetchone()
        last=c.execute("SELECT state,error,created FROM requests WHERE state IN ('DONE','FAILED') ORDER BY created DESC LIMIT 1").fetchone()
    now=time.time()
    return {'connected':bool(row and (0<=now-row['heartbeat']<12 or row['busy_until']>now)),
            'last_error':last['error'] if last and last['state']=='FAILED' and now-last['created']<600 and last['error'] in MESSAGES else None}

def request(cfg,kind,prompt,timeout):
    if kind not in KINDS or not isinstance(prompt,str) or len(prompt)>100000:raise GatewayError('解析请求无效。')
    if cfg.get('ai_backend')=='workstation-broker' and not health(cfg)['connected']:
        raise GatewayError('个人电脑上的Codex解析器未连接；材料已保留，请稍后重试。')
    identifier=uuid.uuid4().hex;deadline=time.time()+timeout
    with connection(cfg) as c:
        c.execute('INSERT INTO requests(id,kind,prompt,created,deadline,state) VALUES(?,?,?,?,?,?)',
            (identifier,kind,prompt,time.time(),deadline,'QUEUED'))
    while time.time()<deadline:
        with connection(cfg) as c:row=c.execute('SELECT * FROM requests WHERE id=?',(identifier,)).fetchone()
        if row['state']=='DONE':return json.loads(row['result'])
        if row['state']=='FAILED':
            raise ParserFailure(row['error'])
        time.sleep(.2)
    with connection(cfg) as c:
        c.execute("UPDATE requests SET state='EXPIRED' WHERE id=? AND state IN ('QUEUED','RUNNING')",(identifier,))
    raise GatewayError('Codex解析等待超时；材料保留，请稍后重试，未提交台架。')

def claim(cfg):
    with connection(cfg) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE requests SET state='EXPIRED' WHERE deadline<? AND state IN ('QUEUED','RUNNING')",(time.time(),))
        row=c.execute("SELECT * FROM requests WHERE state='QUEUED' ORDER BY created LIMIT 1").fetchone()
        if not row:return None
        token=uuid.uuid4().hex
        c.execute("UPDATE requests SET state='RUNNING',claim=? WHERE id=? AND state='QUEUED'",(token,row['id']))
        return {'id':row['id'],'kind':row['kind'],'prompt':row['prompt'],'claim':token,
            'remaining':max(1,row['deadline']-time.time())}

def finish(cfg,packet):
    if set(packet)-{'id','claim','result','error'}:raise ValueError('unknown parser response')
    with connection(cfg) as c:
        row=c.execute("SELECT * FROM requests WHERE id=? AND claim=? AND state='RUNNING'",(packet['id'],packet['claim'])).fetchone()
        if row is None or row['deadline']<time.time():return
        value=packet.get('result');raw=json.dumps(value,ensure_ascii=False)
        if len(raw)>65536:raise ValueError('oversized parser response')
        c.execute('UPDATE requests SET state=?,result=?,error=? WHERE id=? AND claim=?',
            ('FAILED' if packet.get('error') else 'DONE',None if packet.get('error') else raw,
             (packet['error'] if packet.get('error') in MESSAGES else 'PARSER_FAILED') if packet.get('error') else None,packet['id'],packet['claim']))

def serve(cfg):
    with broker_guard(cfg):return serve_connected(cfg)

def serve_connected(cfg):
    import sys
    touch(cfg);print(json.dumps({'type':'ready'}),flush=True)
    while True:
        value=claim(cfg)
        if value:
            touch(cfg,time.time()+value['remaining'])
            print(json.dumps({'type':'request',**value},ensure_ascii=False),flush=True)
            line=sys.stdin.readline()
            if not line:return
            finish(cfg,json.loads(line))
            touch(cfg)
        else:
            touch(cfg)
            print(json.dumps({'type':'heartbeat'}),flush=True);time.sleep(1)

if __name__=='__main__':
    import sys
    from .config import load
    serve(load(sys.argv[1]))
