"""Changing unstarted evaluation never changes inputs or repeats bench replay."""
import concurrent.futures
import copy
import tempfile
import threading
import unittest
from unittest.mock import Mock
from bench_gateway.config import load
from bench_gateway.core import Store,GatewayError
from bench_gateway.execution_controls import set_goal
from bench_gateway.intake import Intake
from bench_gateway.parser import quick
from bench_gateway.service import Service
from bench_gateway.tasks import Tasks
from bench_gateway.task_refs import reference
from bench_gateway.task_cards import snapshot,workflow
from bench_gateway.worker import tick

class ExecutionControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg={'state_root':self.tmp.name,'mode':'live','live_enabled':True,'workstation_evaluation_enabled':True,'auto_execute_on_complete':True}
        self.store=Store(self.tmp.name);self.user='ou_goal_owner';self.other='ou_goal_other';self.session='feishu:oc_goal:'+self.user
        for u in (self.user,self.other):self.store.allow(u)
        self.tasks=Tasks(self.store);self.intake=Intake(self.store)
    def job(self,evaluate=True):
        self.intake.update(self.user,{'package':'A','goal':'replay_and_evaluate' if evaluate else 'replay'},new=True)
        payload={'action':'replay','evaluate_after':evaluate,'scenes':[1],'baseline':{'data':{'evaluator':{'argv':['reviewed-test-never-run']}}},'assets':{}}
        job,_=self.store.submit(self.user,self.session,'run-'+self.tasks.current(self.user)['id'],payload);self.tasks.link(self.user,job)
        return job
    def control(self,enable=False,target=''):
        return set_goal(self.cfg,self.store,self.user,self.session,'replay_and_evaluate' if enable else 'replay',target)
    def complete(self,job):
        self.store.complete_replay(job,{'task':'/data/tasks/'+job['id'],'run_id':job['id'],'state':'PASS'})
        return self.store.job(self.user,job['id'])
    def test_disable_in_queue_freezes_inputs_and_suppresses_later_evaluation(self):
        job=self.job();task=copy.deepcopy(self.tasks.current(self.user))
        self.assertEqual('APPLIED',self.control()['state'])
        claimed=self.store.claim();done=self.complete(claimed)
        self.assertEqual('SUCCEEDED',done['state']);self.assertEqual('SKIPPED',done['result']['evaluation_state'])
        self.assertNotIn('evaluation_job',done['result']);self.assertEqual(1,len(self.store.jobs(self.user)))
        self.assertEqual(job['payload'],done['payload']);self.assertEqual(task['data'],self.tasks.current(self.user)['data'])
        self.assertEqual('skipped',workflow(snapshot(self.store,done))[-1]['status'])
    def test_disable_during_replay_does_not_create_next_draft_or_repeat(self):
        job=self.job();running=self.store.claim();before=self.tasks.current(self.user)
        self.assertEqual('APPLIED',self.control()['state']);self.assertEqual(before,self.tasks.current(self.user))
        done=self.complete(running);self.assertEqual('SUCCEEDED',done['state']);self.assertIsNone(self.store.claim('postprocess'))
    def test_enable_during_replay_uses_validated_frozen_evaluator(self):
        job=self.job(False);running=self.store.claim()
        self.assertEqual('APPLIED',self.control(True)['state'])
        done=self.complete(running);self.assertEqual('POSTPROCESSING',done['state'])
        self.assertEqual(False,done['payload']['evaluate_after']);self.assertEqual('QUEUED',self.store.job(self.user,done['result']['evaluation_job'])['state'])
    def test_enable_without_bound_evaluator_is_rejected_without_side_effects(self):
        job=self.job(False)
        with self.store.connect() as c:
            import json
            p=job['payload'];p['baseline']['data'].pop('evaluator');c.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(p),job['id']))
        self.assertEqual('UNAVAILABLE',self.control(True)['state'])
        with self.store.connect() as c:self.assertEqual(0,c.execute('SELECT COUNT(*) FROM execution_controls').fetchone()[0])
    def test_queued_evaluation_can_be_cancelled_atomically(self):
        self.job();parent=self.complete(self.store.claim());child=parent['result']['evaluation_job']
        self.assertEqual('APPLIED',self.control()['state'])
        self.assertEqual('CANCELLED',self.store.job(self.user,child)['state']);self.assertIsNone(self.store.claim('postprocess'))
        done=self.store.job(self.user,parent['id']);self.assertEqual('SUCCEEDED',done['state']);self.assertEqual('SKIPPED',done['result']['evaluation_state'])
    def test_claimed_evaluation_is_not_falsely_cancelled(self):
        self.job();parent=self.complete(self.store.claim());child=self.store.claim('postprocess');before=self.tasks.current(self.user)
        self.assertEqual('TOO_LATE',self.control()['state'])
        self.assertEqual(before,self.tasks.current(self.user));self.assertEqual('RUNNING',self.store.job(self.user,child['id'])['state'])
        self.assertTrue(snapshot(self.store,self.store.job(self.user,parent['id']))['evaluate_after'])
    def test_handoff_race_cannot_dispatch_cancelled_evaluation(self):
        self.job();running=self.store.claim();gate=threading.Barrier(2)
        def finish():gate.wait();return self.complete(running)
        def cancel():gate.wait();return self.control()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            a=pool.submit(finish);b=pool.submit(cancel);a.result();self.assertEqual('APPLIED',b.result()['state'])
        parent=self.store.job(self.user,running['id']);self.assertEqual('SUCCEEDED',parent['state']);self.assertIsNone(self.store.claim('postprocess'))
    def test_cancel_vs_claim_has_one_honest_outcome(self):
        self.job();parent=self.complete(self.store.claim());gate=threading.Barrier(2)
        def claim():gate.wait();return self.store.claim('postprocess')
        def cancel():gate.wait();return self.control()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            a=pool.submit(claim);b=pool.submit(cancel);claimed=a.result();control=b.result()
        self.assertEqual('TOO_LATE' if claimed else 'APPLIED',control['state'])
        self.assertEqual('RUNNING' if claimed else 'CANCELLED',self.store.job(self.user,parent['result']['evaluation_job'])['state'])
    def test_completed_goal_command_never_replays_or_creates_draft(self):
        self.job(False);done=self.complete(self.store.claim());before=self.tasks.current(self.user)
        self.assertEqual('CLOSED',self.control()['state']);added=self.control(True)
        self.assertIn('已有结果',added['text']);self.assertEqual(before,self.tasks.current(self.user));self.assertEqual(1,len(self.store.jobs(self.user)))
    def test_foreign_reference_cannot_control_task(self):
        job=self.job();ref=reference(self.store,self.user,job_id=job['id'])
        with self.assertRaises(GatewayError):set_goal(self.cfg,self.store,self.other,'feishu:oc_other:'+self.other,'replay',ref)
    def test_ambiguous_active_task_and_next_draft_requires_reference(self):
        job=self.job();self.store.claim();ref=reference(self.store,self.user,job_id=job['id'])
        self.intake.update(self.user,{'yaml_request':'config/a.yaml flag=true'});before=self.tasks.current(self.user)
        self.assertEqual('AMBIGUOUS',self.control()['state']);self.assertEqual(before,self.tasks.current(self.user))
        self.assertEqual('APPLIED',self.control(target=ref)['state']);self.assertEqual(before,self.tasks.current(self.user))
    def test_future_goal_is_material_but_current_goal_is_control(self):
        self.assertEqual('set_goal',quick('本次仅回灌')['action']);self.assertEqual('prepare',quick('下次仅回灌')['action'])
        for text in ('取消评测是什么意思？','本次仅回灌？','能不能只回灌？'):self.assertIsNone(quick(text))
    def test_worker_rechecks_control_at_handoff_instead_of_stale_payload(self):
        job=self.job();owner=self
        class Runner:
            def execute(self,claimed):
                owner.control();return {'state':'PASS','task':'/data/tasks/'+claimed['id']}
        tick(self.cfg,self.store,Runner());done=self.store.job(self.user,job['id'])
        self.assertEqual('SUCCEEDED',done['state']);self.assertNotIn('evaluation_job',done['result'])
    def test_plain_goal_material_intent_controls_current_execution_without_new_draft(self):
        job=self.job();self.store.claim();before=self.tasks.current(self.user);service=Service(self.cfg)
        service.handle(self.session,'这次不要评测',{'action':'prepare','inputs':{'goal':'replay'}})
        self.assertEqual('APPLIED',service.execution_control['state']);self.assertEqual(before,self.tasks.current(self.user))
    def test_mixed_scene_and_goal_changes_are_kept_for_next_request(self):
        self.job();self.store.claim();service=Service(self.cfg)
        service.handle(self.session,'下一轮仅回灌第2场景',{'action':'prepare','inputs':{'goal':'replay'},'scenes':[2]})
        current=self.tasks.current(self.user);self.assertIsNone(current['job_id'])
        self.assertEqual('2',current['data']['scene_scope']);self.assertEqual('replay',current['data']['goal'])
    def test_hold_during_execution_does_not_claim_to_stop_or_create_a_draft(self):
        self.job();self.store.claim();before=self.tasks.current(self.user);service=Service(self.cfg)
        answer=service.handle(self.session,'暂缓执行',quick('暂缓执行'))
        self.assertIn('未中断',answer);self.assertEqual(before,self.tasks.current(self.user))
    def test_goal_action_cannot_discard_material_changes(self):
        self.job();before=self.tasks.current(self.user);service=Service(self.cfg)
        with self.assertRaises(GatewayError):service.handle(self.session,'只回灌并换配置',{'action':'set_goal','inputs':{'goal':'replay','yaml_request':'a.yaml flag=true'}})
        self.assertEqual(before,self.tasks.current(self.user))
    def test_inflight_old_card_ack_does_not_delay_goal_refresh(self):
        from bench_gateway.task_cards import deliver
        job=self.job();self.store.claim();client=Mock();client._json.return_value={'data':{'message_id':'om_control'}}
        cfg={**self.cfg,'task_cards_enabled':True,'notifications_enabled':True}
        deliver(cfg,self.store,client=client)
        self.store.stage(job,'正式回灌中')
        def old_ack(*args,**kwargs):
            self.control();return {'data':{'message_id':'om_control'}}
        client._json.side_effect=old_ack;deliver(cfg,self.store,client=client)
        client._json.side_effect=None
        self.assertEqual(1,deliver(cfg,self.store,client=client))
        import json
        body=json.loads(client._json.call_args.kwargs['body'])
        self.assertIn('仅回放',body['content'])
        self.assertNotIn('回放后评测',body['content'])
    def test_selecting_next_mode_alone_never_restarts_completed_material(self):
        from bench_gateway.autoflow import AutoFlow
        self.job(False);self.complete(self.store.claim());service=Service(self.cfg)
        service.handle(self.session,'下次仅回灌',quick('下次仅回灌'))
        task=self.tasks.current(self.user);self.assertIsNone(task['job_id']);self.assertTrue(service.defer_auto)
        with self.store.connect() as c:self.assertEqual('AWAITING_MATERIAL',c.execute('SELECT state FROM automatic_tasks WHERE task_id=?',(task['id'],)).fetchone()[0])
        AutoFlow(self.cfg,self.store).tick();self.assertEqual(1,len(self.store.jobs(self.user)))
        self.intake.update(self.user,{'scene_scope':'2'})
        AutoFlow(self.cfg,self.store).arm(self.user,self.session)
        with self.store.connect() as c:self.assertEqual('WAITING',c.execute('SELECT state FROM automatic_tasks WHERE task_id=?',(task['id'],)).fetchone()[0])
    def test_initial_goal_choice_waits_for_material_instead_of_starting(self):
        service=Service(self.cfg);service.handle(self.session,'仅回灌',quick('仅回灌'))
        self.assertTrue(service.defer_auto);self.assertEqual('replay',self.intake.get(self.user)['goal'])
        self.assertEqual([],self.store.jobs(self.user))
    def test_selected_next_goal_survives_arrival_of_new_release(self):
        self.job(False);self.complete(self.store.claim());service=Service(self.cfg)
        service.handle(self.session,'下次仅回灌',quick('下次仅回灌'))
        self.intake.update(self.user,{'package':'https://example.test/new.app.tar.gz'})
        self.assertEqual('replay',self.intake.get(self.user)['goal'])
