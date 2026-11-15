import base64
from contextlib import nullcontext
import json
from pathlib import Path
import shlex
import subprocess

from .core import EvaluationOutcomeUnknown, GatewayError, PrecheckFailed, PreDispatchRejected, digest, owner_key, within
from .transfer import SSH


class Executor:
    def __init__(self,cfg,store):
        self.cfg,self.store=cfg,store
        self.ssh=SSH(cfg)
        self.root=Path(cfg['project_root'])

    def asset_payload(self,assets):
        return assets

    def request(self,owner,operation,**values):
        code=(self.root/'vendor/run_stream_batch.py').read_bytes()
        provenance=json.loads((self.root/'vendor/provenance.json').read_text(encoding='utf-8'))
        if digest(self.root/'vendor/run_stream_batch.py')!=provenance['run_stream_batch.py']['sha256']:
            raise GatewayError('回灌入口副本发生变化，需重新验证')
        if operation in ('smoke','replay'):
            from .runtime_bundle import load_contract
            values['runtime_contract']=load_contract(self.root,self.cfg)
        if operation in ('archive_assembly','verify_assembly'):
            values['assembly_code']=base64.b64encode((self.root/'bench_gateway/assembly_remote.py').read_bytes()).decode()
            values['assembly_code_sha256']=digest(self.root/'bench_gateway/assembly_remote.py')
        if operation in ('bind_candidate','review_template') or values.get('baseline',{}).get('kind')=='candidate-v1':
            from .yaml_runtime import artifact
            yaml_artifact,_=artifact(self.root)
            if self.cfg.get('yaml_runtime_archive')!=yaml_artifact['path']:
                raise GatewayError('候选绑定依赖的 YAML 组件尚未部署')
            values['yaml_runtime']=yaml_artifact
            values['candidate_code']={name:base64.b64encode((self.root/'bench_gateway'/(name+'.py')).read_bytes()).decode()
                                      for name in ('candidate_files','candidate_adapter','candidate_remote')}
            values['candidate_implementation']={name:digest(self.root/'bench_gateway'/(name+'.py')) for name in values['candidate_code']}
            values['candidate_implementation']['batch']=provenance['run_stream_batch.py']['sha256']
        from .dataset_sources import VALIDATOR_CODE
        return {'operation':operation,'owner':owner,'owner_root':self.store.remote_root(owner,self.cfg['remote_users_root']),
                'dataset_catalog':self.cfg.get('dataset_catalog'),'dataset_validator':VALIDATOR_CODE,
                'batch_code':base64.b64encode(code).decode(),**values}

    def remote(self,request,job_id,read_only=False):
        code=(self.root/'bench_gateway/remote_agent.py').read_text(encoding='utf-8')
        payload=base64.b64encode(json.dumps(request,ensure_ascii=False).encode()).decode()
        # Payload includes an inventory: stdin avoids Windows argv limits.
        body='import sys\nsys.argv=["gateway",'+repr(payload)+']\n'+code.replace('from __future__ import annotations','')
        logdir=self.store.root/'runs'/job_id
        logdir.mkdir(parents=True,exist_ok=True)
        with (logdir/'ssh.log').open('a',encoding='utf-8') as err:
            # No forced timeout on an activated transaction. Network loss becomes NEEDS_REVIEW.
            limit=600 if request.get('operation') in ('bind_candidate','archive_assembly','verify_assembly') else 180
            if request.get('operation') in ('reconcile_precheck','reconcile_replay'):
                limit=max(15,min(120,int(self.cfg.get('reconciliation_timeout_seconds',60))))
            from .progress import Follow
            following=Follow(self.store,request.get('owner'),job_id,logdir/'ssh.log') if request.get('operation') in ('smoke','replay') else nullcontext()
            with following:
                result=subprocess.run(self.ssh.argv()+['python3 -'],input=body,text=True,encoding='utf-8',stdout=subprocess.PIPE,stderr=err,timeout=limit if read_only else None)
        (logdir/'remote-output.json').write_text(result.stdout,encoding='utf-8')
        if result.returncode==255:raise ConnectionError('SSH 已断开，远端执行结果未知；请核验后续接，不自动重跑')
        try: value=json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ConnectionError('远程响应不完整，需检查 Runner 收据') from None
        rejection=value.get('rejection') if isinstance(value,dict) else None
        if (result.returncode in (0,2) and value.get('state')=='BLOCKED'
                and request.get('operation')=='smoke'
                and request.get('baseline',{}).get('kind')=='candidate-v1'
                and request.get('_dispatch_started',False) is False
                and isinstance(request.get('run_id'),str) and request['run_id']==job_id
                and isinstance(value.get('error'),str) and bool(value['error'])
                and isinstance(rejection,dict)
                and set(rejection)=={'schema_version','code','run_id','hardware_actions','no_previous_attempt','runner_idle'}
                and type(rejection.get('schema_version')) is int and rejection['schema_version']==1
                and rejection.get('code')=='PREDISPATCH_REJECTED'
                and rejection.get('run_id')==request['run_id']
                and rejection.get('hardware_actions')=='NOT_RUN'
                and rejection.get('no_previous_attempt') is True
                and rejection.get('runner_idle') is True):
            raise PreDispatchRejected(value['error'],rejection)
        if result.returncode or value.get('state')=='BLOCKED':
            raise GatewayError(value.get('error','远程执行未完成'))
        return value

    def admit(self,user,assets,source_run):
        if not self.cfg.get('live_enabled'):raise GatewayError('真实执行尚未启用')
        if not source_run or len(source_run)>80 or not all(c.isalnum() or c in '._-' for c in source_run):raise GatewayError('源 run ID 无效')
        return self.remote(self.request(user,'admit',assets=assets,source_run=source_run),'admit-'+source_run,True)

    def bind_candidate(self,user,assets,preparation,template):
        self.store.require_user(user)
        if preparation.get('owner')!=user:raise GatewayError('不能绑定其他用户的材料')
        return self.remote(self.request(user,'bind_candidate',assets=assets,preparation=preparation,
                          template=template,actor=owner_key(user)),preparation['id']+'-bind',True)

    def availability(self,job):
        if (self.cfg.get('mode')=='simulate' or not self.cfg.get('live_enabled')
                or not self.cfg.get('resource_probe_enabled') or job['payload']['action']!='replay'):
            return {'state':'READY'}
        from .availability import probe
        return probe(self.cfg)

    def execute(self,job):
        p=job['payload']
        if p['action']=='evaluate' and (p.get('baseline',{}).get('data',{}).get('evaluator') or {}).get('kind')=='workstation-offline-v1':
            if not self.cfg.get('workstation_evaluation_enabled'):
                raise GatewayError('工作站离线评测开关尚未启用；已有结果保留，不需要重新回灌')
            return self.evaluate(job)
        if p['action']=='deployment_check':
            # Operator-only probe, excluded from the model schema. Never dispatch it to SSH.
            if self.cfg.get('mode')!='simulate' or self.cfg.get('live_enabled'):raise GatewayError('部署联通验证仅允许模拟模式')
            report=within(self.store.user_root(job['owner']),p['local_report'])
            if report.suffix!='.txt' or digest(report)!=p['report_sha256']:raise GatewayError('部署验证 TXT 不匹配')
            return {'simulated':True,'goal':'deployment_check','task':'NOT_RUN','local_report':str(report)}
        if self.cfg.get('mode')=='simulate':
            return {'simulated':True,'task':'SIMULATION/'+job['id'],'goal':p['action']}
        if not self.cfg.get('live_enabled'):raise GatewayError('真实执行尚未启用，请先完成 SSH 与基线配置')
        if p['action']=='evaluate':return self.evaluate(job)
        # Persisted assets belong to this user; never search another namespace.
        for a in [p['assets']['package'],*p['assets']['dependencies']]:
            if a['owner']!=job['owner']:raise GatewayError('任务引用跨用户资产')
        dataset=p['assets']['dataset']
        if dataset['owner']!=job['owner'] and not (dataset['owner']=='shared' and dataset.get('origin')=='dataset_catalog'):
            raise GatewayError('任务引用了未授权的数据集')
        return self.replay_pipeline(job)

    def replay_pipeline(self,job):
        p=job['payload'];smoke_id=job['id']+'-smoke'
        smoke_scenes=[p['scenes'][0]] if p['scenes'] else [1]
        shared={'assets':p['assets'],'baseline':p['baseline']['data'],'actor':owner_key(job['owner'])}
        smoke=(job.get('result') or {}).get('smoke')
        candidate=p['baseline']['data'].get('kind')=='candidate-v1'
        if candidate:
            bound=p['baseline']['data']['binding']['input_digest']
            if smoke and smoke.get('input_digest')!=bound:raise GatewayError('本次预检收据不属于当前冻结材料，不进入全量')
        if not smoke:
            self.store.stage(job,'小场景预检中')
            try:
                smoke=self.remote(self.request(job['owner'],'smoke',run_id=smoke_id,scenes=smoke_scenes,**shared),smoke_id)
            except PreDispatchRejected:
                # The remote receipt already proves no attempt occurred. Diagnosing
                # rejected input again would fail the same checks and hide that proof.
                raise
            except GatewayError:
                self.store.stage(job,'预检失败，检查软件包及依赖')
                try:
                    diagnostics=self.remote(self.request(job['owner'],'diagnose',run_id=smoke_id,**shared),smoke_id+'-diagnose',True)
                except Exception:
                    diagnostics={'summary':'诊断未完成，需核对 SSH、软件包、依赖与恢复状态。','safe_to_continue':False}
                self.store.stage(job,'预检失败，诊断已记录',diagnostics=diagnostics)
                raise PrecheckFailed(diagnostics) from None
            if smoke.get('state')=='RESOURCE_BUSY':return smoke
            if smoke.get('state')!='PASS' or smoke.get('run_id')!=smoke_id:raise GatewayError('本次小场景预检收据未通过，不进入正式回灌')
            if candidate and smoke.get('input_digest')!=bound:raise GatewayError('预检收据与当前候选不一致，不进入正式回灌')
            self.store.stage(job,'小场景预检通过',smoke=smoke)
        self.store.stage(job,'正式回灌中')
        result=self.remote(self.request(job['owner'],'replay',run_id=job['id'],scenes=p['scenes'],smoke_run=smoke_id,smoke_scenes=smoke_scenes,**shared),job['id'])
        if result.get('state')=='RESOURCE_BUSY':return result
        if result.get('state')!='PASS' or result.get('run_id')!=job['id']:raise GatewayError('正式回灌收据与本任务不匹配')
        if candidate and result.get('input_digest')!=bound:raise GatewayError('正式回灌收据与冻结材料不一致')
        result['smoke']=smoke
        if candidate and self.cfg.get('defer_assembly_archive'):
            import time
            result.update(assembly_pending=True,assembly={'state':'PENDING'},replay_completed_at=time.time())
            return result
        return self.save_assembly(job,result) if candidate else result

    def verify_assembly(self,job,receipt):
        if receipt.get('source_run')!=job['id'] or receipt.get('owner_root')!=self.store.remote_root(job['owner'],self.cfg['remote_users_root']):
            raise GatewayError('装配收据不属于本人的本次回灌任务')
        return self.remote(self.request(job['owner'],'verify_assembly',run_id=job['id'],assembly_receipt=receipt),job['id']+'-archive-verify',True)

    def save_assembly(self,job,result,record_stage=True):
        """Software archiving cannot turn a verified replay into a failed replay."""
        if (result.get('simulated') or result.get('state')!='PASS' or result.get('run_id')!=job['id']
                or job['payload'].get('retained_result') or job['payload'].get('action')!='replay'
                or job['payload'].get('baseline',{}).get('data',{}).get('kind')!='candidate-v1'):
            return result
        result=dict(result)
        try:
            p=job['payload'];smoke=result.get('smoke') or (job.get('result') or {}).get('smoke')
            if (not smoke or smoke.get('state')!='PASS' or smoke.get('run_id')!=job['id']+'-smoke'
                    or smoke.get('input_digest')!=p['baseline']['data']['binding']['input_digest']):
                raise GatewayError('本次预检收据缺失，不能建立成功装配存档')
            result['smoke']=smoke
            if record_stage:self.store.stage(job,'回灌完成，保存本人装配包',task=result['task'])
            result['assembly_archive']=self.remote(self.request(job['owner'],'archive_assembly',
                run_id=job['id'],smoke_run=job['id']+'-smoke',scenes=p['scenes'],smoke_scenes=p['scenes'][:1],
                assets=p['assets'],baseline=p['baseline']['data'],actor=owner_key(job['owner'])),job['id']+'-archive',True)
            from .assemblies import record_success
            result.update(record_success(self.cfg,self.store,job,result))
        except Exception:
            result['assembly_archive_warning']='回灌已完成，但成功装配包尚未完成归档登记；原结果保留，不会重新刷包或回灌。'
        return result

    def evaluate(self,job):
        from .cloud_provider import profile_for_job
        if profile_for_job(self.cfg,job) is not None:
            from .cloud_provider import evaluate
            return evaluate(self.cfg,self.store,job)
        p=job['payload']; adapter=p['baseline']['data'].get('evaluator')
        if isinstance(adapter,dict) and adapter.get('kind')=='workstation-offline-v1':
            from .workstation_evaluation import evaluate
            return evaluate(self.cfg,self.store,job)
        if not adapter:
            raise GatewayError('尚未绑定本人可用的评测入口/认证；回灌结果已保留，无需重新回灌')
        # Evaluator is an operator-reviewed local command. It can manage per-user LPAI auth.
        # Model output cannot supply commands, auth environment, or report paths.
        argv=adapter.get('argv')
        if not isinstance(argv,list) or not argv or any(not isinstance(v,str) for v in argv):raise GatewayError('评测入口无效')
        request={'job_id':job['id'],'owner_key':owner_key(job['owner']),'source_task':p['source_task'],'source_job':p['source_job']}
        r=subprocess.run(argv,input=json.dumps(request,ensure_ascii=False),text=True,encoding='utf-8',capture_output=True,timeout=adapter.get('timeout',7200))
        if r.returncode:raise EvaluationOutcomeUnknown('评测入口已调用但退出异常，请核对远端评测结果；不会自动重跑')
        try:
            receipt=json.loads(r.stdout)
            return self.evaluate_receipt(job,receipt)
        except Exception as exc:
            reason=str(exc) if isinstance(exc,GatewayError) else '评测收据或报告无法解析'
            raise EvaluationOutcomeUnknown(reason+'；评测已调用，请核验后续接，无需重新回灌') from None

    def evaluate_receipt(self,job,receipt):
        """Validate an existing evaluation result without invoking its adapter again."""
        from .cloud_provider import profile_for_job
        if profile_for_job(self.cfg,job) is not None:
            from .cloud_provider import recover_receipt
            return recover_receipt(self.cfg,self.store,job,receipt)
        p=job['payload']
        if p.get('action')!='evaluate':raise GatewayError('收据只能交付给评测任务')
        adapter=p['baseline']['data'].get('evaluator')
        if isinstance(adapter,dict) and adapter.get('kind')=='workstation-offline-v1':
            from .workstation_evaluation import validate_receipt
            return validate_receipt(self.cfg,self.store,job,receipt)
        if not adapter:raise GatewayError('原任务没有已绑定的评测入口与校验规则')
        if not isinstance(receipt,dict) or receipt.get('source_job')!=p['source_job'] or receipt.get('job_id')!=job['id'] or receipt.get('state')!='PASS':raise GatewayError('评测收据与本任务不匹配')
        if not isinstance(receipt.get('local_report'),str):raise GatewayError('评测收据缺少原始 TXT 路径')
        report=within(self.store.user_root(job['owner']),receipt['local_report'])
        if report.suffix.lower()!='.txt' or not report.is_file():raise GatewayError('评测收据需要本人目录内的原始 TXT 文件')
        if digest(report)!=receipt.get('report_sha256'):raise GatewayError('原始报告内容与收据不一致')
        from importlib.util import spec_from_file_location,module_from_spec
        try:
            spec=spec_from_file_location('gateway_report',self.root/'vendor/parse_eval_report.py');mod=module_from_spec(spec);spec.loader.exec_module(mod)
            parsed=mod.parse_report(report); validity=mod.validate_result(parsed,adapter.get('validity',{}))
        except Exception:raise GatewayError('原始 TXT 内容或评测校验规则未通过，无法续交付') from None
        if validity['decision']!='VALID':raise GatewayError('TXT 报告有效样本或任务覆盖未通过')
        return {'task':p['source_task'],'report':str(report),'local_report':str(report),'metrics':parsed['metrics']}
