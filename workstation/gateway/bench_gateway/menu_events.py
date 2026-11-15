"""Authenticated bot-menu events display cards without creating chat commands."""
import hashlib
import json
import time
from .core import GatewayError

PREFIX = '@@BENCH_MENU@@'


def show(service, session, mid, *, retire_previous=True):
    from .card_ui import feedback
    from .tasks import Tasks
    from .service import identity
    from .input_cards import ensure, present
    from .inline_materials import reset
    cfg, store = service.cfg, service.store
    user = identity(session)
    service.open_materials(user, session)
    task = Tasks(store).current(user)
    ensure(cfg, store, user, session, task)
    until = time.monotonic() + 6
    old_mid = None
    while True:
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM input_cards WHERE task_id=? AND owner=?', (task['id'], user)).fetchone()
            native = c.execute('SELECT lease_until FROM task_cards WHERE job=?', (task['job_id'],)).fetchone() if task['job_id'] else None
            if row['lease_until'] <= time.time() and (not native or native['lease_until'] <= time.time()):
                old_mid = row['message_id']
                history = json.loads(row['history'])
                if old_mid and old_mid != mid:
                    history.append({'generation': row['generation'], 'message_id': old_mid, 'revision': row['revision']})
                # This visit already created a visible card. Do not resurface it
                # again when present() checks the previous idle timestamp.
                c.execute('UPDATE input_cards SET message_id=?,session=?,digest=?,last_sent=0,next_attempt=0,surface_requested=0,last_activity=?,history=?,generation=generation+1 WHERE task_id=? AND owner=?',
                          (mid, session, '', time.time(), json.dumps(history[-30:]), task['id'], user))
                if task['job_id']:
                    c.execute('UPDATE task_cards SET message_id=?,sent_digest=?,last_sent=0,next_attempt=0 WHERE job=?', (mid, '', task['job_id']))
                break
        if time.monotonic() >= until: raise GatewayError('原卡片正在更新，请稍后再点击菜单；未修改材料。')
        time.sleep(.1)
    reset(store, task['id']); feedback(store, user, task['id'], '')
    if task['job_id']:
        from .task_cards import deliver
        deliver(cfg, store, job_id=task['job_id'], deadline=time.monotonic() + 9)
    elif not present(cfg, store, user, session):
        raise GatewayError('材料卡暂未更新完成，入口会继续重试；未提交台架。')
    if retire_previous and old_mid and old_mid != mid:
        try:
            from .task_cards import _client
            from .card_entities import update
            update(store, _client(cfg), old_mid, {'schema': '2.0', 'config': {'update_multi': True},
                'header': {'template': 'blue', 'title': {'tag': 'plain_text', 'content': '台架评测'}},
                'body': {'elements': [{'tag': 'markdown', 'content': '材料卡已更新，请使用聊天底部的最新卡片。'}]}}, lambda: 4)
        except Exception: pass
    return 'NO_REPLY'


def handle(service, session, text):
    from .card_ui import PREFIX as CARD_PREFIX, decode
    from .service import identity
    from .card_entities import send_owner, update
    from .task_cards import _client
    cfg, store = service.cfg, service.store
    if not cfg.get('native_material_menu'): raise GatewayError('菜单直接开卡尚未启用。')
    event = decode(cfg, session, CARD_PREFIX + text[len(PREFIX):]);user=identity(session)
    if event.get('action')!='open_materials' or event.get('value',{}).get('menu_version')!=2 or event.get('chat_id')!=user:
        raise GatewayError('菜单服务已更新，请重新点击“当前配置”。')
    eid=event['event_id']
    with store.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS menu_events(event_id TEXT PRIMARY KEY,owner TEXT NOT NULL,
          session TEXT NOT NULL,message_id TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL)''')
        row=c.execute('SELECT * FROM menu_events WHERE event_id=?',(eid,)).fetchone()
        if row:
            if row['owner']!=user or row['session']!=session:raise GatewayError('菜单事件身份不一致。')
            if row['state']=='DONE':return 'NO_REPLY'
        else:c.execute('INSERT INTO menu_events VALUES(?,?,?,?,?,?)',(eid,user,session,'','PROCESSING',time.time()))
    client=_client(cfg);key=hashlib.sha256((cfg['feishu_app_id']+':menu:'+user+':'+eid).encode()).hexdigest()[:32]
    loading={'schema':'2.0','config':{'update_multi':True,'enable_forward':False},
             'header':{'template':'blue','title':{'tag':'plain_text','content':'台架评测'}},
             'body':{'elements':[{'tag':'markdown','content':'正在打开材料卡…'}]}}
    mid,chat=send_owner(store,client,user,loading,key,lambda:8)
    native='feishu:'+chat+':'+user
    with store.connect() as c:c.execute('UPDATE menu_events SET message_id=? WHERE event_id=? AND owner=?',(mid,eid,user))
    try:show(service,native,mid)
    except Exception:
        # This is the newly created menu card, not a historical message.
        failure={**loading,'body':{'elements':[{'tag':'markdown','content':'材料卡暂未打开。请稍后重试；已有材料保留，未提交任务。'}]}}
        update(store,client,mid,failure,lambda:6)
        with store.connect() as c:c.execute("UPDATE menu_events SET state='FAILED' WHERE event_id=? AND owner=?",(eid,user))
        return 'NO_REPLY'
    with store.connect() as c:c.execute("UPDATE menu_events SET state='DONE' WHERE event_id=? AND owner=?",(eid,user))
    return 'NO_REPLY'
