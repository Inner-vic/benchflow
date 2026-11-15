import hashlib
import json
import re
import time
from pathlib import Path

from .core import GatewayError, Store, within


def identity(session):
    parts = session.split(":")
    if len(parts) != 3 or parts[0] not in ("feishu", "lark"):
        raise GatewayError("仅支持带用户身份的飞书会话；共享会话和群话题入口已禁用")
    from .core import owner_key
    from .pcm_test1_ingress import is_pcm
    if is_pcm(parts[2]):
        raise GatewayError("内部入口标识不能作为飞书用户")
    owner_key(parts[2])
    return parts[2]


def validate_intent(value):
    keys = {"action", "package", "dependencies", "dataset", "baseline", "scenes", "task_id", "reply", "asset_kind", "asset_name", "asset_url", "asset_path", "manifest", "evaluate_after", "target", "inputs"}
    if not isinstance(value, dict) or set(value) - keys:
        raise GatewayError("不接受额外身份、路径或命令字段")
    if value.get("action") not in {"help", "assets", "assemblies", "queue", "status", "cancel", "replay", "evaluate", "evaluation_status", "register", "new_task", "prepare", "materials", "check_materials", "preparation_status", "pause_auto", "resume_auto", "repeat_replay", "set_goal", "open_materials"}:
        raise GatewayError("不支持的操作")
    for k in keys - {"dependencies", "scenes", "evaluate_after", "inputs"}:
        if k in value and (not isinstance(value[k], str) or len(value[k]) > 4000):
            raise GatewayError("任务字段类型或长度错误")
    scenes = value.get("scenes", [])
    if not isinstance(scenes, list) or len(scenes) > 1000 or any(type(x) is not int or x < 1 for x in scenes) or scenes != sorted(set(scenes)):
        raise GatewayError("场景编号必须为升序且不重复的正整数")
    deps = value.get("dependencies", [])
    if type(value.get('evaluate_after',False)) is not bool:
        raise GatewayError('evaluate_after 必须为布尔值')
    if not isinstance(deps, list) or len(deps) > 30 or any(not isinstance(x, str) or len(x) > 80 for x in deps) or len(deps) != len(set(deps)):
        raise GatewayError("依赖列表无效")
    from .intake import validate
    validate(value.get("inputs",{}))
    return value


