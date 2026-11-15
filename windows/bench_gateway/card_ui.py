"""Native card input and controls. Files remain real Feishu chat attachments."""
import base64
import hashlib
import hmac
import json
from pathlib import Path,PurePosixPath
from urllib.parse import urlsplit
import time
import uuid
from .core import GatewayError

PREFIX='@@BENCH_CARD@@'
ROLES={'camera':'相机 SO','lidar':'雷达 SO','m100':'M100 SO','yaml':'YAML'}

def enabled(cfg):return bool(cfg and cfg.get('card_callbacks_enabled'))

def schema(store):
    from .tasks import Tasks
    Tasks(store)
    with store.connect() as c:c.executescript('''
      CREATE TABLE IF NOT EXISTS card_ui(task_id TEXT PRIMARY KEY REFERENCES task_requests(id),owner TEXT NOT NULL,
        nonce TEXT NOT NULL UNIQUE,editing INTEGER NOT NULL DEFAULT 0,opened REAL NOT NULL DEFAULT 0,
        opened_revision INTEGER NOT NULL DEFAULT 0,opened_values TEXT NOT NULL DEFAULT '{}',
        prior_auto TEXT NOT NULL DEFAULT '',feedback TEXT NOT NULL DEFAULT '',feedback_at REAL NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS card_callback_events(event_id TEXT PRIMARY KEY,owner TEXT NOT NULL,
        state TEXT NOT NULL,reply TEXT NOT NULL DEFAULT '',created REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS card_expected_uploads(task_id TEXT NOT NULL,owner TEXT NOT NULL,
        role TEXT NOT NULL,after_time REAL NOT NULL,PRIMARY KEY(task_id,role));''')
    with store.connect() as c:
        if 'opened_revision' not in {r['name'] for r in c.execute('PRAGMA table_info(card_ui)')}:
            c.execute('ALTER TABLE card_ui ADD COLUMN opened_revision INTEGER NOT NULL DEFAULT 0')
        if 'opened_values' not in {r['name'] for r in c.execute('PRAGMA table_info(card_ui)')}:
            c.execute("ALTER TABLE card_ui ADD COLUMN opened_values TEXT NOT NULL DEFAULT '{}'")

def ensure(store,task):
    schema(store)
    with store.connect() as c:
        c.execute('INSERT OR IGNORE INTO card_ui(task_id,owner,nonce) VALUES(?,?,?)',(task['id'],task['owner'],uuid.uuid4().hex))
        return dict(c.execute('SELECT * FROM card_ui WHERE task_id=? AND owner=?',(task['id'],task['owner'])).fetchone())

def upload_waits(c,user,tid):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='card_expected_uploads'").fetchone():return []
    rows=c.execute('SELECT role,after_time FROM card_expected_uploads WHERE owner=? AND task_id=?',(user,tid)).fetchall()
    waiting=[]
    for row in rows:
        received=c.execute("SELECT 1 FROM task_attachments WHERE owner=? AND task_id=? AND created>=? AND state IN ('ASSOCIATED','DUPLICATE') AND json_extract(data,'$.role')=?",(user,tid,row['after_time'],row['role'])).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='task_attachments'").fetchone() else None
        if not received:waiting.append(row['role'])
    return waiting

def blocks(c,user):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='card_ui'").fetchone():return False
    row=c.execute('SELECT u.task_id,u.editing FROM card_ui u JOIN task_focus f ON f.task_id=u.task_id AND f.owner=u.owner WHERE u.owner=?',(user,)).fetchone()
    return bool(row and (row['editing'] or upload_waits(c,user,row['task_id'])))

