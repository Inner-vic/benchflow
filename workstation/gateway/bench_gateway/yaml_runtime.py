"""Pinned pure-Python YAML support, isolated from the shared Runner environment."""
import base64
import hashlib
import json
from pathlib import Path
import time
from .core import GatewayError

REMOTE_ROOT='/home/lixiang/bench-gateway/yaml-runtime'


def artifact(root):
    directory=Path(root)/'vendor/yaml-runtime'
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    raw=(directory/'pyyaml.zip').read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=manifest.get('sha256'):raise GatewayError('YAML 运行组件与登记版本不一致')
    return {'path':REMOTE_ROOT+'/'+digest+'/pyyaml.zip','sha256':digest,'version':manifest['version']},raw


REMOTE_CHECK=r'''
import base64,hashlib,json,pathlib,os,sys,uuid
sys.dont_write_bytecode=True
request=json.loads(base64.b64decode(sys.argv[1]));expected=request['artifact']
root=pathlib.Path('/home/lixiang/bench-gateway/yaml-runtime');path=root/expected['sha256']/'pyyaml.zip'
assert str(path)==expected['path'] and path.resolve()==path, 'invalid YAML runtime path'
if request['deploy']:
    raw=base64.b64decode(request['content'],validate=True)
    assert len(raw)<1024*1024 and hashlib.sha256(raw).hexdigest()==expected['sha256']
    path.parent.mkdir(parents=True,exist_ok=True)
    assert path.parent.resolve()==path.parent and not path.is_symlink()
    if not path.exists():
        tmp=path.with_name('.pyyaml-'+uuid.uuid4().hex)
        try:
            with tmp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            tmp.chmod(0o440);os.replace(tmp,path)
        finally:
            if tmp.exists():tmp.unlink()
assert path.is_file() and path.resolve()==path and hashlib.sha256(path.read_bytes()).hexdigest()==expected['sha256'], 'YAML runtime missing or changed'
sys.path.insert(0,str(path))
import yaml
assert yaml.__version__==expected['version'] and yaml.__file__.startswith(str(path)+'/')
assert yaml.safe_load('test: true\ncount: 2\n')=={'test':True,'count':2}
print(json.dumps({'state':'PASS','artifact':expected,'pure_python':not yaml.__with_libyaml__,'bench_contacted':False,'runner_modified':False}))
'''


def check(cfg,deploy=False):
    from .transfer import SSH
    expected,raw=artifact(cfg['project_root'])
    request={'artifact':expected,'deploy':deploy}
    if deploy:request['content']=base64.b64encode(raw).decode()
    payload=base64.b64encode(json.dumps(request).encode()).decode()
    body='import sys\nsys.argv=["yaml-runtime",'+repr(payload)+']\n'+REMOTE_CHECK
    result=json.loads(SSH(cfg).python(body,timeout=40,fresh=True))
    return {**result,'checked_at':time.time()}


def prepare(cfg,ssh,request,timeout=150):
    from .candidate_adapter import COMMON,configuration_overrides
    # Administrator-owned replay overlay; callers cannot choose ignored fields.
    request={**request,'semantic_overrides':{COMMON:configuration_overrides('<run_id>')}}
    expected,_=artifact(cfg['project_root'])
    if cfg.get('yaml_runtime_archive')!=expected['path']:
        raise GatewayError('YAML 准备组件尚未部署，请管理员检查')
    bootstrap='''import sys,pathlib,hashlib
sys.dont_write_bytecode=True
p=pathlib.Path(PATH)
if p.resolve()!=p or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=DIGEST:
    raise ValueError('YAML runtime missing or changed')
sys.path.insert(0,str(p))
'''.replace('PATH',repr(expected['path'])).replace('DIGEST',repr(expected['sha256']))
    worker=(Path(cfg['project_root'])/'bench_gateway/yaml_worker.py').read_text(encoding='utf-8')
    payload=base64.b64encode(json.dumps(request,ensure_ascii=False).encode()).decode()
    body=bootstrap+'\nsys.argv=["yaml-worker",'+repr(payload)+']\nexec(compile('+repr(worker)+',"gateway_yaml_worker","exec"))\n'
    return json.loads(ssh.python(body,timeout=timeout))
