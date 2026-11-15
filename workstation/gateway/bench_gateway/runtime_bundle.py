"""Versioned installer deployment, independent of the shared Runner installation."""
import base64
import hashlib
import json
import time
from pathlib import Path

from .core import GatewayError

REMOTE_ROOT = '/home/lixiang/bench-gateway/runtime/releases'
FILES = {'direct_package_install.py', 'ssh_transport.py'}


def bundle(root):
    directory = Path(root) / 'vendor/runtime'
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    hashes = manifest['files']
    if set(hashes) != FILES:
        raise GatewayError('工作站安装组件清单无效')
    contents = {}
    for name, expected in hashes.items():
        raw = (directory / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise GatewayError('工作站安装组件与固定版本不一致')
        contents[name] = base64.b64encode(raw).decode('ascii')
    identity = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'release': REMOTE_ROOT + '/' + identity, 'files': hashes}, contents


def load_contract(root, cfg):
    contract, _ = bundle(root)
    if cfg.get('workstation_runtime_release') != contract['release']:
        raise GatewayError('工作站安装组件尚未部署或版本未绑定，请管理员执行部署检查')
    return contract


# Only Python syntax/import/CLI-help checks. No bench SSH, Runner submit, or lock writes.
REMOTE = r'''
import base64, hashlib, importlib.util, json, os, pathlib, shutil, subprocess, sys, tempfile
sys.dont_write_bytecode=True
request=json.loads(base64.b64decode(sys.argv[1]))
contract=request['contract']; hashes=contract['files']
assert set(hashes)=={'direct_package_install.py','ssh_transport.py'}, 'unexpected runtime files'
identity=hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
base=pathlib.Path('/home/lixiang/bench-gateway/runtime/releases')
release=base/identity
assert str(release)==contract['release'], 'unexpected release directory'
if request['action']=='deploy':
    assert base.resolve()==base, 'runtime root contains a link'
    base.mkdir(parents=True,exist_ok=True)
    assert not base.is_symlink() and base.resolve()==base
    if not release.exists():
        pending=pathlib.Path(tempfile.mkdtemp(prefix='.pending-',dir=str(base)))
        try:
            for name,expected in hashes.items():
                raw=base64.b64decode(request['contents'][name],validate=True)
                assert len(raw)<512*1024 and hashlib.sha256(raw).hexdigest()==expected, 'runtime hash mismatch'
                compile(raw,str(pending/name),'exec')
                with (pending/name).open('xb') as stream:
                    stream.write(raw);stream.flush();os.fsync(stream.fileno())
                (pending/name).chmod(0o440)
            (pending/'manifest.json').write_text(json.dumps(contract,sort_keys=True)+'\n',encoding='utf-8')
            pending.rename(release)
        finally:
            if pending.exists():shutil.rmtree(pending)
checks=[]
for name,expected in hashes.items():
    path=release/name
    ok=path.is_file() and not path.is_symlink() and path.resolve()==path
    if ok:ok=hashlib.sha256(path.read_bytes()).hexdigest()==expected
    checks.append({'component':name,'status':'PASS' if ok else 'MISSING_OR_CHANGED'})
if not all(c['status']=='PASS' for c in checks):
    print(json.dumps({'state':'BLOCKED','release':str(release),'checks':checks,'bench_contacted':False}));sys.exit(0)
legacy=pathlib.Path('/home/lixiang/bench-eval')
dependencies=[legacy/'bin/bench-eval',legacy/'config/ssh_config',pathlib.Path('/mnt/data/bench-eval/tools/cleanup_bench_outputs.py')]
dependencies += [legacy/'current/automation'/name for name in ('target_workflow.py','runner_state.py','bench_transaction.py','ssh_transport.py','evaluation_runner.py','prediction_transfer.py','workflow_planner.py','workflow_receipts.py','parse_eval_report.py')]
for path in dependencies:
    item={'component':str(path),'status':'PASS' if path.is_file() else 'MISSING'}
    if path.is_file():item['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    checks.append(item)
for binary in ('python3','ssh','rsync'):
    checks.append({'component':binary,'status':'PASS' if shutil.which(binary) else 'MISSING'})
result=subprocess.run([sys.executable,str(release/'direct_package_install.py'),'--help'],text=True,capture_output=True,timeout=10)
checks.append({'component':'installer-cli','status':'PASS' if result.returncode==0 else 'FAILED'})
try:
    sys.path.insert(0,str(legacy/'current/automation'))
    from target_workflow import validate_target_job
    from runner_state import claim_bench,release_bench
    assert callable(validate_target_job) and callable(claim_bench) and callable(release_bench)
    checks.append({'component':'runner-imports','status':'PASS'})
except Exception as exc:checks.append({'component':'runner-imports','status':'FAILED','error_type':type(exc).__name__})
print(json.dumps({'state':'PASS' if all(c['status']=='PASS' for c in checks) else 'BLOCKED','release':str(release),'checks':checks,'bench_contacted':False,'runner_modified':False,'hardware_accepted':False}))
'''


def inspect(cfg, deploy=False):
    from .transfer import SSH
    contract, contents = bundle(cfg['project_root'])
    request = {'contract': contract, 'action': 'deploy' if deploy else 'check'}
    if deploy:
        request['contents'] = contents
    # File contents travel on stdin, never the Windows command line or chat transport.
    body = 'import sys\nsys.argv=["runtime",' + repr(base64.b64encode(json.dumps(request).encode()).decode()) + ']\n' + REMOTE
    started=time.monotonic()
    value = json.loads(SSH(cfg).python(body, timeout=60, fresh=True))
    return {**value, 'checked_at': time.time(), 'duration_seconds': round(time.monotonic()-started,2)}
