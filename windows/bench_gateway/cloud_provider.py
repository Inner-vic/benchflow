"""Cloud execution for new evaluation jobs; the pinned source inspector is reused."""
import base64
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time

from .core import EvaluationOutcomeUnknown, GatewayError, owner_key, within
from .config import save
from .transfer import SSH
from . import workstation_evaluation as workstation


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def profile_for_job(cfg, job):
    """Append-only epochs preserve the backend of already registered jobs."""
    policies=cfg.get('cloud_evaluation_profiles') or []
    epochs=[p['effective_after'] for p in policies]
    if epochs!=sorted(set(epochs)):
        raise GatewayError('云端评测策略版本记录无效，已有结果保留。')
    eligible=[p for p in policies if p['effective_after']<=job['created']]
    if not eligible:
        return None
    profile=eligible[-1]['profile']
    body={k:v for k,v in profile.items() if k!='profile_sha256'}
    if hashlib.sha256(canonical(body)).hexdigest()!=profile.get('profile_sha256'):
        raise GatewayError('云端评测配置内容与已登记版本不一致。')
    return profile


def task_gt_binding(cfg, dataset, created, profile=None):
    """Reject an unassociated new task before it can use a historical default."""
    from .cloud_truth import POLICY,binding_from_dataset
    profile=profile if profile is not None else profile_for_job(cfg,{'created':created})
    cutoff=cfg.get('evaluation_gt_binding_required_after')
    if cutoff is not None and created>=cutoff and (profile or {}).get('gt_policy')!=POLICY:
        raise GatewayError('本次评测尚未绑定数据集对应的GT；已停止，不能默认使用V151真值。回灌结果保留。')
    if (profile or {}).get('gt_policy')==POLICY:
        try:return binding_from_dataset(dataset,profile)
        except ValueError:raise GatewayError('本次数据集尚未明确关联云端GT；请管理员关联该数据源对应的GT路径，或选择仅回灌。不会默认使用V151真值。') from None
    return None


def frozen_gt_binding(cfg,context,job,profile):
    """A new evaluation may bind GT to old predictions without rewriting them."""
    binding=job['payload'].get('evaluation_gt_binding')
    if binding is None:return task_gt_binding(cfg,context.get('dataset'),job['created'],profile)
    from .cloud_truth import POLICY,validate_binding
    try:
        if profile.get('gt_policy')!=POLICY:raise ValueError('historical policy cannot override GT')
        validate_binding(binding,profile)
        source=context['dataset']
        identity={k:source[k] for k in ('remote_path','manifest','sha256')}
        if binding['source_identity']!=hashlib.sha256(canonical(identity)).hexdigest():raise ValueError('GT source identity mismatch')
    except (ValueError,KeyError,TypeError):
        raise GatewayError('本次GT关联与原回灌数据源不一致，未提交评测。') from None
    return binding


def transport(cfg, request):
    process=subprocess.run([cfg['cloud_sdk_python'],'-B',str(Path(cfg['project_root'])/'bench_gateway/cloud_storage.py')],
                           input=json.dumps(request),text=True,encoding='utf-8',capture_output=True,timeout=300)
    try:
        value=json.loads(process.stdout)
        if process.returncode or value.get('ok') is not True:
            raise ValueError('SDK response failed')
        return value['data']
    except (ValueError,KeyError,TypeError):
        raise GatewayError('云端存储连接或授权未通过，原回灌结果已保留。') from None


def ready(cfg, profile):
    status=transport(cfg,{'action':'heartbeat','remote_prefix':profile['volume_prefix']})
    age=time.time()-status.get('checked_at',0)
    if (status.get('profile_sha256')!=profile['profile_sha256'] or status.get('instance')!=profile['instance']
            or status.get('state') not in ('READY','EVALUATING') or not -30<=age<=60):
        if cfg.get('cloud_browser_autostart') is True:
            from .cloud_bootstrap import EnvironmentTool
            import os
            tool=EnvironmentTool(cfg,profile,
                binary=str(Path(os.environ['LOCALAPPDATA'])/'bin/lpai-agent.exe'),
                control_root=Path(cfg['project_root'])/'runtime/cloud-environment-control',backend='browser')
            result=tool.ensure(allow_start=True,wait_seconds=120)
            if result['state']=='READY':
                # Independently check the live receiver again. No evaluation
                # is exported/submitted until normal readiness checks pass.
                return ready({**cfg,'cloud_browser_autostart':False},profile)
            reason=result.get('reason') or '请管理员查看环境启动记录。'
            raise GatewayError('云端评测环境暂未就绪（'+result['state']+'）。'+reason+' 已有回灌结果保留，无需重新回灌。')
        raise GatewayError('云端评测实例未就绪，请管理员启动 bench-eval-offline；已有回灌结果保留，无需重新回灌。')
    if cfg.get('cloud_browser_autostart') is True:
        # A previous bounded wait may have ended just before startup completed.
        # Reconcile its durable intent once, so a later pause is a new startup
        # rather than remaining blocked by the old pending operation forever.
        control=Path(cfg['project_root'])/'runtime/cloud-environment-control'
        operation=control/'operation.json'
        try:pending=operation.exists() and json.loads(operation.read_text(encoding='utf-8')).get('state')!='READY'
        except (OSError,ValueError,AttributeError):raise GatewayError('云端环境启动记录无效，请管理员核验；不自动覆盖。') from None
        if pending:
            from .cloud_bootstrap import EnvironmentTool
            import os
            tool=EnvironmentTool(cfg,profile,binary=str(Path(os.environ['LOCALAPPDATA'])/'bin/lpai-agent.exe'),
                                 control_root=control,backend='browser')
            result=tool.ensure(allow_start=False,wait_seconds=0)
            if result['state']!='READY':raise GatewayError('云端环境恢复结果待核验；不重复启动或提交评测。')
    return status


