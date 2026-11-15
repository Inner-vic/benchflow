"""Persistent, bounded material checks independent of the hardware queue."""
import base64
import hashlib
import json
import math
from pathlib import Path,PurePosixPath
import re
import time
import uuid

from .core import GatewayError
from .intake import Intake


LABELS={'package':'软件包','camera_so':'相机 SO','lidar_so':'雷达 SO','m100_so':'M100 SO','dataset':'数据集','scene_scope':'场景范围','yaml_request':'YAML','lpai_target':'LPAI'}
TARGETS={'camera_so':'lib/libxcamera.so','lidar_so':'lib/libxlidar.so','m100_so':'lib/liburoad_m100.so'}
PHASES={'核验本人工作站目录','核验本人装配存档引用','核验数据源与本次场景范围','准备M100编译产物','按版本配方装配软件包',
        '检查软件包与 SO 静态兼容','整理本次 YAML 修改要求','生成本次 YAML 候选与差异','绑定本次回放适配器',
        '列出云端数据场景','下载回放数据到工作站'}
for _role in ('package',*TARGETS):
    PHASES.update({'解析'+LABELS[_role]+'来源','下载'+LABELS[_role]+'到工作站','登记'+LABELS[_role]+'工作站文件'})


def _record_progress(store,job,phase):
    """Only annotate an existing owner's active frozen preparation; never submit."""
    if phase not in PHASES:raise GatewayError('材料检查阶段名称无效')
    if (not isinstance(job,dict) or not isinstance(job.get('id'),str) or not job['id']
            or not isinstance(job.get('owner'),str) or not isinstance(job.get('payload'),dict)):return None
    with store.connect() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparations'").fetchone():return None
        connection.execute('BEGIN IMMEDIATE')
        row=connection.execute("SELECT * FROM preparations WHERE id=? AND owner=? AND state='RUNNING'",
                               (job['id'],job['owner'])).fetchone()
        if not row:return None
        if fingerprint(job['payload'])!=row['fingerprint'] or fingerprint(json.loads(row['payload']))!=row['fingerprint']:return None
        result=json.loads(row['result']) if row['result'] else {}
        if not isinstance(result,dict):return None
        previous=result.get('progress') or {};now=time.time()
        if not isinstance(previous,dict):previous={}
        def timestamp(value,fallback):
            return value if type(value) in (int,float) and math.isfinite(value) and 0<=value<=now else fallback
        started=timestamp(previous.get('started_at'),timestamp(row['updated'],now))
        phase_started=timestamp(previous.get('phase_started_at'),now) if previous.get('phase')==phase else now
        progress={'phase':phase,'phase_started_at':phase_started,'started_at':started}
        result['progress']=progress
        connection.execute("UPDATE preparations SET result=?,updated=? WHERE id=? AND owner=? AND state='RUNNING'",
                           (json.dumps(result,ensure_ascii=False),now,job['id'],job['owner']))
    messages={'准备M100编译产物':'正在工作站准备 M100。需要编译时，上次约2分钟；完成后自动继续检查，不需要上传大 SO。',
              '按版本配方装配软件包':'已匹配软件版本，正在工作站补齐已核验依赖和配置；原始包保留，尚未操作台架。',
              '下载软件包到工作站':'正在把软件包下载到工作站，完成后自动检查；暂未占用台架。',
              '整理本次 YAML 修改要求':'正在整理本次 YAML 修改要求；只生成任务副本，完成后返回检查结果。',
              '绑定本次回放适配器':'软件和数据已通过静态检查，正在准备本次回放配置；尚未执行台架。'}
    if previous.get('phase')!=phase and phase in messages:
        from .progress import notify_input
        notify_input(store,job['owner'],row['session'],'preparation:'+job['id']+':'+phase,messages[phase])
    return progress


