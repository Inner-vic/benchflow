import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.business import capability_issues
from bench_gateway.core import GatewayError,format_result
from bench_gateway.executor import Executor
from bench_gateway.service import Service
from bench_gateway.worker import postprocess_tick
import test_bindings as binding_fixtures
import test_gateway as fixtures


ADAPTER={'kind':'workstation-offline-v1','schema_version':1}


class BuiltinEvaluationFlowTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.GatewayTests();self.case.setUp();self.addCleanup(self.case.tearDown)
        c=self.case
        c.cfg['workstation_evaluation_enabled']=True
        self.baseline=c.s.add_baseline(fixtures.A,'retained-for-evaluation',{
            'kind':'retained-result-v1','gate_verified':False,'asset_ids':{},'evaluator':ADAPTER})

    def source(self,state='SUCCEEDED'):
        c=self.case
        job,_=c.s.submit(fixtures.A,fixtures.SESSION,'historical-source',{
            'action':'replay','baseline':self.baseline,'source_run':'original-real-producer','retained_result':True})
        # No finish()/outbox: an operator registers an existing source without a
        # completion notification pretending that a new replay was performed.
        with c.s.connect() as connection:
            connection.execute('UPDATE jobs SET state=?,result=? WHERE id=?',
                (state,'{"state":"PASS","run_id":"original-real-producer","task":"/mnt/data/bench-eval/tasks/original-real-producer"}',job['id']))
        return c.s.job(fixtures.A,job['id'])

    def test_workstation_adapter_satisfies_evaluation_without_satisfying_lpai(self):
        self.assertEqual(capability_issues({'goal':'replay_and_evaluate'},ADAPTER),[])
        issues=capability_issues({'goal':'replay_and_evaluate','lpai_target':'team'},ADAPTER)
        self.assertEqual([i['code'] for i in issues],['LPAI_NOT_READY'])

    def test_owned_retained_result_can_enter_real_evaluation_while_hardware_is_simulated(self):
        c=self.case;source=self.source()
        with patch('bench_gateway.workstation_evaluation.validate_descriptor'):
            reply=c.service.handle(fixtures.SESSION,'评测已有结果',{'action':'evaluate','task_id':source['id']})
        self.assertIn('工作站真实离线评测',reply);self.assertNotIn('【模拟模式',reply)
        child=next(j for j in c.s.jobs(fixtures.A) if j['payload']['action']=='evaluate')
        self.assertEqual(child['lane'],'postprocess')
        self.assertEqual(child['payload']['source_assets'],{})
        self.assertEqual(child['payload']['source_run'],'original-real-producer')

    def test_simulated_source_cannot_enter_real_workstation_evaluation(self):
        c=self.case;source=self.source('SIMULATED')
        with patch('bench_gateway.workstation_evaluation.validate_descriptor'),self.assertRaisesRegex(GatewayError,'模拟任务'):
            c.service.handle(fixtures.SESSION,'评测模拟结果',{'action':'evaluate','task_id':source['id']})
        self.assertEqual(len(c.s.jobs(fixtures.A)),1)

    def test_disabled_workstation_evaluation_does_not_admit_the_request(self):
        c=self.case;source=self.source();c.cfg['workstation_evaluation_enabled']=False
        with patch('bench_gateway.workstation_evaluation.validate_descriptor'),self.assertRaisesRegex(GatewayError,'开关'):
            c.service.handle(fixtures.SESSION,'评测已有结果',{'action':'evaluate','task_id':source['id']})
        self.assertEqual(len(c.s.jobs(fixtures.A)),1)

    def test_requested_evaluation_subset_cannot_be_silently_ignored(self):
        c=self.case;source=self.source()
        with self.assertRaisesRegex(GatewayError,'全部场景'):
            c.service.handle(fixtures.SESSION,'只评测场景1',{'action':'evaluate','task_id':source['id'],'scenes':[1]})
        self.assertEqual(len(c.s.jobs(fixtures.A)),1)

    def test_executor_dispatches_builtin_evaluation_independent_of_hardware_gate(self):
        c=self.case
        job={'payload':{'action':'evaluate','baseline':self.baseline}}
        expected={'evaluation_state':'PARTIAL','local_report':'original.txt'}
        with patch.object(Executor,'evaluate',return_value=expected) as run:
            self.assertEqual(Executor(c.cfg,c.s).execute(job),expected)
        run.assert_called_once_with(job)
        c.cfg['workstation_evaluation_enabled']=False
        with self.assertRaisesRegex(GatewayError,'开关'):Executor(c.cfg,c.s).execute(job)

    def test_hardware_replay_remains_simulated(self):
        c=self.case
        result=Executor(c.cfg,c.s).execute({'id':'not-dispatched','payload':{'action':'replay'}})
        self.assertTrue(result['simulated'])

    def test_evaluation_environment_query_uses_real_runtime_probe_without_a_job(self):
        from bench_gateway.parser import quick
        c=self.case;c.cfg['default_evaluator']=ADAPTER
        with patch('bench_gateway.workstation_evaluation.validate_descriptor'),patch('bench_gateway.workstation_evaluation.probe_runtime',return_value={'state':'PASS'}) as probe:
            reply=c.service.handle(fixtures.SESSION,'评测环境',quick('评测环境'))
        self.assertIn('本次真实 SSH 核验通过',reply);self.assertIn('无需 LPAI 登录',reply)
        probe.assert_called_once();self.assertEqual(c.s.jobs(fixtures.A),[])

    def test_registered_result_is_not_a_replay_adapter_or_a_new_replay(self):
        c=self.case;source=self.source()
        with self.assertRaisesRegex(GatewayError,'不能作为'):
            c.service.handle(fixtures.SESSION,'用历史结果回灌',{'action':'replay','baseline':self.baseline['name']})
        text=format_result(source,'SUCCEEDED',source['result'])
        self.assertIn('已有回灌结果已登记',text);self.assertIn('未重新刷包或回灌',text)
        self.assertNotIn(self.baseline['name'],str(c.service.context(fixtures.A)['baselines']))

    def test_automatic_handoff_freezes_source_assets_and_partial_txt_is_delivered(self):
        c=self.case;assets={'dataset':{'owner':'shared','data':{'manifest':'/frozen/manifest.json'}}}
        job,_=c.s.submit(fixtures.A,fixtures.SESSION,'real-replay',{
            'action':'replay','baseline':self.baseline,'assets':assets,'evaluate_after':True})
        running=c.s.claim()
        c.s.complete_replay(running,{'state':'PASS','run_id':job['id'],'task':'/tasks/'+job['id']})
        parent=c.s.job(fixtures.A,job['id']);child=c.s.job(fixtures.A,parent['result']['evaluation_job'])
        self.assertEqual(child['payload']['source_assets'],assets)
        self.assertEqual(child['payload']['source_run'],job['id'])
        output={'task':'/tasks/'+job['id'],'report':'/reports/original.txt','local_report':'/reports/original.txt',
                'evaluation_state':'PARTIAL','warnings':['VISION_GT_MISSING']}
        postprocess_tick(c.cfg,c.s,SimpleNamespace(execute=lambda _:output))
        parent=c.s.job(fixtures.A,job['id']);child=c.s.job(fixtures.A,child['id'])
        self.assertEqual(parent['state'],'PARTIAL_SUCCESS');self.assertEqual(child['state'],'PARTIAL_SUCCESS')
        self.assertEqual(parent['result']['report'],output['report'])
        text=format_result(parent,parent['state'],parent['result'])
        self.assertIn('部分结果',text);self.assertIn('VISION_GT_MISSING',text)
        with c.s.connect() as connection:
            notice=connection.execute('SELECT attachment FROM outbox WHERE job=?',(parent['id'],)).fetchone()
        self.assertEqual(notice['attachment'],output['local_report'])

    def test_candidate_binding_freezes_default_workstation_evaluator(self):
        c=binding_fixtures.BindingTests();c.setUp();self.addCleanup(c.tearDown)
        c.cfg['default_evaluator']=copy.deepcopy(ADAPTER)
        with patch('bench_gateway.workstation_evaluation.validate_descriptor'):
            baseline=c.bind()
        self.assertEqual(baseline['data']['evaluator'],ADAPTER)
        c.cfg['default_evaluator']['kind']='changed'
        self.assertEqual(baseline['data']['evaluator']['kind'],'workstation-offline-v1')