def export_predictions(cfg, context, source, directory):
    """One SSH export, no content hashing and no data passed through chat."""
    data={'scenes':source['scene_names'],'required_tasks':source['discovery']['required_tasks'],
          'prediction_path':source['prediction_path'],'owner_root':context['owner_root'],'job_id':context['run_id']}
    code='DATA='+repr(data)+r'''
import json,pathlib,tarfile
root=pathlib.Path(DATA['owner_root'])/'evaluations'/DATA['job_id']/'cloud-export'
root.mkdir(parents=True,exist_ok=False)
pred=pathlib.Path(DATA['prediction_path']).resolve(strict=True);items=[];groups={};frames=set()
for scene in DATA['scenes']:
 path=(pred/scene).resolve(strict=True);assert pred in path.parents and path.is_dir()
 for frame in sorted(path.iterdir()):
  if not frame.is_dir():continue
  assert pred in frame.resolve().parents
  for p in sorted(frame.iterdir()):
   role='trajectory' if p.name.startswith('pred_traj_') and p.suffix=='.npy' else 'vision' if p.name.startswith('pred_img_') and p.suffix=='.json' else 'road' if p.name.startswith('pred_road_') and p.suffix=='.npy' else None
   if role not in DATA['required_tasks']:continue
   assert p.is_file() and pred in p.resolve().parents
   items.append((p,'predictions/'+p.relative_to(pred).as_posix()));groups[role]=groups.get(role,0)+1;frames.add(str(frame))
assert items and all(groups.get(role,0) for role in DATA['required_tasks'])
archive=root/'predictions.tar.gz'
with tarfile.open(archive,'w:gz',compresslevel=1) as tar:
 for p,name in items:tar.add(p,arcname=name,recursive=False)
print(json.dumps({'path':str(archive),'bytes':archive.stat().st_size,'files':len(items),'candidate_frames':len(frames),'groups':groups}))
'''
    ssh=SSH(cfg)
    exported=json.loads(ssh.python(code,timeout=120))
    path=directory/'predictions.tar.gz'
    with path.open('xb') as output:
        result=subprocess.run(ssh.argv()+['cat -- '+shlex.quote(exported['path'])],stdout=output,
                              stderr=subprocess.PIPE,timeout=180)
    if result.returncode or path.stat().st_size!=exported['bytes']:
        raise GatewayError('回灌预测文件传输未完成；未自动重试。')
    save(directory/'export.json',exported)
    return exported


def paths(cfg, store, job, profile):
    cloud_id='cloud-eval-'+time.strftime('%Y%m%d',time.gmtime(job['created']))+'-'+hashlib.sha256((job['owner']+'\0'+job['id']).encode()).hexdigest()[:12]
    suffix=owner_key(job['owner'])+'/'+cloud_id+'/'
    local=within(store.user_root(job['owner']),store.user_root(job['owner'])/'reports'/job['id'])
    return cloud_id,local,profile['volume_prefix']+suffix,profile['run_root']+'/'+suffix.rstrip('/')