def public_asset(a):
    # No local or remote path, source URL, or other users' catalog enters the model context.
    from .dataset_sources import reference
    return {**{k: a[k] for k in ("id", "kind", "version")},'name':reference(a),'scope':'shared' if a.get('owner')=='shared' else 'personal'}


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg["state_root"])
        self.store.request_scoped_supplements=bool(cfg.get('inline_material_card'))
        if cfg.get('require_explicit_dataset'):
            self.store.default_dataset_path=''
        elif cfg.get('inline_material_card') and cfg.get('default_dataset_path','').startswith('/'):
            self.store.default_dataset_path=cfg['default_dataset_path']
        from .confirmations import configure
        configure(cfg,self.store)
        from .dataset_sources import DatasetSources
        self.datasets=DatasetSources(cfg,self.store)
        self.registry=None
        if cfg.get('asset_backend')=='workstation':
            from .registry import Registry
            self.registry=Registry(cfg,self.store)

    def assets(self,user):
        snapshot=getattr(self,'_asset_snapshot',None)
        personal=list(snapshot[1]) if snapshot and snapshot[0]==user else (self.registry.list(user) if self.registry else self.store.list_assets(user))
        return personal+self.datasets.list(user)

    def assembly_text(self,user):
        from .assemblies import list_assemblies
        rows=list_assemblies(self.cfg,self.store,user)
        if not rows:
            return '本人已验证装配：暂无。原始 APP 入库或静态检查通过不算成功装配存档。\n请提供 CCU APP 来源及匹配三份 SO；真实回放成功后会保存本人装配包，后续无需重新下载。'
        return '本人已验证装配（仅本人可用）：\n'+'\n'.join(
            row['name']+' · v'+str(row['version'])+' · '+row['id'] for row in rows
        )+'\n使用“复用装配 <asm-id>”选择精确版本，再提供本次数据集、范围及目标；仍需本次材料检查和小场景预检。'

    def update_inputs(self,user,values,new=False,expected_task=None):
        """A selected software archive starts a fresh task, never a stale binding."""
        from .intake import Intake
        from .assemblies import resolve_assembly,selection_inputs
        from .tasks import Tasks
        before=Tasks(self.store).current(user)
        intake=Intake(self.store);values={k:v for k,v in (values or {}).items() if v}
        previous=intake.get(user) or {}
        if values.get('package'):
            from .app_sources import normalize
            original=values['package'];values['package']=normalize(self.cfg,original)
            if original!=values['package'] and before:
                from .material_summary import describe
                if describe(self.store,user,task=before).get('software_url')==values['package']:
                    values['package']=previous.get('package',values['package'])
        if values.get('assembly_ref'):
            saved=resolve_assembly(self.cfg,self.store,user,values['assembly_ref'])
            selected=selection_inputs(saved)
            if any(values.get(k) and values[k]!=selected[k] for k in ('package','camera_so','lidar_so','m100_so')):
                raise GatewayError('选择已装配存档时不能同时替换软件包或 SO。新组合请新建任务提供原包与依赖，重新核验后归档。')
            if values.get('m100_mode')=='build_if_missing':
                raise GatewayError('已装配存档使用冻结的 M100 成品；尝试其他构建请新建任务，不能沿用旧装配结论。')
            intake.update(user,{**selected,**values,'assembly_ref':selected['assembly_ref']},new=True,expected_task=expected_task)
        else:intake.update(user,values,new=new,expected_task=expected_task)
        current=Tasks(self.store).current(user)
        return {'next_request':bool(before and before['job_id'] and current['id']!=before['id']),
                'unchanged':bool(before and before['id']==current['id'] and before['revision']==current['revision']),
                'new_release':bool(not new and values.get('package') and previous.get('package') and values['package']!=previous['package']),
                'detached_archive':bool(previous.get('assembly_ref') and not current['data'].get('assembly_ref'))}

    def material_ack(self,user,values,change=None,new=False):
        """The ordinary chat shows what changed and what to send next, not a task sheet."""
        from .business import LABELS,missing_inputs
        from .intake import Intake
        change=change or {};draft=Intake(self.store).get(user) or {}
        if change.get('unchanged'):return '已收到，内容与当前材料一致，不重复提交。'
        if change.get('next_request'):first='已收到，修改用于下一次回放；当前执行保持不变。'
        elif change.get('new_release'):first='已收到新的软件包，已单独准备这一版。'
        elif values.get('assembly_ref'):first='已选择本人成功装配存档；APP 和三份 SO 使用已冻结版本。'
        elif new:first='已重新开始准备，直接发送软件包链接或 SO、YAML 即可。'
        else:
            names=[LABELS.get(k,k) for k,v in values.items() if v and k in LABELS]
            first='已更新'+('：'+'、'.join(names) if names else '本次材料')+'。'
        from .intake import CLEAR_YAML
        if values.get('yaml_request','').strip()==CLEAR_YAML:first+='已取消额外 YAML 修改，保留原配置作为输入。'
        if change.get('detached_archive'):first+='更换依赖后重新检查匹配。'
        missing=[LABELS[i['field']] for i in missing_inputs(draft)
                 if not (i['code']=='M100_BUILD_REQUIRED' and self.cfg.get('m100_build_enabled'))]
        following=('还需要：'+'、'.join(missing)+'。') if missing else ('材料已齐备，将自动检查并排队。' if self.cfg.get('auto_execute_on_complete') else '材料已记录，说“检查材料”即可核验；确认执行后才会刷包和回放。')
        if self.cfg.get('require_material_confirmation'):
            following=('还需要：'+'、'.join(missing)+'。' if missing else '材料已登记。')+'请核对卡片，点击“确认执行”后才检查并排队。'
        elif self.cfg.get('auto_execute_on_complete'):
            with self.store.connect() as c:
                paused=c.execute("SELECT 1 FROM automatic_tasks a JOIN task_focus f ON f.task_id=a.task_id WHERE f.owner=? AND a.owner=? AND a.state='PAUSED'",(user,user)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automatic_tasks'").fetchone() else None
            if paused:following=following+'自动提交已暂停。' if missing else '材料已记录，自动提交已暂停。'
        if not draft.get('m100_so') and draft.get('m100_mode')=='build_if_missing' and self.cfg.get('m100_build_enabled'):
            following+='M100 不必先上传，检查时会匹配工作站编译配置；版本不支持会直接提示。'
        from .material_summary import describe,text
        return first+'\n'+text(describe(self.store,user,provided=values))+'\n'+following

    def asset(self,user,kind,name):
        if kind!='dataset':return self.registry.get(user,kind,name) if self.registry else self.store.asset(user,kind,name)
        from .dataset_sources import reference
        if name=='共享18场景':name='共享:18场景'
        by_path=isinstance(name,str) and name.startswith('/')
        if by_path and ('..' in name.split('/') or any(x in name for x in ('\n','\r','\x00'))):raise GatewayError('工作站数据集路径无效。')
        matches=[a for a in self.assets(user) if a['kind']=='dataset' and (
            a.get('data',{}).get('remote_path','').rstrip('/')==name.rstrip('/') if by_path else name in (a['id'],a['name'],reference(a)))]
        latest={}
        for a in matches:
            key=(a['owner'],a.get('origin','personal'),a['name'])
            if key not in latest or a['version']>latest[key]['version']:latest[key]=a
        if len(latest)!=1:raise GatewayError('该工作站路径尚未接入，或存在多个数据版本；请补充 prepared manifest，由管理员核验来源。' if by_path else '数据集名称不存在或有歧义，请查询可用数据集；共享数据使用“共享:名称”')
        asset=next(iter(latest.values()))
        return self.datasets.validate(user,asset) if asset.get('origin')=='dataset_catalog' else asset

    def review_records(self,user,baselines=None):
        """Local owner-scoped business view; viewing a task never probes hardware."""
        from .intake import Intake
        from .preparation import PreparationQueue,fingerprint
        from .bindings import KIND,validate_candidate
        draft=Intake(self.store).get(user) or {}
        preparation=PreparationQueue(self.cfg,self.store).latest(user)
        if baselines is None:
            with self.store.connect() as c:
                baselines=[self.store.unpack(r) for r in c.execute('SELECT * FROM baselines WHERE owner=?',(user,))]
        relevant=[b for b in baselines if b['data'].get('kind')==KIND and preparation
                  and b['data'].get('preparation_id')==preparation['id']]
        current=[]
        for baseline in relevant:
            try:validate_candidate(self.cfg,self.store,user,baseline);current.append(baseline)
            except GatewayError:pass
        chosen=current[0] if len(current)==1 else (max(relevant,key=lambda b:b.get('created',0)) if relevant else None)
        stale=bool(preparation and fingerprint(draft)!=preparation['fingerprint']) or bool(relevant and len(current)!=1)
        return draft,preparation,chosen,stale

    def open_materials(self,user,session):
        from .tasks import Tasks
        from .intake import Intake
        from .autoflow import AutoFlow
        tasks=Tasks(self.store);task=tasks.current(user)
        if task and task['job_id']:
            job=self.store.job(user,task['job_id'])
            if job['state'] in ('QUEUED','RUNNING','POSTPROCESSING','NEEDS_REVIEW'):
                return '当前任务已提交，材料保持冻结；新文件用于下一次任务。\n'+self.task_review(user)
            # Opening the next card is not execution. Its later confirmation may
            # intentionally repeat a completed successful run with identical inputs.
            # The shared bench can have changed independently of this private history.
            Intake(self.store).update(user,{},incoming_kind='entry',
                                      force_run=job['state'] in ('SUCCEEDED','PARTIAL_SUCCESS'))
            task=tasks.current(user)
        task=task or tasks.ensure(user,session)
        AutoFlow(self.cfg,self.store).arm(user,session,task)
        self.defer_auto=True;self.material_card=True;self.surface_material_card=True
        return self.material_ack(user,{})

    def task_review(self,user):
        from .business import render_review
        draft,preparation,baseline,stale=self.review_records(user)
        from .tasks import Tasks
        task=Tasks(self.store).current(user)
        from .task_refs import reference
        ref=reference(self.store,user,task_id=task['id']) if task else ''
        control_note=''
        if task and task['job_id']:
            with self.store.connect() as c:control=c.execute('SELECT evaluate_after FROM execution_controls WHERE job=? AND owner=?',(task['job_id'],user)).fetchone()
            if control:control_note='当前执行目标：'+('回放后评测' if control['evaluate_after'] else '仅回放')+'；以下保留提交时任务单，材料未修改。\n'
        if task and task['state']=='NO_CHANGE':
            return '任务：'+ref+'\n执行输入无有效变更，未重复提交回放。需要按相同材料重跑时，请发送“重新回放”。'
        return ('任务：'+ref+'\n' if task else '')+control_note+render_review(draft,preparation,baseline,self.store.jobs(user),stale=stale,evaluator=self.cfg.get('default_evaluator'))

    def context(self, user):
        self.store.require_user(user)
        with self.store.connect() as c:
            baselines=[self.store.unpack(r) for r in c.execute('SELECT * FROM baselines WHERE owner=?',(user,))]
        try:assets=self.assets(user);asset_status='READY'
        except GatewayError:assets=[];asset_status='WORKSTATION_UNAVAILABLE'
        from .intake import Intake
        from .bindings import KIND,candidate_summary
        from .business import missing_inputs,capability_issues
        draft,preparation,selected,stale=self.review_records(user,baselines)
        from .assemblies import list_assemblies
        assemblies=[{k:row[k] for k in ('id','name','version')} for row in list_assemblies(self.cfg,self.store,user)]
        from .tasks import Tasks
        active_task=Tasks(self.store).current(user)
        resolved=(preparation.get('result') or {}).get('assets',{}) if preparation and preparation['state']=='STATIC_PASSED' and not stale else {}
        return {"material_draft":Intake(self.store).get(user),"assets": [public_asset(a) for a in assets], 'asset_status':asset_status,
                'assemblies':assemblies,'current_material_task':({'id':active_task['id'],'revision':active_task['revision'],'job_id':active_task['job_id']} if active_task else None),
                'business_status':{'missing_fields':[i['field'] for i in missing_inputs(draft) if i['field'] not in resolved],
                    'capability_issues':capability_issues(draft,(selected or {}).get('data',{}).get('evaluator') or self.cfg.get('default_evaluator')),
                    'preparation_state':preparation['state'] if preparation else 'DRAFT','stale':stale},
                'baselines':[candidate_summary(self.cfg,self.store,user,b) if b['data'].get('kind')==KIND else
                             {'name':b['name'],'asset_ids':b['data']['asset_ids'],'gate_verified':b['data'].get('gate_verified',False)} for b in baselines if b['data'].get('kind')!='retained-result-v1'],
                "jobs": [{"id": j["id"], "state": j["state"], "action": j["payload"]["action"]} for j in self.store.jobs(user)]}

    def prepared_replay(self,user,intent,text):
        from .bindings import KIND,match_selection,validate_candidate
        from .intake import Intake
        explicit=intent.get('baseline')
        if explicit:
            selected=self.store.baseline(user,explicit)
            if selected['data'].get('kind')!=KIND:return None
            assets,scenes=validate_candidate(self.cfg,self.store,user,selected)
        else:
            with self.store.connect() as c:
                rows=[self.store.unpack(row) for row in c.execute('SELECT * FROM baselines WHERE owner=?',(user,))]
            candidates=[row for row in rows if row['data'].get('kind')==KIND]
            current=[]
            for candidate in candidates:
                try:
                    frozen,scope=validate_candidate(self.cfg,self.store,user,candidate)
                    current.append((candidate,frozen,scope))
                except GatewayError:continue
            if len(current)>1:
                raise GatewayError('当前材料对应多个候选适配器，请管理员明确本次绑定后再提交。')
            if not current:
                if candidates and Intake(self.store).get(user):
                    raise GatewayError('当前材料没有有效的候选绑定，请重新检查并绑定；不会回退旧软件包或旧适配器执行。')
                return None
            selected,assets,scenes=current[0]
        match_selection(assets,scenes,intent,text)
        return {'action':'replay','assets':assets,'baseline':selected,'scenes':scenes,
                'evaluate_after':intent.get('evaluate_after',False) or
                    (Intake(self.store).get(user) or {}).get('goal')=='replay_and_evaluate'}

    def save_goal_selection(self,user,session,values):
        """Choosing a future mode alone is not permission to repeat the bench run."""
        from .intake import Intake
        from .tasks import Tasks
        from .autoflow import AutoFlow
        Intake(self.store).update(user,values,incoming_kind='goal_selection')
        task=Tasks(self.store).current(user);flow=AutoFlow(self.cfg,self.store);flow.arm(user,session,task)
        with self.store.connect() as c:
            c.execute("UPDATE automatic_tasks SET state='AWAITING_MATERIAL',updated=? WHERE task_id=? AND owner=? AND state!='PAUSED'",(time.time(),task['id'],user))
        self.defer_auto=True
        return '执行目标已保存为'+('仅回放' if values['goal']=='replay' else '回放后评测')+'。后续材料到达后自动检查；也可发送“恢复自动执行”开始。仅选择目标不会重新刷包或回放。'

    def handle(self, session, text, intent, request_id=None, *, expected_task=None):
        user = identity(session)
        self.store.require_user(user)
        v = validate_intent(intent)
        if v['action'] in ('prepare','check_materials') and v.get('scenes'):
            v={**v,'inputs':{**v.get('inputs',{}),'scene_scope':','.join(str(i) for i in v['scenes'])}}
        only_goal={k:value for k,value in v.get('inputs',{}).items() if value}
        if v['action']=='prepare' and set(only_goal)=={'goal'} and re.search(r'下次|下一次',text):
            return self.save_goal_selection(user,session,only_goal)
        if v['action']=='set_goal' and (set(only_goal)!={'goal'} or any(v.get(k) for k in ('scenes','package','dataset','dependencies','baseline'))):
            raise GatewayError('本次目标控制只接受执行目标与任务编号；文件、配置或场景变更请另行提交，已提交材料保持冻结。')
        if v['action']=='set_goal' or (v['action']=='prepare' and set(only_goal)=={'goal'} and not any(v.get(k) for k in ('package','dataset','dependencies','baseline')) and not re.search(r'下次|下一次',text)):
            from .execution_controls import set_goal
            selected=set_goal(self.cfg,self.store,user,session,only_goal.get('goal',''),v.get('task_id',''))
            if selected['state']!='DRAFT':
                self.execution_control=selected
                return selected['text']
            from .tasks import Tasks
            previous=Tasks(self.store).current(user)
            with self.store.connect() as c:
                waiting=c.execute("SELECT 1 FROM automatic_tasks WHERE task_id=? AND state='AWAITING_MATERIAL'",(previous['id'],)).fetchone() if previous and c.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_tasks'").fetchone() else None
            if not previous or waiting:return self.save_goal_selection(user,session,only_goal)
            change=self.update_inputs(user,only_goal)
            return self.material_ack(user,only_goal,change)
        from .task_refs import PUBLIC
        if PUBLIC.fullmatch(v.get('task_id','')):
            from .tasks import Tasks
            selected=Tasks(self.store).get(user,v['task_id'])
            if not selected['job_id']:raise GatewayError('这份任务尚未提交执行，请继续补充材料。')
            v={**v,'task_id':selected['job_id']}
        action = v["action"]
        if action=='open_materials':return self.open_materials(user,session)
        if action=='repeat_replay':
            from .tasks import Tasks
            from .intake import Intake
            from .autoflow import AutoFlow
            task=Tasks(self.store).current(user)
            if not task or not task['data'].get('package'):raise GatewayError('尚无可重复执行的软件材料。')
            if task['job_id'] and self.store.job(user,task['job_id'])['state']=='NEEDS_REVIEW':
                raise GatewayError('原执行结果待核验，请先核对原任务，不重复派发。')
            Intake(self.store).update(user,{},incoming_kind='repeat',force_run=True)
            AutoFlow(self.cfg,self.store).arm(user,session)
            self.material_card=True
            return '已登记重新回放材料，请核对卡片并点击“确认执行”。' if self.cfg.get('require_material_confirmation') else '已登记重新回放请求，将按当前材料重新检查并排队。'
        if action in ('pause_auto','resume_auto'):
            from .autoflow import AutoFlow
            if not self.cfg.get('auto_execute_on_complete'):return '自动处理尚未启用。'
            if action=='pause_auto' and text.strip() in ('暂缓执行','暂不执行'):
                from .tasks import Tasks
                from .task_refs import reference
                current=Tasks(self.store).current(user)
                if current and current['job_id']:
                    existing=self.store.job(user,current['job_id'])
                    if existing['state'] in ('QUEUED','RUNNING','POSTPROCESSING'):
                        ref=reference(self.store,user,job_id=existing['id'])
                        answer=ref+' 已提交，“暂缓执行”未中断本任务。'+('尚在排队时可发送“取消任务 '+ref+'”。' if existing['state']=='QUEUED' else '回放过程中可发送“本次仅回放”跳过后续评测；已派发的阶段不会中断。')
                        self.execution_control={'state':'NO_STOP','text':answer}
                        return answer
            return AutoFlow(self.cfg,self.store).control(user,session,action=='pause_auto')
        if action=='assemblies':return self.assembly_text(user)
        if action=='evaluation_status':
            from .cloud_provider import profile_for_job,ready
            from time import time as current_time
            cloud=profile_for_job(self.cfg,{'created':current_time()})
            if cloud is not None:
                health=ready(self.cfg,cloud)
                return ('云端评测：'+cloud['instance']+' · 接收程序已就绪。\n'+
                        ('GT：按本次数据集明确关联，不使用通用默认真值。\n' if cloud.get('gt_policy')=='task-dataset-v1' or self.cfg.get('evaluation_gt_binding_required_after') else '历史策略 GT：'+cloud['gt_root']+'\n')+
                        '根据本次回放清单自动匹配场景；仅上传所需预测文件，交付原始 TXT。\n'
                        '实例停止或 GT 缺失时明确提示；不会重新回放，也不会自动降级到其他评测后端。')
            if not self.cfg.get('workstation_evaluation_enabled') or not self.cfg.get('default_evaluator'):
                return '工作站离线评测尚未启用。已有结果可保留；请管理员核验固定评测环境和默认绑定。'
            from .workstation_evaluation import probe_runtime,validate_descriptor
            validate_descriptor(self.cfg,self.cfg['default_evaluator'])
            runtime=probe_runtime(self.cfg)
            if runtime.get('state')!='PASS':raise GatewayError('工作站评测环境核验未通过')
            return '工作站离线评测：本次真实 SSH 核验通过。\n固定评测工具、worker 和动态库正常；使用独立评测队列，不占台架。\n已有本人任务可发送“评测任务 <任务编号>”；管理员可按本人授权登记历史结果。\n默认评测源结果全部场景，保留原生任务开关及阈值；缺少 GT 或有效样本时交付部分原始 TXT 并说明缺项。\n计算无需 LPAI 登录；LPAI 上传仍为独立的未接入能力。'
        if action in ('check_materials','preparation_status'):
            from .preparation import PreparationQueue
            preparation=PreparationQueue(self.cfg,self.store)
            if action=='preparation_status':return preparation.summary(user)
            change={}
            if any(v.get('inputs',{}).values()):
                change=self.update_inputs(user,v['inputs'])
            if self.cfg.get('require_material_confirmation'):
                from .confirmations import active
                from .tasks import Tasks
                task=Tasks(self.store).current(user)
                with self.store.connect() as c:confirmed=active(c,user,task,session) if task else None
                if not confirmed:return self.open_materials(user,session)
            return ('新增材料已用于下一次回放，已提交任务不变。\n' if change.get('next_request') else '')+preparation.submit(user,session)
        if action in ('new_task','prepare','materials'):
            if action=='materials':return self.task_review(user)
            values=v.get('inputs',{});change=self.update_inputs(user,values,new=action=='new_task')
            return self.material_ack(user,values,change,new=action=='new_task')
        if action == "help":
            from .intake import GUIDE
            return v.get("reply") or GUIDE
        if action == "assets":
            assets = self.assets(user)
            from .dataset_sources import reference
            prefix='工作站本人资产目录：'+self.store.remote_root(user,self.cfg['remote_users_root'])+'\n' if self.registry else ''
            own=[a for a in assets if a['owner']==user];shared=[a for a in assets if a['owner']=='shared']
            result=prefix+"本人资产：\n" + ("\n".join(f"{a['kind']} · {a['name']} · v{a['version']}" for a in own) or "尚未登记")
            if shared:result+='\n共享数据集（只读，直接引用）：\n'+'\n'.join(f"{reference(a)} · {a['data']['scene_count']} 场景 · v{a['version']}" for a in shared)
            return result+'\n'+self.assembly_text(user)
        if action == "queue":
            from .queue_status import QueueStatus
            queue=QueueStatus(self.store)
            if self.cfg.get('resource_probe_enabled'):
                from .availability import probe
                measured=probe(self.cfg)
                queue.resource(measured)
            from .availability import connection_text
            heading='台架连接状态（本次真实 SSH 只读检查）\n' if self.cfg.get('resource_probe_enabled') else '台架连接状态（检查未启用）\n'
            detail=connection_text(measured) if self.cfg.get('resource_probe_enabled') else '请管理员启用连接探测，当前没有实时结果。'
            execution='执行权限：真实刷包/回放尚未启用；不影响上述 SSH 实测。' if self.cfg.get('mode')=='simulate' or not self.cfg.get('live_enabled') else '执行权限：真实任务已启用。'
            current=measured if self.cfg.get('resource_probe_enabled') else {'state':'UNKNOWN','checked_at':0}
            from .tasks import Tasks
            return heading+detail+'\n'+queue.text(user,resource=current)+'\n'+execution+'\n'+Tasks(self.store).text(user)
        if action in ("status", "cancel"):
            job = self.store.job(user, v.get("task_id", ""))
            if action == "cancel":
                if job['state']=='POSTPROCESSING':
                    self.store.cancel(user,job['result']['evaluation_job'])
                    return f"回放已完成；已取消尚未执行的评测：{job['result']['evaluation_job']}。\n回放结果保留在：{job['result']['task']}"
                self.store.cancel(user, job["id"])
                return f"已取消等待任务：{job['id']}"
            from .queue_status import QueueStatus
            if job['state']=='QUEUED':return QueueStatus(self.store).text(user,job['id'])
            from .core import format_result
            return format_result(job, job["state"], job["result"] or {"error": "等待派发" if job["state"] == "QUEUED" else "执行中；请稍后查询"})
        if action == "register":
            from .core import name_check
            name_check(v.get('asset_name',''))
            if v.get('asset_kind')=='dataset':
                if v.get('asset_name','').startswith('共享'):
                    raise GatewayError('共享数据集由本机统一配置，普通消息不能新建或覆盖；已有共享数据集可直接引用')
                if not self.registry or not v.get('asset_path') or not v.get('manifest'):
                    raise GatewayError('数据集请提供工作站目录和已准备的 manifest；大文件不走 CC Connect。开发空间数据先直传或挂载到工作站，再接入目录')
                from .core import remote_within
                root=self.store.remote_root(user,self.cfg['remote_users_root'])
                path=remote_within(root,v['asset_path']);manifest=remote_within(path,v['manifest'])
                evidence=self.registry.ssh.inspect_dataset(path,manifest)
                a=self.registry.record(user,'dataset',v['asset_name'],{'remote_path':path,'manifest':manifest,**evidence})
                return f"已在本人工作站目录登记数据集 {a['name']} v{a['version']}。"
            if v.get('asset_kind') not in ('package','dependency'):
                raise GatewayError('只登记软件包、依赖和数据集')
            if v['asset_kind']=='dependency' and not v.get('target'):
                raise GatewayError('请说明依赖文件在 APP 中的替换位置，例如 lib/liburoad_m100.so')
            if v.get('asset_path'):
                if not self.registry:raise GatewayError('需要工作站资产后端')
                a=self.registry.remote_file(user,v['asset_kind'],v['asset_name'],v['asset_path'],v.get('target') or None)
                return f"已在本人工作站目录登记 {a['name']} v{a['version']}，未经过个人电脑搬运。"
            if self.registry:
                from .asset_fetch import fetch
                a=fetch(self.registry,user,v['asset_kind'],v['asset_name'],v.get('asset_url',''),v.get('target') or None)
                return f"已由工作站直接下载并登记 {a['name']} v{a['version']}，文件未经过 CC Connect 或个人电脑中转。请继续检查本次材料的兼容性。"
            from .transfer import download
            source = download(self.cfg, self.store, user, v.get("asset_url", ""))
            if self.registry:
                try:a=self.registry.import_file(user,v['asset_kind'],v.get('asset_name',''),source,v.get('target') or None)
                finally:source.unlink(missing_ok=True)
            else:a = self.store.import_file(user, v["asset_kind"], v.get("asset_name", ""), source)
            return f"已在本人{'工作站' if self.registry else ''}目录登记 {a['name']} v{a['version']}。文件入库不等于组合已通过小场景验证。"
        if action=='replay' and expected_task is None and self.cfg.get('require_material_confirmation'):
            if any(v.get('inputs',{}).values()):self.update_inputs(user,v['inputs'])
            return self.open_materials(user,session)
        if action in ('replay','evaluate') and any(v.get('inputs',{}).values()):
            self.update_inputs(user,v['inputs'])
            detail='（含 YAML 变更）' if v['inputs'].get('yaml_request') else ''
            raise GatewayError('本条新增材料已保存到本次草稿'+detail+'，请先发送“检查材料”；材料核验并绑定适配器后再执行，不会忽略本条变更。')
        if (action=='replay' and expected_task is None and self.cfg.get('auto_execute_on_complete')
                and not any(v.get(k) for k in ('baseline','package','dataset','dependencies','scenes','evaluate_after'))):
            if self.cfg.get('require_material_confirmation'):return self.open_materials(user,session)
            from .tasks import Tasks
            current=Tasks(self.store).current(user)
            if current and current['job_id']:
                from .core import format_result
                previous=self.store.job(user,current['job_id'])
                return '当前材料已提交，不重复执行。\n'+format_result(previous,previous['state'],previous.get('result') or {})
            from .autoflow import AutoFlow
            AutoFlow(self.cfg,self.store).arm(user,session,current)
            return self.material_ack(user,{})
        if action == "evaluate":
            if self.cfg.get("replay_only_no_dump_enabled"):raise GatewayError("不再支持对已完成回放追加评测；请提交新的回放后评测任务。")
            if v.get('scenes'):
                raise GatewayError('已有结果评测固定覆盖源任务的全部场景，当前不支持在评测时另选场景编号；本次未提交，不会静默忽略所选范围。')
            previous = self.store.job(user, v.get("task_id", ""))
            if previous['state']=='POSTPROCESSING':
                from .core import format_result
                return '该回放任务已安排评测，无需重复提交。\n'+format_result(previous,previous['state'],previous['result'])
            if (previous.get('result') or {}).get('evaluation_state')=='NEEDS_REVIEW':
                raise GatewayError('此前评测执行结果待核验，请先核对评测收据；已保留回放目录，无需重新回放')
            if previous["state"] not in ("SUCCEEDED", "PARTIAL_SUCCESS", "SIMULATED") or not previous.get("result", {}).get("task"):
                raise GatewayError("需要本人已有且已完成的回放任务")
            if previous['state']=='SIMULATED' and self.cfg.get('mode')!='simulate':raise GatewayError('模拟任务没有真实回放结果，不能用于真实评测')
            if previous["payload"]["action"] != "replay":
                raise GatewayError("评测源必须是本人回放任务")
            # A new evaluation may use the current reviewed execution policy;
            # the completed replay and its original frozen baseline stay intact.
            baseline=json.loads(json.dumps(previous['payload']['baseline'],ensure_ascii=False))
            old_evaluator=baseline.get('data',{}).get('evaluator') or {}
            current_evaluator=self.cfg.get('default_evaluator') or {}
            if old_evaluator.get('kind')=='workstation-offline-v1' and current_evaluator.get('kind')=='workstation-offline-v1':
                from .workstation_evaluation import validate_descriptor
                validate_descriptor(self.cfg,old_evaluator);validate_descriptor(self.cfg,current_evaluator)
                baseline['data']['evaluator']=json.loads(json.dumps(current_evaluator))
            payload = {"action": "evaluate", "source_job": previous["id"], "source_task": previous["result"]["task"],
                       "baseline": baseline, "source_assets": previous["payload"].get("assets") or {},
                       "source_run": previous['payload'].get('source_run') or previous['result'].get('run_id') or previous['id']}
        else:
            from .intake import Intake
            current_draft=Intake(self.store).get(user) or {}
            if v.get('baseline') and self.store.baseline(user,v['baseline'])['data'].get('kind')=='retained-result-v1':
                raise GatewayError('此记录仅登记已有回放结果供评测，不能作为刷包或回放适配器。')
            payload=self.prepared_replay(user,v,text)
            if payload is None:
                if current_draft.get('yaml_request'):
                    raise GatewayError('本次有 YAML 变更要求，需先检查材料并将候选配置绑定回放适配器；当前不能用旧包和旧基线忽略该变更执行。若本次确实保留原包配置，请明确发送“取消 YAML 修改”。')
                from .tasks import Tasks
                focus=Tasks(self.store).current(user)
                with self.store.connect() as c:
                    tracked=focus and c.execute('SELECT 1 FROM task_lineage WHERE task_id=? AND owner=?',(focus['id'],user)).fetchone()
                    explicit={r['field'] for r in c.execute('SELECT field FROM task_explicit_fields WHERE task_id=? AND owner=?',(focus['id'],user))} if tracked else set(current_draft)
                if any(current_draft.get(field) for field in ('package','camera_so','lidar_so','m100_so')) or any(current_draft.get(field) and field in explicit for field in ('dataset','scene_scope')):
                    raise GatewayError('本次草稿已有软件、依赖或数据范围，需先发送“检查材料”并绑定本次候选；不能用旧基线忽略这些材料执行。')
                package = self.asset(user, "package", v.get("package", ""))
                dataset = self.asset(user, "dataset", v.get("dataset", ""))
                dependencies = [self.asset(user, "dependency", name) for name in v.get("dependencies", [])]
                ids = {"package": package["id"], "dataset": dataset["id"], "dependencies": sorted(a["id"] for a in dependencies)}
                if v.get('baseline'):
                    baseline=self.store.baseline(user,v['baseline'])
                else:
                    with self.store.connect() as c:
                        candidates=[self.store.unpack(r) for r in c.execute('SELECT * FROM baselines WHERE owner=?',(user,))]
                    candidates=[b for b in candidates if b['data'].get('asset_ids')==ids and b['data'].get('gate_verified') and b['data'].get('kind')!='candidate-v1']
                    if len(candidates)!=1:
                        raise GatewayError('这套输入尚未绑定唯一可用的回放适配器。接入后会由本次任务先执行小场景预检，再正式回放。')
                    baseline=candidates[0]
                if ids != baseline["data"]["asset_ids"]:
                    raise GatewayError("输入版本变化，需要核对回放适配器。实际任务仍会先执行本次小场景预检。")
                if not baseline["data"].get("gate_verified"):
                    raise GatewayError("回放适配器尚未核验，暂不受理执行")
                payload = {"action": "replay", "assets": {"package": package, "dataset": dataset, "dependencies": dependencies},
                           "baseline": baseline, "scenes": v.get("scenes", []),
                           "evaluate_after":v.get('evaluate_after',False) or current_draft.get('goal')=='replay_and_evaluate'}
        from .business import capability_issues,make_contract
        from .intake import Intake
        draft=Intake(self.store).get(user) or {}
        evaluator=payload['baseline']['data'].get('evaluator')
        if self.cfg.get('evaluation_gt_binding_required_after') is not None and (payload['action']=='evaluate' or payload.get('evaluate_after')):
            from .cloud_provider import task_gt_binding
            dataset=(payload.get('source_assets') or payload.get('assets') or {}).get('dataset',{}).get('data')
            if payload['action']=='evaluate' and isinstance(dataset,dict) and not dataset.get('evaluation_source'):
                # Only a newer catalog entry for the exact same replay source
                # may add GT. Original assets/receipts remain frozen.
                from .dataset_sources import reference
                original=payload['source_assets']['dataset']
                associated=self.asset(user,'dataset',reference(original))['data']
                base=lambda d:{k:v for k,v in d.items() if k!='evaluation_source'}
                if base(associated)!=base(dataset):raise GatewayError('当前登记数据源与原回放来源不同，不能替换原结果的GT关联。')
                dataset=associated
            binding=task_gt_binding(self.cfg,dataset,time.time())
            if payload['action']=='evaluate' and binding is not None:payload['evaluation_gt_binding']=binding
        if isinstance(evaluator,dict) and evaluator.get('kind')=='workstation-offline-v1':
            from .workstation_evaluation import validate_descriptor
            validate_descriptor(self.cfg,evaluator)
            if (payload['action']=='evaluate' or payload.get('evaluate_after')) and not self.cfg.get('workstation_evaluation_enabled'):
                raise GatewayError('工作站离线评测单独开关尚未启用；已有回放结果保留。')
            if payload['action']=='evaluate' and previous['state']=='SIMULATED':
                raise GatewayError('模拟任务没有真实预测数据，不能提交工作站真实评测。')
        if payload['action']=='replay':
            requested={**draft,'goal':'replay_and_evaluate' if payload.get('evaluate_after') else 'replay'}
            evaluator=payload['baseline']['data'].get('evaluator')
            blocked=capability_issues(requested,evaluator)
            if blocked:
                raise GatewayError('本次任务尚不能提交：\n'+'\n'.join(i['message']+'；'+i['next_action'] for i in blocked)+'\n材料检查和候选可保留；不会先回放再忽略所选目标。')
            # Legacy reviewed baselines predate prepared assets; describe the
            # actual selected snapshot, never resolve names again for the sheet.
            selected=json.loads(json.dumps(payload['baseline'],ensure_ascii=False))
            selected['data']['assets']=payload['assets'];selected['data']['scenes']=payload['scenes']
            for role in ('package','dataset'):
                requested.setdefault(role,payload['assets'][role]['name'])
            from .preparation import TARGETS,PreparationQueue
            for role,target in TARGETS.items():
                item=next((a for a in payload['assets']['dependencies'] if a['data'].get('target')==target),None)
                if item:requested.setdefault(role,item['name'])
            requested.setdefault('scene_scope',','.join(str(i) for i in payload['scenes']) or '全部')
            preparation=PreparationQueue(self.cfg,self.store).latest(user)
            if not preparation or preparation['id']!=selected['data'].get('preparation_id'):preparation=None
            payload['business_contract']=make_contract(requested,preparation,selected,evaluate_after=payload.get('evaluate_after',False))
            from .material_summary import describe
            payload['business_contract']['input_summary']=describe(self.store,user,assets=payload['assets'])
        else:
            blocked=capability_issues({'goal':'replay_and_evaluate'},payload['baseline']['data'].get('evaluator'))
            if blocked:raise GatewayError(blocked[0]['message']+'；请管理员接入评测器后再对已有结果提交评测，回放目录保留。')
        # Without a transport message ID, retries are identified by the command and
        # its frozen request. A later preparation, scope or goal is a new task even
        # when the user repeats the same short command.
        if request_id:
            key=request_id
        else:
            def canonical(value):
                value=json.loads(json.dumps(value,ensure_ascii=False))
                for field in ('assets','source_assets'):
                    if field in value and 'dependencies' in value[field]:
                        value[field]['dependencies'].sort(key=lambda asset:asset['id'])
                return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            text_key=session+"\0"+text.strip()
            key=hashlib.sha256((text_key+"\0"+canonical(payload)).encode()).hexdigest()
            # Preserve retry protection for jobs admitted by the text-only version,
            # but never let an old key consume a different current request.
            prior_payload={k:value for k,value in payload.items() if k!='business_contract'}
            previous_keys=[hashlib.sha256((text_key+'\0'+canonical(prior_payload)).encode()).hexdigest(),
                           hashlib.sha256(text_key.encode()).hexdigest()]
            with self.store.connect() as connection:
                for previous_key in previous_keys:
                    previous=connection.execute('SELECT payload FROM jobs WHERE owner=? AND request_key=?',(user,previous_key)).fetchone()
                    if previous:
                        old={k:value for k,value in json.loads(previous['payload']).items() if k!='business_contract'}
                        if canonical(old)==canonical(prior_payload):key=previous_key;break
        if expected_task:
            from .execution_identity import identity as execution_identity,cache_base
            expected_task={**expected_task,'execution_identity':execution_identity(self.store,user,payload)}
            with self.store.connect() as c:lineage=c.execute('SELECT base_job_id FROM task_lineage WHERE task_id=? AND owner=?',(expected_task['task_id'],user)).fetchone()
            if lineage:cache_base(self.store,user,lineage['base_job_id'])
        job, duplicate = self.store.submit(user, session, key, payload,**({'expected_task':expected_task} if expected_task else {}))
        if payload['action']=='replay' and not expected_task:
            from .tasks import Tasks
            Tasks(self.store).link(user,job)
        from .queue_status import QueueStatus
        prefix='【模拟模式，真实台架执行关闭】\n' if self.cfg.get('mode')=='simulate' else ''
        if payload['action']=='evaluate' and (evaluator or {}).get('kind')=='workstation-offline-v1':
            from .cloud_provider import profile_for_job
            prefix=('【云端离线评测，不占用台架】\n' if profile_for_job(self.cfg,job)
                    else '【工作站真实离线评测，不占用台架】\n')
        admitted='已进入独立评测队列，不占用台架队列。' if job['lane']=='postprocess' else '输入已就绪，进入串行队列。'
        if job['payload'].get('baseline',{}).get('data',{}).get('kind')=='candidate-v1' and job['lane']=='hardware':
            admitted='本次材料和配置已绑定，进入串行队列；仍须通过本次小场景预检才执行全量回放。'
        reply=prefix+("该请求已受理过，不会重复执行。" if duplicate else admitted)+f"任务：{job['id']}；状态：{job['state']}。"
        if payload.get('business_contract'):
            contract=payload['business_contract']
            reply+='\n执行任务单：'+str(contract.get('software_name') or payload['assets']['package']['name'])+'；正式 '+str(len(payload['scenes']))+' 场景；先预检场景 '+str((contract.get('precheck') or {}).get('scene') or '按已选范围确定')+'。'
        if job['payload'].get('evaluate_after'):reply+='\n本次目标为回放后评测；回放完成后进入独立评测队列。'
        if job['state']=='QUEUED':reply+='\n'+QueueStatus(self.store).text(user,job['id'])
        return reply