def _elapsed(value,now):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0 or value>now:return None
    seconds=int(now-value)
    return (str(seconds//3600)+'小时'+str(seconds%3600//60)+'分' if seconds>=3600 else
            str(seconds//60)+'分'+str(seconds%60)+'秒' if seconds>=60 else str(seconds)+'秒')


def _download_progress(store,job,value):
    fields=('bytes','total_bytes','files','total_files')
    if any(type(value.get(k)) is not int or value[k]<0 for k in fields):return
    if value['bytes']>value['total_bytes'] or value['files']>value['total_files']:return
    detail='回放数据下载：%.1f / %.1f MiB（%d / %d 文件）'%(value['bytes']/1024**2,value['total_bytes']/1024**2,value['files'],value['total_files'])
    with store.connect() as c:
        row=c.execute("SELECT result FROM preparations WHERE id=? AND owner=? AND state='RUNNING'",(job['id'],job['owner'])).fetchone()
        if not row:return
        result=json.loads(row['result'] or '{}');progress=result.get('progress') or {}
        if progress.get('phase')!='下载回放数据到工作站':return
        progress.update(detail=detail,download=value);result['progress']=progress
        c.execute('UPDATE preparations SET result=?,updated=? WHERE id=? AND owner=? AND state=\'RUNNING\'',
            (json.dumps(result,ensure_ascii=False),time.time(),job['id'],job['owner']))


def material_name(draft,field,source):
    """One personal software name gives the four reviewed roles stable aliases."""
    label=draft.get('software_name','').strip()
    if label:
        from .core import name_check
        name_check(label)
        if len(label)>60:raise GatewayError('软件名称最多60个字符，请缩短后重新提交。')
        return label+{'package':'','camera_so':'-相机SO','lidar_so':'-雷达SO','m100_so':'-M100SO'}[field]
    return '材料-'+field+'-'+hashlib.sha256(source.encode()).hexdigest()[:12]


def fingerprint(draft):
    return hashlib.sha256(json.dumps(draft,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def scene_selection(text,count):
    if type(count) is not int or not 0<count<=10000:raise GatewayError('数据集场景数量无效')
    if text.strip() in ('全部','全部场景','所有','所有场景'):return list(range(1,count+1))
    value=text.replace('场景','').replace('第','').replace('个','').strip()
    value=re.sub(r'\s*([-到至])\s*',r'\1',value)
    values=[]
    for part in re.split(r'[,，、\s]+',value):
        if not part:continue
        if re.fullmatch(r'\d+',part):values.append(int(part))
        elif re.fullmatch(r'\d+\s*[-到至]\s*\d+',part):
            left,right=map(int,re.split(r'[-到至]',part))
            if not (1<=left<=right<=count):raise GatewayError('场景范围超出数据集或倒序')
            values.extend(range(left,right+1))
        else:raise GatewayError('场景范围未明确，请提供“全部”或编号，例如 1,3,5-8')
    if not values or any(n<1 or n>count for n in values):raise GatewayError('场景编号超出数据集，请核对范围')
    return sorted(set(values))


def validate_yaml_receipt(receipt,job,package,planned,owner_root):
    from .core import remote_within
    if (receipt.get('schema_version')!=1 or receipt.get('scope')!='task-copy-only'
            or receipt.get('preparation_id')!=job['id'] or receipt.get('package_sha256')!=package['sha256']
            or receipt.get('package_modified') is not False or receipt.get('bench_modified') is not False):
        raise ValueError('YAML receipt identity mismatch')
    task_root=owner_root+'/preparations/'+job['id']+'/yaml'
    receipt_path=remote_within(task_root,receipt['receipt_path']);directory=PurePosixPath(receipt_path).parent
    outputs=receipt.get('outputs');changes=receipt.get('changes');operations=planned['operations']
    if not isinstance(outputs,list) or not 1<=len(outputs)<=4 or not isinstance(changes,list) or len(changes)!=len(operations):
        raise ValueError('YAML receipt inventory mismatch')
    targets=set()
    for output in outputs:
        target=output['target'];parts=PurePosixPath(target)
        if parts.is_absolute() or '..' in parts.parts or not parts.parts or parts.parts[0]=='livc' or target in targets:
            raise ValueError('invalid candidate target')
        targets.add(target)
        for key in ('path','diff_path'):
            remote_within(task_root,output[key])
            if PurePosixPath(output[key]).parent!=directory:raise ValueError('candidate outside receipt directory')
        for key in ('sha256','before_sha256','diff_sha256'):
            if not re.fullmatch('[a-f0-9]{64}',output[key]):raise ValueError('invalid candidate hash')
    seen=set()
    for change,operation in zip(changes,operations):
        target=operation['target'].removeprefix('livc/')
        if (change.get('target') not in targets or not (change['target']==target or change['target'].endswith('/'+target))
                or change.get('operation')!=operation['operation'] or change.get('path')!=operation['path']):
            raise ValueError('YAML receipt differs from plan')
        identity=(change['target'],tuple(change['path']))
        if identity in seen:raise ValueError('duplicate resolved YAML change')
        seen.add(identity)
    if targets!={item['target'] for item in changes}:raise ValueError('unexpected YAML output')


class MaterialCheck:
    def __init__(self,cfg,store):self.cfg,self.store=cfg,store

    def run(self,job):
        from .service import Service
        from .asset_fetch import fetch
        service=Service(self.cfg);user=job['owner'];draft=job['payload'];issues=[];resolved={};checks=[];build_result={};assembly_result={}
        timings=[];phase='核验本人工作站目录';started=time.monotonic()
        def mark(value):
            nonlocal phase
            phase=value
            return _record_progress(self.store,job,value)
        deadline=time.monotonic()+600
        class BudgetSSH:
            def __init__(self,transport):self.transport=transport
            def python(self,code,args=(),timeout=60,**kwargs):
                remaining=deadline-time.monotonic()
                if remaining<=0:raise GatewayError('本轮材料检查达到总时限，请先将材料准备到工作站后重试')
                call_started=time.monotonic()
                try:return self.transport.python(code,args,timeout=min(timeout,remaining),**kwargs)
                finally:timings.append({'phase':phase,'seconds':round(time.monotonic()-call_started,3)})
        if service.registry:service.registry.ssh=BudgetSSH(service.registry.ssh)
        service.datasets.ssh=BudgetSSH(service.datasets.ssh)
        def issue(field,code,message,next_action):
            issues.append(dict(field=field,code=code,message=message,next_action=next_action))
        if not service.registry:
            return {'status':'NEEDS_INPUT','issues':[{'field':'package','code':'WORKSTATION_REQUIRED','message':'尚未配置工作站资产后端','next_action':'请管理员配置后再检查'}]}
        self.store.require_user(user)
        # Listing only this user's catalog is deliberate; no cross-user name fallback.
        mark('核验本人工作站目录')
        try:assets=service.registry.list(user)
        except GatewayError:
            return {'status':'NEEDS_INPUT','issues':[{'field':'package','code':'WORKSTATION_UNAVAILABLE','message':'本人工作站资产目录暂不可读','next_action':'检查工作站 SSH 或权限后重新检查材料；不需要台架在线'}]}
        service._asset_snapshot=(user,assets)
        # A changed SO detaches the old assembly qualification, but unchanged
        # private archived APP/SOs remain valid sources for the new combination.
        references={draft.get(k,'') for k in ('package',*TARGETS)}
        if any(re.fullmatch(r'asm-[a-f0-9]{24}-(?:package|camera_so|lidar_so|m100_so)',x) for x in references):
            from .assemblies import list_assemblies,assembly_assets
            for saved in list_assemblies(self.cfg,self.store,user):
                archived=assembly_assets(saved)
                assets.extend(a for a in archived.values() if a['id'] in references and not any(old['id']==a['id'] for old in assets))
        if draft.get('assembly_ref'):
            mark('核验本人装配存档引用')
            from .assemblies import resolve_assembly,assembly_assets,selection_inputs
            try:
                saved=resolve_assembly(self.cfg,self.store,user,draft['assembly_ref'])
                expected=selection_inputs(saved)
                if any(draft.get(role)!=expected[role] for role in ('package','camera_so','lidar_so','m100_so')):
                    raise GatewayError('本次装配引用与冻结软件或 SO 不一致，请重新选择本人存档；新组合需新建任务')
                assets.extend(assembly_assets(saved).values())
                checks.append({'field':'package','check':'saved_assembly_selected','status':'PASS',
                               'detail':'已选本人成功装配 '+saved['id']+'；本轮仍逐项核验存量文件和本次配置，不重用旧预检'})
            except GatewayError as exc:
                return {'status':'NEEDS_INPUT','issues':[{'field':'package','code':'ASSEMBLY_UNAVAILABLE',
                    'message':str(exc),'next_action':'查询本人可复用软件并选择精确存档；不存在时从 CCU APP 和匹配 SO 开始，不自动回退原始包'}]}
        for field in ('package','camera_so','lidar_so','m100_so'):
            source=draft.get(field,'').strip()
            if not source:continue
            kind='package' if field=='package' else 'dependency'
            target=TARGETS.get(field)
            mark('解析'+LABELS[field]+'来源')
            try:
                remaining=int(deadline-time.monotonic())
                if remaining<=30:raise GatewayError('本轮材料准备已达时限，请先将文件准备到工作站后重新检查')
                matches=[a for a in assets if a['kind']==kind and source in (a['name'],a['id'])]
                if matches:a=max(matches,key=lambda a:a['version'])
                elif source.startswith('https://'):
                    name=material_name(draft,field,source)
                    mark('下载'+LABELS[field]+'到工作站')
                    a=fetch(service.registry,user,kind,name,source,target,timeout=min(300,remaining-30))
                elif source.startswith('/'):
                    name=material_name(draft,field,source)
                    mark('登记'+LABELS[field]+'工作站文件')
                    a=service.registry.remote_file(user,kind,name,source,target)
                else:raise GatewayError('本人资产中没有这个名称；请提供本人已登记名称、HTTPS 文件链接或本人工作站绝对路径')
                if a['owner']!=user:raise GatewayError('不允许引用其他用户的文件')
                if field in TARGETS and PurePosixPath(a['data'].get('target','')).name!=PurePosixPath(TARGETS[field]).name:
                    raise GatewayError('该依赖登记的替换目标不对应本项 SO，请提供正确的相机、雷达或 M100 文件')
                if draft.get('software_name') and a.get('origin')!='assembly_catalog' and a['name']!=material_name(draft,field,source):
                    alias=material_name(draft,field,source)
                    existing=[x for x in assets if x['kind']==kind and x['name']==alias and x['data']==a['data']]
                    a=max(existing,key=lambda x:x['version']) if existing else service.registry.record(user,kind,alias,a['data'])
                resolved[field]=a
            except GatewayError as exc:issue(field,'SOURCE_UNAVAILABLE',str(exc),'补充可读取的本人文件来源后重新检查材料')
            except Exception:issue(field,'SOURCE_CHECK_FAILED','来源检查未完成','检查工作站连接和文件权限后重新检查材料')
        dataset=None;selected=[];cloud_plan=None
        mark('核验数据源与本次场景范围')
        try:
            from .cloud_dataset_paths import is_cloud_path
            from .cloud_datasets import CloudDatasets
            if not draft.get('dataset'):raise GatewayError('请提供工作站数据集路径，或已接入开发空间的数据根目录。')
            if draft['dataset'].startswith(('http://','https://')):
                raise GatewayError('数据集请填写工作站绝对路径或 /lpai/volumes/... 数据根目录，不使用网页链接。')
            if is_cloud_path(draft['dataset']):
                mark('列出云端数据场景');cloud_plan=CloudDatasets(self.cfg,self.store).plan(user,draft['dataset'],draft.get('scene_scope','全部'))
            else:
                dataset=service.asset(user,'dataset',draft['dataset'])
                source=dataset['data'].get('cloud_source') or {}
                if source:
                    requested=scene_selection(draft.get('scene_scope','全部'),source['source_scene_count'])
                    if requested!=source['source_indices']:
                        mark('列出云端数据场景');cloud_plan=CloudDatasets(self.cfg,self.store).plan(user,source['input_root'],draft.get('scene_scope','全部'))
            if dataset and dataset.get('origin')!='dataset_catalog':
                from .dataset_sources import VALIDATOR_CODE
                verify=VALIDATOR_CODE+'\nimport base64,sys\nprint(json.dumps(check_source(json.loads(base64.b64decode(sys.argv[1])))))\n'
                encoded=base64.b64encode(json.dumps(dataset['data']).encode()).decode()
                current=json.loads(service.registry.ssh.python(verify,(encoded,),timeout=120))
                if current['sha256']!=dataset['data'].get('sha256') or current['scene_count']!=dataset['data'].get('scene_count'):
                    raise GatewayError('数据集 manifest 或场景数与登记值不一致，请重新核验数据源')
            count=len(cloud_plan['scenes']) if cloud_plan else dataset['data'].get('scene_count')
            if not isinstance(count,int) or count<=0:raise GatewayError('数据源未记录有效场景数量，请管理员核验 manifest')
            selected=list(range(1,count+1)) if cloud_plan or dataset['data'].get('cloud_source') else scene_selection(draft.get('scene_scope',''),count)
            detail=('已选云端回放数据，软件及配置检查通过后下载到本人工作站目录；GT保留云端' if cloud_plan else
                f'已核对数据源清单及目录，共 {count} 场景，本次选择 {len(selected)} 场景；未搬运数据')
            if dataset and dataset.get('origin')=='dataset_catalog':detail+='；复用登记版本，本轮未逐帧复查'
            checks.append({'field':'dataset','check':'source_and_scenes','status':'PASS','detail':detail})
        except GatewayError as exc:issue('dataset','DATASET_OR_SCENE_INVALID',str(exc),'补充已接入的工作站数据源和明确场景范围')
        except Exception:issue('dataset','DATASET_CHECK_FAILED','数据源核验未完成','检查工作站目录、manifest 和读权限')
        request={'owner_root':self.store.remote_root(user,self.cfg['remote_users_root'])}
        wants_build=not draft.get('m100_so','').strip() and draft.get('m100_mode')=='build_if_missing'
        # A catalog hit avoids a second inspection pass. It is not a success
        # receipt: SHA/ELF/ABI checks below still verify the chosen file.
        if wants_build and all(field in resolved for field in ('package','camera_so','lidar_so')):
            from .m100_build import reusable
            cached,cached_receipt=reusable(self.cfg,user,resolved['package'],resolved,assets)
            if cached:resolved['m100_so']=cached;build_result=cached_receipt
        for field in ('package',*TARGETS):request[field]=resolved[field]['data'] if field in resolved else None
        inspection={}
        def inspect_materials():
            module_root=Path(self.cfg['project_root'])/'bench_gateway'
            abi_source=base64.b64encode((module_root/'abi_symbols.py').read_bytes()).decode()
            bootstrap=("import base64,sys,types\n"
                       "_abi=types.ModuleType('bench_gateway_abi_symbols')\n"
                       "exec(compile(base64.b64decode("+repr(abi_source)+"),'abi_symbols.py','exec'),_abi.__dict__)\n"
                       "sys.modules['bench_gateway_abi_symbols']=_abi\n")
            code=bootstrap+(module_root/'material_inspector.py').read_text(encoding='utf-8')
            request['cache_version']=hashlib.sha256(code.encode()).hexdigest()
            encoded=base64.b64encode(json.dumps(request).encode()).decode()
            value=json.loads(service.registry.ssh.python(code,(encoded,),timeout=180))
            if value.get('schema_version')!=1 or value.get('status') not in ('PASS','NEEDS_INPUT'):raise ValueError('invalid receipt')
            if value['status']!='PASS' and not value.get('issues'):raise ValueError('incomplete failure receipt')
            return value
        mark('检查软件包与 SO 静态兼容')
        try:
            inspection=inspect_materials()
            other_issues=[item for item in inspection.get('issues',[]) if item.get('field')!='m100_so']
            if not issues and not other_issues and all(field in resolved for field in ('package','camera_so','lidar_so')):
                from .package_assembly import prepare
                from .recipes import select
                recipe=select(self.cfg,resolved['package']['data']['sha256'],raw=True)
                assembling=bool(recipe and resolved['package']['data']['sha256']==(recipe.get('assembly') or {}).get('source_sha256'))
                if assembling:mark('按版本配方装配软件包')
                try:
                    prepared,assembly_result=prepare(self.cfg,self.store,service.registry,user,resolved['package'],resolved)
                    if assembly_result:
                        resolved['package']=prepared;request['package']=prepared['data']
                        checks.append({'field':'package','check':'release_assembly','status':'PASS','detail':'按已核验版本配方生成本人 APP 副本；原包保留，修改清单已记录'})
                        if wants_build and 'm100_so' not in resolved:
                            from .m100_build import reusable
                            cached,cached_receipt=reusable(self.cfg,user,prepared,resolved,assets)
                            if cached:resolved['m100_so']=cached;request['m100_so']=cached['data'];build_result=cached_receipt
                except GatewayError as exc:issue('package','RELEASE_ASSEMBLY_BLOCKED',str(exc),'补齐本包匹配的依赖，或请管理员核对版本装配记录；不自动换库。')
                mark('检查软件包与 SO 静态兼容')
            if wants_build and 'm100_so' not in resolved and not issues and not other_issues and all(field in resolved for field in ('package','camera_so','lidar_so')):
                from .m100_build import build,BuildError
                mark('准备M100编译产物')
                try:
                    resolved['m100_so'],build_result=build(self.cfg,self.store,service.registry,user,resolved['package'],
                                                        seconds=min(300,int(deadline-time.monotonic())-150),dependencies=resolved)
                    request['m100_so']=resolved['m100_so']['data']
                    mark('检查软件包与 SO 静态兼容')
                    inspection=inspect_materials()
                except BuildError as exc:
                    inspection['issues']=[item for item in inspection.get('issues',[]) if item.get('field')!='m100_so']
                    issue('m100_so',exc.code,str(exc),exc.next_action)
            elif assembly_result and not issues:
                inspection=inspect_materials()
            issues.extend(inspection.get('issues',[]));checks.extend(inspection.get('checks',[]))
        except Exception:issue('package','STATIC_CHECK_INCOMPLETE','工作站静态检查未完成','检查工作站日志、文件格式与大小；本次未连接台架，不自动重试')
        if issues:
            if draft.get('yaml_request') and 'package' not in resolved:
                issue('yaml_request','YAML_PACKAGE_REQUIRED','缺少可读取的软件包，未生成配置候选。','先补齐本人软件包，再检查材料。')
            return {'status':'NEEDS_INPUT','issues':issues,'checks':checks,'assets':resolved,'dataset':dataset,
                    'scenes':selected,'evidence':inspection.get('evidence',{}),'m100_build':build_result,'package_assembly':assembly_result,
                    'timings':{'total_seconds':round(time.monotonic()-started,3),'ssh_calls':timings},
                    'hardware_actions':'NOT_RUN','runtime_compatibility':'NOT_VERIFIED',
                    'diagnostic_policy':'材料存在明确缺口或不匹配，停止后续 YAML 解析和绑定；不自动换库或反复探索。'}
        yaml_result={}
        if draft.get('yaml_request'):
            if 'package' not in resolved:
                issue('yaml_request','YAML_PACKAGE_REQUIRED','YAML 任务副本尚未生成','先提供本人可读取的软件包，再检查配置变更')
            else:
                try:
                    from .yaml_plan import plan
                    from .yaml_runtime import prepare
                    mark('整理本次 YAML 修改要求')
                    remaining=deadline-time.monotonic()
                    if remaining<30:raise GatewayError('本轮材料检查时间不足以处理 YAML，请稍后重新检查')
                    planned=plan({**self.cfg,'yaml_plan_timeout_seconds':min(90,remaining-20)},draft['yaml_request'])
                    if planned.get('status')!='READY':
                        issue('yaml_request','YAML_DETAILS_REQUIRED',planned.get('question') or 'YAML 修改要求尚不明确','补充文件、完整字段路径及新值，或替换来源与 APP 内目标位置')
                    else:
                        remaining=deadline-time.monotonic()
                        if remaining<10:raise GatewayError('本轮材料检查时间不足，尚未启动 YAML 副本生成')
                        mark('生成本次 YAML 候选与差异')
                        yaml_result=prepare(self.cfg,service.registry.ssh,{'owner_root':request['owner_root'],
                            'preparation_id':job['id'],'package':resolved['package']['data'],'plan':planned,
                            'limits':{'duration_seconds':min(120,remaining-5)}},timeout=min(150,remaining))
                        if yaml_result.get('status')=='PREPARED' and yaml_result.get('outputs'):
                            validate_yaml_receipt(yaml_result,job,resolved['package']['data'],planned,request['owner_root'])
                            checks.append({'field':'yaml_request','check':'task_copy_prepared','status':'PASS','detail':'已生成本次 YAML 候选和差异；原软件包未修改，未应用到台架'})
                        elif yaml_result.get('status')=='NEEDS_INPUT':
                            for item in yaml_result.get('issues',[])[:12]:
                                issue('yaml_request',item.get('code','YAML_CHECK_FAILED'),item.get('message','YAML 任务副本未生成'),item.get('next_action','请核对文件和字段要求'))
                            if not yaml_result.get('issues'):raise ValueError('incomplete YAML receipt')
                        else:raise ValueError('invalid YAML receipt')
                except GatewayError as exc:
                    if yaml_result.get('status')=='PREPARED':yaml_result={'status':'NOT_VERIFIED'}
                    issue('yaml_request','YAML_PREPARATION_BLOCKED',str(exc),'补充明确的配置要求或请管理员检查 YAML 准备组件')
                except Exception:
                    if yaml_result.get('status')=='PREPARED':yaml_result={'status':'NOT_VERIFIED'}
                    issue('yaml_request','YAML_PREPARATION_INCOMPLETE','YAML 准备未完成；远端是否留下任务副本需核对','检查工作站与准备记录后重新检查；本次不自动重复修改')
        if cloud_plan and not issues:
            mark('下载回放数据到工作站');download_started=time.monotonic()
            try:
                from .cloud_datasets import CloudDatasets
                dataset=CloudDatasets(self.cfg,self.store).prepare(user,job,cloud_plan,
                    progress=lambda value:_download_progress(self.store,job,value))
                selected=list(range(1,dataset['data']['scene_count']+1))
                checks.append({'field':'dataset','check':'cloud_replay_prepared','status':'PASS',
                    'detail':'回放数据已落地本人工作站目录；GT未下载，本次来源和场景已冻结'})
            except GatewayError as exc:
                issue('dataset','CLOUD_DATASET_PREPARATION_FAILED',str(exc),'核对本次数据准备记录，或提供已接入的工作站路径；未提交台架。')
            finally:deadline+=time.monotonic()-download_started
        # Upload/evaluation is a later phase; lack of team credentials never blocks replay-only preparation.
        delivery='工作站结果目录'
        if draft.get('lpai_target') in ('team','personal'):delivery+='；LPAI 访问身份及上传端点尚待接入'
        return {'status':'NEEDS_INPUT' if issues else 'STATIC_PASSED','issues':issues,'checks':checks,
                'assets':resolved,'dataset':dataset,'scenes':selected,'evidence':inspection.get('evidence',{}),
                'yaml_preparation':yaml_result,'m100_build':build_result,'package_assembly':assembly_result,
                'timings':{'total_seconds':round(time.monotonic()-started,3),'ssh_calls':timings},
                'delivery':delivery,'hardware_actions':'NOT_RUN','runtime_compatibility':'NOT_VERIFIED',
                'adapter_binding':{'state':'BINDING'} if not issues and self.cfg.get('default_adapter_template') else 'REQUIRED_BEFORE_HARDWARE_QUEUE'}


class PreparationQueue:
    def __init__(self,cfg,store):
        self.cfg,self.store=cfg,store
        with store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS preparations(
              id TEXT PRIMARY KEY,owner TEXT NOT NULL,session TEXT NOT NULL,fingerprint TEXT NOT NULL,
              state TEXT NOT NULL,payload TEXT NOT NULL,result TEXT,created REAL NOT NULL,updated REAL NOT NULL,
              sent INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0)''')
            if 'request_id' not in {row['name'] for row in c.execute('PRAGMA table_info(preparations)')}:
                c.execute('ALTER TABLE preparations ADD COLUMN request_id TEXT')

    def submit(self,user,session,*,expected_task=None):
        self.store.require_user(user)
        from .tasks import Tasks
        task=Tasks(self.store).ensure(user,session)
        if task['job_id']:raise GatewayError('当前任务已提交；直接补充新材料即可用于下一次回放，已提交材料保持不变。')
        draft=Intake(self.store).get(user)
        if not draft:raise GatewayError('请直接提供软件包链接或 SO、YAML；“帮助”可查看材料清单。')
        if not any(draft.get(k) for k in ('package','camera_so','lidar_so','dataset')):
            raise GatewayError('尚未提供材料，请先补充软件包、相机/雷达 SO 和数据集来源')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if self.cfg.get('require_material_confirmation'):
                from .confirmations import active
                current_confirm=c.execute('SELECT * FROM task_requests WHERE id=? AND owner=?',(task['id'],user)).fetchone()
                if not current_confirm or not active(c,user,dict(current_confirm),session):
                    raise GatewayError('材料已登记，下载、编译和检查将在卡片“确认执行”后开始。')
            if expected_task:
                current=c.execute('SELECT t.* FROM task_requests t JOIN task_focus f ON f.task_id=t.id WHERE t.owner=? AND f.owner=?',(user,user)).fetchone()
                auto=c.execute('SELECT * FROM automatic_tasks WHERE task_id=? AND owner=?',(expected_task['task_id'],user)).fetchone()
                if (not current or current['id']!=expected_task['task_id'] or current['revision']!=expected_task['revision']
                        or current['job_id'] or fingerprint(json.loads(current['data']))!=expected_task['fingerprint']
                        or not auto or auto['state']!='WAITING' or auto['revision']!=current['revision']):
                    raise GatewayError('当前材料或自动处理状态已变化，未启动检查。')
            active=c.execute("SELECT * FROM preparations WHERE owner=? AND (request_id=? OR (request_id IS NULL AND ?=1)) AND (state IN ('QUEUED','RUNNING') OR (state='STATIC_PASSED' AND json_extract(result,'$.adapter_binding.state')='BINDING'))",(user,task['id'],task['legacy'])).fetchone()
            if active:return f"已有材料检查 {active['id']}，不重复启动。可发送“查看准备状态”；当前检查使用提交时草稿，后续修改需另行检查。"
            pid='prep-'+uuid.uuid4().hex[:16];now=time.time()
            c.execute('INSERT INTO preparations(id,owner,session,fingerprint,state,payload,created,updated,request_id) VALUES(?,?,?,?,?,?,?,?,?)',
                      (pid,user,session,fingerprint(draft),'QUEUED',json.dumps(draft,ensure_ascii=False),now,now,task['id']))
            c.execute('UPDATE task_requests SET prep_id=?,session=?,updated=? WHERE owner=? AND id=?',(pid,session,now,user,task['id']))
        return f'已受理材料检查：{pid}。不占台架；缺少 M100 时会尝试已配置的工作站编译。完成后回复，有问题会直接指出需要补齐的材料。'

    def latest(self,user):
        self.store.require_user(user)
        from .tasks import Tasks
        task=Tasks(self.store).current(user)
        with self.store.connect() as c:
            if task:
                row=c.execute('SELECT * FROM preparations WHERE owner=? AND (request_id=? OR (request_id IS NULL AND ?=1)) ORDER BY created DESC,id DESC LIMIT 1',(user,task['id'],task['legacy'])).fetchone()
            else:row=c.execute('SELECT * FROM preparations WHERE owner=? ORDER BY created DESC,id DESC LIMIT 1',(user,)).fetchone()
        return self.store.unpack(row) if row else None

    def summary(self,user,job=None):
        self.store.require_user(user)
        job=job or self.latest(user)
        if not job:return '尚未提交材料检查。请先提供材料，再发送“检查材料”。'
        if job['owner']!=user:raise GatewayError('不能查看其他用户的材料检查')
        from .business import render_review
        from .bindings import KIND,validate_candidate
        current=Intake(self.store).get(user) or {}
        if job.get('request_id'):
            from .tasks import Tasks
            current=Tasks(self.store).get(user,job['request_id'])['data']
        stale=fingerprint(current)!=job['fingerprint']
        with self.store.connect() as c:
            relevant=[self.store.unpack(r) for r in c.execute('SELECT * FROM baselines WHERE owner=?',(user,))]
        relevant=[b for b in relevant if b['data'].get('kind')==KIND and b['data'].get('preparation_id')==job['id']]
        valid=[]
        for baseline in relevant:
            try:validate_candidate(self.cfg,self.store,user,baseline,allow_other_task=True);valid.append(baseline)
            except GatewayError:pass
        chosen=valid[0] if len(valid)==1 else (relevant[-1] if relevant else None)
        stale=stale or bool(relevant and len(valid)!=1)
        lines=[('材料任务：'+job['request_id']+'\n' if job.get('request_id') else '')+render_review(current,job,chosen,self.store.jobs(user),stale=stale,evaluator=self.cfg.get('default_evaluator'))]
        if stale:lines.append('草稿已变化或旧绑定已过期；请对当前材料重新检查。')
        result=job.get('result') or {}
        active=job['state']=='RUNNING' or (job['state']=='STATIC_PASSED' and isinstance(result.get('adapter_binding'),dict)
                and result['adapter_binding'].get('state')=='BINDING')
        progress=result.get('progress') or {}
        if active and isinstance(progress,dict) and progress.get('phase') in PHASES:
            now=time.time();total=_elapsed(progress.get('started_at'),now);phase_time=_elapsed(progress.get('phase_started_at'),now)
            detail='；本阶段已用 '+phase_time if phase_time is not None else ''
            if total is not None:detail+='，本轮累计 '+total
            lines.append('当前实际阶段：'+progress['phase']+detail+'。')
            if stale:lines.append('上述进度属于已提交的材料快照；当前草稿需要另行检查。')
        passed=[LABELS.get(c['field'],c['field']) for c in result.get('checks',[]) if c.get('status')=='PASS']
        if passed:lines.append('已完成静态检查项：'+'、'.join(dict.fromkeys(passed))+'。')
        if job['state']=='NEEDS_INPUT':lines.append('已停止本轮检查，不反复尝试。')
        lines.append('本次材料检查未进入台架队列；静态通过不代表运行兼容或已回放。')
        return '\n'.join(lines)

    def finish(self,job,result):
        state=result.get('status','NEEDS_INPUT')
        if state not in ('STATIC_PASSED','NEEDS_INPUT'):raise GatewayError('材料检查结果无效')
        with self.store.connect() as c:
            c.execute("UPDATE preparations SET state=?,result=?,updated=? WHERE id=? AND state='RUNNING'",
                      (state,json.dumps(result,ensure_ascii=False),time.time(),job['id']))

    def recover(self):
        with self.store.connect() as c:rows=[self.store.unpack(r) for r in c.execute("SELECT * FROM preparations WHERE state='RUNNING'")]
        for row in rows:self.finish(row,{'status':'NEEDS_INPUT','issues':[{'field':'package','code':'INTERRUPTED','message':'材料检查进程曾中断，已保存输入','next_action':'核对工作站文件后重新检查材料；不会自动重复下载或操作台架'}]})
        with self.store.connect() as c:
            rows=[self.store.unpack(r) for r in c.execute("SELECT * FROM preparations WHERE state='STATIC_PASSED' AND json_extract(result,'$.adapter_binding.state')='BINDING'")]
            for row in rows:
                result=row['result'];result['adapter_binding']={'state':'NEEDS_INPUT','message':'适配器绑定进程中断；请重新检查材料或由管理员核对已有绑定收据'}
                result.pop('progress',None)
                c.execute('UPDATE preparations SET result=?,sent=0,updated=? WHERE id=?',(json.dumps(result,ensure_ascii=False),time.time(),row['id']))

    def tick(self,checker=None):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT 1 FROM preparations WHERE state='RUNNING' OR (state='STATIC_PASSED' AND json_extract(result,'$.adapter_binding.state')='BINDING')").fetchone():return None
            row=c.execute("SELECT * FROM preparations WHERE state='QUEUED' ORDER BY created,id LIMIT 1").fetchone()
            if not row:return None
            job=self.store.unpack(row)
            c.execute("UPDATE preparations SET state='RUNNING',updated=? WHERE id=?",(time.time(),job['id']))
        try:
            self.store.require_user(job['owner'])
            result=(checker or MaterialCheck(self.cfg,self.store)).run(job)
            if not isinstance(result,dict) or result.get('status') not in ('STATIC_PASSED','NEEDS_INPUT'):
                raise GatewayError('材料检查结果无效')
        except Exception:
            result={'status':'NEEDS_INPUT','issues':[{'field':'package','code':'CHECK_FAILED','message':'本次材料检查未完成','next_action':'检查工作站连接与文件权限后重新检查；未执行台架任务'}]}
        if result.get('status')=='STATIC_PASSED' and self.cfg.get('default_adapter_template'):
            result['adapter_binding']={'state':'BINDING'}
            progress=_record_progress(self.store,job,'绑定本次回放适配器')
            if progress:result['progress']=progress
        self.finish(job,result)
        if result.get('status')=='STATIC_PASSED' and self.cfg.get('default_adapter_template'):
            binding_started=time.monotonic()
            try:
                from .bindings import bind_preparation
                baseline=bind_preparation(self.cfg,self.store,job['owner'],preparation_id=job['id'])
                binding={'state':'READY_FOR_GATE','baseline_name':baseline['name'],'input_digest':baseline['data']['binding']['input_digest']}
            except GatewayError as exc:binding={'state':'NEEDS_INPUT','message':str(exc)}
            except Exception:binding={'state':'NEEDS_INPUT','message':'工作站适配器绑定未完成，请检查连接和材料；不会自动重试或提交台架任务'}
            result['adapter_binding']=binding
            result.setdefault('timings',{})['binding_seconds']=round(time.monotonic()-binding_started,3)
            result.pop('progress',None)
            with self.store.connect() as c:
                c.execute('UPDATE preparations SET result=?,sent=0,updated=? WHERE id=? AND state=\'STATIC_PASSED\'',
                          (json.dumps(result,ensure_ascii=False),time.time(),job['id']))
        return job['id']

    def completion_text(self,user,job):
        from .tasks import Tasks
        current=Tasks(self.store).get(user,job['request_id'])['data'] if job.get('request_id') else Intake(self.store).get(user)
        label='任务 '+(job.get('request_id') or job['id'])+'：'
        if fingerprint(current or {})!=job['fingerprint']:
            return label+'提交后材料已有更新，本次检查结果保留。请重新检查当前材料；尚未提交台架。'
        result=job.get('result') or {};binding=result.get('adapter_binding') or {}
        if job['state']=='NEEDS_INPUT':
            items=result.get('issues') or []
            details=['、'.join(filter(None,(LABELS.get(x.get('field'),'材料'),str(x.get('message',''))[:180],str(x.get('next_action',''))[:220]))) for x in items[:3]]
            return label+'材料准备已停止，未占台架。\n'+('\n'.join(details) or '请管理员核对本次准备记录。')+'\n其他已收材料保留，只需补充或修正上述项目，再说“检查材料”。'
        if isinstance(binding,dict) and binding.get('state')=='READY_FOR_GATE':
            compiled='M100已在工作站准备并核验，无需上传。' if (result.get('m100_build') or {}).get('state') in ('BUILT','REUSED') else ''
            if result.get('package_assembly'):compiled+='APP已按核验配方装配，原始包保留。'
            if self.cfg.get('auto_execute_on_complete'):
                with self.store.connect() as c:
                    automatic=c.execute("SELECT state FROM automatic_tasks WHERE task_id=? AND owner=?",(job.get('request_id'),user)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automatic_tasks'").fetchone() else None
                if automatic and automatic['state'] in ('WAITING','CHECKING','SUBMITTED'):
                    return label+'材料检查通过，已进入自动处理流程。'+compiled
            return label+'材料检查通过，待本次小场景预检。'+compiled+'\n回复“执行回放”提交；回复“查看任务单”核对详细版本与配置。'
        message=binding.get('message','回放配置尚未接入，请管理员处理。') if isinstance(binding,dict) else '回放配置尚未接入，请管理员处理。'
        return label+'材料静态检查已通过，暂不能提交台架：'+str(message)[:300]

    def deliver(self,sender,deadline=None):
        if not self.cfg.get('notifications_enabled'):return 0
        with self.store.connect() as c:
            active=[self.store.unpack(row) for row in c.execute("SELECT * FROM preparations WHERE state='RUNNING' OR (state='STATIC_PASSED' AND json_extract(result,'$.adapter_binding.state')='BINDING')")]
        for row in active:
            progress=(row.get('result') or {}).get('progress') or {};now=time.time()
            phase=progress.get('phase');started=progress.get('phase_started_at',now)
            if phase in PHASES and type(started) in (int,float) and now-started>=45:
                from .progress import notify_input
                bucket=int((now-started)//60)
                notify_input(self.store,row['owner'],row['session'],'preparation-wait:'+row['id']+':'+phase+':'+str(bucket),
                             '材料准备仍在进行：'+phase+'，本阶段已用 '+(_elapsed(started,now) or '不足1秒')+'。无需重复提交。')
        with self.store.connect() as c:
            rows=[self.store.unpack(r) for r in c.execute("SELECT * FROM preparations WHERE state IN ('STATIC_PASSED','NEEDS_INPUT') AND sent=0 AND next_attempt<=? AND COALESCE(json_extract(result,'$.adapter_binding.state'),'')!='BINDING' ORDER BY created LIMIT 10",(time.time(),))]
        count=0
        for row in rows:
            if deadline is not None and time.monotonic()>=deadline:break
            from .input_cards import enabled as unified_enabled,route
            if unified_enabled(self.cfg):
                routed=route(self.store,{'event_key':'preparation:'+row['id']+':complete','owner':row['owner'],'session':row['session']})
                if routed=='CARD':
                    with self.store.connect() as c:c.execute('UPDATE preparations SET sent=1 WHERE id=? AND owner=?',(row['id'],row['owner']))
                    continue
                if routed=='WAIT':continue
            try:
                self.store.require_user(row['owner'])
                text=self.completion_text(row['owner'],row)
                if deadline is not None and time.monotonic()>=deadline:break
                ok=sender({'session':row['session'],'text':text,'attachment':None})
            except Exception as exc:
                if getattr(exc,'notification_budget_expired',False):break
                ok=False
            delay=min(300,max(5,self.cfg.get('notification_retry_seconds',5))*2**min(row['attempts'],6))
            with self.store.connect() as c:c.execute('UPDATE preparations SET sent=?,attempts=attempts+1,next_attempt=? WHERE id=?',(int(bool(ok)),time.time()+delay,row['id']))
            count+=int(bool(ok))
        return count