def evaluate(cfg, store, job, collect_only=False):
    profile=profile_for_job(cfg,job)
    if profile is None:
        raise GatewayError('该任务未绑定云端评测策略。')
    # Validate the policy before the source context can read any remote data.
    from .cloud_truth import POLICY
    if profile.get('gt_policy')!=POLICY:task_gt_binding(cfg,None,job['created'],profile)
    context=workstation._context(cfg,store,job)
    binding=frozen_gt_binding(cfg,context,job,profile)
    if binding is not None:
        from .cloud_prediction_source import inspect
        source=inspect(cfg,context)
    else:source=workstation.resolve_task(cfg,store,job)
    cloud_id,local,prefix,remote=paths(cfg,store,job,profile)
    local.mkdir(parents=True,exist_ok=True)
    checkpoint=local/'cloud-plan.json'
    native_request={'run_id':cloud_id,'owner':job['owner'],'owner_key':owner_key(job['owner']),
                    'source_run':context['expected_run'],'source_job':context['source_job'],'job_id':job['id'],
                    'scenes':source['scene_names'],'required_tasks':source['discovery']['required_tasks'],
                    **({'truth_frames':{r['scene']:r['truth_frames'] for r in source['discovery']['scene_inputs']}} if binding is None else {}),
                    'tool_hashes':profile['tool_hashes'],'profile_sha256':profile['profile_sha256']}
    if binding is not None:native_request['gt_binding']=binding
    dispatched=checkpoint.exists()
    if dispatched:
        saved=json.loads(checkpoint.read_text(encoding='utf-8'))
        if saved['request']!=native_request or saved['source_identity']!=source['source_identity']:
            raise GatewayError('已登记的云端评测输入发生变化，仅保留原结果，不重新提交。')
    elif collect_only:
        raise GatewayError('尚无云端派发记录，续收不会创建新任务。')
    else:
        if job['state'] not in ('QUEUED','RUNNING'):
            raise GatewayError('已结束或待核验任务只能续收。')
        ready(cfg,profile)
        store.stage(job,'结果评测：上传所需回灌预测到开发空间')
        save(checkpoint,{'state':'EXPORTING','request':native_request,'source_identity':source['source_identity'],
                         'remote_attempt':remote,'profile':profile})
        export_predictions(cfg,context,source,local)
        save(local/'request.json',native_request)
        key_root=Path(cfg.get('cloud_signing_root',str(Path(cfg['project_root'])/'runtime/cloud-signing')))
        private=within(key_root,key_root/cfg.get('cloud_signing_private_key','request-private.pem'))
        signed=subprocess.run([cfg['cloud_node_binary'],str(Path(cfg['project_root'])/'tools/cloud_sign.js'),
                               'sign',str(private),str(local/'request.json'),str(local/'request.sig')],
                              capture_output=True,timeout=20)
        if signed.returncode:
            raise GatewayError('本机云端请求签名失败，尚未提交评测。')
        # Any error after this durable boundary is reconciled at the same cloud ID.
        save(checkpoint,{'state':'SUBMITTING','request':native_request,'source_identity':source['source_identity'],
                         'remote_attempt':remote,'profile':profile})
        try:
            upload=transport(cfg,{'action':'upload','remote_prefix':prefix,'local_directory':str(local),
                                  'files':['predictions.tar.gz','request.json','request.sig']})
            save(local/'cloud-upload.json',upload)
        except Exception:
            raise EvaluationOutcomeUnknown('云端提交结果待核验；仅续收 '+remote+'，不自动重跑。') from None
    last=None
    until=time.monotonic()+(1 if collect_only else 2700)
    while True:
        try:
            status=transport(cfg,{'action':'status','remote_prefix':prefix})
        except Exception:
            raise EvaluationOutcomeUnknown('云端连接中断，结果待核验；仅续收 '+remote+'，不重新计算。') from None
        current=status.get('state')
        if current in ('SUCCEEDED','PARTIAL_SUCCESS'):
            break
        if current in ('FAILED','REJECTED'):
            raise GatewayError('云端评测未完成：'+str(status.get('error','原因待核验'))[:400]+'；回灌结果已保留，不自动重跑。')
        if current!=last:
            store.stage(job,'结果评测：云端'+{'PREPARING':'匹配GT并准备输入','EVALUATING':'正在计算'}.get(current,'等待接收本次任务'))
            last=current
        if time.monotonic()>=until:
            raise EvaluationOutcomeUnknown('云端评测结果待核验；固定任务目录 '+remote+'；仅续收，不自动重跑。')
        time.sleep(5)
    try:
        payload=transport(cfg,{'action':'collect','remote_prefix':prefix,'cloud_directory':remote})
    except Exception:
        raise EvaluationOutcomeUnknown('云端已结束但TXT尚未续收；保留固定目录 '+remote+'，不重新计算。') from None
    native=payload['receipt']
    raw=base64.b64decode(payload['report_base64'],validate=True)
    if not raw or len(raw)>workstation.MAX_REPORT:
        raise GatewayError('云端原始TXT为空或超限。')
    report=local/'evaluation-report.txt'
    workstation._write_once(report,raw)
    receipt={'schema_version':1,'kind':'lpai-mounted-v1','job_id':job['id'],'source_job':context['source_job'],
             'source_task':context['source_task'],'owner_key':owner_key(job['owner']),'profile_sha256':profile['profile_sha256'],
             'remote_attempt':remote,'native_receipt':native,'local_report':str(report),
             'report_sha256':hashlib.sha256(raw).hexdigest(),'source':source}
    if binding is not None:receipt['gt_binding']=binding
    result=validate_receipt(cfg,store,job,receipt,source=source)
    path=local/'gateway-evaluation-receipt.json'
    workstation._write_once(path,canonical(receipt)+b'\n')
    result['receipt_path']=str(path)
    workstation._write_once(local/'evaluation-summary.json',canonical(result)+b'\n')
    return result


