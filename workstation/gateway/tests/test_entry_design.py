"""Business defaults, private version edits and public task references."""
import copy
import html
import json
import tempfile
import unittest
from unittest.mock import patch
from bench_gateway.core import Store,GatewayError
from bench_gateway.intake import Intake
from bench_gateway.tasks import Tasks
from bench_gateway.task_refs import reference
from bench_gateway.parser import quick
from bench_gateway.yaml_plan import plan
from bench_gateway.task_cards import snapshot,render,workflow

class EntryDesignTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.user='ou_entry_owner';self.other='ou_entry_other'
        for user in (self.user,self.other):self.store.allow(user)
        self.intake=Intake(self.store);self.tasks=Tasks(self.store)
    def freeze(self):
        self.intake.update(self.user,{'package':'https://example.test/A.app.tar.gz','camera_so':'camera','lidar_so':'lidar','m100_so':'M100','goal':'replay','scene_scope':'1,2','yaml_request':'config/a.yaml flag=true'})
        task=self.tasks.current(self.user)
        payload={'action':'replay','assets':{'package':{'id':'a-exact','owner':self.user,'data':{'sha256':'a'*64}},'dependencies':[]},'scenes':[1,2],'evaluate_after':False}
        job,_=self.store.submit(self.user,'feishu:oc_test:'+self.user,'once',payload);self.tasks.link(self.user,job)
        self.store.finish(job,'SUCCEEDED',{'task':'/data/tasks/'+job['id']})
        return self.tasks.current(self.user),self.store.job(self.user,job['id'])
    def test_new_draft_defaults_to_evaluation_and_explicit_replay_survives_supplements(self):
        self.intake.update(self.user,{'package':'A'})
        self.assertEqual('replay_and_evaluate',self.intake.get(self.user)['goal'])
        self.intake.update(self.user,{'goal':'replay'})
        self.intake.update(self.user,{'camera_so':'C'})
        self.assertEqual('replay',self.intake.get(self.user)['goal'])
    def test_next_request_resets_goal_and_retains_exact_materials_and_frozen_history(self):
        old,job=self.freeze();ref=reference(self.store,self.user,task_id=old['id'])
        self.intake.update(self.user,{'yaml_request':'config/a.yaml flag=false'})
        new=self.tasks.current(self.user)
        self.assertEqual('replay_and_evaluate',new['data']['goal'])
        self.assertEqual('a-exact',new['data']['package']);self.assertEqual('camera',new['data']['camera_so'])
        self.assertEqual('1,2',new['data']['scene_scope']);self.assertIn('flag=false',new['data']['yaml_request'])
        self.assertEqual(old,self.tasks.get(self.user,old['id']));self.assertEqual(job,self.store.job(self.user,job['id']))
        self.assertNotEqual(ref,reference(self.store,self.user,task_id=new['id']))
    def test_explicit_goal_in_new_request_overrides_default(self):
        self.freeze();self.intake.update(self.user,{'goal':'replay','scene_scope':'2'})
        self.assertEqual('replay',self.intake.get(self.user)['goal'])
    def test_new_release_drops_old_package_specific_inputs_and_replay_only_goal(self):
        self.freeze();self.intake.update(self.user,{'package':'https://example.test/B.app.tar.gz'})
        draft=self.intake.get(self.user)
        self.assertEqual('replay_and_evaluate',draft['goal']);self.assertEqual('camera',draft['camera_so'])
        self.assertNotIn('m100_so',draft);self.assertNotIn('yaml_request',draft)
    def test_public_number_is_stable_owner_scoped_and_switchable(self):
        self.intake.update(self.user,{'package':'A'});one=self.tasks.current(self.user)
        ref=reference(self.store,self.user,task_id=one['id']);self.assertRegex(ref,r'^TJ-\d{8}-\d{4}$')
        self.intake.update(self.user,{'camera_so':'C'})
        self.assertEqual(ref,reference(self.store,self.user,task_id=one['id']))
        self.intake.update(self.user,{'package':'B'},new=True)
        self.tasks.command(self.user,'切换任务 '+ref)
        self.assertEqual(one['id'],self.tasks.current(self.user)['id'])
        self.assertIn(ref,self.tasks.text(self.user))
        with self.assertRaises(GatewayError):self.tasks.get(self.other,ref)
        with self.assertRaises(GatewayError):self.tasks.command(self.other,'切换任务 '+ref)
    def test_fixed_card_title_body_parameters_and_no_internal_id_or_normal_dependency_line(self):
        task,job=self.freeze()
        job['payload']['business_contract']={'input_summary':{'software':'2737','software_url':'https://example.test/A.app.tar.gz','scene_scope':'1,2','dependencies':'沿用本人相机、雷达、M100'}}
        job['state']='RUNNING';job['result']={}
        value=snapshot(self.store,job);card=render(value);raw=json.dumps(card,ensure_ascii=False)
        self.assertEqual('台架评测',card['header']['title']['content']);self.assertNotIn('subtitle',card['header'])
        self.assertIn('LIVC APP',raw);self.assertIn('https://example.test/A.app.tar.gz',raw)
        self.assertIn('TJ-',raw);self.assertNotIn(job['id'],raw)
        self.assertNotIn('沿用本人相机',raw);self.assertEqual('已跳过',workflow(value)[3]['hint'])
    def test_exact_natural_yaml_switch_has_no_model_calls_and_last_value_wins(self):
        text='把 common_param.yaml 的 log.concise 打开'
        with patch('bench_gateway.yaml_plan.subprocess.run') as model:
            self.assertEqual(text,quick(text)['inputs']['yaml_request'])
            result=plan({},text+'；后续修改（同一字段以此为准）：把 common_param.yaml 的 log.concise 关闭')
        model.assert_not_called();self.assertEqual(1,len(result['operations']))
        self.assertEqual('false',result['operations'][0]['value_json'])
    def test_full_file_replacement_supersedes_earlier_field_edits(self):
        text='config/a.yaml flag=true；用工作站文件 /data/new.yaml 替换 APP 内 config/a.yaml'
        with patch('bench_gateway.yaml_plan.subprocess.run') as model:result=plan({},text)
        model.assert_not_called();self.assertEqual(['replace'],[op['operation'] for op in result['operations']])
    def test_m100_help_is_read_only_and_explicit_about_new_version_requirements(self):
        result=quick('M100编译说明')
        self.assertEqual('help',result['action']);self.assertIn('提交号',result['reply']);self.assertIn('锁定文件',result['reply'])
        self.assertEqual([],self.store.jobs(self.user));self.assertIsNone(self.tasks.current(self.user))
    def test_release_version_is_compact_and_original_link_is_preserved(self):
        _,job=self.freeze()
        original='https://example.test/CCU_LIVC_AVM-release-SW0058_ULTRA_ID0111.20260918-2737.app.tar.gz'
        job['payload']['business_contract']={'input_summary':{
            'software':'VM-release-SW0058_ULTRA_ID0111.20260918-2737.app.tar.gz-prepared',
            'software_url':original}}
        value=snapshot(self.store,job)
        raw=json.dumps(render(value),ensure_ascii=False)
        self.assertIn('SW0058 · 构建 2737',raw);self.assertNotIn('tar.gz-prepared',raw)
        self.assertIn(original,raw)
        self.assertIn('['+original+']',html.unescape(raw))
    def test_partial_evaluation_names_missing_truth_without_claiming_full_completion(self):
        _,job=self.freeze();job['state']='PARTIAL_SUCCESS';job['payload']['evaluate_after']=True
        job['result'].update(report='report.txt',evaluation_state='PARTIAL',warnings=['VISION_GT_MISSING'])
        value=snapshot(self.store,job)
        self.assertEqual('评测部分完成',value['label']);self.assertIn('视觉真值',value['action'])
        self.assertEqual('blocked',workflow(value)[3]['status']);self.assertEqual('orange',value['tone'])
