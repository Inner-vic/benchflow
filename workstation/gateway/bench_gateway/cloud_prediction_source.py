"""Reuse retained-result lineage checks without requiring workstation GT."""
import json
from . import workstation_evaluation as workstation
from .core import GatewayError
from .transfer import SSH

DISCOVERY=r'''
    package_root,_=worker.verify_pin(pin)
    switches=worker.task_switches((package_root/'etc/evaluation_param.yaml').read_text())
    required=request.get('required_tasks')
    if required is None:required=[k for k,v in switches.items() if v]
    if not required or set(required)-{k for k,v in switches.items() if v}:raise ValueError('unsupported task scope')
    names=sorted({f['path'].split('/')[0] for f in receipt['files']})
    if not names or any(not worker.SAFE.fullmatch(s) for s in names):raise ValueError('unsafe prediction scene')
    actual=sorted(p.name for p in product.iterdir() if p.is_dir())
    if actual!=names:raise ValueError('prediction scene inventory differs')
    for f in receipt['files']:
        p=pathlib.PurePosixPath(f['path'])
        if p.is_absolute() or '..' in p.parts or '\\' in f['path']:raise ValueError('unsafe prediction member')
    discovery={'scenes':names,'required_tasks':required,'task_switches':switches,
               'scene_inputs':[{'scene':s} for s in names],'warnings':[],
               'gt_location':'CLOUD_TASK_BINDING','workstation_gt_read':False}
'''

def inspect(cfg,context):
    bundle=workstation.verify_vendor(cfg)
    worker=(bundle['root']/'scripts/directory_worker.py').read_text(encoding='utf-8')
    scope=(bundle['root']/'scripts/report_scope.py').read_text(encoding='utf-8')
    needle="    discovery=worker.discover({'prediction_path':str(product),'scenes':None,'required_tasks':request.get('required_tasks')},pin)"
    guard=workstation.REMOTE_GUARD
    if guard.count(needle)!=1:raise GatewayError('云端预测来源检查器版本不一致。')
    guard=guard.replace(needle,DISCOVERY)
    code=('import types,sys,json\nscope=types.ModuleType("report_scope");sys.modules["report_scope"]=scope\n'
          'exec('+repr(scope)+',scope.__dict__)\nworker=types.ModuleType("directory_worker")\n'
          'exec('+repr(worker)+',worker.__dict__)\n'+guard+
          '\nresult=inspect_source('+repr(context)+','+repr(json.loads(workstation.canonical(bundle['pin'])))+',worker)\nprint(json.dumps(result))')
    try:source=json.loads(SSH(cfg).python(code,timeout=120,fresh=True))
    except Exception:raise GatewayError('云端评测的工作站预测来源核对失败；不读取工作站GT，不提交评测。') from None
    return source