def describe(cfg,store,task,*,job=None,evaluation_state=''):
    if not enabled(cfg) or not task:return {}
    ui=ensure(store,task)
    with store.connect() as c:
        files=[dict(r) for r in c.execute('SELECT id,filename,state,data FROM task_attachments WHERE owner=? AND task_id=? ORDER BY created DESC LIMIT 8',(task['owner'],task['id']))] if c.execute("SELECT 1 FROM sqlite_master WHERE name='task_attachments'").fetchone() else []
        waits=upload_waits(c,task['owner'],task['id'])
    draft=task['data']
    from .confirmations import enabled as needs_confirmation,active
    with store.connect() as c:confirmation=active(c,task['owner'],task) if needs_confirmation(cfg) else None
    materials={}
    with store.connect() as c:
        for role,field in [('camera','camera_so'),('lidar','lidar_so'),('m100','m100_so')]:
            source=draft.get(field,'');row=c.execute("SELECT data FROM assets WHERE id=? AND owner=? AND kind='dependency'",(source,task['owner'])).fetchone()
            data=json.loads(row['data']) if row else {}
            filename=data.get('filename')
            if not filename and source:
                raw=urlsplit(source).path if source.startswith(('https://','http://')) else source
                if raw.endswith('.so'):filename=PurePosixPath(raw.replace('\\','/')).name
            current=filename or ('已选择本人材料' if source else '自动准备（已支持版本）' if role=='m100' and draft.get('m100_mode')=='build_if_missing' else '待提供')
            if role in waits:current+='；等待替换附件' if source else '；等待附件'
            materials[ROLES[role]]=current
    inline={}
    if cfg.get('inline_material_card') and not task.get('job_id'):
        from .inline_materials import describe as inline_describe
        inline=inline_describe(cfg,store,task)
    return {**ui,**inline,'draft_goal':draft.get('goal','replay_and_evaluate'),'files':files,'waiting_uploads':waits,
            'materials':materials,'prefill':json.loads(ui['opened_values']) if ui['editing'] else form_values(store,task),
            'confirm_required':needs_confirmation(cfg),'confirmed':bool(confirmation),
            'job_state':job['state'] if job else '', 'evaluation_state':evaluation_state,
            'submitted':bool(task.get('job_id'))}

def form_values(store,task):
    from .material_summary import describe as summary,public_url
    d=task['data'];s=summary(store,task['owner'],task=task)
    app=s.get('software_url') or (d.get('package','') if not d.get('package','').startswith(('https://','http://')) else '')
    build=d.get('build_reference','')
    if build.startswith(('https://','http://')):build=public_url(build)
    values={'app_source':app,'build_reference':build,'dataset':s.get('dataset','') if d.get('dataset') else '',
            'scene_scope':d.get('scene_scope',''),'goal':d.get('goal','replay_and_evaluate'),
            'm100_mode':d.get('m100_mode','build_if_missing'),'replay_mode':d.get('replay_mode','stream')}
    return {k:v for k,v in values.items() if isinstance(v,str) and len(v)<=1000}

def key(cfg):
    raw=json.loads((Path(cfg['project_root'])/'runtime/credentials.json').read_text(encoding='utf-8'))
    if raw.get('app_id')!=cfg['feishu_app_id']:raise GatewayError('卡片应用身份不一致。')
    return raw['app_secret'].encode()

def decode(cfg,session,text,*,secret=None):
    if not enabled(cfg):raise GatewayError('卡片回调尚未启用，请先完成应用回调配置。')
    from .service import identity
    try:
        packet,signature=text[len(PREFIX):].rsplit('.',1)
        raw=base64.urlsafe_b64decode(packet+'='*(-len(packet)%4))
        if len(raw)>65536 or not hmac.compare_digest(hmac.new(secret or key(cfg),raw,hashlib.sha256).hexdigest(),signature):raise ValueError()
        data=json.loads(raw)
        user=identity(session);parts=session.split(':')
        if data['schema']!=1 or data['app_id']!=cfg['feishu_app_id'] or data['operator_id']!=user or data['chat_id']!=parts[1]:raise ValueError()
        if not isinstance(data['event_id'],str) or not data['event_id'] or len(data['event_id'])>200:raise ValueError()
        if not isinstance(data['value'],dict) or not isinstance(data.get('form_value') or {},dict):raise ValueError()
        return data
    except Exception:raise GatewayError('卡片来源或操作人核验未通过，本次未修改任务。') from None

def feedback(store,user,tid,text):
    now=time.time()
    with store.connect() as c:
        c.execute('UPDATE card_ui SET feedback=?,feedback_at=? WHERE task_id=? AND owner=?',(text[:500],now,tid,user))
        c.execute('UPDATE input_cards SET last_sent=0,next_attempt=0 WHERE task_id=? AND owner=?',(tid,user))
        row=c.execute('SELECT job_id FROM task_requests WHERE id=? AND owner=?',(tid,user)).fetchone()
        if row and row['job_id']:
            c.execute('UPDATE jobs SET updated=? WHERE id=? AND owner=?',(now,row['job_id'],user))
            c.execute('UPDATE task_cards SET last_sent=0,next_attempt=0 WHERE job=?',(row['job_id'],))

