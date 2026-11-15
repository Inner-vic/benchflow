"""Archive -> a new task -> current checks; no real SSH or hardware."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import test_assemblies as fixtures
from bench_gateway.assemblies import assembly_assets,list_assemblies,record_success,resolve_assembly
from bench_gateway.core import GatewayError,format_result
from bench_gateway.executor import Executor
from bench_gateway.intake import Intake
from bench_gateway.preparation import MaterialCheck
from bench_gateway.service import Service


class AssemblyFlowTests(unittest.TestCase):
    example=fixtures.AssemblyCatalogTests.example
    save=fixtures.AssemblyCatalogTests.save
    record=fixtures.AssemblyCatalogTests.record
    update_payload=fixtures.AssemblyCatalogTests.update_payload

    def setUp(self):
        fixtures.AssemblyCatalogTests.setUp(self)
        self.cfg.update(state_root=str(self.store.root),project_root=str(Path(__file__).resolve().parents[1]),
                        mode='simulate',live_enabled=False,notifications_enabled=False)
        self.service=Service(self.cfg);self.session='feishu:oc_assembly:'+self.user

    def select(self,record=None,**inputs):
        record=record or self.record()
        return self.service.handle(self.session,'复用装配 '+record['id'],{
            'action':'prepare','inputs':{'assembly_ref':record['id'],**inputs}})

    def test_select_is_a_new_task_without_old_yaml_dataset_goal_or_queue(self):
        Intake(self.store).update(self.user,{'package':'old','camera_so':'old-camera',
            'dataset':'old-private-data','scene_scope':'1','goal':'replay_and_evaluate',
            'yaml_request':'old task output path','lpai_target':'personal'},new=True)
        count=len(self.store.jobs(self.user));reply=self.select()
        draft=Intake(self.store).get(self.user)
        self.assertEqual(draft['goal'],'replay_and_evaluate');self.assertEqual(draft['lpai_target'],'none')
        self.assertNotIn('yaml_request',draft)
        self.assertEqual('共享18场景',draft['dataset']);self.assertEqual('全部',draft['scene_scope'])
        self.assertEqual(draft['m100_mode'],'provided')
        self.assertTrue(draft['package'].endswith('-package'))
        self.assertNotIn('还需要：数据集、场景范围',reply)
        self.assertEqual(len(self.store.jobs(self.user)),count)

    def test_selection_keeps_only_current_message_dataset_goal_and_yaml(self):
        self.select(dataset='共享18场景',scene_scope='全部',goal='replay_and_evaluate',yaml_request='new explicit YAML')
        draft=Intake(self.store).get(self.user)
        self.assertEqual(draft['goal'],'replay_and_evaluate')
        self.assertEqual(draft['yaml_request'],'new explicit YAML')
        self.assertEqual(draft['dataset'],'共享18场景')

    def test_missing_or_foreign_archive_never_falls_back_or_changes_draft(self):
        record=self.record();Intake(self.store).update(self.other,{'package':'bob-raw'},new=True)
        with self.assertRaises(GatewayError):
            self.service.handle('feishu:oc_bob:'+self.other,'reuse',{'action':'prepare','inputs':{'assembly_ref':record['id']}})
        self.assertEqual(Intake(self.store).get(self.other)['package'],'bob-raw')
        with self.assertRaises(GatewayError):self.select({'id':'asm-'+'0'*24})

    def test_switching_any_dependency_does_not_keep_success_archive_label(self):
        for values in ({'m100_so':'new'},{'package':'new'},{'camera_so':'new'},{'m100_mode':'build_if_missing'}):
            with self.subTest(values=values):
                self.select();before=Intake(self.store).get(self.user)
                self.service.update_inputs(self.user,values)
                current=Intake(self.store).get(self.user)
                self.assertNotIn('assembly_ref',current)
                for key,value in values.items():self.assertEqual(current[key],value)
                if 'package' not in values:self.assertEqual(current['package'],before['package'])

    def test_current_yaml_dataset_scope_and_optional_evaluation_remain_editable(self):
        self.select();old=Intake(self.store).get(self.user)
        self.service.update_inputs(self.user,{'dataset':'new-data','scene_scope':'1,2','yaml_request':'explicit change','goal':'replay_and_evaluate'})
        current=Intake(self.store).get(self.user)
        for key in ('assembly_ref','package','camera_so','lidar_so','m100_so'):self.assertEqual(current[key],old[key])
        self.assertEqual(current['goal'],'replay_and_evaluate')

    def material_service(self):
        registry=Mock();registry.list.return_value=[]
        registry.ssh.python.return_value=json.dumps({'schema_version':1,'status':'PASS','checks':[]})
        dataset={'id':'shared-data','kind':'dataset','owner':'shared','origin':'dataset_catalog','name':'shared',
                 'data':{'scene_count':2,'sha256':'7'*64}}
        return SimpleNamespace(registry=registry,datasets=SimpleNamespace(ssh=Mock()),asset=Mock(return_value=dataset))

    def test_material_check_uses_exact_archive_assets_without_download_or_alias(self):
        record=self.record();self.select(record,dataset='shared',scene_scope='全部')
        fake=self.material_service();transport=fake.registry.ssh;draft=Intake(self.store).get(self.user)
        with patch('bench_gateway.service.Service',return_value=fake),patch('bench_gateway.asset_fetch.fetch') as fetch:
            result=MaterialCheck(self.cfg,self.store).run({'id':'prep-new','owner':self.user,'payload':draft})
        self.assertEqual(result['status'],'STATIC_PASSED');self.assertEqual(result['scenes'],[1,2])
        self.assertEqual(result['assets'],assembly_assets(record))
        fetch.assert_not_called();fake.registry.remote_file.assert_not_called();fake.registry.record.assert_not_called()
        request=json.loads(__import__('base64').b64decode(transport.python.call_args.args[1][0]))
        self.assertEqual(request['package']['sha256'],record['data']['artifact']['sha256'])

    def test_tampered_archive_material_reference_stops_before_download(self):
        self.select(dataset='shared',scene_scope='全部');draft=Intake(self.store).get(self.user);draft['camera_so']='other'
        with patch('bench_gateway.service.Service',return_value=self.material_service()),patch('bench_gateway.asset_fetch.fetch') as fetch:
            result=MaterialCheck(self.cfg,self.store).run({'id':'prep-new','owner':self.user,'payload':draft})
        self.assertEqual(result['status'],'NEEDS_INPUT');self.assertEqual(result['issues'][0]['code'],'ASSEMBLY_UNAVAILABLE')
        fetch.assert_not_called()

    def test_archive_failure_preserves_real_replay_result_and_does_not_retry(self):
        executor=object.__new__(Executor);executor.cfg=self.cfg;executor.store=self.store
        executor.request=lambda owner,op,**values:{'operation':op,**values}
        executor.remote=Mock(side_effect=TimeoutError('never expose token'))
        job=copy.deepcopy(self.job);job['payload']['scenes']=[1,2]
        result=executor.save_assembly(job,self.result)
        self.assertEqual(result['state'],'PASS');self.assertEqual(result['task'],self.result['task'])
        self.assertIn('尚未完成归档',result['assembly_archive_warning']);self.assertNotIn('token',str(result))
        executor.remote.assert_called_once();self.assertEqual(list_assemblies(self.cfg,self.store,self.user),[])

    def test_saved_assembly_is_reported_even_when_later_evaluation_is_partial(self):
        self.job=self.update_payload(self.job,lambda payload:payload.update(evaluate_after=True))
        result={**self.result,**self.save()};self.store.complete_replay(self.job,result)
        parent=self.store.job(self.user,self.job['id']);evaluation=self.store.claim('postprocess')
        self.assertIsNotNone(evaluation)
        self.store.finish(evaluation,'PARTIAL_SUCCESS',{'report':'raw.txt','local_report':'raw.txt','warnings':['VISION_GT_MISSING']})
        parent=self.store.job(self.user,self.job['id'])
        self.assertEqual(parent['state'],'PARTIAL_SUCCESS')
        self.assertEqual(parent['result']['assembly']['id'],result['assembly']['id'])
        self.assertIn('本人成功装配存档',format_result(parent,parent['state'],parent['result']))
        self.assertEqual(len(list_assemblies(self.cfg,self.store,self.user)),1)


if __name__=='__main__':unittest.main()
