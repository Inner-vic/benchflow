import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bench_gateway.bindings import bind_preparation, implementation_snapshot, validate_candidate
from bench_gateway.core import GatewayError, Store
from bench_gateway.intake import Intake
from bench_gateway.preparation import PreparationQueue
from bench_gateway.service import Service, validate_intent


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for name in ('candidate_files', 'candidate_adapter', 'candidate_remote'):
            path=self.root/'bench_gateway'/(name+'.py')
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('# isolated administrator '+name+'\n',encoding='utf-8')
        batch=self.root/'vendor/run_stream_batch.py'
        batch.parent.mkdir(parents=True,exist_ok=True)
        batch.write_text('# isolated administrator batch\n',encoding='utf-8')
        (self.root/'vendor/provenance.json').write_text(json.dumps({'run_stream_batch.py':{
            'sha256':hashlib.sha256(batch.read_bytes()).hexdigest()}}),encoding='utf-8')
        self.cfg={'project_root':str(self.root),'state_root':str(self.root/'state'),
                  'mode':'simulate','live_enabled':False,'remote_users_root':'/data/users',
                  'notifications_enabled':False,'default_adapter_template':'reviewed',
                  'adapter_templates':{'reviewed':{'template_id':'tpl-001','source_run':'source-verified',
                                                  'snapshot':{'sha256':'a'*64}}}}
        self.store=Store(self.cfg['state_root']);self.user='ou_binding_alice';self.other='ou_binding_bob'
        self.session='feishu:oc_alice:'+self.user
        for user in (self.user,self.other):self.store.allow(user)
        self.service=Service(self.cfg)
        root=self.store.remote_root(self.user,self.cfg['remote_users_root'])
        self.package=self.store.add_asset(self.user,'package','A',{'sha256':'1'*64,'remote_path':root+'/packages/a.tgz'})
        self.dataset=self.store.add_asset(self.user,'dataset','D',{'sha256':'2'*64,'scene_count':3,'manifest':root+'/datasets/d.json'})
        self.sos={}
        for index,(field,name,target) in enumerate((('camera_so','camera','libxcamera.so'),('lidar_so','lidar','libxlidar.so'),('m100_so','M100','liburoad_m100.so')),3):
            self.sos[field]=self.store.add_asset(self.user,'dependency',name,{'sha256':str(index)*64,
                'remote_path':root+'/dependencies/'+target,'target':'lib/'+target})
        self.draft={'goal':'replay','package':'A','dataset':'D','camera_so':'camera','lidar_so':'lidar',
                    'm100_so':'M100','m100_mode':'provided','scene_scope':'1,2','lpai_target':'none',
                    'yaml_request':'common_param.yaml debug.enable=true'}
        Intake(self.store).update(self.user,self.draft,new=True)
        self.queue=PreparationQueue(self.cfg,self.store)
        self.preparation=self.make_preparation()
        self.receipt={'state':'READY_FOR_GATE','path':root+'/preparations/'+self.preparation['id']+'/binding.json',
                      'sha256':'a'*64,'input_digest':'b'*64,'template_id':'tpl-001',
                      'implementation_hashes':implementation_snapshot(self.root)}

    def tearDown(self):self.tmp.cleanup()

    def make_preparation(self):
        self.queue.submit(self.user,self.session)
        preparation=self.queue.latest(self.user)
        result={'status':'STATIC_PASSED','assets':{'package':self.package,**self.sos},'dataset':self.dataset,
                'scenes':[1,2],'yaml_preparation':{'status':'PREPARED','outputs':[{'target':'config/common_param.yaml','sha256':'c'*64}]} }
        with self.store.connect() as c:
            c.execute("UPDATE preparations SET state='STATIC_PASSED',result=? WHERE id=?",(json.dumps(result),preparation['id']))
        return self.queue.latest(self.user)

    def bind(self,receipt=None,**kwargs):
        with patch('bench_gateway.executor.Executor.bind_candidate',return_value=receipt or self.receipt,create=True):
            return bind_preparation(self.cfg,self.store,self.user,**kwargs)

    def change_result(self,update):
        result=copy.deepcopy(self.queue.latest(self.user)['result']);update(result)
        with self.store.connect() as c:c.execute('UPDATE preparations SET result=? WHERE id=?',(json.dumps(result),self.preparation['id']))

    def test_binding_freezes_preparation_without_claiming_hardware_gate_passed(self):
        with patch('bench_gateway.executor.Executor.bind_candidate',return_value=self.receipt,create=True) as remote:
            bound=bind_preparation(self.cfg,self.store,self.user)
        data=bound['data']
        self.assertEqual(data['kind'],'candidate-v1');self.assertEqual(data['readiness'],'READY_FOR_GATE')
        self.assertIs(data['gate_verified'],False)
        self.assertEqual(data['preparation_id'],self.preparation['id'])
        self.assertEqual(data['preparation_fingerprint'],self.preparation['fingerprint'])
        self.assertEqual(data['scenes'],[1,2])
        self.assertEqual(data['assets']['package'],self.package)
        self.assertEqual(data['binding']['implementation_hashes'],implementation_snapshot(self.root))
        self.assertEqual(len(data['assets']['dependencies']),3)
        self.assertEqual(remote.call_args.args[0],self.user)
        self.assertEqual(remote.call_args.args[2]['owner'],self.user)
        self.assertEqual(remote.call_args.args[3],self.cfg['adapter_templates']['reviewed'])
        self.assertEqual(self.store.jobs(self.user),[])

    def test_same_binding_digest_is_idempotent(self):
        first=self.bind();second=self.bind()
        self.assertEqual(first['id'],second['id'])
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM baselines').fetchone()[0],1)

    def test_new_preparation_can_bind_same_content_digest_without_overwriting_old_record(self):
        first=self.bind();new_preparation=self.make_preparation()
        second=self.bind()
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(second['data']['preparation_id'],new_preparation['id'])
        with self.assertRaises(GatewayError):validate_candidate(self.cfg,self.store,self.user,first)
        validate_candidate(self.cfg,self.store,self.user,second)

    def test_existing_name_is_not_overwritten_with_different_input(self):
        first=self.bind(name='my-binding')
        with self.assertRaises(GatewayError):self.bind({**self.receipt,'input_digest':'d'*64},name='my-binding')
        self.assertEqual(self.store.baseline(self.user,'my-binding'),first)

    def test_cross_user_preparation_and_cross_user_material_are_rejected(self):
        with patch('bench_gateway.executor.Executor.bind_candidate',create=True) as remote:
            with self.assertRaises(GatewayError):
                bind_preparation(self.cfg,self.store,self.other,preparation_id=self.preparation['id'])
            remote.assert_not_called()
        self.change_result(lambda result:result['assets']['package'].update(owner=self.other))
        with self.assertRaises(GatewayError):self.bind()

    def test_only_catalog_shared_datasets_are_accepted(self):
        self.change_result(lambda result:result['dataset'].update(owner='shared',origin='dataset_catalog'))
        bound=self.bind();self.assertEqual(bound['data']['assets']['dataset']['owner'],'shared')
        self.change_result(lambda result:result['dataset'].pop('origin'))
        with self.assertRaises(GatewayError):self.bind()

    def test_stale_draft_and_pending_latest_check_do_not_bind(self):
        Intake(self.store).update(self.user,{'scene_scope':'全部'})
        with patch('bench_gateway.executor.Executor.bind_candidate',create=True) as remote:
            with self.assertRaises(GatewayError):self.bind()
            remote.assert_not_called()
        Intake(self.store).update(self.user,{'scene_scope':'1,2'})
        self.queue.submit(self.user,self.session)
        with self.assertRaises(GatewayError):self.bind(preparation_id=self.preparation['id'])

    def test_draft_changed_during_remote_binding_does_not_store_stale_candidate(self):
        def remote(*args):
            Intake(self.store).update(self.user,{'camera_so':'camera-B'})
            return self.receipt
        with patch('bench_gateway.executor.Executor.bind_candidate',side_effect=remote,create=True):
            with self.assertRaises(GatewayError):bind_preparation(self.cfg,self.store,self.user)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM baselines').fetchone()[0],0)

    def test_remote_receipt_must_have_correct_readiness_hash_owner_and_template(self):
        bads=[{**self.receipt,'state':'PASS'},{**self.receipt,'state':'SIMULATED'},
              {**self.receipt,'sha256':'bad'},{**self.receipt,'template_id':'other'},
              {**self.receipt,'path':self.store.remote_root(self.other,self.cfg['remote_users_root'])+'/binding.json'}]
        for receipt in bads:
            with self.subTest(receipt=receipt),self.assertRaises(GatewayError):self.bind(receipt)

    def test_remote_receipt_requires_exact_complete_current_implementation_identity(self):
        valid=self.receipt['implementation_hashes']
        invalid=[None,{}, {k:v for k,v in valid.items() if k!='batch'},
                 {**valid,'extra':'0'*64}, {**valid,'batch':'bad'},
                 {**valid,'candidate_remote':'e'*64}]
        for identity in invalid:
            with self.subTest(identity=identity),self.assertRaises(GatewayError):
                self.bind({**self.receipt,'implementation_hashes':identity})
        self.assertEqual(self.store.jobs(self.user),[])

    def test_changed_installed_helper_marks_candidate_stale_before_queue_or_ssh(self):
        bound=self.bind()
        for name in ('candidate_files','candidate_adapter','candidate_remote'):
            path=self.root/'bench_gateway'/(name+'.py')
            original=path.read_bytes()
            path.write_bytes(original+b'# new administrator release\n')
            with self.subTest(name=name):
                with self.assertRaisesRegex(GatewayError,'实现已更新'):
                    validate_candidate(self.cfg,self.store,self.user,bound)
                summary=self.service.context(self.user)['baselines'][0]
                self.assertFalse(summary['current']);self.assertEqual('RECHECK_REQUIRED',summary['readiness'])
                with patch('bench_gateway.executor.Executor.remote',side_effect=AssertionError('must reject before SSH')):
                    with self.assertRaises(GatewayError):
                        self.service.handle(self.session,'执行回灌',{'action':'replay'})
                self.assertEqual(self.store.jobs(self.user),[])
            path.write_bytes(original)
        validate_candidate(self.cfg,self.store,self.user,bound)

    def test_batch_change_requires_matching_provenance_and_fresh_binding(self):
        bound=self.bind()
        batch=self.root/'vendor/run_stream_batch.py'
        batch.write_bytes(batch.read_bytes()+b'# new administrator release\n')
        with self.assertRaisesRegex(GatewayError,'入口校验失败'):
            validate_candidate(self.cfg,self.store,self.user,bound)
        (self.root/'vendor/provenance.json').write_text(json.dumps({'run_stream_batch.py':{
            'sha256':hashlib.sha256(batch.read_bytes()).hexdigest()}}),encoding='utf-8')
        with self.assertRaisesRegex(GatewayError,'实现已更新'):
            validate_candidate(self.cfg,self.store,self.user,bound)
        self.receipt['implementation_hashes']=implementation_snapshot(self.root)
        self.make_preparation()
        current=self.bind()
        self.assertNotEqual(bound['id'],current['id'])
        validate_candidate(self.cfg,self.store,self.user,current)

    def test_old_candidate_without_recorded_implementation_is_recheck_required(self):
        bound=self.bind()
        del bound['data']['binding']['implementation_hashes']
        with self.assertRaisesRegex(GatewayError,'缺少完整实现身份'):
            validate_candidate(self.cfg,self.store,self.user,bound)
        with self.store.connect() as c:
            c.execute('UPDATE baselines SET data=? WHERE id=?',(json.dumps(bound['data']),bound['id']))
        summary=self.service.context(self.user)['baselines'][0]
        self.assertFalse(summary['current']);self.assertEqual('RECHECK_REQUIRED',summary['readiness'])
        with self.assertRaises(GatewayError):
            self.service.handle(self.session,'执行回灌',{'action':'replay'})
        self.assertEqual(self.store.jobs(self.user),[])

    def test_missing_sources_or_invalid_provenance_never_claim_candidate_is_current(self):
        self.bind()
        for path in (self.root/'bench_gateway/candidate_files.py',self.root/'vendor/provenance.json'):
            raw=path.read_bytes()
            path.unlink()
            with self.subTest(path=path.name):
                summary=self.service.context(self.user)['baselines'][0]
                self.assertFalse(summary['current']);self.assertEqual('RECHECK_REQUIRED',summary['readiness'])
                with self.assertRaises(GatewayError):implementation_snapshot(self.root)
            path.write_bytes(raw)
        (self.root/'vendor/provenance.json').write_text('{broken json',encoding='utf-8')
        with self.assertRaises(GatewayError):implementation_snapshot(self.root)

    def test_local_implementation_upgrade_during_binding_does_not_save_old_receipt(self):
        def changed(*_args):
            path=self.root/'bench_gateway/candidate_adapter.py'
            path.write_bytes(path.read_bytes()+b'# deployment changed during RPC\n')
            return self.receipt
        with patch('bench_gateway.executor.Executor.bind_candidate',side_effect=changed):
            with self.assertRaisesRegex(GatewayError,'实现已更新'):
                bind_preparation(self.cfg,self.store,self.user)
        with self.store.connect() as c:
            self.assertEqual(0,c.execute('SELECT count(*) FROM baselines').fetchone()[0])

    def test_unprepared_yaml_and_missing_so_are_not_bound(self):
        self.change_result(lambda result:result.update(yaml_preparation={'status':'NOT_VERIFIED'}))
        with self.assertRaises(GatewayError):self.bind()
        self.change_result(lambda result:result['assets'].pop('camera_so'))
        with self.assertRaises(GatewayError):self.bind()

    def test_yaml_candidate_can_queue_but_still_requires_current_small_scenario_gate(self):
        bound=self.bind()
        with patch.object(self.service,'asset',side_effect=AssertionError('must use frozen inputs')):
            response=self.service.handle(self.session,'执行回灌',{'action':'replay'})
        job=self.store.jobs(self.user)[0]
        self.assertEqual(job['state'],'QUEUED');self.assertEqual(job['payload']['scenes'],[1,2])
        self.assertEqual(job['payload']['baseline']['id'],bound['id'])
        self.assertIs(job['payload']['baseline']['data']['gate_verified'],False)
        self.assertIn('本次小场景预检',response)

    def test_newer_registered_version_does_not_replace_frozen_prepared_asset(self):
        bound=self.bind()
        newer=self.store.add_asset(self.user,'package','A',{'sha256':'e'*64})
        self.service.handle(self.session,'用 A 执行回灌',{'action':'replay','package':'A'})
        package=self.store.jobs(self.user)[0]['payload']['assets']['package']
        self.assertEqual(package['id'],bound['data']['assets']['package']['id'])
        self.assertNotEqual(package['id'],newer['id'])

    def test_explicit_selected_inputs_must_match_the_frozen_candidate(self):
        self.bind()
        variants=[{'package':'B'},{'dataset':'other'},{'dependencies':['M100']},
                  {'dependencies':['camera','lidar','not-M100']},{'scenes':[2,3]}]
        for selection in variants:
            with self.subTest(selection=selection),self.assertRaises(GatewayError):
                self.service.handle(self.session,'执行指定回灌',{'action':'replay',**selection})
        self.assertEqual(self.store.jobs(self.user),[])

    def test_explicit_matching_inputs_are_admitted(self):
        self.bind()
        self.service.handle(self.session,'执行场景 1、2 回灌',{'action':'replay','package':'A','dataset':'D',
            'dependencies':['M100','camera','lidar'],'scenes':[1,2]})
        self.assertEqual(len(self.store.jobs(self.user)),1)

    def test_explicit_all_scenes_cannot_silently_use_candidate_subset(self):
        self.bind()
        with self.assertRaises(GatewayError):
            self.service.handle(self.session,'执行全部场景回灌',{'action':'replay','scenes':[]})
        self.assertEqual(self.store.jobs(self.user),[])

    def test_new_check_invalidates_older_candidate_even_with_same_draft_fingerprint(self):
        bound=self.bind();self.make_preparation()
        with self.assertRaises(GatewayError):validate_candidate(self.cfg,self.store,self.user,bound)
        with self.assertRaises(GatewayError):self.service.handle(self.session,'执行回灌',{'action':'replay'})

    def test_unbound_yaml_cannot_fall_back_to_legacy_baseline(self):
        dependencies=list(self.sos.values())
        self.store.add_baseline(self.user,'old',{'gate_verified':True,'asset_ids':{'package':self.package['id'],
            'dataset':self.dataset['id'],'dependencies':sorted(asset['id'] for asset in dependencies)}})
        with self.assertRaises(GatewayError):
            self.service.handle(self.session,'执行回灌',{'action':'replay','baseline':'old','package':'A','dataset':'D',
                'dependencies':['camera','lidar','M100']})
        self.assertEqual(self.store.jobs(self.user),[])

    def test_fake_gate_passed_or_changed_template_is_not_a_candidate(self):
        bound=self.bind()
        forged=copy.deepcopy(bound);forged['data']['gate_verified']=True
        with self.assertRaises(GatewayError):validate_candidate(self.cfg,self.store,self.user,forged)
        self.cfg['adapter_templates']['reviewed']['template_id']='tpl-002'
        with self.assertRaises(GatewayError):validate_candidate(self.cfg,self.store,self.user,bound)

    def test_yaml_receipt_or_template_mutation_invalidates_binding_even_with_same_ids(self):
        bound=self.bind()
        self.change_result(lambda result:result['yaml_preparation']['outputs'][0].update(sha256='f'*64))
        with self.assertRaises(GatewayError):validate_candidate(self.cfg,self.store,self.user,bound)
        self.change_result(lambda result:result['yaml_preparation']['outputs'][0].update(sha256='c'*64))
        self.cfg['adapter_templates']['reviewed']['snapshot']['sha256']='f'*64
        with self.assertRaises(GatewayError):validate_candidate(self.cfg,self.store,self.user,bound)

    def test_new_inputs_on_replay_or_evaluation_are_saved_and_never_ignored(self):
        self.bind()
        for action in ('replay','evaluate'):
            with self.subTest(action=action),self.assertRaises(GatewayError):
                self.service.handle(self.session,'更换软件再执行',{'action':action,'inputs':{'package':'new-package'}})
            self.assertEqual(Intake(self.store).get(self.user)['package'],'new-package')
        self.assertEqual(self.store.jobs(self.user),[])

    def test_context_has_candidate_summary_without_remote_binding_paths(self):
        bound=self.bind()
        summary=self.service.context(self.user)['baselines'][0]
        self.assertEqual(summary['preparation_id'],bound['data']['preparation_id'])
        self.assertTrue(summary['current']);self.assertEqual(summary['readiness'],'READY_FOR_GATE')
        self.assertNotIn('/data/users',json.dumps(summary));self.assertNotIn('binding',summary)
        Intake(self.store).update(self.user,{'package':'new-package'})
        summary=self.service.context(self.user)['baselines'][0]
        self.assertFalse(summary['current']);self.assertEqual(summary['readiness'],'RECHECK_REQUIRED')

    def test_chat_cannot_supply_adapter_templates_or_shell_commands(self):
        for field in ('template','adapter_templates','command','binding'):
            with self.subTest(field=field),self.assertRaises(GatewayError):
                validate_intent({'action':'replay',field:'anything'})

    def test_cli_bind_preparation_works_while_live_is_disabled(self):
        from bench_gateway.cli import main
        argv=['gateway','bind-preparation','--user',self.user,'--template','reviewed','--name','named','--preparation',self.preparation['id']]
        with patch('sys.argv',argv),patch('bench_gateway.cli.load',return_value=self.cfg),\
             patch('bench_gateway.bindings.bind_preparation',return_value={'state':'READY_FOR_GATE'}) as bind,\
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            main()
        self.assertEqual(bind.call_args.args[2:],(self.user,'reviewed','named',self.preparation['id']))
        self.assertEqual(json.loads(stdout.getvalue())['state'],'READY_FOR_GATE')


if __name__=='__main__':unittest.main()
