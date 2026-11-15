"""Owner-scoped bridge to the existing pinned workstation offline evaluator.

The upstream scripts are reused byte for byte. This bridge supplies source
identity, an isolated attempt and gateway receipts; it never contacts a bench,
uploads to LPAI, installs packages or accepts executable commands from a job.
"""
import base64
import hashlib
import importlib.util
import json
import re
from pathlib import Path, PurePosixPath

from .core import EvaluationOutcomeUnknown, GatewayError, owner_key, within
from .transfer import SSH
from .evaluation_direct import POLICY


KIND='workstation-offline-v1'
SAFE=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
MAX_REPORT=16*1024*1024
LEGACY_BRIDGE='35c2de681781c7c1d28012a33abf90f77a9b13de611a70619ef244029c950028'
FILES={
    'scripts/evaluate_directory.py':'d62d89d6453254fa5ac7eaaba906763e6f6480e54f8a13f1951ffc262efa7607',
    'scripts/directory_worker.py':'7531544e94626c2a2e66322b09e0cb5110eb171136ed933e4e5fee2071b5e56a',
    'scripts/report_scope.py':'2d8617dd331887abfea78bc8e3787bd61c1b1a4309d2594cce3eacac4c5d2e3b',
    'scripts/shared_runtime.py':'9d4998201c9aebbf64170dbe5f17d8b97c82106bb26160b3f3126b18bc7b331a',
    'scripts/runtime_support.py':'432eccb26871ba7c3acd53181f10152291adbd87e9931209173fadab3a59fecd',
    'scripts/runtime/parse_eval_report.py':'6a03148eacd54710ba29d67079489cb7288af2b4e5e4c9403532242db33dabf9',
    'assets/shared-workstation.json':'2fafd73675120dfc1a3ad79fd9d7fbabbb0f9ed9245294bf8ba0c0819fe868f3',
}


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def identity(value):return hashlib.sha256(canonical(value)).hexdigest()


def verify_vendor(cfg):
    root=Path(cfg['project_root'])/'vendor/offline_eval'
    try:
        manifest=json.loads((root/'provenance.json').read_text(encoding='utf-8'))
        if manifest.get('schema_version')!=1 or set(manifest.get('files',{}))!=set(FILES):raise ValueError('inventory')
        for name,expected in FILES.items():
            path=root/name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError(name)
            record=manifest['files'][name]
            if record.get('sha256')!=expected or record.get('size_bytes')!=path.stat().st_size:raise ValueError(name)
        pin=json.loads((root/'assets/shared-workstation.json').read_text(encoding='utf-8'))
        if pin.get('schema_version')!=1 or pin.get('download_or_deploy') is not False:raise ValueError('pin')
        return {'root':root,'hashes':dict(FILES),'pin':pin}
    except (OSError,ValueError,KeyError,TypeError):
        raise GatewayError('工作站评测组件或固定版本校验未通过，请管理员核验部署；尚未计算评测') from None


def pin_contract(cfg):return json.loads(canonical(verify_vendor(cfg)['pin']))


def descriptor(cfg):
    bundle=verify_vendor(cfg)
    result={'schema_version':1,'kind':KIND,'implementation_sha256':bundle['hashes'],'pin':bundle['pin'],
            'bridge_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'input_verification':POLICY,
            'direct_worker_sha256':hashlib.sha256(Path(__file__).with_name('evaluation_direct.py').read_bytes()).hexdigest(),
            'validity':{'minimum_candidate_frames':1,'minimum_completed_frames':1,'required_tasks':[]},
            'required_tasks':None,'timeout_seconds':9000,'uploads':False,'requires_bench':False}
    result['descriptor_sha256']=identity(result)
    return result


def legacy_descriptor(cfg):
    """Recognize the exact previously deployed contract for saved receipts."""
    value=descriptor(cfg)
    for key in ('descriptor_sha256','input_verification','direct_worker_sha256'):value.pop(key)
    value['bridge_sha256']=LEGACY_BRIDGE
    value['descriptor_sha256']=identity(value)
    return value


def validate_descriptor(cfg,value):
    if not isinstance(value,dict) or canonical(value) not in (canonical(descriptor(cfg)),canonical(legacy_descriptor(cfg))):
        raise GatewayError('冻结的工作站评测器与当前固定实现不一致，请管理员核验后重新绑定')
    return value


