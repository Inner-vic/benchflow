"""One durable private Card 2.0 per replay; coalesced updates and text fallback."""
import datetime
import hashlib
import html
import json
import re
import threading
import time
import uuid
from urllib.parse import quote

from .core import GatewayError
from .feishu_files import FeishuFiles

ACTIVE=('QUEUED','RUNNING','POSTPROCESSING')
_clients={};_clients_lock=threading.Lock()


class DeadlineReached(Exception):pass


def escaped(value,limit=160):
    value=html.escape(str(value)[:limit])
    for char in '*~[]()#_`':value=value.replace(char,'&#'+str(ord(char))+';')
    return value


def duration(seconds):
    seconds=max(0,int(seconds)//30*30)
    return (str(seconds//3600)+'小时'+str(seconds%3600//60)+'分' if seconds>=3600 else
            str(seconds//60)+'分'+str(seconds%60)+'秒' if seconds>=60 else str(seconds)+'秒')


def _fresh_resource(resource,now):
    if not resource:return None
    try:value=json.loads(resource['data'])
    except (ValueError,TypeError,KeyError):return None
    if not isinstance(value,dict):return None
    stamp=value.get('checked_at')
    if type(stamp) not in (int,float) or not -30<=now-stamp<=60:return None
    return value


def snapshot(store,job,now=None,cfg=None):
    now=time.time() if now is None else now;result=job.get('result') or {};payload=job['payload'];state=job['state']
    with store.connect() as c:
        phase=c.execute('SELECT * FROM task_progress WHERE job=?',(job['id'],)).fetchone()
        blocked=c.execute("SELECT 1 FROM jobs WHERE lane='hardware' AND state='NEEDS_REVIEW' LIMIT 1").fetchone()
        resource=c.execute('SELECT data FROM resource_status WHERE id=1').fetchone()
        waiting_ahead=0
        if state=='QUEUED' and payload.get('action')=='replay':
            waiting_ahead=c.execute("SELECT COUNT(*) FROM jobs WHERE lane='hardware' AND state='QUEUED' AND id!=? AND (created<? OR (created=? AND id<?))",(job['id'],job['created'],job['created'],job['id'])).fetchone()[0]
        evaluation=c.execute('SELECT state,updated FROM jobs WHERE id=? AND owner=?',(result['evaluation_job'],job['owner'])).fetchone() if result.get('evaluation_job') else None
        from .execution_controls import desired
        evaluate_after=desired(c,job)
    phase=dict(phase) if phase else {};progress=result.get('execution_progress') or {}
    total=len(payload.get('scenes') or []);completed=progress.get('completed',0)
    if type(completed) is not int:completed=0
    completed=max(0,min(completed,total))
    rank=phase.get('rank',0);tone='blue';action='';detail=''
    stage='结果评测' if payload.get('action')=='evaluate' else '数据回放' if rank>=25 else '启动预检' if rank>=20 else '软件部署'
    label=stage+'中'
    if state=='RUNNING':
        if rank>=60:label='回放收尾';detail='回放结果已核验，正在完成交付。'
        elif rank>=50:label='回放收尾';detail='正在收集结果并检查文件完整性。'
        elif rank>=45:label='回放收尾';detail='正在确认正式服务恢复和台架状态。'
        elif rank>=40:label='回放收尾';detail='场景回放结束，正在恢复台架正式服务。'
        elif rank>=30:detail='按本次选择的场景顺序回放。'
        elif rank>=25:label='启动预检通过';detail='正在准备正式回放。'
        elif rank>=20:
            selected=(payload.get('scenes') or [1])[0]
            detail='场景 '+str(selected)+'：检查软件启动、依赖和输出。通过后正式回放 '+str(total)+' 个场景。'
        else:detail='核对台架软件、依赖和本次配置，按需刷包部署。'
        if payload.get('action')=='evaluate':label='结果评测中';detail='基于已有回放结果执行评测，完成后交付原始 TXT。'
    if state=='QUEUED':
        label='就绪排队';queue_line='前方 '+str(waiting_ahead)+' 项等待。';action=queue_line
        r=_fresh_resource(resource,now) if payload.get('action')=='replay' else None
        if r:
            if r.get('state') not in ('READY','RESOURCE_BUSY'):
                from .availability import description
                action=queue_line+'\n'+description(r.get('state','UNKNOWN'))
                waiting={'BENCH_UNREACHABLE':'等待台架连接','WORKSTATION_UNAVAILABLE':'等待工作站连接',
                         'BENCH_SSH_FAILED':'台架连接待处理','STATUS_UNAVAILABLE':'台架状态待核验'}
                if r.get('state') in waiting:label=waiting[r['state']];tone='orange'
            elif r.get('state')=='RESOURCE_BUSY':
                action=queue_line+'\n台架占用中，等待资源释放。'
        if blocked:
            barrier='存在待核验任务，暂缓派发；待管理员处理。'
            action=action+' '+barrier if tone=='orange' else queue_line+'\n'+barrier
    elif state=='POSTPROCESSING':
        label='结果评测中';completed=total
        detail='回放已完成，评测排队中。' if result.get('evaluation_state')=='QUEUED' else '回放结果目录可用，正在生成评测 TXT。'
    elif state in ('SUCCEEDED','PARTIAL_SUCCESS'):
        label='回放完成' if payload.get('action')=='replay' else '评测完成';tone='green';completed=total
        action=''
        if payload.get('action')=='replay' and result.get('report'):label='回放与评测完成'
        if state=='PARTIAL_SUCCESS':
            label='评测未完成' if evaluate_after else '后续处理需检查'
            tone='orange';action='回放结果已保留，请核对评测状态及 TXT 中的缺项。' if evaluate_after else '回放结果已保留，请核对未完成的处理。'
            if evaluate_after and result.get('report') and result.get('evaluation_state')=='PARTIAL':
                label='评测部分完成'
                if 'VISION_GT_MISSING' in (result.get('warnings') or []):
                    action='缺少对应视觉真值，视觉指标未完成。请提供视觉真值的工作站路径以补齐评测数据；已计算指标与缺项见 TXT，回放结果保留。'
    elif state=='SIMULATED':label='模拟验证完成';tone='grey';action='本次未刷包、未回放或评测。'
    elif state=='CANCELLED':label='已取消';tone='grey';action='未继续派发台架。'
    elif state in ('FAILED','NEEDS_REVIEW'):
        label=stage+('待核验' if state=='NEEDS_REVIEW' else '已停止');tone='red'
        diagnostic=result.get('diagnostics') or {}
        action=(diagnostic.get('summary') or '请管理员核对执行收据或补齐匹配材料。')+' '+(diagnostic.get('next_action') or '已收材料保留，无需重复提交整套材料。')
        if state=='NEEDS_REVIEW' and str(result.get('error','')).startswith('SSH 已断开，远端执行结果未知'):
            label='执行连接中断，待核验'
            action='执行连接已中断，当前结果未知。请现场同事检查工作站与台架连接，由管理员核对执行及恢复收据；连接恢复后不会自动重跑。已提交材料保留。'
    if state=='QUEUED' and payload.get('action')=='evaluate':label='等待评测';action='等待评测资源。'
    if state=='RUNNING' and str(result.get('stage','')).startswith('预检失败'):
        label=stage+'未通过';tone='orange';action='正在核对失败原因及台架恢复，正式回放未启动。'
    end=now if state in ACTIVE else evaluation['updated'] if evaluation and evaluate_after else result.get('replay_completed_at') or job['updated']
    start=phase.get('started') or job['created']
    input_summary=dict((payload.get('business_contract') or {}).get('input_summary') or {})
    from .material_summary import dataset_details
    ds=(payload.get('assets') or {}).get('dataset') or {}
    if ds:input_summary.update(dataset_details(input_summary.get('dataset',''),ds,job['owner']))
    software=input_summary.get('software') or (payload.get('business_contract') or {}).get('software_name') or (payload.get('assets') or {}).get('package',{}).get('name') or '台架回放'
    dataset=(payload.get('assets') or {}).get('dataset',{}).get('name') or '已绑定数据源'
    assembly=(result.get('assembly') or {}).get('state')
    smoke=result.get('smoke') or {};binding=payload.get('baseline',{}).get('data',{}).get('binding',{})
    precheck='通过' if (smoke.get('state')=='PASS' and smoke.get('run_id')==job['id']+'-smoke'
                       and binding.get('input_digest') and smoke.get('input_digest')==binding['input_digest']) else '待完成'
    archive='已归档' if assembly=='SAVED' else '后台归档中' if result.get('assembly_pending') else '归档待核验，回放结果保留' if result.get('archive_state')=='NEEDS_REVIEW' else ''
    from .task_refs import reference
    ref=reference(store,job['owner'],job_id=job['id'])
    ui={}
    if cfg and cfg.get('card_callbacks_enabled'):
        from .tasks import Tasks
        from .card_ui import describe as ui_describe
        with store.connect() as c:linked=c.execute('SELECT id FROM task_requests WHERE job_id=? AND owner=?',(job['id'],job['owner'])).fetchone()
        if linked:ui=ui_describe(cfg,store,Tasks(store).get(job['owner'],linked['id']),job=job,evaluation_state=evaluation['state'] if evaluation else '')
    options=''
    if payload.get('action')=='replay':
        if state in ('QUEUED','RUNNING'):
            options='可发送“本次仅回放”或“本次回放后评测”调整后续步骤；已开始的回放继续执行。'
        elif state=='POSTPROCESSING':
            options='评测尚在排队，可发送“本次仅回放”取消后续评测。' if evaluation and evaluation['state']=='QUEUED' else '评测已派发，当前阶段的目标已锁定。'
        elif state=='SUCCEEDED' and not evaluate_after and ref:options='需要追加评测可发送“评测任务 '+ref+'”，复用已有回放结果。'
    # Real execution observations and terminal receipts supply status. The card
    # never derives success from a count, text message or timer.
    from .card_materials import describe as material_display
    display=material_display(store,job['owner'],job=job,summary=input_summary,files=ui.get('files',[]))
    queue_text=''
    if state in ('QUEUED','RUNNING') and job.get('lane','hardware')=='hardware':
        from .queue_status import QueueStatus
        queue_text=QueueStatus(store).roster_text(job['owner'],job['id'],card=True)
    from .arrival_analysis import view as arrival_view
    arrival=arrival_view(store,job) if cfg and cfg.get('card_callbacks_enabled') and cfg.get('arrival_analysis_enabled',True) else {}
    return {'job':job['id'],'reference':ref,'state':state,'label':label,'tone':tone,'completed':completed,'total':total,
            'elapsed':duration(end-start),'software':software,'dataset':dataset,'action':action[:500],
            'task':result.get('task','') if state in ('SUCCEEDED','PARTIAL_SUCCESS','POSTPROCESSING') else '',
            'archive':archive,'precheck':precheck,'demo':bool(payload.get('presentation_demo')),
            'phase_rank':phase.get('rank',0),'evaluate_after':evaluate_after,'control_options':'' if ui else options,'card_ui':ui,
            'observed':phase.get('observed') or job['updated'],'input_summary':input_summary,'material_display':display,
            'detail':detail,'started':phase.get('started'),'evaluation_only':payload.get('action')=='evaluate',
            'queue_text':queue_text,'arrival':arrival}


def workflow(value):
    """Observed milestones, not an estimate of time or a second execution state."""
    labels=['软件部署','启动预检','数据回放','结果评测']
    state=value['state'];rank=value.get('phase_rank',0)
    evaluation=bool(value.get('evaluate_after') or value.get('evaluation_only'))
    if value.get('evaluation_only'):
        status='done' if state=='SUCCEEDED' else 'blocked' if state in ('FAILED','NEEDS_REVIEW','PARTIAL_SUCCESS') else 'pending' if state in ('QUEUED','SIMULATED','CANCELLED') else 'active'
        return [{'label':'结果评测','status':status}]
    statuses=['pending']*4
    if not value.get('input_phase'):
        if state=='SUCCEEDED':statuses=['done']*4
        elif state=='QUEUED' and value.get('precheck')=='通过':statuses[:2]=['done','done']
        elif state not in ('QUEUED','SIMULATED','CANCELLED'):
            # Recovery, collection and output checks remain part of replay until
            # authoritative completion. Scene count alone cannot finish replay.
            current=2 if rank>=25 else 1 if rank>=20 else 0
            if state=='POSTPROCESSING' or state=='PARTIAL_SUCCESS' and evaluation:current=3
            statuses=['done' if i<current else 'active' if i==current else 'pending' for i in range(4)]
            if state in ('FAILED','NEEDS_REVIEW','PARTIAL_SUCCESS'):statuses[current]='blocked'
    if not evaluation:statuses[3]='skipped'
    return [{'label':label,'status':status,**({'hint':'已跳过'} if status=='skipped' else {})} for label,status in zip(labels,statuses)]


def scene_progress(value):
    """A bounded text bar, supported by the existing Markdown component."""
    if value.get('input_phase'):return escaped(value.get('detail',''),220)
    if value.get('evaluation_only'):
        if value['state']=='SIMULATED':return '本次为模拟验证，未执行评测。'
        if value['state']=='CANCELLED':return '本次评测已取消。'
        return '基于已有回放结果执行评测。'
    total=value['total'];done=value['completed'];state=value['state']
    if state=='SIMULATED':return '本次为模拟验证，未执行场景回放。'
    if not total:return '场景范围已绑定'
    if state=='QUEUED':return '正式回放尚未开始 · 共 '+str(total)+' 个场景'
    if state=='CANCELLED':return '本次已取消，未执行场景回放。'
    if state in ('RUNNING','FAILED','NEEDS_REVIEW') and value.get('phase_rank',0)<30:
        return escaped(value.get('detail') or '正式回放尚未开始。',220)
    width=min(total,12);filled=done*width//total;percent=done*100//total
    bar=("<font color='"+value['tone']+"'>"+'▰'*filled+'</font>' if filled else '')
    if width>filled:bar+="<font color='grey'>"+'▱'*(width-filled)+'</font>'
    text='场景回放 **'+str(done)+'/'+str(total)+'** · '+str(percent)+'%\n'+bar
    if done==total and state not in ('SUCCEEDED','PARTIAL_SUCCESS','POSTPROCESSING'):
        text+='\n场景回放结束，正在收尾，尚未完成。'
    if value.get('detail') and (value.get('phase_rank',0)>=40 or state=='POSTPROCESSING'):text+='\n'+escaped(value['detail'],180)
    return text


def workflow_columns(value):
    columns=[]
    for step in workflow(value):
        status=step['status'];symbol={'done':'✓','active':'●','pending':'○','blocked':'!','skipped':'—'}[status]
        color=value['tone'] if status in ('active','blocked') else 'grey'
        label='**'+step['label']+'**' if status in ('active','blocked') else step['label']
        columns.append({'tag':'column','width':'weighted','weight':1,'elements':[
            {'tag':'markdown','content':"<font color='"+color+"'>"+symbol+'</font>\n'+label+('\n'+step['hint'] if step.get('hint') else ''),
             'text_align':'center','text_size':'notation'}]})
    return {'tag':'column_set','flex_mode':'flow','horizontal_spacing':'4px','columns':columns}


def render(value):
    from .wording import display_text
    value=dict(value)
    for field in ('label','detail','action','stage','archive'):
        if field in value:value[field]=display_text(value[field])
    """Four compact blocks: phase/progress, nodes, required action/result, reference."""
    if value.get('input_phase') and value.get('confirmation_required'):
        from .card_ui import render_material_review
        card=render_material_review(value)
        if len(json.dumps(card,ensure_ascii=False).encode())>28*1024:raise GatewayError('材料确认卡片超过上限')
        return card
    if value.get('material_display'):
        from .card_layout import status,materials,details,shell,md
        from .card_ui import decorate
        note='任务 '+escaped(value.get('reference',''))
        card=shell(value,[status(value,scene_progress(value)),workflow_columns(value),materials(value),details(value),md(note,text_size='notation')])
        card=decorate(card,value)
        if len(json.dumps(card,ensure_ascii=False).encode())>28*1024:raise GatewayError('任务状态卡片超过上限')
        return card
    tone=value['tone'];demo='【展示验证】' if value['demo'] else ''
    if value['demo']:note='进度界面示意，仅验证显示，不操作台架。'
    else:note=('任务 '+escaped(value['reference'])) if value.get('reference') else '材料接收' if value.get('input_phase') else '任务记录'
    title=demo+'台架评测'
    primary={'tag':'column_set','flex_mode':'none','columns':[{'tag':'column','width':'weighted','weight':1,
        'background_style':tone+'-50' if tone in ('blue','green','red') else 'grey-50','padding':'12px','vertical_spacing':'4px',
        'elements':[{'tag':'markdown','content':'**'+escaped(value['label'])+'**','text_size':'heading-2'},
                    {'tag':'markdown','content':scene_progress(value)}]}]}
    if value.get('queue_text'):primary['columns'][0]['elements'].append({'tag':'markdown','content':value['queue_text'],'text_size':'notation'})
    inputs=value.get('input_summary') or {}
    from .material_summary import public_url
    software=str(value['software'])
    release=re.search(r'(SW\d+).*?\.(\d{8})-(\d+)\.app',software)
    if release:software=release[1]+' · 构建 '+release[3]
    software=escaped(software,120);url=public_url(inputs.get('software_url'))
    if url:
        from urllib.parse import quote
        software+='\n软件包链接：['+escaped(url,4000)+']('+quote(url,safe=':/%._~-')+')'
    goal='仅评测' if value.get('evaluation_only') else '回放后评测' if value.get('evaluate_after') else '仅回放'
    scope=inputs.get('scene_scope','')
    action='软件：'+software+'\n数据：'+escaped(inputs.get('dataset') or value['dataset'],70)+(' · '+escaped(scope,40) if scope else '')+'\n目标：'+goal
    if inputs.get('dataset_source'):action+='\n数据来源：'+escaped(inputs['dataset_source'],1000)
    if inputs.get('build_reference'):action+='\n构建来源：'+escaped(inputs['build_reference'],1000)
    if inputs.get('changes') and inputs['changes']!='沿用当前材料':action+='\n本次修改：'+escaped(inputs['changes'],100)
    if value['action']:action+='\n'+('待处理：' if value['state'] in ('FAILED','NEEDS_REVIEW','PARTIAL_SUCCESS','NEEDS_INPUT') else '')+escaped(value['action'],400 if value.get('input_phase') else 240)
    if value.get('task'):
        path=str(value['task']).replace('`','')
        action+='\n结果目录：\n`'+html.escape(path)+'`'
    if value.get('archive') and '待核验' in value['archive']:action+='\n装配：'+escaped(value['archive'])
    if value.get('control_options'):note+='\n'+escaped(value['control_options'],200)
    card={'schema':'2.0','config':{'update_multi':True,'width_mode':'default','enable_forward':False,
        'summary':{'content':demo+value['label']+(' · '+str(value['completed'])+'/'+str(value['total']) if not value.get('input_phase') and not value.get('evaluation_only') and value['state'] not in ('QUEUED','SIMULATED','CANCELLED') and (value.get('phase_rank',0)>=30 or value['state'] in ('SUCCEEDED','POSTPROCESSING','PARTIAL_SUCCESS')) else '')}},
        'header':{'title':{'tag':'plain_text','content':title},'template':tone,
                  'icon':{'tag':'standard_icon','token':'todo_colorful'}},
        'body':{'direction':'vertical','padding':'12px 12px 20px 12px','vertical_spacing':'12px','elements':[
            primary,workflow_columns(value),{'tag':'markdown','content':action},
            {'tag':'markdown','content':note,'text_size':'notation'}]}}
    from .card_ui import decorate
    card=decorate(card,value)
    if len(json.dumps(card,ensure_ascii=False).encode())>28*1024:raise GatewayError('任务状态卡片超过上限')
    return card


def _client(cfg):
    key=(cfg['project_root'],cfg.get('feishu_app_id'))
    with _clients_lock:
        if key not in _clients:_clients[key]=FeishuFiles(cfg)
        return _clients[key]


def deliver(cfg,store,*,deadline=None,client=None,fallback=None,job_id=None):
    from .input_cards import enabled,handoff
    blocked=handoff(store) if enabled(cfg) else set()
    if not cfg.get('task_cards_enabled') or not cfg.get('notifications_enabled'):return 0
    from .arrival_analysis import schema as arrival_schema
    arrival_schema(store)
    now=time.time();count=0;client=client or _client(cfg)
    def budget():
        remaining=8 if deadline is None else deadline-time.monotonic()
        if remaining<.2:raise DeadlineReached()
        return min(8,remaining)
    with store.connect() as c:
        rows=c.execute('''SELECT t.job FROM task_cards t JOIN jobs j ON j.id=t.job
          LEFT JOIN task_progress p ON p.job=t.job WHERE t.next_attempt<=? AND t.lease_until<=?
          AND (? IS NULL OR t.job=?)
          AND (t.last_sent=0 OR j.updated>t.last_sent OR p.observed>t.last_sent
               OR EXISTS(SELECT 1 FROM arrival_analyses a WHERE a.job=t.job AND a.updated>t.last_sent)
               OR (j.state IN ('QUEUED','RUNNING','POSTPROCESSING') AND t.last_sent<=?))
          ORDER BY CASE WHEN j.state IN ('FAILED','NEEDS_REVIEW','SUCCEEDED') THEN 0 ELSE 1 END,t.last_sent LIMIT 10''',
          (now,now,job_id,job_id,now-30)).fetchall()
    for candidate in rows:
        if candidate['job'] in blocked:continue
        try:budget()
        except DeadlineReached:break
        token=uuid.uuid4().hex
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            claimed=c.execute('UPDATE task_cards SET lease_token=?,lease_until=? WHERE job=? AND lease_until<=?',
                              (token,time.time()+30,candidate['job'],time.time())).rowcount
            if not claimed:continue
            row=dict(c.execute('SELECT * FROM task_cards WHERE job=?',(candidate['job'],)).fetchone())
            sampled_at=time.time()
            j=c.execute('SELECT * FROM jobs WHERE id=?',(candidate['job'],)).fetchone();job=store.unpack(j)
        success=False;digest=None;message_id=row['message_id'];value=None;aborted=False
        try:
            from .service import identity
            user=identity(job['session']);chat=job['session'].split(':')[1]
            if user!=job['owner']:raise GatewayError('状态卡片私聊归属不一致')
            store.require_user(user)
            value=snapshot(store,job,cfg=cfg);card=render(value)
            serialized=json.dumps(card,ensure_ascii=False,separators=(',',':'))
            digest=hashlib.sha256(serialized.encode()).hexdigest()
            if digest==row['sent_digest']:success=True
            else:
                client.auth(timeout=budget())
                if message_id:
                    if cfg.get('inline_material_card'):
                        from .card_entities import update
                        update(store,client,message_id,card,budget)
                    else:client._json('/im/v1/messages/'+quote(message_id,safe=''),method='PATCH',
                                      body=json.dumps({'content':serialized}).encode(),timeout=budget())
                else:
                    key=hashlib.sha256((str(cfg.get('feishu_app_id'))+':'+str(cfg.get('notification_namespace','default'))+':'+job['owner']+':'+job['id']).encode()).hexdigest()[:32]
                    response=client._json('/im/v1/messages?receive_id_type=chat_id',body=json.dumps(
                        {'receive_id':chat,'msg_type':'interactive','content':serialized,'uuid':key},ensure_ascii=False).encode(),timeout=budget())
                    message_id=response['data']['message_id']
                success=True;count+=1
        except DeadlineReached:aborted=True
        except Exception:pass
        with store.connect() as c:
            if success:
                c.execute('''UPDATE task_cards SET message_id=?,sent_digest=?,last_sent=?,attempts=0,next_attempt=0,
                  lease_token=NULL,lease_until=0 WHERE job=? AND lease_token=?''',
                  (message_id,digest,sampled_at,job['id'],token))
            elif aborted:
                c.execute('UPDATE task_cards SET lease_token=NULL,lease_until=0 WHERE job=? AND lease_token=?',(job['id'],token))
            else:
                # Persist any known message ID even when an update is uncertain;
                # future attempts always patch that card rather than create another.
                c.execute('''UPDATE task_cards SET message_id=?,attempts=attempts+1,next_attempt=?,lease_token=NULL,lease_until=0
                  WHERE job=? AND lease_token=?''',(message_id,time.time()+min(300,5*2**min(row['attempts'],6)),job['id'],token))
        if not success and fallback and value and row['attempts']>=2 and time.time()-row['fallback_at']>=120:
            try:
                budget();text='任务 '+job['id']+'：'+value['label']+'，已完成 '+str(value['completed'])+'/'+str(value['total'])+' 场景。'+value['action']
                if fallback({'session':job['session'],'text':text,'attachment':None,'job':job['id']}):
                    with store.connect() as c:c.execute('UPDATE task_cards SET fallback_at=? WHERE job=?',(time.time(),job['id']))
            except Exception:pass
    return count