def handle(service,user,session,data):
    """Called before the model parser, after signature and operator validation."""
    cfg=service.cfg;store=service.store;schema(store)
    from .tasks import Tasks
    from .intake import Intake,validate
    from .input_cards import ensure as ensure_card
    from .autoflow import AutoFlow
    tasks=Tasks(store);nonce=data['value'].get('nonce','');tid=None
    with store.connect() as c:
        ui=c.execute('SELECT * FROM card_ui WHERE nonce=? AND owner=?',(nonce,user)).fetchone()
        if not ui:return '未生效：卡片已过期或不属于本人，请使用当前任务卡片。'
        ui=dict(ui);tid=ui['task_id']
        card=c.execute('SELECT i.message_id,i.session,t.job_id FROM input_cards i JOIN task_requests t ON t.id=i.task_id AND t.owner=i.owner WHERE i.task_id=? AND i.owner=?',(tid,user)).fetchone()
        if not card or card['session']!=session:return '未生效：该卡片不属于当前本人私聊。'
        native=c.execute('SELECT message_id FROM task_cards WHERE job=?',(card['job_id'],)).fetchone() if card and card['job_id'] else None
        if data['message_id'] not in {card['message_id'] if card else None,native['message_id'] if native else None}:return '未生效：这张卡片已被替换，请使用当前任务卡片。'
        c.execute('BEGIN IMMEDIATE')
        focus=c.execute('SELECT task_id FROM task_focus WHERE owner=?',(user,)).fetchone()
        if not card['job_id'] and (not focus or focus['task_id']!=tid):
            return '未生效：这份材料已切换，请使用当前材料卡；未覆盖当前任务。'
        old=c.execute('SELECT state,reply FROM card_callback_events WHERE event_id=?',(data['event_id'],)).fetchone()
        if old:
            service.defer_auto=True
            return '该操作已处理，当前状态以原卡片为准；未重复执行。' if old['state']=='DONE' else '该操作已收到，正在核对；不会重复提交任务。'
        c.execute("INSERT INTO card_callback_events(event_id,owner,state,created) VALUES(?,?,'PROCESSING',?)",(data['event_id'],user,time.time()))
    task=tasks.get(user,tid);action=data['action'];form=data.get('form_value') or {};refresh=True
    try:
        if action=='confirm_materials':
            if not cfg.get('inline_material_card'):raise GatewayError('卡内直接确认尚未启用。')
            from .inline_materials import submit
            try:
                current=submit(service,user,session,task,ui,data)
            finally:
                current=tasks.current(user)
                if current and current['id']!=tid:
                    tid=current['id'];ensure(store,current);ensure_card(cfg,store,user,session,current)
            service.defer_auto=True;text='已确认本次材料，开始检查；通过后进入执行队列。'
        elif action=='confirm_execution':
            if cfg.get('inline_material_card'):raise GatewayError('材料卡已升级，请使用当前输入卡的“确认执行”。')
            from .confirmations import confirm
            confirm(cfg,store,user,session,tid,data['value'].get('revision'),getattr(service,'input_batch_token',None))
            service.defer_auto=True;text='已确认本次材料，开始检查；通过后进入执行队列。'
        elif action=='set_goal':
            service.handle(session,'卡片修改本次目标',{'action':'set_goal','task_id':tid,'inputs':{'goal':data['value'].get('goal','')}})
            outcome=getattr(service,'execution_control',None)
            text=('已生效：' if outcome and outcome['state']=='APPLIED' else '当前选择：' if outcome and outcome['state']=='UNCHANGED' else '未生效：' if outcome else '已保存：')+(outcome['text'] if outcome else '执行目标已更新。')
        elif action=='open_edit' and cfg.get('inline_material_card'):
            if task['job_id']:
                tasks.select(user,tid);Intake(store).update(user,{},incoming_kind='card_edit')
                task=tasks.current(user);tid=task['id'];ensure(store,task);ensure_card(cfg,store,user,session,task)
                AutoFlow(cfg,store).arm(user,session,task)
            service.defer_auto=True;text='请在材料卡直接填写，文件通过聊天附件补充；点击“确认执行”后才开始。'
        elif action=='open_edit':
            if task['job_id']:
                focus=tasks.current(user)
                if focus and focus['id']!=tid and not focus['job_id']:
                    with store.connect() as c:lineage=c.execute('SELECT base_job_id FROM task_lineage WHERE task_id=? AND owner=?',(focus['id'],user)).fetchone()
                    if not lineage or lineage['base_job_id']!=task['job_id'] or focus['data'].get('package')!=task['data'].get('package'):raise GatewayError('已有其他待处理材料，请在当前材料卡继续；未覆盖已有内容。')
                    task=focus
                else:
                    tasks.select(user,tid);Intake(store).update(user,{},incoming_kind='card_edit');task=tasks.current(user)
                tid=task['id'];ui=ensure(store,task)
                ensure_card(cfg,store,user,session,task)
            else:tasks.select(user,tid)
            prefill=form_values(store,task)
            with store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                current=c.execute('SELECT job_id,revision FROM task_requests WHERE id=? AND owner=?',(tid,user)).fetchone()
                if current['job_id']:raise GatewayError('任务刚刚已提交，请点击“为下一次修改材料”。')
                if current['revision']!=task['revision']:raise GatewayError('材料刚刚已更新，请重新打开编辑卡，避免覆盖新材料。')
                if cfg.get('require_material_confirmation'):
                    from .confirmations import invalidate
                    invalidate(c,user,tid,'material editor opened')
                    c.execute("UPDATE automatic_tasks SET state='AWAITING_CONFIRMATION',updated=? WHERE task_id=? AND owner=?",(time.time(),tid,user))
                auto=c.execute('SELECT state FROM automatic_tasks WHERE task_id=?',(tid,)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_tasks'").fetchone() else None
                if not ui['editing']:c.execute('UPDATE card_ui SET editing=1,opened=?,opened_revision=?,opened_values=?,prior_auto=? WHERE task_id=? AND owner=?',(time.time(),current['revision'],json.dumps(prefill,ensure_ascii=False),auto['state'] if auto else '',tid,user))
            service.defer_auto=True
            refresh=not bool(ui['editing']);text='编辑期间暂缓提交；保存后按实际材料继续，原任务不变。' if refresh else '材料输入卡已打开，请在现有表单继续编辑。'
        elif action in ('save_material','cancel_edit'):
            if task['job_id'] or not ui['editing']:raise GatewayError('输入卡已关闭或任务已提交，请重新打开材料编辑。')
            tasks.select(user,tid);values={};roles=[]
            material_arrived=task['revision']>ui['opened_revision']
            if action=='save_material':
                allowed={'app_source','build_reference','dataset','scene_scope','yaml_request','goal','m100_mode','pending_uploads'}
                if set(form)-allowed:raise GatewayError('表单包含未知字段，本次未保存。')
                initial=json.loads(ui['opened_values'])
                for field,raw in form.items():
                    if field=='pending_uploads':
                        if not isinstance(raw,list) or any(role not in ROLES for role in raw):raise GatewayError('待上传文件选项无效。')
                        roles=raw;continue
                    if not isinstance(raw,str) or len(raw)>4000:raise GatewayError('表单值类型或长度不符合要求。')
                    value=raw.strip();name='package' if field=='app_source' else field
                    if value==initial.get(field):continue
                    if value and value!=task['data'].get(name):values[name]=value
                values=validate(values)
                if values and form.get('goal'):values['goal']=form['goal']
                if values:service.update_inputs(user,values)
            task=tasks.current(user)
            if task['id']!=tid:
                with store.connect() as c:c.execute('UPDATE card_ui SET editing=0 WHERE task_id=? AND owner=?',(tid,user))
                feedback(store,user,tid,'新 APP 已建立独立版本材料，请在新卡片继续。')
                tid=task['id'];ensure(store,task);ensure_card(cfg,store,user,session,task)
            with store.connect() as c:
                c.execute('UPDATE card_ui SET editing=0 WHERE task_id=? AND owner=?',(tid,user))
                if action=='save_material':
                    if cfg.get('require_material_confirmation'):roles=[]
                    for old in c.execute('SELECT role FROM card_expected_uploads WHERE task_id=? AND owner=?',(tid,user)).fetchall():
                        if old['role'] not in roles:c.execute('DELETE FROM card_expected_uploads WHERE task_id=? AND owner=? AND role=?',(tid,user,old['role']))
                    for role in roles:c.execute('INSERT OR IGNORE INTO card_expected_uploads VALUES(?,?,?,?)',(tid,user,role,ui['opened']))
            effective=bool(set(values)-{'goal','build_reference','software_name'}) or material_arrived
            if cfg.get('require_material_confirmation'):
                AutoFlow(cfg,store).arm(user,session,task);service.defer_auto=True
            elif not effective and ui['prior_auto'] not in ('WAITING','CHECKING'):
                flow=AutoFlow(cfg,store);flow.arm(user,session,task)
                with store.connect() as c:c.execute("UPDATE automatic_tasks SET state='AWAITING_MATERIAL' WHERE task_id=? AND state!='PAUSED'",(tid,))
                service.defer_auto=True
            elif effective:
                AutoFlow(cfg,store).arm(user,session,task)
            text='已保存等待附件的选择；本人已有材料保留。' if roles else '已保存本次材料；齐备后自动检查。' if effective else '目标或构建信息已保存；未修改执行材料，不会自动重跑。' if values else '编辑已关闭，材料没有变化，未新建执行任务。'
            if cfg.get('require_material_confirmation'):text='材料已保存，请核对后点击“确认执行”。' if values or material_arrived else '编辑已关闭，请核对卡片；只有确认后才执行。'
        elif action in ('pause','resume'):
            if task['job_id']:raise GatewayError('本任务已提交，暂缓材料不会中断已执行阶段。')
            tasks.select(user,tid);text=AutoFlow(cfg,store).control(user,session,action=='pause')
        elif action=='arrival_accuracy':
            if not cfg.get('arrival_analysis_enabled',True):raise GatewayError('到点精度分析当前未启用。')
            if not task['job_id']:raise GatewayError('尚无已交付回放结果可计算。')
            from .arrival_analysis import enqueue
            request=enqueue(store,user,store.job(user,task['job_id']))
            refresh=False;service.defer_auto=True
            text='到点精度分析已登记，状态将在原卡更新；不会重新回放。' if request['state']=='QUEUED' else '该任务已有到点分析记录，状态以原卡为准；未重复计算。'
        elif action=='evaluate':
            if not task['job_id']:raise GatewayError('尚无回放结果可评测。')
            text=service.handle(session,'卡片追加已有结果评测',{'action':'evaluate','task_id':task['job_id']})
        elif action=='resolve_file':
            if task['job_id']:raise GatewayError('任务已提交，文件修改请用于下一次任务。')
            from .attachments import Attachments
            inbox=Attachments(cfg,store,service.registry);aid=data['value'].get('attachment_id','');row=inbox.row(user,aid)
            if row['task_id']!=tid:raise GatewayError('附件不属于这份材料。')
            tasks.select(user,tid);text=inbox.associate(user,aid,role=form.get('role',''),target=form.get('target',''))
        else:raise GatewayError('卡片操作不受支持，本次未修改任务。')
    except GatewayError as exc:text='未生效：'+str(exc)
    if refresh:feedback(store,user,tid,text)
    with store.connect() as c:c.execute("UPDATE card_callback_events SET state='DONE',reply=? WHERE event_id=? AND owner=?",(text,data['event_id'],user))
    service.card_callback_result={'task_id':tid,'text':text,'action':action,
        'confirmed':action in ('confirm_materials','confirm_execution') and text.startswith('已确认本次材料')}
    return text

def present(cfg,store,user,session,result):
    from .tasks import Tasks
    task=Tasks(store).get(user,result['task_id'])
    if task['job_id']:
        from .task_cards import deliver
        deliver(cfg,store,job_id=task['job_id'],deadline=time.monotonic()+6)
        with store.connect() as c:
            row=c.execute('SELECT * FROM task_cards WHERE job=?',(task['job_id'],)).fetchone()
            ui=c.execute('SELECT feedback_at FROM card_ui WHERE task_id=?',(task['id'],)).fetchone()
        return bool(row and row['message_id'] and not row['attempts'] and row['last_sent']>=ui['feedback_at'])
    from .input_cards import present as input_present
    if input_present(cfg,store,user,session):return True
    # A confirmed card can advance to the next check phase while present()
    # compares digests. That phase change is not an undelivered confirmation.
    # Only accept a delivered, same-revision acknowledgement for this owner;
    # other callbacks and failed card deliveries retain their text fallback.
    if not result.get('confirmed'):return False
    with store.connect() as c:
        row=c.execute('''SELECT i.*,u.feedback_at,t.revision AS current_revision,t.job_id
          FROM input_cards i JOIN card_ui u ON u.task_id=i.task_id AND u.owner=i.owner
          JOIN task_requests t ON t.id=i.task_id AND t.owner=i.owner
          WHERE i.task_id=? AND i.owner=? AND i.session=?''',(task['id'],user,session)).fetchone()
        entities_exist=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_entities'").fetchone()
        entity=c.execute('SELECT card_json FROM card_entities WHERE message_id=?',(row['message_id'],)).fetchone() if row and row['message_id'] and cfg.get('inline_material_card') and entities_exist else None
    if not row or row['job_id']:return False
    acknowledged=(row['message_id'] and row['revision']==row['current_revision'] and row['attempts']==0
        and row['state'] in ('WAITING','CHECKING') and not row['surface_requested']
        and row['lease_until']<=time.time() and row['last_sent']>=row['feedback_at'])
    if not acknowledged:return False
    if cfg.get('inline_material_card'):
        from .card_entities import find
        # A successfully replaced confirmation form is the visible ack; merely
        # having sent the older, still editable form cannot suppress a failure.
        return bool(entity and find(json.loads(entity['card_json']),'bench_form') is None)
    return True

def button(label,action,ui,*,primary=False,**values):
    return {'tag':'button','text':{'tag':'plain_text','content':label},'type':'primary_filled' if primary else 'default',
            'behaviors':[{'type':'callback','value':{'action':'bench:'+action,'nonce':ui['nonce'],**values}}]}

def input_field(name,label,placeholder):
    return {'tag':'input','name':name,'label':{'tag':'plain_text','content':label},'placeholder':{'tag':'plain_text','content':placeholder},'max_length':1000}

def material_form(ui):
    fields=[input_field('app_source','LIVC APP 下载链接','已回填本人版本；仅更换软件时修改'),
            input_field('build_reference','构建链接或编号（选填）','版本追溯；不等同于 M100 编译源码'),
            input_field('dataset','数据集','留空沿用；可填写已接入数据源或工作站目录'),
            input_field('scene_scope','场景范围','留空沿用；例如全部或1,2,3'),
            {'tag':'select_static','name':'goal','initial_option':ui['draft_goal'],'options':[
                {'text':{'tag':'plain_text','content':'回放后评测'},'value':'replay_and_evaluate'},
                {'text':{'tag':'plain_text','content':'仅回放'},'value':'replay'}]},
            {'tag':'select_static','name':'m100_mode','initial_option':ui.get('prefill',{}).get('m100_mode','build_if_missing'),'options':[
                {'text':{'tag':'plain_text','content':'M100 自动准备（已支持版本）'},'value':'build_if_missing'},
                {'text':{'tag':'plain_text','content':'M100 使用本人提供的 SO'},'value':'provided'}]},
            input_field('yaml_request','配置修改（可选）','需要修改时填写文件路径、字段与新值；不修改可留空'),
            {'tag':'multi_select_static','name':'pending_uploads','placeholder':{'tag':'plain_text','content':'等待后续附件（仅暂缓，不上传文件，可不选）'},'options':[
                {'text':{'tag':'plain_text','content':name},'value':role} for role,name in ROLES.items()]},
            {'tag':'button','name':'bench_save_'+ui['nonce'],'form_action_type':'submit','type':'primary_filled','width':'fill','text':{'tag':'plain_text','content':'保存修改'}}]
    if ui.get('waiting_uploads'):fields[-2]['selected_values']=ui['waiting_uploads']
    if ui.get('confirm_required'):fields=[f for f in fields if f.get('name')!='pending_uploads']
    for field in fields:
        value=ui.get('prefill',{}).get(field.get('name'))
        if field['tag']=='input' and value:field['default_value']=value
    return {'tag':'form','name':'bench_material_form','elements':fields,'vertical_spacing':'8px'}

def render_material_review(value):
    """A short receipt first, with one explicit execution button."""
    from .task_cards import escaped
    from .card_layout import status,materials,details,shell,feedback as current_feedback
    ui=value.get('card_ui') or {}
    inactive=value['state'] in ('CANCELLED','TRANSFERRED')
    if ui.get('inline') and not inactive:
        if ui.get('confirmed') or value['state']=='CHECKING':
            return shell(value,[status(value),materials(value),details(value)])
        from .inline_materials import render as render_inline
        return render_inline(value)
    problem_files=[f for f in ui.get('files',[]) if f['state'] not in ('ASSOCIATED','DUPLICATE','SUPERSEDED')]
    elements=[status(value),materials(value),details(value)]
    if inactive:controls=[]
    elif ui.get('editing'):
        elements.insert(1,material_form(ui));controls=[button('取消编辑','cancel_edit',ui)]
    else:
        confirm_button=button('已确认，处理中' if ui.get('confirmed') else '确认执行','confirm_execution',ui,primary=True,revision=value['revision'])
        if not value.get('confirm_ready') or ui.get('confirmed'):
            confirm_button.update(disabled=True,disabled_tips={'tag':'plain_text','content':'请先补齐待处理材料' if not ui.get('confirmed') else '已确认，请勿重复提交'})
        controls=[confirm_button,button('调整选项','open_edit',ui)]
    note='任务 '+escaped(value.get('reference',''))
    feedback=current_feedback(value)
    footer=[]
    if feedback and (ui.get('confirmed') or value.get('feedback_current')):footer.append({'tag':'markdown','content':escaped(feedback,240)})
    if controls:footer.append({'tag':'column_set','columns':[{'tag':'column','width':'weighted','weight':1,'elements':[b]} for b in controls]})
    footer.append({'tag':'markdown','content':note,'text_size':'notation'})
    elements.append({'tag':'column_set','columns':[{'tag':'column','width':'weighted','weight':1,'elements':footer}]})
    # Keep explicit role/target resolution on the same card for unknown names.
    pending=next((f for f in problem_files if f['state'] in ('NEEDS_ROLE','NEEDS_TARGET')),None)
    if pending and not inactive and not ui.get('editing'):
        role_choices=['yaml'] if pending['state']=='NEEDS_TARGET' else ['camera','lidar','m100']
        fields=[{'tag':'select_static','name':'role','required':True,'options':[{'text':{'tag':'plain_text','content':ROLES[r]},'value':r} for r in role_choices]}]
        if pending['state']=='NEEDS_TARGET':fields[0]['initial_option']='yaml';fields.append(input_field('target','APP 内 YAML 路径','例如 etc/uroad/config/custom.yaml'))
        fields.append({'tag':'button','name':'bench_file_'+ui['nonce']+'_'+pending['id'],'form_action_type':'submit','type':'primary_filled','text':{'tag':'plain_text','content':'关联附件'}})
        elements.insert(-1,{'tag':'form','name':'bench_file_form','elements':fields})
    return shell(value,elements)

def decorate(card,value):
    ui=value.get('card_ui') or {}
    if not ui:return card
    from .task_cards import escaped
    elements=card['body']['elements'];controls=[]
    if ui.get('editing'):
        elements[1]=material_form(ui)
        controls=[button('取消编辑','cancel_edit',ui)]
    elif value.get('input_phase') and value['state']!='CANCELLED':
        controls=[button('填写或修改信息','open_edit',ui,primary=True),button('继续处理' if value['state']=='PAUSED' or value['state']=='WAITING_SELECTION' else '暂缓执行','resume' if value['state'] in ('PAUSED','WAITING_SELECTION') else 'pause',ui)]
    elif value['state'] in ('QUEUED','RUNNING'):
        enable=not value['evaluate_after']
        controls=[button('回放后评测' if enable else '改为仅回放','set_goal',ui,primary=True,goal='replay_and_evaluate' if enable else 'replay'),button('为下一次修改材料','open_edit',ui)]
    elif value['state']=='POSTPROCESSING' and ui['evaluation_state']=='QUEUED':
        controls=[button('取消后续评测','set_goal',ui,primary=True,goal='replay')]
    elif value['state'] in ('SUCCEEDED','PARTIAL_SUCCESS','FAILED','CANCELLED'):
        controls=[button('为下一次修改材料','open_edit',ui,primary=True)]
        if value['state']=='SUCCEEDED' and not value['evaluate_after']:controls.append(button('评测已有结果','evaluate',ui))
    arrival=value.get('arrival') or {}
    if arrival:
        if arrival.get('detail'):
            labels={'QUEUED':'等待计算','RUNNING':'计算中','SUCCEEDED':'计算完成','NEEDS_INPUT':'无法计算','FAILED':'计算未完成','NEEDS_REVIEW':'结果待核验'}
            panel=elements[0]['columns'][0]
            panel['elements'][0:0]=[
                {'tag':'markdown','content':'**到点精度 · '+labels.get(arrival['state'],'处理中')+'**','text_size':'bench_status' if value.get('material_display') else 'heading-3'},
                {'tag':'markdown','content':escaped(arrival['detail'],220)}]
            if arrival['state'] in ('NEEDS_INPUT','FAILED','NEEDS_REVIEW'):panel['background_style']='yellow-50'
        b=button('计算到点精度','arrival_accuracy',ui)
        if arrival['state']!='AVAILABLE':
            b.update(disabled=True,disabled_tips={'tag':'plain_text','content':'已有分析记录，状态及报告以原卡为准；不会重复计算'})
        controls.append(b)
    footer=[]
    from .card_layout import feedback as current_feedback
    feedback=current_feedback(value) if value.get('material_display') else ui.get('feedback')
    if feedback:footer.append({'tag':'markdown','content':escaped(feedback,240)})
    if value.get('input_phase'):
        if ui.get('materials'):footer.append({'tag':'markdown','content':'\n'.join(name+'：'+escaped(text,180) for name,text in ui['materials'].items())})
        names={'ASSOCIATED':'已保存并关联','DUPLICATE':'已存在，无重复修改','RECEIVED':'转存中','STORED':'待关联','FAILED':'保存失败','NEEDS_ROLE':'待选择用途','NEEDS_TARGET':'待选择YAML目标','SUPERSEDED':'已被替换'}
        files=[f for f in ui.get('files',[]) if f['state']!='SUPERSEDED'][:4]
        if files:footer.append({'tag':'markdown','content':'\n'.join(escaped(f['filename'],80)+'：'+names.get(f['state'],'处理中') for f in files)})
        if ui.get('waiting_uploads'):footer.append({'tag':'markdown','content':'本次等待补充或替换：'+'、'.join(ROLES[r] for r in ui['waiting_uploads'])})
        footer.append({'tag':'markdown','content':'SO、YAML 请通过聊天窗口附件发送；保存到本人工作站后关联本任务。大数据集使用工作站目录或开发空间直传。','text_size':'notation'})
    elif value['state']=='POSTPROCESSING' and ui['evaluation_state']!='QUEUED':
        footer.append({'tag':'markdown','content':'评测已派发，当前阶段不可再修改；完成后更新结果。','text_size':'notation'})
    if controls:
        for b in controls:b['width']='fill'
        footer.append({'tag':'column_set','flex_mode':'stretch','columns':[{'tag':'column','width':'weighted','weight':1,'elements':[b]} for b in controls]})
    old=elements[-1];footer.append(old)
    elements[-1]={'tag':'column_set','columns':[{'tag':'column','width':'weighted','weight':1,'elements':footer}]}
    # Resolve one pending attachment at a time using a real form callback.
    pending=next((f for f in ui.get('files',[]) if f['state'] in ('NEEDS_ROLE','NEEDS_TARGET')),None)
    if pending and not ui.get('editing'):
        role_choices=['yaml'] if pending['state']=='NEEDS_TARGET' else ['camera','lidar','m100']
        fields=[{'tag':'select_static','name':'role','required':True,'options':[{'text':{'tag':'plain_text','content':ROLES[r]},'value':r} for r in role_choices]}]
        if pending['state']=='NEEDS_TARGET':fields[0]['initial_option']='yaml'
        if pending['state']=='NEEDS_TARGET':fields.append(input_field('target','APP 内 YAML 路径','例如 etc/uroad/config/custom.yaml'))
        fields.append({'tag':'button','name':'bench_file_'+ui['nonce']+'_'+pending['id'],'form_action_type':'submit','type':'primary_filled','text':{'tag':'plain_text','content':'关联附件'}})
        elements.insert(-1,{'tag':'form','name':'bench_file_form','elements':fields})
    return card
