"""One private material card, handed to the existing execution card on admission."""
import hashlib
import json
import time
import uuid
from urllib.parse import quote

from .core import GatewayError
from .tasks import Tasks
from .material_summary import describe


def enabled(cfg):
    return bool(cfg.get('unified_status_cards') and cfg.get('task_cards_enabled') and cfg.get('notifications_enabled') and cfg.get('auto_execute_on_complete'))


def schema(store):
    Tasks(store)
    with store.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS input_cards(
          task_id TEXT PRIMARY KEY REFERENCES task_requests(id),owner TEXT NOT NULL,session TEXT NOT NULL,
          generation INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'NEW',
          message_id TEXT,digest TEXT NOT NULL DEFAULT '',last_sent REAL NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
          lease_token TEXT,lease_until REAL NOT NULL DEFAULT 0,fallback_at REAL NOT NULL DEFAULT 0,
          history TEXT NOT NULL DEFAULT '[]',reopen INTEGER NOT NULL DEFAULT 0)''')
        c.execute('BEGIN IMMEDIATE')
        columns={r['name'] for r in c.execute('PRAGMA table_info(input_cards)')}
        for name,definition in [('last_activity','REAL NOT NULL DEFAULT 0'),('surface_requested','INTEGER NOT NULL DEFAULT 0')]:
            if name not in columns:c.execute('ALTER TABLE input_cards ADD COLUMN '+name+' '+definition)


def ensure(cfg,store,user,session,task=None):
    if not enabled(cfg):return False
    from .service import identity
    if identity(session)!=user:raise GatewayError('材料卡片私聊归属不一致')
    task=task or Tasks(store).current(user)
    if not task:return False
    schema(store)
    with store.connect() as c:
        c.execute('INSERT OR IGNORE INTO input_cards(task_id,owner,session) VALUES(?,?,?)',(task['id'],user,session))
    return True


def activity(cfg,store,user,session,task,*,force=False):
    """A new intake visit must be visible; polling must never create messages."""
    if not cfg.get('require_material_confirmation') or task['job_id']:return
    from .service import identity
    if identity(session)!=user:raise GatewayError('材料卡片私聊归属不一致')
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM input_cards WHERE owner=? AND task_id=? AND session=?',(user,task['id'],session)).fetchone()
        if not row:return
        from .confirmations import active
        idle=time.time()-row['last_activity']>=1800
        surface=bool(row['message_id'] and (force or idle) and not active(c,user,task,session))
        c.execute('UPDATE input_cards SET last_activity=?,surface_requested=MAX(surface_requested,?),next_attempt=0 WHERE task_id=? AND owner=?',
                  (time.time(),int(surface),task['id'],user))


def snapshot(cfg,store,row):
    task=Tasks(store).get(row['owner'],row['task_id']);draft=task['data']
    from .business import missing_inputs,LABELS
    from .preparation import fingerprint
    with store.connect() as c:
        focus=c.execute('SELECT task_id FROM task_focus WHERE owner=?',(row['owner'],)).fetchone()
        auto=c.execute('SELECT * FROM automatic_tasks WHERE task_id=? AND owner=?',(task['id'],row['owner'])).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automatic_tasks'").fetchone() else None
        prep=c.execute('SELECT * FROM preparations WHERE id=? AND owner=?',(auto['preparation_id'],row['owner'])).fetchone() if auto and auto['preparation_id'] else None
        pending=c.execute("SELECT filename,state FROM task_attachments WHERE owner=? AND task_id=? AND state NOT IN ('ASSOCIATED','SUPERSEDED','DUPLICATE')",(row['owner'],task['id'])).fetchall() if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_attachments'").fetchone() else []
        aborted=c.execute("SELECT 1 FROM input_batches WHERE owner=? AND state='ABORTED' LIMIT 1",(row['owner'],)).fetchone()
    result=json.loads(prep['result'] or '{}') if prep and prep['fingerprint']==fingerprint(draft) else {}
    missing=[LABELS[i['field']] for i in missing_inputs(draft) if not (i['code']=='M100_BUILD_REQUIRED' and cfg.get('m100_build_enabled'))]
    action='';state='MATERIALS';label='待补充材料';detail='已收材料保留，补齐后自动检查。';tone='blue'
    astate=auto['state'] if auto else ''
    if not focus or focus['task_id']!=task['id']:
        state='CANCELLED';label='材料已切换';detail='这份材料未继续提交。';tone='grey'
    elif task['job_id']:
        state='TRANSFERRED';label='已进入执行流程';detail='排队与执行进度继续在本卡片更新。'
    elif astate=='PAUSED':
        state='PAUSED';label='自动执行已暂停';detail='材料保留，恢复后继续处理。';tone='grey'
        if aborted:action='上次材料接收未完成，可重发未成功的文件；当前保持暂停。'
    elif aborted:
        state='NEEDS_INPUT';label='材料接收未完成';tone='orange'
        detail='已收材料保留。';action='重发未成功的材料，或核对后发送“检查材料”。'
    elif astate=='AWAITING_MATERIAL':
        state='WAITING_SELECTION';label='执行目标已保存';detail='等待后续材料；也可发送“恢复自动执行”开始。本次尚未提交台架。'
        if missing:action='待补充：'+'、'.join(missing)+'。'
    elif astate=='NO_CHANGE':
        state='NO_CHANGE';label='无有效变更';detail='未重复执行台架。';tone='grey'
        action='需要按相同材料重跑时，发送“重新回灌”。'
    elif astate=='BLOCKED':
        state='NEEDS_INPUT';label='材料需要修正';tone='orange'
        issues=result.get('issues') or []
        action='\n'.join(str(i.get('message',''))+'；'+str(i.get('next_action','')) for i in issues[:3]) or auto['error'][:350]
        detail='补充或修正以下问题，其余材料保留。'
    elif pending:
        problems={'NEEDS_ROLE':'请说明是相机、雷达还是M100 SO。','NEEDS_TARGET':'请说明要替换的 APP 内 YAML 路径。','FAILED':'传输未完成，请重发或重试该附件。','RECEIVED':'正在转存到本人工作站目录。','STORED':'正在关联材料。'}
        detail='\n'.join(str(x['filename'])+'：'+problems.get(x['state'],'正在处理。') for x in pending[:3])
        if any(x['state']=='FAILED' for x in pending):state='NEEDS_INPUT';label='附件保存未完成';tone='orange'
    elif missing:action='待补充：'+'、'.join(missing)+'。'
    elif astate=='CHECKING':
        state='CHECKING';label='检查材料';progress=result.get('progress') or {};detail=progress.get('detail') or progress.get('phase') or '核验软件、依赖及配置。'
        if detail=='核验数据源与本次场景范围':
            detail='核对数据集清单、可读目录和本次场景范围；不下载数据。'
    else:
        state='WAITING';label='材料已收到';detail='材料短暂合并后检查，通过即自动排队；可在此时调整执行目标或暂缓。'
    summary=describe(store,row['owner'],task=task)
    from .card_ui import describe as ui_describe
    ui=ui_describe(cfg,store,task)
    if ui.get('editing') and state not in ('CANCELLED','TRANSFERRED'):
        state='EDITING';label='编辑任务材料';detail='保存后使用最新材料继续；编辑期间不提交台架。'
    elif ui.get('waiting_uploads') and state not in ('PAUSED','CANCELLED','TRANSFERRED'):
        from .card_ui import ROLES
        state='WAITING_FILES';label='等待补充附件';detail='请通过聊天窗口发送：'+'、'.join(ROLES[r] for r in ui['waiting_uploads'])+'。齐备后自动检查。'
    confirm_required=bool(cfg.get('require_material_confirmation'))
    confirm_ready=state not in ('CANCELLED','TRANSFERRED') and not missing and not pending and not aborted and not ui.get('editing') and astate!='BLOCKED'
    if confirm_required and state not in ('CANCELLED','TRANSFERRED','EDITING') and not ui.get('confirmed'):
        if astate=='BLOCKED':state='NEEDS_INPUT';label='请修正材料后确认';tone='orange'
        else:
            state='AWAITING_CONFIRMATION';label='请确认本次材料' if confirm_ready else '待补充材料';tone='blue' if confirm_ready else 'orange'
            detail='可继续发送或替换文件；点击“确认执行”后才检查并排队。'
            if missing:action='待补充：'+'、'.join(missing)+'。'
    if confirm_required and ui.get('editing') and state not in ('CANCELLED','TRANSFERRED'):detail='已有材料已回填；保存后仍需点击“确认执行”。'
    if confirm_required and ui.get('confirmed') and state=='WAITING':
        label='材料已确认';detail='正在准备检查，通过后进入队列。'
    from .card_materials import describe as material_display
    display=material_display(store,row['owner'],task=task,summary=summary,files=ui.get('files',[]))
    from .task_cards import duration
    from .task_refs import reference
    return {'task_id':task['id'],'reference':reference(store,row['owner'],task_id=task['id']),'revision':task['revision'],'state':state,'label':label,'tone':tone,
            'detail':detail,'action':action[:450],'input_summary':summary,'complete':not missing and not pending,
            'software':summary.get('software','台架回灌'),'dataset':summary.get('dataset','待明确数据集'),
            'job':task['id'],'completed':0,'total':0,'elapsed':duration((time.time() if state=='CHECKING' else task['updated'])-task['created']),
            'task':'','archive':'','precheck':'待完成','demo':bool(cfg.get('presentation_demo')),'phase_rank':0,
            'evaluate_after':draft.get('goal')=='replay_and_evaluate','observed':prep['updated'] if prep and result else task['updated'],
            'input_phase':True,'card_ui':ui,'confirmation_required':confirm_required,'confirm_ready':confirm_ready,'material_display':display,
            'feedback_current':ui.get('feedback_at',0)>=task['updated'],
            'control_options':'' if ui else '可发送“仅回灌”或“回灌后评测”选择目标；“暂缓执行”保留材料，等待继续。'}


def handoff(store):
    """Freeze card identity before native notifications; wait for any in-flight input PATCH."""
    blocked=set();schema(store)
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        rows=c.execute('''SELECT i.*,t.job_id,t.revision AS current_revision,j.session AS job_session
          FROM input_cards i JOIN task_requests t ON t.id=i.task_id AND t.owner=i.owner
          JOIN jobs j ON j.id=t.job_id AND j.owner=t.owner WHERE i.state!='TRANSFERRED' ''').fetchall()
        for row in rows:
            if row['session']!=row['job_session']:continue
            if row['lease_until']>time.time():blocked.add(row['job_id']);continue
            # Repaired input must start a fresh card, preserving the old error card.
            reuse=row['message_id'] and not row['reopen']
            if reuse:c.execute('UPDATE task_cards SET message_id=? WHERE job=? AND message_id IS NULL',(row['message_id'],row['job_id']))
            c.execute("UPDATE input_cards SET state='TRANSFERRED' WHERE task_id=? AND owner=?",(row['task_id'],row['owner']))
    return blocked


def deliver(cfg,store,*,deadline=None,client=None,fallback=None,task_id=None):
    if not enabled(cfg):return 0
    from .task_cards import _client,render,DeadlineReached
    from .service import identity
    schema(store);client=client or _client(cfg);count=0
    with store.connect() as c:
        rows=[dict(x) for x in c.execute("SELECT * FROM input_cards WHERE state!='TRANSFERRED' AND lease_until<=? AND next_attempt<=?"+(' AND task_id=?' if task_id else '')+' ORDER BY last_sent LIMIT 20',(time.time(),time.time(),*([task_id] if task_id else [])))]
    for row in rows:
        if deadline and deadline-time.monotonic()<.3:break
        if identity(row['session'])!=row['owner']:continue
        value=snapshot(cfg,store,row)
        if not row['surface_requested'] and value.get('card_ui',{}).get('editing') and row['state']=='EDITING' and row['message_id'] and row['last_sent']>=value['card_ui']['feedback_at']:
            continue  # Keep in-progress client inputs intact; save/cancel refreshes the card.
        if value['state']=='TRANSFERRED':continue
        if not row['surface_requested'] and row['message_id'] and row['revision']==value['revision'] and row['state']==value['state'] and time.time()-row['last_sent']<5:continue
        token=uuid.uuid4().hex;previous_surface=None
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('UPDATE input_cards SET lease_token=?,lease_until=? WHERE task_id=? AND owner=? AND lease_until<=?',(token,time.time()+30,row['task_id'],row['owner'],time.time())).rowcount:continue
            row=dict(c.execute('SELECT * FROM input_cards WHERE task_id=? AND owner=?',(row['task_id'],row['owner'])).fetchone())
            if row['surface_requested']:previous_surface=row['message_id']
            if row['surface_requested'] or row['reopen'] and value['complete'] and value['state'] not in ('NEEDS_INPUT','MATERIALS','PAUSED'):
                history=json.loads(row['history']);history.append({'generation':row['generation'],'message_id':row['message_id'],'revision':row['revision']})
                row.update(generation=row['generation']+1,message_id=None,digest='',history=json.dumps(history[-30:]),reopen=0,surface_requested=0)
                c.execute('UPDATE input_cards SET generation=?,message_id=NULL,digest=?,history=?,reopen=0,surface_requested=0 WHERE task_id=? AND lease_token=?',(row['generation'],'',row['history'],row['task_id'],token))
        if previous_surface and cfg.get('inline_material_card'):
            from .inline_materials import reset as reset_inline
            reset_inline(store,row['task_id']);value=snapshot(cfg,store,row)
        ok=False;aborted=False;mid=row['message_id'];digest='';sampled=time.time()
        def budget():
            remaining=deadline-time.monotonic() if deadline else 8
            if remaining<.2:raise DeadlineReached()
            return min(8,remaining)
        try:
            serialized=json.dumps(render(value),ensure_ascii=False,separators=(',',':'));digest=hashlib.sha256(serialized.encode()).hexdigest()
            if digest!=row['digest']:
                client.auth(timeout=budget())
                if mid:
                    if cfg.get('inline_material_card'):
                        from .card_entities import update
                        update(store,client,mid,json.loads(serialized),budget)
                    else:client._json('/im/v1/messages/'+quote(mid,safe=''),method='PATCH',body=json.dumps({'content':serialized}).encode(),timeout=budget())
                else:
                    key=hashlib.sha256((str(cfg.get('feishu_app_id'))+':input:'+row['owner']+':'+row['task_id']+':'+str(row['generation'])).encode()).hexdigest()[:32]
                    if cfg.get('inline_material_card'):
                        from .card_entities import send
                        mid=send(store,client,row['session'].split(':')[1],json.loads(serialized),key,budget)
                    else:
                        response=client._json('/im/v1/messages?receive_id_type=chat_id',body=json.dumps({'receive_id':row['session'].split(':')[1],'msg_type':'interactive','content':serialized,'uuid':key},ensure_ascii=False).encode(),timeout=budget())
                        mid=response['data']['message_id']
                count+=1
            ok=True
            if previous_surface and previous_surface!=mid:
                try:
                    old_value={**value,'label':'材料确认卡已更新','detail':'请使用聊天底部的最新材料卡。','tone':'grey',
                               'confirm_ready':False,'card_ui':{**(value.get('card_ui') or {}),'editing':False,'files':[]}}
                    old_card=render(old_value);old_card['body']['elements']=old_card['body']['elements'][:2]+[
                        {'tag':'markdown','content':'此卡片已停用，已登记材料保留。','text_size':'notation'}]
                    if cfg.get('inline_material_card'):
                        from .card_entities import update
                        # A form-to-form partial receipt update intentionally
                        # preserves client inputs. Retire the old form entirely.
                        old_card['body']['elements']=[{'tag':'markdown','content':'此卡片已停用，请使用聊天底部的最新材料卡。','text_size':'notation'}]
                        update(store,client,previous_surface,old_card,budget)
                    else:client._json('/im/v1/messages/'+quote(previous_surface,safe=''),method='PATCH',
                                      body=json.dumps({'content':json.dumps(old_card,ensure_ascii=False,separators=(',',':'))}).encode(),timeout=budget())
                except Exception:pass  # Current message identity rejects old callbacks even if this cosmetic update fails.
        except DeadlineReached:aborted=True
        except Exception:pass
        with store.connect() as c:
            if ok:c.execute('''UPDATE input_cards SET message_id=?,digest=?,last_sent=?,revision=?,state=?,reopen=?,attempts=0,next_attempt=0,
              last_activity=CASE WHEN last_activity=0 THEN ? ELSE last_activity END,
              lease_token=NULL,lease_until=0 WHERE task_id=? AND lease_token=?''',(mid,digest,sampled,value['revision'],value['state'],int(value['state']=='NEEDS_INPUT' or row['reopen']),sampled,row['task_id'],token))
            elif aborted:c.execute('UPDATE input_cards SET lease_token=NULL,lease_until=0 WHERE task_id=? AND lease_token=?',(row['task_id'],token))
            else:c.execute('UPDATE input_cards SET message_id=?,attempts=attempts+1,next_attempt=?,lease_token=NULL,lease_until=0 WHERE task_id=? AND lease_token=?',(mid,time.time()+min(120,5*2**min(row['attempts'],5)),row['task_id'],token))
        if not ok and not aborted and fallback and row['attempts']>=2 and time.time()-row['fallback_at']>=120:
            try:
                if fallback({'session':row['session'],'text':value['label']+'。'+value['detail']+'\n'+value['action'],'attachment':None}):
                    with store.connect() as c:c.execute('UPDATE input_cards SET fallback_at=? WHERE task_id=?',(time.time(),row['task_id']))
            except Exception:pass
    handoff(store)
    return count


def present(cfg,store,user,session,*,surface=False):
    task=Tasks(store).current(user)
    if not ensure(cfg,store,user,session,task):return False
    activity(cfg,store,user,session,task,force=surface)
    deadline=time.monotonic()+9
    deliver(cfg,store,deadline=deadline,task_id=task['id'])
    # The worker may already own this card's send lease. Wait briefly for that
    # exact update instead of falling back to a second receipt message.
    while True:
        current=Tasks(store).current(user)
        if not current or current['id']!=task['id'] or current['job_id']:return False
        with store.connect() as c:row=c.execute('SELECT * FROM input_cards WHERE task_id=? AND owner=?',(task['id'],user)).fetchone()
        if not row:return False
        row=dict(row)
        if row['message_id'] and row['revision']==current['revision'] and row['attempts']==0 and not row['surface_requested'] and row['lease_until']<=time.time():
            from .task_cards import render
            value=snapshot(cfg,store,row)
            expected=hashlib.sha256(json.dumps(render(value),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            if row['digest']==expected:return True
        if row['lease_until']<=time.time() or time.monotonic()+.1>=deadline:return False
        time.sleep(.05)


def route(store,row,job=None):
    """CARD consumes duplicate notices; WAIT retains fallback; TEXT is legacy delivery."""
    with store.connect() as c:
        if job:
            source=c.execute('SELECT owner,payload FROM jobs WHERE id=?',(job,)).fetchone()
            payload=json.loads(source['payload']) if source else {}
            if payload.get('action')=='evaluate' and payload.get('automatic') and payload.get('source_job'):
                parent=c.execute('SELECT id FROM jobs WHERE id=? AND owner=?',(payload['source_job'],source['owner'])).fetchone()
                if parent:job=parent['id']
            elif payload.get('action')!='replay':return 'TEXT'
            card=c.execute('SELECT * FROM task_cards WHERE job=?',(job,)).fetchone()
            if card and card['message_id']:return 'CARD'
            return 'TEXT' if card and card['attempts']>=3 else 'WAIT'
        key=row.get('event_key','');tid=None
        if key.startswith(('auto-submitted:','auto-blocked:','no-change:')):tid=key.split(':')[1]
        elif key.startswith(('preparation:','preparation-wait:')):
            p=c.execute('SELECT request_id FROM preparations WHERE id=? AND owner=?',(key.split(':')[1],row['owner'])).fetchone();tid=p['request_id'] if p else None
        elif key.startswith('att-'):
            p=c.execute('SELECT task_id FROM task_attachments WHERE id=? AND owner=?',(key,row['owner'])).fetchone();tid=p['task_id'] if p else None
        elif key.startswith('parse-wait:'):
            p=c.execute('SELECT task_id FROM task_focus WHERE owner=?',(row['owner'],)).fetchone();tid=p['task_id'] if p else None
        if not tid:return 'TEXT'
        card=c.execute('SELECT * FROM input_cards WHERE task_id=? AND owner=? AND session=?',(tid,row['owner'],row['session'])).fetchone()
        if not card:return 'TEXT'
        if card['message_id'] or card['fallback_at']:return 'CARD'
        if card['state']=='TRANSFERRED':
            p=c.execute('SELECT job_id FROM task_requests WHERE id=? AND owner=?',(tid,row['owner'])).fetchone()
            return route(store,row,job=p['job_id']) if p and p['job_id'] else 'WAIT'
        return 'TEXT' if card['attempts']>=3 else 'WAIT'
