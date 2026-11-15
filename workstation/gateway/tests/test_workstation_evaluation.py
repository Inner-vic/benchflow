"""Isolated bridge tests; no SSH, evaluator binary, bench or upload is invoked."""
import base64
import copy
import hashlib
import contextlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.core import EvaluationOutcomeUnknown,GatewayError,Store
from bench_gateway import workstation_evaluation as evaluation


class WorkstationEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        project=Path(__file__).resolve().parents[1]
        self.cfg={'project_root':str(project),'state_root':str(self.root/'state'),
                  'remote_users_root':'/mnt/data/bench-gateway/users','workstation':'ubuntu-workstation'}
        self.store=Store(self.cfg['state_root']);self.user='ou_eval_alice';self.other='ou_eval_bob'
        for user in (self.user,self.other):self.store.allow(user)
        self.session='feishu:oc_alice:'+self.user
        self.descriptor=evaluation.descriptor(self.cfg)
        self.baseline={'data':{'kind':'candidate-v1','evaluator':self.descriptor}}
        owner=self.store.remote_root(self.user,self.cfg['remote_users_root'])
        assets={'package':{'owner':self.user,'data':{'sha256':'a'*64,'remote_path':owner+'/package/asset.bin'}},
                'dataset':{'owner':'shared','origin':'dataset_catalog','data':{
                    'remote_path':'/mnt/data/bench-eval/datasets/shared18','manifest':'/mnt/data/bench-eval/inputs/shared18.json','sha256':'b'*64}},
                'dependencies':[]}
        source,_=self.store.submit(self.user,self.session,'source',{'action':'replay','assets':assets,'baseline':self.baseline})
        self.run=source['id'];self.task='/mnt/data/bench-eval/tasks/'+self.run
        self.store.finish(source,'SUCCEEDED',{'state':'PASS','run_id':self.run,'task':self.task})
        self.source_job=self.store.job(self.user,self.run)
        self.job,_=self.store.submit(self.user,self.session,'eval',{'action':'evaluate','source_job':self.run,
            'source_task':self.task,'source_assets':assets,'baseline':self.baseline})
        product='/mnt/data/bench-eval/cache/predictions/'+self.run+'/predictions'
        self.source={'source_task':self.task,'task':self.task,'prediction_path':product,'product':product,
            'product_receipt':product+'.bench-eval-product.json','receipt':product+'.bench-eval-product.json',
            'producer_run_id':self.run,'product_identity':'c'*64,'product_receipt_sha256':'d'*64,
            'job_sha256':'e'*64,'prepared_manifest_sha256':'b'*64,'index_sha256':'f'*64,
            'source_identity':'1'*64,'scene_names':['scene-a'],'scene_count':1,'hardware_actions':'NOT_RUN',
            'discovery':{'producer_run_id':self.run,'prediction_path':product,'scenes':['scene-a'],
                'required_tasks':['vision'],'prediction_receipt_sha256':'d'*64,
                'prepared_manifest_sha256':'b'*64,'job_sha256':'e'*64,
                'scene_inputs':[{'scene':'scene-a','truth_frames':2,'vision_gt_json_files':2}],'warnings':[]}}
        self.raw=('候选预测帧: 2\r\n至少完成一项任务: 2\r\n未匹配真值时间戳: 0\r\n'
                  '评测场景数: 1\r\n[1] scene-a: 2/2\r\nOverall mAP: 0.85\r\n').encode('utf-8')

    def payload(self,source=None,raw=None):
        source=copy.deepcopy(source or self.source);raw=self.raw if raw is None else raw
        attempt=self.store.remote_root(self.user,self.cfg['remote_users_root'])+'/evaluations/'+self.job['id']
        native={'run_id':self.job['id'],'prediction_path':source['prediction_path'],'scenes':source['scene_names'],
            'scene_count':1,'source_postchecks_passed':False,'native_global_aggregation':True,
            'input_verification':evaluation.POLICY,'input_content_verified':False,'path_checks_passed':True,
            'source_data_hash_passes':0,'dataset_identity_scope':'manifest-and-paths',
            'package_manifest_sha256':self.descriptor['pin']['evaluator']['manifest_sha256'],
            'config_path':attempt+'/evaluation_param.yaml','config_sha256':'2'*64,'dataset_identity':'3'*64,
            'report_sha256':hashlib.sha256(raw).hexdigest(),'discovery':source['discovery'],
            'scene_coverage':[{'index':1,'scene':'scene-a','completed_frames':2,'truth_frames':2}]}
        return {'type':'result','state':'EVALUATION_COMPLETED','source':source,'receipt':native,
                'remote_attempt':attempt,'report_base64':base64.b64encode(raw).decode('ascii')}

    def remote(self,payload=None):
        payload=payload or self.payload()
        def call(cfg,request,read_only=True):
            if request['action']=='inspect':return {'type':'result','state':'SOURCE_VERIFIED','source':copy.deepcopy(payload['source'])}
            return copy.deepcopy(payload)
        return call

    def run_evaluation(self,payload=None,collect_only=False):
        with patch.object(evaluation,'_remote',side_effect=self.remote(payload)):
            return evaluation.evaluate(self.cfg,self.store,self.job,collect_only=collect_only)

    def receipt(self,result):return json.loads(Path(result['receipt_path']).read_text(encoding='utf-8'))

    def test_descriptor_is_stable_without_local_paths_or_commands(self):
        value=evaluation.descriptor(self.cfg)
        self.assertEqual(value['kind'],'workstation-offline-v1')
        self.assertNotIn('argv',value);self.assertNotIn(str(self.root),json.dumps(value))
        self.assertFalse(value['uploads']);self.assertFalse(value['requires_bench'])
        evaluation.validate_descriptor(self.cfg,value)
        with self.assertRaises(GatewayError):evaluation.validate_descriptor(self.cfg,{**value,'argv':['sh']})

    def test_modified_vendor_and_matching_modified_manifest_are_both_rejected(self):
        copyroot=self.root/'project/vendor/offline_eval'
        shutil.copytree(Path(self.cfg['project_root'])/'vendor/offline_eval',copyroot)
        cfg={**self.cfg,'project_root':str(self.root/'project')}
        self.assertEqual(evaluation.descriptor(cfg),self.descriptor)
        target=copyroot/'scripts/report_scope.py';target.write_text('# unreviewed modification\n',encoding='utf-8')
        provenance=copyroot/'provenance.json';data=json.loads(provenance.read_text(encoding='utf-8'))
        data['files']['scripts/report_scope.py'].update(sha256=hashlib.sha256(target.read_bytes()).hexdigest(),size_bytes=target.stat().st_size)
        provenance.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaises(GatewayError):evaluation.verify_vendor(cfg)

    def test_legacy_descriptor_and_receipt_are_recoverable_but_never_execute(self):
        old=evaluation.legacy_descriptor(self.cfg)
        self.assertEqual(old['bridge_sha256'],evaluation.LEGACY_BRIDGE)
        evaluation.validate_descriptor(self.cfg,old)
        forged={**old,'input_verification':evaluation.POLICY}
        with self.assertRaises(GatewayError):evaluation.validate_descriptor(self.cfg,forged)
        payload=copy.deepcopy(self.job['payload']);payload['baseline']['data']['evaluator']=old
        with self.store.connect() as c:c.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),self.job['id']))
        self.job=self.store.job(self.user,self.job['id'])
        response=self.payload();native=response['receipt'];native['source_postchecks_passed']=True
        for key in ('input_verification','input_content_verified','path_checks_passed','source_data_hash_passes','dataset_identity_scope'):native.pop(key)
        with patch.object(evaluation,'_remote',side_effect=self.remote(response)):
            result=evaluation.evaluate(self.cfg,self.store,self.job,collect_only=True)
        self.assertEqual(self.receipt(result)['descriptor_sha256'],old['descriptor_sha256'])
        with patch.object(evaluation,'_remote') as remote:
            with self.assertRaisesRegex(GatewayError,'旧评测请求'):evaluation.evaluate(self.cfg,self.store,self.job)
            remote.assert_not_called()

    def test_new_evaluation_of_completed_legacy_replay_uses_current_policy_without_rewriting_source(self):
        from bench_gateway.service import Service
        payload=copy.deepcopy(self.source_job['payload'])
        payload['baseline']['data']['evaluator']=evaluation.legacy_descriptor(self.cfg)
        with self.store.connect() as c:c.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),self.run))
        old=self.store.job(self.user,self.run)
        self.store.finish(self.job,'CANCELLED',{'error':'isolated previous queued fixture'})
        cfg={**self.cfg,'mode':'live','workstation_evaluation_enabled':True,'default_evaluator':self.descriptor}
        with patch.object(evaluation,'_remote',side_effect=self.remote()):
            Service(cfg).handle(self.session,'评测任务 '+self.run,{'action':'evaluate','task_id':self.run},'new-evaluation-policy')
        jobs=[j for j in self.store.jobs(self.user) if j['payload'].get('source_job')==self.run and j['id']!=self.job['id']]
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['payload']['baseline']['data']['evaluator'],self.descriptor)
        self.assertEqual(self.store.job(self.user,self.run)['payload'],old['payload'])

    def test_complete_report_is_saved_byte_for_byte_with_native_receipts(self):
        result=self.run_evaluation()
        self.assertEqual(result['evaluation_state'],'COMPLETE');self.assertEqual(result['warnings'],[])
        self.assertEqual(Path(result['local_report']).read_bytes(),self.raw)
        self.assertEqual(result['evaluated_scenes'],['scene-a'])
        for filename in ('directory-receipt.json','report-metadata.json','evaluation-summary.json'):
            self.assertTrue((Path(result['local_report']).parent/filename).is_file())
        with patch.object(evaluation,'_remote',side_effect=self.remote()):
            recovered=evaluation.validate_receipt(self.cfg,self.store,self.job,self.receipt(result))
        self.assertEqual(recovered['metrics'],result['metrics'])

    def test_missing_visual_truth_delivers_partial_txt_without_claiming_complete(self):
        source=copy.deepcopy(self.source);source['discovery']['warnings']=['VISION_GT_MISSING']
        source['discovery']['scene_inputs'][0]['vision_gt_json_files']=0
        result=self.run_evaluation(self.payload(source))
        self.assertEqual(result['evaluation_state'],'PARTIAL');self.assertIn('VISION_GT_MISSING',result['warnings'])
        self.assertEqual(Path(result['local_report']).read_bytes(),self.raw)

    def test_only_na_metrics_remain_a_partial_deliverable(self):
        raw=self.raw.replace(b'Overall mAP: 0.85',b'Overall mAP: N/A')
        result=self.run_evaluation(self.payload(raw=raw))
        self.assertEqual(result['evaluation_state'],'PARTIAL')
        self.assertIn('REQUESTED_TASKS_OR_VALID_SAMPLES_INCOMPLETE',result['warnings'])
        self.assertEqual(Path(result['local_report']).read_bytes(),raw)

    def test_report_scene_scope_mismatch_cannot_be_delivered(self):
        payload=self.payload(raw=self.raw.replace(b'scene-a',b'scene-b'))
        with self.assertRaises(EvaluationOutcomeUnknown):self.run_evaluation(payload)

    def test_report_hash_or_native_tool_identity_mismatch_cannot_succeed(self):
        for change in ({'report_sha256':'0'*64},{'package_manifest_sha256':'0'*64},{'path_checks_passed':False},
                       {'input_content_verified':True},{'source_postchecks_passed':True}):
            with self.subTest(change=change):
                payload=self.payload();payload['receipt'].update(change)
                with self.assertRaises(EvaluationOutcomeUnknown):self.run_evaluation(payload)

    def test_cross_user_source_is_rejected_before_ssh(self):
        changed=copy.deepcopy(self.job['payload']);changed['source_job']='not-owned'
        with self.store.connect() as connection:
            connection.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(changed),self.job['id']))
        job=self.store.job(self.user,self.job['id'])
        with patch.object(evaluation,'_remote') as remote:
            with self.assertRaises(GatewayError):evaluation.evaluate(self.cfg,self.store,job)
            remote.assert_not_called()

    def test_source_assets_and_submitted_payload_cannot_be_replaced(self):
        job=copy.deepcopy(self.job);job['payload']['source_assets']['dataset']['data']['sha256']='0'*64
        with patch.object(evaluation,'_remote') as remote:
            with self.assertRaises(GatewayError):evaluation.evaluate(self.cfg,self.store,job)
            remote.assert_not_called()

    def test_simulated_or_unfinished_source_is_rejected_before_ssh(self):
        for state in ('SIMULATED','QUEUED','RUNNING','NEEDS_REVIEW'):
            with self.subTest(state=state):
                with self.store.connect() as connection:connection.execute('UPDATE jobs SET state=? WHERE id=?',(state,self.run))
                with patch.object(evaluation,'_remote') as remote:
                    with self.assertRaises(GatewayError):evaluation.evaluate(self.cfg,self.store,self.job)
                    remote.assert_not_called()

    def test_automatic_handoff_only_accepts_its_bound_child(self):
        with self.store.connect() as connection:
            result={**self.source_job['result'],'evaluation_job':self.job['id']}
            connection.execute("UPDATE jobs SET state='POSTPROCESSING',result=? WHERE id=?",(json.dumps(result),self.run))
        self.assertEqual(self.run_evaluation()['evaluation_state'],'COMPLETE')
        with self.store.connect() as connection:
            result['evaluation_job']='other-child';connection.execute('UPDATE jobs SET result=? WHERE id=?',(json.dumps(result),self.run))
        with patch.object(evaluation,'_remote') as remote:
            with self.assertRaises(GatewayError):evaluation.evaluate(self.cfg,self.store,self.job)
            remote.assert_not_called()

    def test_predispatch_source_failure_is_known_and_never_executes(self):
        with patch.object(evaluation,'_remote',side_effect=GatewayError('source changed')) as remote:
            with self.assertRaises(GatewayError) as error:evaluation.evaluate(self.cfg,self.store,self.job)
        self.assertNotIsInstance(error.exception,EvaluationOutcomeUnknown)
        self.assertEqual(remote.call_count,1);self.assertEqual(remote.call_args.args[1]['action'],'inspect')

    def test_disconnect_after_dispatch_is_unknown_and_has_stable_attempt(self):
        def call(cfg,request,read_only=True):
            if request['action']=='inspect':return {'state':'SOURCE_VERIFIED','source':self.source}
            raise ConnectionError('lost')
        with patch.object(evaluation,'_remote',side_effect=call):
            with self.assertRaises(EvaluationOutcomeUnknown) as error:evaluation.evaluate(self.cfg,self.store,self.job)
        self.assertIn('/evaluations/'+self.job['id'],str(error.exception))

    def test_unknown_attempt_can_only_collect_without_execute(self):
        self.store.finish(self.job,'NEEDS_REVIEW',{'error':'disconnect'})
        self.job=self.store.job(self.user,self.job['id'])
        with patch.object(evaluation,'_remote') as remote:
            with self.assertRaises(GatewayError):evaluation.evaluate(self.cfg,self.store,self.job)
            remote.assert_not_called()
        actions=[];real=self.remote()
        def call(cfg,request,read_only=True):actions.append(request['action']);return real(cfg,request,read_only)
        with patch.object(evaluation,'_remote',side_effect=call):
            self.assertEqual(evaluation.evaluate(self.cfg,self.store,self.job,collect_only=True)['evaluation_state'],'COMPLETE')
        self.assertEqual(actions,['inspect','collect'])

    def test_existing_local_txt_cannot_be_overwritten(self):
        result=self.run_evaluation();Path(result['local_report']).write_bytes(b'changed by another process')
        with self.assertRaises(EvaluationOutcomeUnknown):self.run_evaluation()

    def test_recovery_rejects_foreign_receipt_source_or_complete_claim(self):
        source=copy.deepcopy(self.source);source['discovery']['warnings']=['VISION_GT_MISSING']
        result=self.run_evaluation(self.payload(source));receipt=self.receipt(result)
        for change in ({'source_job':'another'},{'owner_key':'u_other'},{'evaluation_state':'COMPLETE'},
                       {'report_sha256':'0'*64},{'descriptor_sha256':'0'*64}):
            with self.subTest(change=change),patch.object(evaluation,'_remote',side_effect=self.remote(self.payload(source))):
                with self.assertRaises(GatewayError):evaluation.validate_receipt(self.cfg,self.store,self.job,{**receipt,**change})

    def test_public_recovery_compares_the_actual_remote_receipt_without_execution(self):
        result=self.run_evaluation();receipt=self.receipt(result);actions=[];real=self.remote()
        def call(cfg,request,read_only=True):
            actions.append(request['action'])
            if request['action']=='collect':self.assertIs(read_only,False)
            return real(cfg,request,read_only)
        with patch.object(evaluation,'_remote',side_effect=call):
            evaluation.validate_receipt(self.cfg,self.store,self.job,receipt)
        self.assertEqual(actions,['inspect','collect'])
        forged=copy.deepcopy(receipt);forged['native_receipt']['dataset_identity']='9'*64
        with patch.object(evaluation,'_remote',side_effect=self.remote()):
            with self.assertRaisesRegex(GatewayError,'真实评测产物不一致'):
                evaluation.validate_receipt(self.cfg,self.store,self.job,forged)

    def test_public_collect_has_a_full_scan_budget_without_starting_an_evaluator(self):
        result=self.run_evaluation();receipt=self.receipt(result);actions=[];payload=self.payload()
        original=evaluation._remote
        def call(cfg,request,read_only=True):
            actions.append(request['action'])
            if request['action']=='inspect':return {'state':'SOURCE_VERIFIED','source':copy.deepcopy(self.source)}
            self.assertEqual(request['action'],'collect')
            # Simulate a 474-second GT integrity scan without sleeping. The old
            # 180-second budget must fail while the full 9000-second budget can
            # collect the existing attempt; no execute path is provided.
            with patch('bench_gateway.workstation_evaluation.SSH') as ssh:
                def transport(code,args=(),timeout=60,**kwargs):
                    self.assertGreaterEqual(timeout,500)
                    self.assertIn("'action': 'collect'",code)
                    return json.dumps(payload)
                ssh.return_value.python.side_effect=transport
                return original(cfg,request,read_only)
        with patch.object(evaluation,'_remote',side_effect=call):
            verified=evaluation.validate_receipt(self.cfg,self.store,self.job,receipt)
        self.assertEqual(verified['evaluation_state'],'COMPLETE')
        self.assertEqual(actions,['inspect','collect'])

    def test_remote_protocol_allows_progress_but_never_extra_final_records(self):
        good='\n'.join(json.dumps(x) for x in ({'type':'progress','phase':'prepared'},
                                              {'type':'result','state':'PASS'}))
        with patch('bench_gateway.workstation_evaluation.SSH') as ssh:
            ssh.return_value.python.return_value=good
            self.assertEqual(evaluation._remote(self.cfg,{'action':'runtime_probe'})['state'],'PASS')
            code=ssh.return_value.python.call_args.args[0]
            self.assertIn('def bridge(',code);self.assertNotIn('LPAI',code)
            ssh.return_value.python.return_value=good+'\n'+json.dumps({'type':'result','state':'PASS'})
            with self.assertRaises(GatewayError):evaluation._remote(self.cfg,{'action':'runtime_probe'})
            ssh.return_value.python.return_value=json.dumps({'type':'error','state':'EVALUATION_BLOCKED','message':'Task index does not match'})
            with self.assertRaisesRegex(GatewayError,'Task index does not match'):
                evaluation._remote(self.cfg,{'action':'inspect'})

    def test_inspect_task_rejects_non_task_paths_before_network(self):
        for task in ('/etc/passwd','/mnt/data/bench-eval/tasks','/mnt/data/bench-eval/tasks/a/../b',None):
            with self.subTest(task=task),patch.object(evaluation,'_remote') as remote:
                with self.assertRaises(GatewayError):evaluation.inspect_task(self.cfg,task)
                remote.assert_not_called()


class RemoteSourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.run='real-run'
        self.tasks=self.root/'tasks';self.task=self.tasks/self.run
        self.product=self.root/'predictions'/self.run/'predictions';self.product.mkdir(parents=True)
        (self.task/'inputs').mkdir(parents=True)
        self.prepared=self.root/'inputs/prepared.json';self.prepared.parent.mkdir();self.prepared.write_text('{}')
        self.truth=self.root/'datasets/example';self.truth.mkdir(parents=True)
        self.receipt_path=self.product.with_name('predictions.bench-eval-product.json')
        receipt={'state':'SUCCEEDED','product_type':'workstation_predictions','producer_run_id':self.run,
                 'workstation_product':str(self.product),'product_identity':'a'*64,'inventory_sha256':'b'*64,
                 'files':[{'path':'scene-a/pred_img.txt'}]}
        self.receipt_path.write_text(json.dumps(receipt));(self.task/self.receipt_path.name).write_bytes(self.receipt_path.read_bytes())
        index={'state':'PASS','task_id':self.run,'run_id':self.run,'canonical_product':str(self.product),
               'producer_receipt':str(self.receipt_path),'product_identity':'a'*64,'inventory_sha256':'b'*64,
               'file_count':1,'scene_count':1,'scenes':[{'scene':'scene-a','run_id':self.run}]}
        (self.task/'index.json').write_text(json.dumps(index))
        (self.task/'inputs/job.json').write_text(json.dumps({'run_id':self.run,'replay_input':{
            'prepared_manifest':str(self.prepared),'dataset_root':str(self.truth)},'app_artifact':{'sha256':'c'*64}}))
        self.pin={'prediction_root':str(self.root/'predictions'),'worker':{'sha256':'fixed'},'evaluator':{'version':'fixed'},
                  'discovery':{'tasks_root':str(self.tasks),'prepared_manifest_root':str(self.prepared.parent),'truth_root':str(self.truth.parent)}}
        def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        def inside(path,root):
            path,root=Path(path).resolve(),Path(root).resolve()
            if root not in path.parents:raise ValueError('outside')
            return path
        self.worker=SimpleNamespace(SAFE=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),sha=sha,inside=inside,
            discover=lambda req,pin:{'scenes':['scene-a'],'required_tasks':['vision'],'scene_inputs':[]})
        namespace={};exec(evaluation.REMOTE_GUARD,namespace);self.inspect=namespace['inspect_source'];self.bridge=namespace['bridge']
        self.request={'source_task':str(self.task),'expected_run':self.run,'required_tasks':None,'dataset':None}

    def test_remote_task_product_job_and_copy_are_bound_to_one_source(self):
        first=self.inspect(self.request,copy.deepcopy(self.pin),self.worker)
        second=self.inspect(self.request,copy.deepcopy(self.pin),self.worker)
        self.assertEqual(first['source_identity'],second['source_identity'])
        self.assertEqual(first['producer_run_id'],self.run);self.assertEqual(first['scene_names'],['scene-a'])

    def test_foreign_run_product_and_modified_producer_copy_are_rejected(self):
        with self.assertRaises(ValueError):self.inspect({**self.request,'expected_run':'other'},copy.deepcopy(self.pin),self.worker)
        (self.task/self.receipt_path.name).write_text('{}')
        with self.assertRaises(ValueError):self.inspect(self.request,copy.deepcopy(self.pin),self.worker)

    def test_dataset_scope_is_exact_before_deriving_upstream_allowed_parents(self):
        dataset={'manifest':str(self.prepared),'remote_path':str(self.truth),'sha256':self.worker.sha(self.prepared)}
        self.assertEqual(self.inspect({**self.request,'dataset':dataset},copy.deepcopy(self.pin),self.worker)['producer_run_id'],self.run)
        for change in ({'sha256':'0'*64},{'manifest':str(self.root/'elsewhere.json')},{'remote_path':str(self.truth.parent)}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.inspect({**self.request,'dataset':{**dataset,**change}},copy.deepcopy(self.pin),self.worker)

    def attempt_request(self,action):
        source=self.inspect(self.request,copy.deepcopy(self.pin),self.worker)
        users=self.root/'missing/subtree/users';key='u_'+'a'*24
        return {**self.request,'action':action,'expected_source_identity':source['source_identity'],
                'run_id':'eval-123','users_root':str(users),'owner_root':str(users/key),'owner_key':key}

    def test_execute_creates_only_the_trusted_owner_attempt_parent(self):
        request=self.attempt_request('execute');self.worker.execute=lambda req,pin,plan:{'type':'result','state':'TEST_ONLY_NO_EVALUATOR'}
        with contextlib.redirect_stdout(io.StringIO()):self.bridge(request,copy.deepcopy(self.pin),self.worker)
        attempts=Path(request['owner_root'])/'evaluations'
        self.assertTrue(attempts.is_dir())
        self.assertFalse((attempts/request['run_id']).exists())
        if __import__('os').name!='nt':self.assertEqual(attempts.stat().st_mode & 0o777,0o700)

    def test_collect_never_creates_a_missing_owner_directory(self):
        request=self.attempt_request('collect')
        with self.assertRaises(ValueError):self.bridge(request,copy.deepcopy(self.pin),self.worker)
        self.assertFalse(Path(request['users_root']).exists())

    def test_owner_directory_must_match_the_fixed_user_key_and_parent(self):
        for change in ({'owner_root':str(self.root/'elsewhere')},{'owner_key':'u_wrong'},
                       {'users_root':str(self.root/'other-users')}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.bridge({**self.attempt_request('execute'),**change},copy.deepcopy(self.pin),self.worker)
        self.assertFalse((self.root/'elsewhere').exists())


if __name__=='__main__':unittest.main()
