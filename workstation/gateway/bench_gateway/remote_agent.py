"""Sent over SSH stdin. Uses the workstation's existing Runner; never installs AI there."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import types


def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def read(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))


def under(root, path):
    root = Path(root).resolve(strict=True)
    p = Path(path).resolve(strict=True)
    if p == root or root not in p.parents:
        raise ValueError('input outside owner directory')
    return p


def batch_module(code):
    m = types.ModuleType('gateway_stream_batch')
    m.__file__ = '/tmp/gateway_stream_batch.py'
    exec(compile(base64.b64decode(code), m.__file__, 'exec'), m.__dict__)
    return m


def candidate_modules(req):
    if set(req['candidate_implementation'])!={'candidate_files','candidate_adapter','candidate_remote','batch'}:
        raise ValueError('invalid candidate implementation hashes')
    runtime=req['yaml_runtime'];path=Path(runtime['path'])
    expected=Path('/home/lixiang/bench-gateway/yaml-runtime')/runtime['sha256']/'pyyaml.zip'
    if path!=expected or path.resolve()!=path or sha(path)!=runtime['sha256']:
        raise ValueError('candidate YAML runtime changed')
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(path))
    result={}
    if set(req['candidate_code'])!={'candidate_files','candidate_adapter','candidate_remote'}:raise ValueError('invalid candidate implementation inventory')
    for name,encoded in req['candidate_code'].items():
        code=base64.b64decode(encoded)
        if hashlib.sha256(code).hexdigest()!=req['candidate_implementation'][name]:raise ValueError('candidate implementation changed')
        module=types.ModuleType('gateway_'+name);module.__file__=name+'.py'
        exec(compile(code,module.__file__,'exec'),module.__dict__)
        result[name]=module
    return result


def own_file(root, a):
    data = a['data']
    p = under(root, data['remote_path'])
    if sha(p) != data['sha256']:
        raise ValueError('owned asset content changed')
    return p


def runtime_installer(contract):
    """Only the administrator-pinned release may supply executable installation code."""
    if not isinstance(contract, dict):
        raise ValueError('gateway workstation runtime is not configured')
    hashes=contract.get('files',{})
    if set(hashes)!={'direct_package_install.py','ssh_transport.py'}:
        raise ValueError('invalid workstation runtime inventory')
    identity=hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    release=Path('/home/lixiang/bench-gateway/runtime/releases')/identity
    if contract.get('release')!=str(release) or release.resolve()!=release:
        raise ValueError('invalid workstation runtime directory')
    for name,expected in hashes.items():
        path=release/name
        if not path.is_file() or path.is_symlink() or path.resolve()!=path or sha(path)!=expected:
            raise ValueError('workstation runtime missing or changed; run deployment check')
    return release/'direct_package_install.py'


def dataset_input(req,owner_root,asset):
    data=asset['data']
    if asset.get('origin')=='dataset_catalog':
        if not req.get('dataset_catalog'):raise ValueError('dataset catalog unavailable')
        catalog=read(req['dataset_catalog'])
        matches=[a for a in catalog['datasets'] if a['id']==asset['id'] and a['owner'] in ('shared',req['owner'])]
        if len(matches)!=1 or matches[0]!=asset or asset['kind']!='dataset':raise ValueError('dataset source is not authorized')
        namespace={};exec(req['dataset_validator'],namespace)
        if namespace['check_registered_source'](data)!=data:raise ValueError('dataset source changed')
        return Path(data['manifest']),[Path(data['remote_path']),*[Path(p) for p in data['camera_roots']]]
    if asset['owner']!=req['owner']:raise ValueError('foreign private dataset')
    manifest=under(owner_root,data['manifest']);under(owner_root,data['remote_path'])
    if sha(manifest)!=data['sha256']:raise ValueError('dataset manifest changed')
    return manifest,[Path(owner_root)]


def dataset_path(roots,path):
    p=Path(path).resolve(strict=True)
    if not any(p==r.resolve(strict=True) or r.resolve(strict=True) in p.parents for r in roots):
        raise ValueError('dataset path outside approved source')
    return p


def snapshots(m, run, manifest):
    paths = [m.BASE/'adapters'/run/name for name in ('job.json','profile.json','small_stream_adapter.py','sw0058_gate_probe_driver_reference.py')]
    paths += [m.ROOT/'runs'/run/name for name in ('state.json','restore-verification.json','workstation-predictions.json')]
    paths += [Path(manifest),m.ROOT/'cache/predictions'/run/'predictions.bench-eval-product.json']
    return {str(p):sha(p) for p in paths}


def formal_manifest(package, dependencies):
    entries = {}
    with tarfile.open(package,'r:gz') as archive:
        for item in archive:
            p = PurePosixPath(item.name)
            if not p.parts or p.parts[0]!='livc' or '..' in p.parts or p.is_absolute():
                raise ValueError('unsafe package layout')
            rel='/'.join(p.parts[1:])
            if not rel or item.isdir():continue
            if item.isfile():
                h=hashlib.sha256(); f=archive.extractfile(item)
                for b in iter(lambda:f.read(1048576),b''):h.update(b)
                entries[rel]={'kind':'file','sha256':h.hexdigest()}
            elif item.issym():entries[rel]={'kind':'link','target':item.linkname}
            else:raise ValueError('unsupported archive member')
    for a in dependencies:
        target=a['data']['target']; p=PurePosixPath(target)
        if p.is_absolute() or '..' in p.parts or not p.parts:raise ValueError('unsafe dependency target')
        entries[target]={'kind':'file','sha256':a['data']['sha256']}
    if not entries:raise ValueError('empty package')
    return entries


def formal_matches(m, entries):
    # Use the exact existing workstation->bench transport policy, with a fixed read-only probe.
    sys.path.insert(0,str(m.BASE/'current/automation'))
    from ssh_transport import command, environment
    probe = '''import pathlib,json,hashlib,os
root=pathlib.Path('/app/livc'); expected=EXPECTED; mismatch=[]
for name,item in expected.items():
    p=root/name
    if item['kind']=='link':ok=p.is_symlink() and os.readlink(p)==item['target']
    else:
        ok=p.is_file() and not p.is_symlink()
        if ok:
            h=hashlib.sha256()
            with p.open('rb') as f:
                for b in iter(lambda:f.read(1048576),b''):h.update(b)
            ok=h.hexdigest()==item['sha256']
            if item.get('executable'):ok=ok and bool(p.stat().st_mode & 0o111)
    if not ok:mismatch.append(name)
print(json.dumps({'matches':not mismatch,'mismatches':mismatch[:20]}))
'''.replace('EXPECTED',repr(entries))
    argv=command('ssh',['-F',str(m.BASE/'config/ssh_config')], 'keyboard-interactive-empty')
    r=subprocess.run(argv+['bench-avm','python3 -'],input=probe,text=True,capture_output=True,timeout=300,env=environment('keyboard-interactive-empty'))
    if r.returncode:raise ValueError('formal identity probe failed')
    return json.loads(r.stdout)['matches']


def archive_assembly(req,m,modules):
    """Recheck this completed replay and save software only; never touch a bench."""
    if req['baseline'].get('kind')!='candidate-v1':raise ValueError('only a frozen candidate can create an assembly')
    remote=modules['candidate_remote']
    receipt,template=remote.load_binding(req,m,modules['candidate_files'],dataset_input,
                                         adapter=modules['candidate_adapter'])
    if req.get('smoke_run')!=req['run_id']+'-smoke' or req.get('smoke_scenes')!=receipt['scenes'][:1] or req['scenes']!=receipt['scenes']:
        raise ValueError('assembly requires this task precheck and complete selected replay')
    raw=base64.b64decode(req['assembly_code'],validate=True)
    if hashlib.sha256(raw).hexdigest()!=req['assembly_code_sha256']:raise ValueError('assembly implementation differs')
    saver=types.ModuleType('gateway_assembly_remote')
    exec(compile(raw,'assembly_remote.py','exec'),saver.__dict__)
    remote.configure_batch(m)
    evidence=[]
    for run,indices,prefix in ((req['run_id'],req['scenes'],''),(req['smoke_run'],req['smoke_scenes'],'smoke_')):
        task=m.ROOT/'tasks'/run
        if task.is_symlink() or not (task/'index.json').is_file():raise ValueError('retained completed task required; do not replay')
        plan=remote.make_plan(m,modules['candidate_adapter'].render,template,receipt['candidate'],run,req['actor'],indices,True)
        for name in ('job.json','profile.json'):
            if remote.canonical(read(m.BASE/'adapters'/run/name))!=remote.canonical(plan[name[:-5]]):
                raise ValueError('completed adapter input differs from frozen candidate')
        checked,product_receipt=m.validate_and_retain(plan,0)
        if Path(checked)!=task:raise ValueError('retained source task differs')
        cleanup=m.ROOT/'maintenance'/(run+'-cleanup.json')
        copied=task/'logs/bench-output-cleanup.json'
        if cleanup.is_symlink() or copied.is_symlink() or sha(cleanup)!=sha(copied):raise ValueError('cleanup copy differs')
        cleaned=read(cleanup);producer=read(product_receipt);accepted=cleaned.get('accepted')
        if (cleaned.get('batch_id')!=run+'-cleanup' or not isinstance(accepted,list) or len(accepted)!=1
                or any(accepted[0].get(key)!=value for key,value in {
                    'run_id':run,'source':producer.get('source'),'workstation_product':producer.get('workstation_product'),
                    'product_receipt':str(product_receipt),'inventory_sha256':producer.get('inventory_sha256')}.items())
                or not producer.get('source') or producer['source'] not in cleaned.get('removed',[])):
            raise ValueError('successful cleanup is not bound to this producer')
        paths=[(prefix+'runner_state' if not prefix else 'smoke_state',m.ROOT/'runs'/run/'state.json'),
               (prefix+'restore',m.ROOT/'runs'/run/'restore-verification.json'),
               (prefix+'product_receipt',Path(product_receipt)),(prefix+'cleanup',cleanup)]
        if not prefix:paths.append(('task_index',task/'index.json'))
        for role,path in paths:evidence.append({'role':role,'path':str(path),'sha256':sha(path)})
    binding=req['baseline']['binding']['path']
    evidence.append({'role':'binding','path':binding,'sha256':sha(binding)})
    proof={'state':'PASS','run_id':req['run_id'],'task':str(m.ROOT/'tasks'/req['run_id']),
           'smoke_run':req['smoke_run'],'files':evidence,'archive_implementation_sha256':req['assembly_code_sha256']}
    return saver.save(receipt['candidate'],req['owner_root'],req['run_id'],receipt['frozen']['input_digest'],proof)


def main(req):
    root=Path(req['owner_root'])
    if req.get('operation')=='verify_assembly':
        raw=base64.b64decode(req['assembly_code'],validate=True)
        if hashlib.sha256(raw).hexdigest()!=req['assembly_code_sha256']:raise ValueError('assembly implementation differs')
        receipt=req['assembly_receipt']
        if receipt.get('source_run')!=req.get('run_id'):raise ValueError('assembly source run differs')
        module=types.ModuleType('gateway_assembly_verify')
        exec(compile(raw,'assembly_remote.py','exec'),module.__dict__)
        return module.verify(receipt,str(root))
    candidate=req.get('operation') in ('review_template','bind_candidate') or req.get('baseline',{}).get('kind')=='candidate-v1'
    if candidate and hashlib.sha256(base64.b64decode(req['batch_code'])).hexdigest()!=req.get('candidate_implementation',{}).get('batch'):
        raise ValueError('candidate batch implementation changed')
    m=batch_module(req['batch_code'])
    action=req['operation']
    if 'run_id' in req and (not isinstance(req['run_id'],str) or not m.SAFE.fullmatch(req['run_id']) or len(req['run_id'])>86):
        raise ValueError('invalid run identity')
    if action in ('review_template','bind_candidate') or req.get('baseline',{}).get('kind')=='candidate-v1':
        modules=candidate_modules(req);remote=modules['candidate_remote']
        if action=='review_template':
            if req.get('deploy_controls'):remote.control_bundle(deploy=True)
            return remote.qualify(m,req['source_run'])
        if action=='bind_candidate':return remote.bind(req,m,modules['candidate_files'],modules['candidate_adapter'],dataset_input)
        if action=='archive_assembly':return archive_assembly(req,m,modules)
        if action!='status':
            try:
                return remote.execute(req,m,modules['candidate_files'],modules['candidate_adapter'],dataset_input,runtime_installer,formal_matches)
            except Exception as exc:
                try:rejection=remote.no_previous_attempt(req,m)
                except Exception:rejection=None
                if rejection:return {'state':'BLOCKED','error':str(exc),'rejection':rejection}
                raise
    if action=='status':
        run=req['run_id']
        p=m.ROOT/'tasks'/run/'index.json'
        out={'state':read(m.ROOT/'runs'/run/'state.json') if (m.ROOT/'runs'/run/'state.json').exists() else None,
             'lock':read(m.BASE/'locks/bench.lock') if (m.BASE/'locks/bench.lock').exists() else None}
        if p.exists():out['task']=str(p.parent);out['index']=read(p)
        return out
    if action=='diagnose':
        checks=[]
        for a in [req['assets']['package'],*req['assets']['dependencies']]:
            item={'name':a['name'],'kind':a['kind'],'target':a['data'].get('target','')}
            try:item['matches']=sha(under(root,a['data']['remote_path']))==a['data']['sha256']
            except Exception:item['matches']=False
            checks.append(item)
        run=m.ROOT/'runs'/req['run_id'];restored=read(run/'restore-verification.json') if (run/'restore-verification.json').exists() else {}
        lock=read(m.BASE/'locks/bench.lock') if (m.BASE/'locks/bench.lock').exists() else None
        own_lock=bool(lock and lock.get('run_id') in (req['run_id'],req['run_id']+'-install'))
        try:formal_ok=formal_matches(m,req['baseline']['formal_manifest'])
        except Exception:formal_ok=None
        log=run/'logs/worker-cli.log'
        logs=[str(log)] if log.exists() else []
        if any(not c['matches'] for c in checks):summary='工作站软件包或依赖与登记内容不一致。'
        elif formal_ok is False:summary='台架生效文件与预期软件包、依赖不一致。'
        else:summary='已记录文件身份检查；需结合小场景预检日志继续定位，不能据此认定根因。'
        return {'state':'DIAGNOSED','asset_checks':checks,'formal_files_match':formal_ok,'restore_verified':restored.get('restore_ok') is True,
                'safe_to_continue':restored.get('restore_ok') is True and not own_lock,'logs':logs,'summary':summary}
    assets=req['assets']; package=own_file(root,assets['package'])
    for a in assets['dependencies']:own_file(root,a)
    dataset=assets['dataset']['data'];manifest,dataset_roots=dataset_input(req,root,assets['dataset'])
    if action=='admit':
        run=req['source_run'];job,profile,adapter,scenes,prefix=m.source_contract(run)
        # A historical run in somebody else's input directory is never a usable source.
        if str(Path(job['app_artifact']['path']).resolve())!=str(package):raise ValueError('source run did not use this owner package path')
        if job['app_artifact']['sha256']!=assets['package']['data']['sha256']:raise ValueError('source package differs')
        if str(Path(job['replay_input']['prepared_manifest']).resolve())!=str(manifest):raise ValueError('source dataset differs')
        for scene in scenes:
            for key in ('scene_root','raw_root','truth_root'):
                if scene.get(key):dataset_path(dataset_roots,scene[key])
        for i in range(1,len(scenes)+1):dataset_path(dataset_roots,prefix+f'camera-s{i:03d}')
        m100=next((a for a in assets['dependencies'] if a['data']['target']=='lib/liburoad_m100.so'),None)
        if not m100 or m100['data']['sha256']!=job['artifacts']['evaluation_library']['sha256']:raise ValueError('M100 is not bound to successful gate')
        entries=formal_manifest(package,assets['dependencies'])
        if not formal_matches(m,entries):raise ValueError('current formal tree differs from admitted package/dependencies')
        return {'source_run':run,'gate_verified':True,'snapshot':snapshots(m,run,manifest),
                'formal_manifest':entries,'manifest_sha256':dataset['sha256']}
    b=req['baseline']
    if not b.get('gate_verified'):raise ValueError('small-scene admission missing')
    for path,expected in b['snapshot'].items():
        if sha(path)!=expected:raise ValueError('admitted baseline evidence changed')
    if action=='recover_delivery':
        state=read(m.ROOT/'runs'/req['run_id']/'state.json')
        restored=read(m.ROOT/'runs'/req['run_id']/'restore-verification.json')
        if state.get('state')!='COMPLETED_TO_REQUESTED_GOAL' or restored.get('restore_ok') is not True:
            raise ValueError('Runner completion/restoration not verified; do not replay')
        args=argparse.Namespace(source_run=b['source_run'],run_id=req['run_id'],task_id=req['run_id'],
            scenes=','.join(str(i) for i in req['scenes']) if req['scenes'] else 'all',actor=req['actor'],resume_delivery=True)
        return m.finish_delivery(m.plan(args),0)
    queue=m.command('queue')
    if queue.get('active') or queue.get('queued') or queue.get('waiting'):
        return {'state':'RESOURCE_BUSY'}
    if action=='evaluate':
        raise ValueError('evaluation uses the separately reviewed evaluator adapter')
    if action not in ('smoke','replay'):raise ValueError('unsupported operation')
    install=runtime_installer(req.get('runtime_contract'))
    if action=='smoke' and len(req['scenes'])!=1:raise ValueError('smoke requires exactly one representative scene')
    if action=='replay':
        if req.get('smoke_run')!=req['run_id']+'-smoke':raise ValueError('this task has no bound smoke run')
        # Historical source runs select a compatible adapter. They never replace this task's smoke.
        args=argparse.Namespace(source_run=b['source_run'],run_id=req['smoke_run'],task_id=req['smoke_run'],
            scenes=','.join(str(i) for i in req['smoke_scenes']),actor=req['actor'],resume_delivery=True)
        m.validate_and_retain(m.plan(args),0)
    if not formal_matches(m,b['formal_manifest']):
        # Never install somebody else's active image by assumption. Reinstall only owned input.
        m.bench_capacity()
        argv=['python3',str(install),'--root',str(m.BASE),'--run-id',req['run_id']+'-install','--package',str(package),'--expected-sha256',assets['package']['data']['sha256'],'--clean-formal','--execute']
        for a in assets['dependencies']:
            d=a['data']
            argv += ['--m100-so',d['remote_path']] if d['target']=='lib/liburoad_m100.so' else ['--replace',d['target']+'='+d['remote_path']]
        # The existing install command has its own lock and recovery receipts.
        r=subprocess.run(argv,text=True,capture_output=True)
        if r.returncode:raise ValueError('owned package installation failed; inspect installer receipts')
        if not formal_matches(m,b['formal_manifest']):raise ValueError('installed package identity failed')
    args=argparse.Namespace(source_run=b['source_run'],run_id=req['run_id'],task_id=req['run_id'],
         scenes=','.join(str(i) for i in req['scenes']) if req['scenes'] else 'all',actor=req['actor'],resume_delivery=False)
    p=m.plan(args)
    # Exact immutable source identities were checked above; m.plan validates scene selection.
    result=m.execute(p)
    if result.get('state')!='PASS':raise ValueError('batch delivery did not pass')
    result['workstation']='ubuntu-workstation'
    return result


if __name__=='__main__':
    try:
        request=json.loads(base64.b64decode(sys.argv[1]))
        print(json.dumps(main(request),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'state':'BLOCKED','error':str(exc)},ensure_ascii=False))
        sys.exit(2)