def recover_receipt(cfg,store,job,receipt):
    """A supplied local receipt cannot replace the actual saved cloud result."""
    profile=profile_for_job(cfg,job)
    _,local,_,_=paths(cfg,store,job,profile)
    result=evaluate(cfg,store,job,collect_only=True)
    actual=json.loads((local/'gateway-evaluation-receipt.json').read_text(encoding='utf-8'))
    if actual!=receipt:
        raise GatewayError('本地评测收据与原云端结果不一致，保持待核验。')
    return result


def validate_receipt(cfg,store,job,receipt,source=None):
    profile=profile_for_job(cfg,job)
    context=workstation._context(cfg,store,job)
    if source is None:
        if profile.get('gt_policy')=='task-dataset-v1':
            from .cloud_prediction_source import inspect
            source=inspect(cfg,context)
        else:source=workstation.resolve_task(cfg,store,job)
    cloud_id,local,prefix,remote=paths(cfg,store,job,profile)
    native=receipt.get('native_receipt') or {}
    from .cloud_truth import POLICY,binding_from_dataset
    if profile.get('gt_policy')==POLICY:
        try:binding=frozen_gt_binding(cfg,context,job,profile)
        except ValueError:raise GatewayError('本次评测的GT来源关联无效，原结果保留。') from None
        if receipt.get('gt_binding')!=binding or native.get('gt_binding')!=binding:
            raise GatewayError('评测收据的GT与本次冻结数据集不一致。')
    if (receipt.get('kind')!='lpai-mounted-v1' or receipt.get('job_id')!=job['id']
            or receipt.get('owner_key')!=owner_key(job['owner']) or receipt.get('source_job')!=context['source_job']
            or receipt.get('source_task')!=context['source_task'] or receipt.get('remote_attempt')!=remote
            or receipt.get('profile_sha256')!=profile['profile_sha256']
            or native.get('run_id')!=cloud_id or native.get('owner')!=job['owner']
            or native.get('source_run')!=context['expected_run']):
        raise GatewayError('云端评测收据与本人任务不匹配。')
    report=within(local,receipt.get('local_report',''))
    raw=report.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=receipt.get('report_sha256') or receipt['report_sha256']!=native.get('report_sha256'):
        raise GatewayError('原始TXT与云端收据不一致。')
    parser=workstation._module(cfg,'scripts/runtime/parse_eval_report.py')
    scope=workstation._module(cfg,'scripts/report_scope.py')
    parsed=parser.parse_report(report)
    coverage=scope.parse_scene_coverage(raw.decode('utf-8'),source['scene_names'])
    expected=(native.get('gt_frames',{}) if profile.get('gt_policy')==POLICY else {r['scene']:r['truth_frames'] for r in source['discovery']['scene_inputs']})
    if set(expected)!=set(source['scene_names']):raise GatewayError('云端GT场景范围与本次任务不一致。')
    if (parsed['metrics']!=native.get('metrics') or coverage['scenes']!=native.get('scene_coverage')
            or any(r['truth_frames']!=expected[r['scene']] for r in coverage['scenes'])):
        raise GatewayError('云端TXT指标或场景覆盖与实际任务不一致。')
    warnings=[]
    missing=set(source['discovery']['required_tasks'])-set(parsed['evaluated_tasks'])
    if missing:
        warnings.append('REQUESTED_TASKS_OR_VALID_SAMPLES_INCOMPLETE')
        if 'vision' in missing and 'VISION_GT_MISSING' in source['discovery'].get('warnings',[]):
            warnings.append('VISION_GT_MISSING')
    if any(r['completed_frames']!=r['truth_frames'] for r in coverage['scenes']):
        warnings.append('SCENE_FRAME_COVERAGE_INCOMPLETE')
    if native.get('source_data_hash_passes')!=0 or native.get('input_content_verified') is not False:
        raise GatewayError('云端评测数据读取策略不符合当前约定。')
    return {'task':context['source_task'],'report':str(report),'local_report':str(report),
            'metrics':parsed['metrics'],'evaluation_state':'PARTIAL' if warnings else 'COMPLETE','warnings':warnings,
            'evaluated_scenes':source['scene_names'],'scene_coverage':coverage['scenes'],
            'remote_attempt':remote,'execution_backend':'lpai-mounted-v1','profile_sha256':profile['profile_sha256'],
            'native_seconds':native['native_seconds']}