def _module(cfg,name):
    root=verify_vendor(cfg)['root'];path=root/name
    spec=importlib.util.spec_from_file_location('gateway_offline_'+identity(str(path))[:12],path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


# This administrative wrapper narrows the unmodified directory worker to the
# exact retained Task and, for new jobs, its owner's frozen dataset descriptor.
REMOTE_GUARD=r'''
import base64,hashlib,json,os,pathlib,re,stat,sys,types

def exact_path(raw):
    p=pathlib.Path(raw)
    if not p.is_absolute() or '..' in p.parts or str(p)!=raw:raise ValueError('normalized absolute path required')
    for entry in (p,*p.parents):
        try:info=entry.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode):raise ValueError('source or attempt path is a symbolic link')
    return p

def bounded_json(path):
    exact_path(str(path))
    if not path.is_file() or path.stat().st_size>64*1024*1024:raise ValueError('source JSON missing or exceeds bound')
    return json.loads(path.read_text(encoding='utf-8'))

def encoded_identity(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()

def private_directory(path):
    missing=[];current=path
    while not current.exists():
        missing.append(current);current=current.parent
    exact_path(str(current))
    if not current.is_dir():raise ValueError('attempt ancestor is not a directory')
    for entry in reversed(missing):
        entry.mkdir(mode=0o700,exist_ok=True)
        exact_path(str(entry))
        if not entry.is_dir():raise ValueError('attempt directory was redirected')

def inspect_source(request,pin,worker):
    task=exact_path(request['source_task']);tasks=exact_path(pin['discovery']['tasks_root'])
    if task.parent!=tasks or not worker.SAFE.fullmatch(task.name):raise ValueError('Task outside the fixed retained Task root')
    index=bounded_json(task/'index.json');producer=index.get('run_id')
    if (index.get('state')!='PASS' or index.get('task_id')!=task.name or not isinstance(producer,str)
            or not worker.SAFE.fullmatch(producer) or request.get('expected_run') not in (None,producer)):
        raise ValueError('Task index does not belong to the expected producer')
    product=exact_path(str(pathlib.Path(pin['prediction_root'])/producer/'predictions'))
    receipt_path=product.with_name('predictions.bench-eval-product.json')
    if index.get('canonical_product')!=str(product) or index.get('producer_receipt')!=str(receipt_path):
        raise ValueError('Task canonical product differs from its fixed producer path')
    receipt=bounded_json(receipt_path);job_path=task/'inputs/job.json';job=bounded_json(job_path)
    if (receipt.get('state')!='SUCCEEDED' or receipt.get('product_type')!='workstation_predictions'
            or receipt.get('producer_run_id')!=producer or receipt.get('workstation_product')!=str(product)
            or job.get('run_id')!=producer or not isinstance(receipt.get('files'),list) or not receipt['files']):
        raise ValueError('Task, producer Job and successful product receipt differ')
    if (not index.get('product_identity') or index.get('product_identity')!=receipt.get('product_identity')
            or not index.get('inventory_sha256') or index.get('inventory_sha256')!=receipt.get('inventory_sha256')
            or index.get('file_count')!=len(receipt['files'])):
        raise ValueError('retained product identity or file count differs')
    copied=task/receipt_path.name
    if not copied.is_file() or worker.sha(copied)!=worker.sha(receipt_path):raise ValueError('retained producer receipt differs')
    replay=job['replay_input'];prepared=exact_path(replay['prepared_manifest']);truth=exact_path(replay['dataset_root'])
    dataset=request.get('dataset')
    if dataset:
        if (dataset.get('manifest')!=str(prepared) or dataset.get('remote_path')!=str(truth)
                or dataset.get('sha256')!=worker.sha(prepared)):
            raise ValueError('producer dataset differs from the frozen source dataset')
        # Exact manifest/root equality above is the authority; these parents only
        # satisfy the upstream worker's containment check, not expand authority.
        pin['discovery']['prepared_manifest_root']=str(prepared.parent)
        pin['discovery']['truth_root']=str(truth.parent)
    else:
        worker.inside(prepared,pin['discovery']['prepared_manifest_root'])
        worker.inside(truth,pin['discovery']['truth_root'])
    package=request.get('package') or {}
    if package and (job.get('app_artifact') or {}).get('sha256')!=package.get('sha256'):
        raise ValueError('producer APP differs from the frozen source package')
    discovery=worker.discover({'prediction_path':str(product),'scenes':None,'required_tasks':request.get('required_tasks')},pin)
    task_scenes=index.get('scenes')
    if (not isinstance(task_scenes,list) or len(task_scenes)!=len(discovery['scenes'])
            or {r.get('scene') for r in task_scenes}!=set(discovery['scenes'])
            or any(r.get('run_id')!=producer for r in task_scenes)
            or index.get('scene_count')!=len(discovery['scenes'])):
        raise ValueError('Task scene inventory differs from the canonical product')
    evidence={'index_sha256':worker.sha(task/'index.json'),'product_receipt_sha256':worker.sha(receipt_path),
              'job_sha256':worker.sha(job_path),'prepared_manifest_sha256':worker.sha(prepared),
              'product_identity':receipt['product_identity'],'prediction_path':str(product),
              'source_task':str(task),'producer_run_id':producer,'scene_names':discovery['scenes'],
              'evaluator_pin_identity':encoded_identity({'worker':pin['worker'],'evaluator':pin['evaluator']})}
    return dict(evidence,source_identity=encoded_identity(evidence),task=str(task),product=str(product),
                product_receipt=str(receipt_path),receipt=str(receipt_path),scene_count=len(discovery['scenes']),
                discovery=discovery,hardware_actions='NOT_RUN')

def bridge(request,pin,worker):
    if request['action']=='runtime_probe':
        import platform,subprocess
        package,_=worker.verify_pin(pin)
        if not os.access(str(package/'run.sh'),os.X_OK) or not os.access(str(package/'build/uroad_offline_eval'),os.X_OK):
            raise ValueError('pinned evaluator is not executable')
        result=subprocess.run(['ldd',str(package/'build/uroad_offline_eval')],capture_output=True,text=True,timeout=20)
        if result.returncode or 'not found' in result.stdout+result.stderr:raise ValueError('pinned evaluator has missing shared libraries')
        print(json.dumps({'type':'result','state':'PASS','python_version':platform.python_version(),
              'worker_verified':True,'package_verified':True,'runtime_dependencies_verified':True,
              'requires_bench':False,'uploads':False,'hardware_actions':'NOT_RUN'}));return
    source=inspect_source(request,pin,worker)
    if request['action']=='inspect':
        print(json.dumps({'type':'result','state':'SOURCE_VERIFIED','source':source}));return
    if request.get('expected_source_identity')!=source['source_identity']:raise ValueError('retained source changed before evaluation')
    run=request['run_id'];owner=exact_path(request['owner_root']);users=exact_path(request['users_root'])
    key=request.get('owner_key','')
    if (not worker.SAFE.fullmatch(run) or len(run)>=110 or not re.fullmatch(r'u_[a-f0-9]{24}',key)
            or owner.parent!=users or owner.name!=key):raise ValueError('invalid owner or stable attempt ID')
    attempts=owner/'evaluations';exact_path(str(attempts))
    if request['action']=='execute':private_directory(attempts)
    elif request['action']!='collect':raise ValueError('unsupported bridge action')
    if not attempts.is_dir():raise ValueError('existing evaluation attempt root is missing; collection does not create work')
    pin['runs_root']=str(attempts)
    ask={'prediction_path':source['prediction_path'],'scenes':source['scene_names'],
         'required_tasks':source['discovery']['required_tasks'],'run_id':run}
    plan=worker.discover(ask,pin)
    result=worker.execute(ask,pin,plan) if request['action']=='execute' else worker.collect(ask,pin,plan)
    result['source']=source;result['remote_attempt']=str(attempts/run)
    print(json.dumps(result,ensure_ascii=False))
'''


def _remote(cfg,request,read_only=True):
    bundle=verify_vendor(cfg);root=bundle['root']
    scope=(root/'scripts/report_scope.py').read_text(encoding='utf-8')
    worker=(root/'scripts/directory_worker.py').read_text(encoding='utf-8')
    direct=Path(__file__).with_name('evaluation_direct.py').read_text(encoding='utf-8')
    code=('import sys,types,json\nscope=types.ModuleType("report_scope")\nsys.modules["report_scope"]=scope\n'
          'exec('+repr(scope)+',scope.__dict__)\nworker=types.ModuleType("directory_worker")\n'
          'exec('+repr(worker)+',worker.__dict__)\ndirect=types.ModuleType("gateway_evaluation_direct")\n'
          'exec('+repr(direct)+',direct.__dict__)\n'
          'direct.install(worker,'+repr(request.get('input_verification')==POLICY)+')\n'+REMOTE_GUARD+
          '\ntry:\n bridge('+repr(request)+','+repr(json.loads(canonical(bundle['pin'])))+',worker)\n'
          'except Exception as error:\n print(json.dumps({"type":"error","state":"EVALUATION_BLOCKED",'
          '"message":str(error)[:500]}))\n')
    try:output=SSH(cfg).python(code,timeout=180 if read_only else 9000,fresh=True)
    except Exception:
        raise GatewayError('工作站评测连接或固定输入核验未完成；请检查本人源结果及评测记录') from None
    if len(output)>32*1024*1024:raise GatewayError('工作站评测响应超过大小限制')
    result=None
    try:
        for line in output.splitlines():
            if not line.strip():continue
            item=json.loads(line)
            if not isinstance(item,dict):raise ValueError('protocol')
            if item.get('type')=='progress':continue
            if item.get('type')=='error':
                raise GatewayError('工作站评测核验未通过：'+str(item.get('message','原因待核验'))[:500])
            if item.get('type')=='result' and result is None:result=item
            else:raise ValueError('protocol')
    except GatewayError:raise
    except (ValueError,TypeError):raise GatewayError('工作站评测响应格式无效；不得据此认定评测成功') from None
    if result is None:raise GatewayError('工作站未返回评测收据')
    return result


def probe_runtime(cfg):
    result=_remote(cfg,{'action':'runtime_probe'})
    if result.get('state')!='PASS':raise GatewayError('固定工作站评测环境未就绪')
    return {**result,'evaluator':descriptor(cfg)}


def inspect_task(cfg,source_task,expected_run=None,dataset=None):
    if expected_run is not None and (not isinstance(expected_run,str) or not SAFE.fullmatch(expected_run)):
        raise GatewayError('源回灌 Run ID 无效')
    if not isinstance(source_task,str):raise GatewayError('需要明确的已留存工作站 Task 路径')
    pin=pin_contract(cfg);task=PurePosixPath(source_task)
    if (not task.is_absolute() or '..' in task.parts
            or task.parent!=PurePosixPath(pin['discovery']['tasks_root']) or not SAFE.fullmatch(task.name)):
        raise GatewayError('仅接受固定工作站 Task 根目录下已留存的完整任务目录')
    result=_remote(cfg,{'action':'inspect','source_task':str(task),'expected_run':expected_run,
                        'dataset':dataset,'required_tasks':None})
    if result.get('state')!='SOURCE_VERIFIED' or not isinstance(result.get('source'),dict):
        raise GatewayError('原回灌 Task 尚未核验')
    return result['source']


def _context(cfg,store,job):
    if (not isinstance(job,dict) or not isinstance(job.get('payload'),dict)
            or job['payload'].get('action')!='evaluate' or not SAFE.fullmatch(str(job.get('id','')))):
        raise GatewayError('需要已登记的本人评测任务')
    user=job['owner'];store.require_user(user)
    p=job['payload'];saved=store.job(user,job['id'])
    if canonical(saved['payload'])!=canonical(p):raise GatewayError('评测请求与本人已登记任务不一致')
    source=store.job(user,p['source_job'])
    if source['owner']!=user or source['payload'].get('action')!='replay':raise GatewayError('评测只能引用本人已完成回灌来源')
    result=source.get('result') or {}
    automatic=(source['state']=='POSTPROCESSING' and result.get('evaluation_job')==job['id'])
    if source['state'] not in ('SUCCEEDED','PARTIAL_SUCCESS') and not automatic:
        raise GatewayError('源回灌尚未真实完成，模拟结果不能用于工作站评测')
    if result.get('simulated') or not result.get('task') or p.get('source_task')!=result['task']:
        raise GatewayError('评测源目录不属于本人的已完成回灌收据')
    run=source['payload'].get('source_run') or source['id']
    if not isinstance(run,str) or not SAFE.fullmatch(run):raise GatewayError('源 Run 身份无效')
    if not source['payload'].get('retained_result') and (result.get('state')!='PASS' or result.get('run_id')!=run):
        raise GatewayError('源回灌完成收据与实际 Run 不匹配')
    evaluator=p['baseline']['data'].get('evaluator');validate_descriptor(cfg,evaluator)
    source_assets=source['payload'].get('assets') or {}
    retained=source['payload'].get('retained_result') is True
    if not retained and not all(source_assets.get(role) for role in ('package','dataset')):
        raise GatewayError('源回灌缺少冻结的软件与数据集身份')
    registered_identity=None
    if retained:
        if (source['payload'].get('baseline',{}).get('data',{}).get('kind')!='retained-result-v1'
                or not re.fullmatch('[a-f0-9]{64}',str(result.get('source_identity','')))):
            raise GatewayError('历史结果必须先由管理员登记本人授权与稳定来源身份')
        registered_identity=result['source_identity']
    if p.get('source_assets') and canonical(p['source_assets'])!=canonical(source_assets):
        raise GatewayError('评测材料与源回灌冻结版本不一致')
    dataset=source_assets.get('dataset')
    if dataset:
        if dataset.get('owner')!=user and not (dataset.get('owner')=='shared' and dataset.get('origin')=='dataset_catalog'):
            raise GatewayError('源数据集未授权给本人使用')
        dataset=dataset['data']
    package=source_assets.get('package')
    if package and package.get('owner')!=user:raise GatewayError('不能使用其他用户的软件材料')
    return {'source_job':source['id'],'source_task':result['task'],'expected_run':run,'dataset':dataset,
            'package':package.get('data') if package else None,'required_tasks':evaluator['required_tasks'],
            'owner_root':store.remote_root(user,cfg['remote_users_root']),'run_id':job['id'],
            'users_root':cfg['remote_users_root'],'owner_key':owner_key(user),
            'registered_source_identity':registered_identity,
            'input_verification':evaluator.get('input_verification'),
            'evaluator_descriptor_sha256':evaluator['descriptor_sha256']}


def resolve_task(cfg,store,job):
    request=_context(cfg,store,job)
    result=_remote(cfg,{**request,'action':'inspect'})
    if result.get('state')!='SOURCE_VERIFIED' or not isinstance(result.get('source'),dict):raise GatewayError('本人回灌源尚未核验')
    if request['registered_source_identity'] and result['source'].get('source_identity')!=request['registered_source_identity']:
        raise GatewayError('历史结果内容或固定评测工具身份已变化，请管理员重新登记')
    return result['source']


def probe(cfg,store,job):return resolve_task(cfg,store,job)


def _write_once(path,body):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise GatewayError('本机评测产物不能是链接')
    if path.exists():
        if path.read_bytes()!=body:raise GatewayError('本机已有不同的评测产物，拒绝覆盖')
    else:
        with path.open('xb') as stream:stream.write(body)


def evaluate(cfg,store,job,collect_only=False):
    if not collect_only and job.get('state') not in ('QUEUED','RUNNING'):
        raise GatewayError('已有或待核验的评测任务只能续收已有结果，不得重新计算')
    request=_context(cfg,store,job)
    if not collect_only and request['input_verification']!=POLICY:
        raise GatewayError('这份旧评测请求仍绑定历史执行器；请用现有回灌结果重新登记评测，旧结果可直接续收')
    source=resolve_task(cfg,store,job)
    root=within(store.user_root(job['owner']),store.user_root(job['owner'])/'reports'/job['id'])
    attempt=request['owner_root']+'/evaluations/'+job['id']
    try:
        payload=_remote(cfg,{**request,'action':'collect' if collect_only else 'execute',
                             'expected_source_identity':source['source_identity']},read_only=False)
        if payload.get('state')!='EVALUATION_COMPLETED' or not isinstance(payload.get('receipt'),dict):raise GatewayError('工作站没有完整原生评测收据')
        if payload.get('source',{}).get('source_identity')!=source['source_identity']:raise GatewayError('评测过程中源结果身份发生变化')
        raw=base64.b64decode(payload['report_base64'],validate=True)
        if not raw or len(raw)>MAX_REPORT:raise GatewayError('原始评测 TXT 为空或超过大小限制')
        native=payload['receipt']
        local_report=root/'evaluation-report.txt'
        receipt={'schema_version':1,'kind':KIND,'state':'PASS','job_id':job['id'],'owner_key':owner_key(job['owner']),
                 'source_job':request['source_job'],'source_task':request['source_task'],
                 'source':source,'descriptor_sha256':request['evaluator_descriptor_sha256'],
                 'remote_attempt':attempt,'native_receipt':native,'local_report':str(local_report),
                 'report_sha256':hashlib.sha256(raw).hexdigest()}
        if native.get('report_sha256')!=receipt['report_sha256']:raise GatewayError('原始报告与远端收据哈希不一致')
        _write_once(local_report,raw)
        result=validate_receipt(cfg,store,job,receipt,source=source,_trusted_payload=payload)
        receipt['evaluation_state']=result['evaluation_state']
        receipt['warnings']=result['warnings']
        path=root/'gateway-evaluation-receipt.json'
        _write_once(root/'directory-receipt.json',canonical(native)+b'\n')
        _write_once(root/'report-metadata.json',canonical({'source':source,'remote_attempt':attempt,
                     'native_global_aggregation':True,'config_path':native.get('config_path'),
                     'config_sha256':native.get('config_sha256'),'evaluated_scenes':result['evaluated_scenes']})+b'\n')
        _write_once(path,canonical(receipt)+b'\n')
        result['receipt_path']=str(path)
        _write_once(root/'evaluation-summary.json',canonical(result)+b'\n')
        return result
    except Exception as exc:
        if isinstance(exc,EvaluationOutcomeUnknown):raise
        detail=str(exc)[:300] if isinstance(exc,GatewayError) else type(exc).__name__
        raise EvaluationOutcomeUnknown('工作站评测结果待核验；稳定评测目录：'+attempt+'；仅续收已有结果，不自动重新计算；'+detail) from None


def validate_receipt(cfg,store,job,receipt,source=None,_trusted_payload=None):
    request=_context(cfg,store,job)
    if (not isinstance(receipt,dict) or receipt.get('schema_version')!=1 or receipt.get('kind')!=KIND
            or receipt.get('state')!='PASS' or receipt.get('job_id')!=job['id']
            or receipt.get('owner_key')!=owner_key(job['owner']) or receipt.get('source_job')!=request['source_job']
            or receipt.get('source_task')!=request['source_task']
            or receipt.get('descriptor_sha256')!=request['evaluator_descriptor_sha256']):
        raise GatewayError('工作站评测收据与本人任务或固定评测器不匹配')
    source=source or resolve_task(cfg,store,job)
    if canonical(receipt.get('source'))!=canonical(source):raise GatewayError('评测收据的源产品身份已变化')
    native=receipt.get('native_receipt') or {};plan=native.get('discovery') or {};scenes=source['scene_names']
    attempt=request['owner_root']+'/evaluations/'+job['id']
    if (receipt.get('remote_attempt')!=attempt or native.get('run_id')!=job['id']
            or native.get('prediction_path')!=source['prediction_path'] or native.get('scenes')!=scenes
            or native.get('scene_count')!=len(scenes)
            or native.get('native_global_aggregation') is not True
            or native.get('package_manifest_sha256')!=pin_contract(cfg)['evaluator']['manifest_sha256']
            or native.get('config_path')!=attempt+'/evaluation_param.yaml'
            or not re.fullmatch('[a-f0-9]{64}',str(native.get('config_sha256','')))
            or not re.fullmatch('[a-f0-9]{64}',str(native.get('dataset_identity','')))):
        raise GatewayError('原生评测收据缺少精确场景、工具、配置或输入复核证据')
    if request['input_verification']==POLICY:
        if (native.get('input_verification')!=POLICY or native.get('path_checks_passed') is not True
                or native.get('input_content_verified') is not False or native.get('source_postchecks_passed') is not False
                or native.get('source_data_hash_passes')!=0 or native.get('dataset_identity_scope')!='manifest-and-paths'):
            raise GatewayError('常规评测收据必须明确路径与结果检查，不能声称已校验整份数据内容')
    elif native.get('source_postchecks_passed') is not True:
        raise GatewayError('历史评测收据缺少原执行证据')
    for key in ('prediction_receipt_sha256','prepared_manifest_sha256','job_sha256'):
        expected=source['product_receipt_sha256'] if key=='prediction_receipt_sha256' else source[key]
        if plan.get(key)!=expected:raise GatewayError('原生评测与回灌来源身份不一致')
    if (plan.get('producer_run_id')!=source['producer_run_id'] or plan.get('scenes')!=scenes
            or plan.get('required_tasks')!=source['discovery']['required_tasks']):raise GatewayError('原生评测任务范围与源任务不一致')
    root=within(store.user_root(job['owner']),store.user_root(job['owner'])/'reports'/job['id'])
    report=within(root,receipt.get('local_report',''))
    if report!=root/'evaluation-report.txt' or not report.is_file() or not 0<report.stat().st_size<=MAX_REPORT:
        raise GatewayError('原始 TXT 必须位于本人的本次评测目录且大小有效')
    raw=report.read_bytes();actual=hashlib.sha256(raw).hexdigest()
    if actual!=receipt.get('report_sha256') or actual!=native.get('report_sha256'):raise GatewayError('原始 TXT 与评测收据内容不一致')
    verified=_trusted_payload
    if verified is None:
        # Public recovery never trusts a user-selected local native receipt: ask
        # the fixed worker to collect this already existing attempt, without any
        # execute fallback, and compare the actual original bytes and receipt.
        verified=_remote(cfg,{**request,'action':'collect','expected_source_identity':source['source_identity']},read_only=False)
    try:
        remote_raw=base64.b64decode(verified['report_base64'],validate=True)
        consistent=(verified.get('state')=='EVALUATION_COMPLETED'
                    and verified.get('source',{}).get('source_identity')==source['source_identity']
                    and verified.get('remote_attempt')==attempt
                    and canonical(verified.get('receipt'))==canonical(native)
                    and len(remote_raw)<=MAX_REPORT and remote_raw==raw)
    except (KeyError,ValueError,TypeError):consistent=False
    if not consistent:raise GatewayError('本机收据或 TXT 与工作站本次真实评测产物不一致；不会重新计算')
    scope=_module(cfg,'scripts/report_scope.py').parse_scene_coverage(raw.decode('utf-8',errors='replace'),scenes)
    if canonical(scope['scenes'])!=canonical(native.get('scene_coverage')):raise GatewayError('TXT 的逐场景覆盖与原生收据不一致')
    denominators={row['scene']:row['truth_frames'] for row in source['discovery']['scene_inputs']}
    if any(row['truth_frames']!=denominators[row['scene']] for row in scope['scenes']):raise GatewayError('TXT 的真值帧分母与本次数据集不一致')
    parser=_module(cfg,'scripts/runtime/parse_eval_report.py')
    try:parsed=parser.parse_report(report)
    except parser.ReportError as exc:
        # A native report with only N/A task metrics is still a useful partial
        # deliverable. Required core counts and exact scene coverage above must
        # remain valid; malformed or incomplete numeric metrics remain errors.
        if 'no supported task metrics' not in str(exc):raise GatewayError('原始 TXT 指标结构不完整，不能核验') from None
        metrics={};text=raw.decode('utf-8',errors='replace')
        for name,(pattern,convert) in parser.CORE_PATTERNS.items():
            match=re.search(pattern,text)
            if match is None:raise GatewayError('原始 TXT 缺少必要的样本计数') from None
            metrics[name]=convert(match.group(1))
        parsed={'metrics':metrics,'evaluated_tasks':[]}
    except Exception:raise GatewayError('原始 TXT 不含可核验的有效评测指标') from None
    adapter=job['payload']['baseline']['data']['evaluator']
    validity=parser.validate_result(parsed,{**adapter['validity'],'required_tasks':source['discovery']['required_tasks']})
    warnings=list(source['discovery'].get('warnings',[]))
    if validity['decision']!='VALID':warnings.append('REQUESTED_TASKS_OR_VALID_SAMPLES_INCOMPLETE')
    if any(row['completed_frames']!=row['truth_frames'] for row in scope['scenes']):warnings.append('SCENE_FRAME_COVERAGE_INCOMPLETE')
    state='PARTIAL' if warnings else 'COMPLETE'
    if receipt.get('evaluation_state') not in (None,state):raise GatewayError('收据声称的完整性与实际报告不一致')
    return {'task':request['source_task'],'report':str(report),'local_report':str(report),'metrics':parsed['metrics'],
            'evaluation_state':state,'warnings':warnings,'evaluated_scenes':scenes,'scene_coverage':scope['scenes'],
            'validity':validity,'remote_attempt':attempt,'source_identity':source['source_identity'],
            'receipt_path':str(root/'gateway-evaluation-receipt.json'),'requires_bench':False,'uploads':False}
