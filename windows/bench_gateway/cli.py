import argparse
import json
from pathlib import Path
import subprocess
import sys

from .config import load,save
from .core import GatewayError,Store,remote_within
from .executor import Executor
from .service import Service
from .transfer import SSH


def main():
    p=argparse.ArgumentParser(description='个人电脑上的飞书台架入口。原台架项目只读。')
    p.add_argument('--root',type=Path)
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('doctor')
    sub.add_parser('connection-check')
    sub.add_parser('evaluator-check')
    q=sub.add_parser('register-result');q.add_argument('--user',required=True);q.add_argument('--task',required=True);q.add_argument('--label',default='')
    q=sub.add_parser('collect-evaluation');q.add_argument('--user',required=True);q.add_argument('--job',required=True)
    q=sub.add_parser('recover-assembly');q.add_argument('--user',required=True);q.add_argument('--job',required=True)
    setup=sub.add_parser('setup');setup.add_argument('--port',type=int,default=18765)
    sub.add_parser('launch')
    allow=sub.add_parser('allow');allow.add_argument('user');allow.add_argument('--label',default='')
    imp=sub.add_parser('import-file');imp.add_argument('--user',required=True);imp.add_argument('--kind',choices=['package','dependency'],required=True);imp.add_argument('--name',required=True);imp.add_argument('--file',type=Path,required=True);imp.add_argument('--target',help='依赖在 APP 内的相对路径')
    sync=sub.add_parser('sync-file');sync.add_argument('--user',required=True);sync.add_argument('--kind',choices=['package','dependency'],required=True);sync.add_argument('--name',required=True)
    rf=sub.add_parser('register-remote');rf.add_argument('--user',required=True);rf.add_argument('--kind',choices=['package','dependency'],required=True);rf.add_argument('--name',required=True);rf.add_argument('--path',required=True);rf.add_argument('--target')
    ds=sub.add_parser('register-dataset');ds.add_argument('--user',required=True);ds.add_argument('--name',required=True);ds.add_argument('--path',required=True);ds.add_argument('--manifest',required=True)
    ds=sub.add_parser('register-dataset-source');who=ds.add_mutually_exclusive_group(required=True);who.add_argument('--shared',action='store_true');who.add_argument('--user');ds.add_argument('--name',required=True);ds.add_argument('--path',required=True);ds.add_argument('--manifest',required=True);ds.add_argument('--camera-prefix');ds.add_argument('--scene-count',type=int);ds.add_argument('--replay-config-root',action='append',default=[]);ds.add_argument('--cloud-dataset-root');ds.add_argument('--cloud-gt-root')
    admit=sub.add_parser('admit-baseline');admit.add_argument('--user',required=True);admit.add_argument('--name',required=True);admit.add_argument('--package',required=True);admit.add_argument('--dataset',required=True);admit.add_argument('--dependency',action='append',default=[]);admit.add_argument('--source-run',required=True);admit.add_argument('--evaluator',type=Path)
    bind=sub.add_parser('bind-preparation');bind.add_argument('--user',required=True);bind.add_argument('--template');bind.add_argument('--name');bind.add_argument('--preparation')
    w=sub.add_parser('worker');w.add_argument('--once',action='store_true')
    q=sub.add_parser('assets');q.add_argument('--user',required=True)
    q=sub.add_parser('jobs');q.add_argument('--user',required=True)
    q=sub.add_parser('parser-smoke');q.add_argument('--user',required=True);q.add_argument('--text',default='你好，我想了解怎么使用台架。')
    q=sub.add_parser('inspect-run');q.add_argument('--user',required=True);q.add_argument('--job',required=True)
    q=sub.add_parser('recover-delivery');q.add_argument('--user',required=True);q.add_argument('--job',required=True)
    q=sub.add_parser('recover-precheck');q.add_argument('--user',required=True);q.add_argument('--job',required=True)
    q=sub.add_parser('recover-evaluation');q.add_argument('--user',required=True);q.add_argument('--task',required=True);q.add_argument('--receipt',type=Path,required=True)
    args=p.parse_args();cfg=load(args.root) if args.root else load();store=Store(cfg['state_root'])
    try:
        if args.command=='setup':
            from .setup import serve
            serve(cfg,args.port);return
        if args.command=='launch':
            from .setup import launch
            raise SystemExit(launch(cfg))
        if args.command=='doctor':
            out={'mode':cfg['mode'],'live_enabled':cfg['live_enabled'],'app_id':cfg['feishu_app_id'],'credentials_saved':(Path(cfg['project_root'])/'runtime/credentials.json').exists(),'original_project':'read_only','users':0}
            with store.connect() as c:out['users']=c.execute('SELECT count(*) FROM users WHERE enabled=1').fetchone()[0]
            for name,argv in [('cc_connect',[cfg['cc_binary'],'--version']),('codex',[cfg['codex_binary'],'--version'])]:
                try:r=subprocess.run(argv,capture_output=True,text=True,timeout=10);out[name]=r.stdout.strip().splitlines()[0] if r.returncode==0 else 'UNAVAILABLE'
                except Exception:out[name]='UNAVAILABLE'
        elif args.command=='connection-check':
            from .availability import probe,connection_text
            measured=probe(cfg);out={**measured,'summary':connection_text(measured)}
        elif args.command=='evaluator-check':
            from .workstation_evaluation import probe_runtime
            out=probe_runtime(cfg)
        elif args.command=='register-result':
            from .retained_results import register_result
            out=register_result(cfg,store,args.user,args.task,args.label)
        elif args.command=='collect-evaluation':
            from .core import worker_lock
            with worker_lock(store.root):
                job=store.job(args.user,args.job)
                if job['state']!='NEEDS_REVIEW' or job['payload']['action']!='evaluate':raise GatewayError('只续收本人待核验评测，不启动新计算')
                from .cloud_provider import profile_for_job
                if profile_for_job(cfg,job) is not None:
                    from .cloud_provider import evaluate
                else:
                    from .workstation_evaluation import evaluate
                out=evaluate(cfg,store,job,collect_only=True)
                store.finish(job,'PARTIAL_SUCCESS' if out.get('evaluation_state')=='PARTIAL' else 'SUCCEEDED',out)
        elif args.command=='recover-assembly':
            from .assembly_recovery import recover_assembly
            out=recover_assembly(cfg,store,args.user,args.job)
        elif args.command=='allow':store.allow(args.user,args.label);out={'state':'ALLOWED','user':args.user}
        elif args.command=='import-file':
            target=None
            if args.kind=='dependency':
                target=args.target or ''
                from pathlib import PurePosixPath
                t=PurePosixPath(target)
                if not target or t.is_absolute() or '..' in t.parts or '\\' in target:raise GatewayError('依赖需要有效的 --target APP内相对路径')
            if cfg.get('asset_backend')=='workstation':
                from .registry import Registry
                out=Registry(cfg,store).import_file(args.user,args.kind,args.name,args.file,target)
            else:out=store.import_file(args.user,args.kind,args.name,args.file,target)
        elif args.command=='sync-file':
            a=store.asset(args.user,args.kind,args.name);remote=SSH(cfg).upload(store,a)
            out=store.add_asset(args.user,args.kind,args.name,{**a['data'],'remote_path':remote})
        elif args.command=='register-dataset-source':
            from .dataset_sources import DatasetSources
            out=DatasetSources(cfg,store).register('shared' if args.shared else args.user,args.name,args.path,args.manifest,args.camera_prefix,args.scene_count,args.replay_config_root,args.cloud_dataset_root,args.cloud_gt_root)
        elif args.command=='register-dataset':
            store.require_user(args.user);root=store.remote_root(args.user,cfg['remote_users_root'])
            path=remote_within(root,args.path);manifest=remote_within(path,args.manifest)
            evidence=SSH(cfg).inspect_dataset(path,manifest)
            from .registry import Registry
            out=Registry(cfg,store).record(args.user,'dataset',args.name,{'remote_path':path,'manifest':manifest,**evidence})
        elif args.command=='register-remote':
            from .registry import Registry
            out=Registry(cfg,store).remote_file(args.user,args.kind,args.name,args.path,args.target)
        elif args.command=='bind-preparation':
            from .bindings import bind_preparation
            out=bind_preparation(cfg,store,args.user,args.template,args.name,args.preparation)
        elif args.command=='admit-baseline':
            service=Service(cfg)
            assets={'package':service.asset(args.user,'package',args.package),'dataset':service.asset(args.user,'dataset',args.dataset),'dependencies':[service.asset(args.user,'dependency',n) for n in args.dependency]}
            data=Executor(cfg,store).admit(args.user,assets,args.source_run)
            data['asset_ids']={'package':assets['package']['id'],'dataset':assets['dataset']['id'],'dependencies':sorted(a['id'] for a in assets['dependencies'])}
            if args.evaluator:data['evaluator']=json.loads(args.evaluator.read_text(encoding='utf-8'))
            out=store.add_baseline(args.user,args.name,data)
        elif args.command=='worker':
            from .worker import run
            run(cfg,store,args.once);return
        elif args.command=='assets':out=Service(cfg).assets(args.user)
        elif args.command=='jobs':out=store.jobs(args.user)
        elif args.command=='parser-smoke':
            from .parser import parse
            service=Service(cfg);out=parse(cfg,args.text,service.context(args.user),[])
        elif args.command=='inspect-run':
            job=store.job(args.user,args.job);executor=Executor(cfg,store)
            out=executor.remote(executor.request(args.user,'status',run_id=job['id']),job['id'],True)
        elif args.command=='recover-precheck':
            from .reconciliation import recover_precheck
            out=recover_precheck(cfg,store,args.user,args.job)
        elif args.command=='recover-evaluation':
            from .core import worker_lock
            with worker_lock(store.root):
                job=store.job(args.user,args.task)
                if job['state']!='NEEDS_REVIEW' or job['payload']['action']!='evaluate':raise GatewayError('只对本人待核验的评测任务续交付')
                try:
                    if args.receipt.stat().st_size>1024*1024:raise GatewayError('评测收据过大')
                    receipt=json.loads(args.receipt.read_text(encoding='utf-8'))
                    out=Executor(cfg,store).evaluate_receipt(job,receipt)
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    if isinstance(exc,GatewayError):raise
                    raise GatewayError('原评测收据或 TXT 报告无法核验，任务保持待核验状态') from None
                store.finish(job,'PARTIAL_SUCCESS' if out.get('evaluation_state')=='PARTIAL' else 'SUCCEEDED',out)
        elif args.command=='recover-delivery':
            from .core import owner_key,worker_lock
            if not cfg.get('live_enabled'):raise GatewayError('真实执行尚未启用')
            with worker_lock(store.root):
                job=store.job(args.user,args.job)
                if job['state']!='NEEDS_REVIEW' or job['payload']['action']!='replay':raise GatewayError('只对待核验的回灌任务续交付')
                executor=Executor(cfg,store);payload=job['payload']
                request=executor.request(args.user,'recover_delivery',run_id=job['id'],actor=owner_key(args.user),assets=payload['assets'],baseline=payload['baseline']['data'],scenes=payload['scenes'])
                out=executor.remote(request,job['id'])
                if out.get('state')!='PASS' or out.get('run_id')!=job['id']:raise GatewayError('交付核验未通过或收据与本任务不匹配')
                if payload['baseline']['data'].get('kind')=='candidate-v1':out=executor.save_assembly(job,out)
                store.complete_replay(job,out,recovered=True)
        print(json.dumps(out,ensure_ascii=False,indent=2))
    except GatewayError as exc:
        print(json.dumps({'state':'BLOCKED','error':str(exc)},ensure_ascii=False));raise SystemExit(2)


if __name__=='__main__':main()
