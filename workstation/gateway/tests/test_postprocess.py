import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_gateway as fixtures
from bench_gateway.core import EvaluationOutcomeUnknown,GatewayError,Store,digest,format_result
from bench_gateway.executor import Executor
from bench_gateway.queue_status import QueueStatus
from bench_gateway.worker import tick,postprocess_tick,deliver,run


class FakeExecutor:
    def __init__(self):self.calls=[]
    def execute(self,job):
        self.calls.append(job['id'])
        if job['payload']['action']=='evaluate':
            return {'task':job['payload']['source_task'],'report':'/reports/original.txt','local_report':'/reports/original.txt'}
        return {'task':'/tasks/'+job['id'],'state':'PASS','run_id':job['id'],'restored':True}


class PostprocessTests(unittest.TestCase):
    def setUp(self):
        fixtures.GatewayTests.setUp(self)
        # These tests exercise a configured postprocessing capability. No real
        # evaluator runs: FakeExecutor or a patched subprocess handles dispatch.
        original=self.s.baseline(fixtures.A,'verified')
        self.s.add_baseline(fixtures.A,'with-evaluator',{
            **original['data'],'evaluator':{'argv':['test-reviewed-evaluator-must-not-run']}})
        self.intent['baseline']='with-evaluator'
    tearDown=fixtures.GatewayTests.tearDown
    enqueue=fixtures.GatewayTests.enqueue

    def deferred(self):
        self.intent['evaluate_after']=True
        job=self.enqueue();tick(self.cfg,self.s,FakeExecutor())
        return self.s.job(fixtures.A,job['id'])

    def test_real_directory_is_durable_before_evaluation_dispatch(self):
        parent=self.deferred();reopened=Store(self.s.root)
        self.assertEqual(parent['state'],'POSTPROCESSING')
        self.assertEqual(parent['result']['task'],'/tasks/'+parent['id'])
        child=reopened.job(fixtures.A,parent['result']['evaluation_job'])
        self.assertEqual(child['state'],'QUEUED');self.assertEqual(child['lane'],'postprocess')
        self.assertEqual(child['payload']['source_job'],parent['id'])
        with reopened.connect() as c:notice=c.execute('SELECT text FROM outbox WHERE job=?',(parent['id'],)).fetchone()[0]
        self.assertIn(parent['result']['task'],notice);self.assertIn('评测等待',notice)
        self.assertNotIn('回灌及评测完成',notice)

    def test_slow_evaluation_does_not_hold_hardware_fifo(self):
        parent=self.deferred();second=self.enqueue('next replay')
        started=threading.Event();release=threading.Event();errors=[]
        class Slow(FakeExecutor):
            def execute(self,job):
                started.set();release.wait(5)
                return super().execute(job)
        def evaluate():
            try:postprocess_tick(self.cfg,self.s,Slow())
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=evaluate);thread.start()
        try:
            self.assertTrue(started.wait(3))
            self.assertEqual(self.s.job(fixtures.A,parent['id'])['result']['evaluation_state'],'RUNNING')
            hardware=FakeExecutor();self.assertEqual(tick(self.cfg,self.s,hardware),second['id'])
            self.assertEqual(hardware.calls,[second['id']]);self.assertTrue(thread.is_alive())
        finally:release.set();thread.join(5)
        self.assertEqual(errors,[])
        self.assertEqual(self.s.job(fixtures.A,parent['id'])['state'],'SUCCEEDED')

    def test_handoff_transaction_rolls_back_both_parent_and_child(self):
        self.intent['evaluate_after']=True;job=self.enqueue();claimed=self.s.claim()
        with self.s.connect() as c:
            c.execute("CREATE TRIGGER fail_handoff BEFORE UPDATE ON jobs WHEN NEW.state='POSTPROCESSING' BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.s.complete_replay(claimed,{'task':'/real/result'})
        self.assertEqual(self.s.job(fixtures.A,job['id'])['state'],'RUNNING')
        self.assertEqual(len(self.s.jobs(fixtures.A)),1)
        with self.s.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_handoff_failure_preserves_replay_receipt_for_review(self):
        self.intent['evaluate_after']=True;job=self.enqueue()
        with patch.object(self.s,'complete_replay',side_effect=RuntimeError('disk failure')):
            tick(self.cfg,self.s,FakeExecutor())
        saved=self.s.job(fixtures.A,job['id'])
        self.assertEqual(saved['state'],'NEEDS_REVIEW');self.assertEqual(saved['result']['task'],'/tasks/'+job['id'])

    def test_completion_updates_parent_notification_with_original_txt(self):
        parent=self.deferred();self.cfg['notifications_enabled']=True
        sent=[];deliver(self.cfg,self.s,lambda row:sent.append(dict(row)) or True)
        postprocess_tick(self.cfg,self.s,FakeExecutor())
        deliver(self.cfg,self.s,lambda row:sent.append(dict(row)) or True)
        results=[r for r in sent if r['_outbox_table']=='outbox']
        self.assertEqual([r['job'] for r in results],[parent['id'],parent['id']])
        self.assertIsNone(results[0]['attachment']);self.assertEqual(results[1]['attachment'],'/reports/original.txt')
        self.assertIn('回灌及评测完成',results[1]['text'])
        queued=[r for r in sent if r['_outbox_table']=='queue_outbox']
        self.assertEqual([r['job'] for r in queued],[parent['id']+'-eval'])
        self.assertIn('等待评测第',queued[0]['text'])
        self.assertEqual(self.s.job(fixtures.A,parent['id'])['result']['evaluation_state'],'SUCCEEDED')

    def test_evaluation_timeout_never_blocks_next_hardware_or_other_evaluation(self):
        parent=self.deferred()
        class TimedOut:
            def execute(self,job):raise subprocess.TimeoutExpired(['private-adapter'],1)
        postprocess_tick(self.cfg,self.s,TimedOut())
        saved=self.s.job(fixtures.A,parent['id'])
        self.assertEqual(saved['state'],'PARTIAL_SUCCESS');self.assertEqual(saved['result']['evaluation_state'],'NEEDS_REVIEW')
        self.assertEqual(saved['result']['task'],parent['result']['task'])
        with self.assertRaises(GatewayError):self.service.handle(fixtures.SESSION,'重新评测',{'action':'evaluate','task_id':parent['id']})
        next_job=self.enqueue('next');self.assertEqual(tick(self.cfg,self.s,FakeExecutor()),next_job['id'])
        self.assertEqual(postprocess_tick(self.cfg,self.s,FakeExecutor()),next_job['id']+'-eval')

    def test_restart_does_not_rerun_evaluation_or_replay(self):
        parent=self.deferred();child=self.s.claim('postprocess')
        self.assertIsNotNone(child)
        with patch('bench_gateway.worker.Executor.execute') as execute:run(self.cfg,self.s,once=True)
        execute.assert_not_called()
        saved=self.s.job(fixtures.A,parent['id'])
        self.assertEqual(saved['state'],'PARTIAL_SUCCESS');self.assertEqual(saved['result']['evaluation_state'],'NEEDS_REVIEW')
        self.assertEqual(self.s.job(fixtures.A,child['id'])['state'],'NEEDS_REVIEW')
        self.assertIsNone(QueueStatus(self.s).head())
        self.assertNotIn('已暂停自动派发',format_result(child,'NEEDS_REVIEW',{}))

    def test_hardware_unknown_outcome_does_not_block_existing_evaluation(self):
        parent=self.deferred();other=self.enqueue('unknown hardware');claimed=self.s.claim()
        self.s.finish(claimed,'NEEDS_REVIEW',{'error':'lost SSH'})
        self.assertIsNone(QueueStatus(self.s).head())
        self.assertEqual(postprocess_tick(self.cfg,self.s,FakeExecutor()),parent['id']+'-eval')
        self.assertEqual(self.s.job(fixtures.A,other['id'])['state'],'NEEDS_REVIEW')

    def test_manual_evaluation_has_separate_lane_and_active_source_dedup(self):
        job=self.enqueue();tick(self.cfg,self.s,FakeExecutor())
        for text in ('评测上次结果','再评测一下'):
            reply=self.service.handle(fixtures.SESSION,text,{'action':'evaluate','task_id':job['id']})
            self.assertIn('评测队列',reply)
        children=[j for j in self.s.jobs(fixtures.A) if j['payload']['action']=='evaluate']
        self.assertEqual(len(children),1);self.assertEqual(children[0]['lane'],'postprocess')
        self.assertIsNone(self.s.claim())
        self.assertEqual(postprocess_tick(self.cfg,self.s,FakeExecutor()),children[0]['id'])

    def test_postprocess_queries_and_cancel_keep_owner_isolation(self):
        parent=self.deferred();child_id=parent['result']['evaluation_job']
        with self.assertRaises(GatewayError):self.s.job(fixtures.B,child_id)
        with self.assertRaises(GatewayError):self.s.cancel(fixtures.B,child_id)
        self.assertNotIn(child_id,QueueStatus(self.s).text(fixtures.B))
        self.assertNotIn(fixtures.A,QueueStatus(self.s).text(fixtures.B))
        own=QueueStatus(self.s).text(fixtures.A,child_id)
        self.assertIn('等待评测第 1 位',own);self.assertIn('0 个就绪等待',own)
        self.s.cancel(fixtures.A,child_id)
        self.assertEqual(self.s.job(fixtures.A,parent['id'])['state'],'PARTIAL_SUCCESS')
        self.assertIsNone(postprocess_tick(self.cfg,self.s,FakeExecutor()))

    def test_existing_evaluation_jobs_migrate_without_changing_request_identity(self):
        job=self.s.submit(fixtures.A,fixtures.SESSION,'legacy-eval',{'action':'evaluate','source_job':'source'})[0]
        QueueStatus(self.s).notice(job,'legacy hardware position')
        with self.s.connect() as c:c.execute("UPDATE jobs SET lane='hardware' WHERE id=?",(job['id'],))
        reopened=Store(self.s.root);saved=reopened.job(fixtures.A,job['id'])
        self.assertEqual(saved['lane'],'postprocess');self.assertEqual(saved['request_key'],'legacy-eval')
        with reopened.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM queue_outbox WHERE job=?',(job['id'],)).fetchone()[0],0)
        self.assertIsNone(reopened.claim());self.assertEqual(reopened.claim('postprocess')['id'],job['id'])

    def test_cancelling_parent_cancels_only_pending_evaluation(self):
        parent=self.deferred()
        reply=self.service.handle(fixtures.SESSION,'取消任务',{'action':'cancel','task_id':parent['id']})
        self.assertIn('回灌已完成',reply);self.assertIn(parent['result']['task'],reply)
        self.assertEqual(self.s.job(fixtures.A,parent['result']['evaluation_job'])['state'],'CANCELLED')
        self.assertEqual(self.s.job(fixtures.A,parent['id'])['result']['task'],parent['result']['task'])

    def test_recovered_delivery_hands_off_requested_evaluation_without_replay(self):
        from bench_gateway.cli import main
        self.intent['evaluate_after']=True;job=self.enqueue();claimed=self.s.claim()
        self.s.finish(claimed,'NEEDS_REVIEW',{'error':'lost after restoration'})
        cfg={**self.cfg,'live_enabled':True}
        receipt={'state':'PASS','run_id':job['id'],'task':'/tasks/recovered'}
        argv=['gateway','recover-delivery','--user',fixtures.A,'--job',job['id']]
        with patch('sys.argv',argv),patch('bench_gateway.cli.load',return_value=cfg),patch('bench_gateway.cli.Executor') as executor,patch('builtins.print'):
            executor.return_value.remote.return_value=receipt
            main()
            self.assertEqual(executor.return_value.request.call_args.args[1],'recover_delivery')
            executor.return_value.execute.assert_not_called()
        parent=self.s.job(fixtures.A,job['id'])
        self.assertEqual(parent['state'],'POSTPROCESSING');self.assertEqual(parent['result']['task'],'/tasks/recovered')
        self.assertEqual(self.s.job(fixtures.A,parent['result']['evaluation_job'])['payload']['source_task'],'/tasks/recovered')
        with self.assertRaises(GatewayError):self.s.complete_replay(parent,receipt,recovered=True)
        self.assertEqual(len(self.s.jobs(fixtures.A)),2)

    def test_recovered_handoff_rejects_wrong_receipt_and_existing_child(self):
        self.intent['evaluate_after']=True;job=self.enqueue();claimed=self.s.claim()
        self.s.finish(claimed,'NEEDS_REVIEW',{'error':'lost'})
        with self.assertRaises(GatewayError):self.s.complete_replay(claimed,{'state':'PASS','run_id':'wrong','task':'/tasks/wrong'},recovered=True)
        self.assertEqual(len(self.s.jobs(fixtures.A)),1)
        self.s.complete_replay(claimed,{'state':'PASS','run_id':job['id'],'task':'/tasks/real'},recovered=True)
        with self.s.connect() as c:c.execute("UPDATE jobs SET state='NEEDS_REVIEW' WHERE id=?",(job['id'],))
        with self.assertRaises(GatewayError):self.s.complete_replay(claimed,{'state':'PASS','run_id':job['id'],'task':'/tasks/replace'},recovered=True)
        self.assertEqual(self.s.job(fixtures.A,job['id']+'-eval')['payload']['source_task'],'/tasks/real')

    def test_invoked_adapter_without_matching_receipt_is_unknown_not_retryable_failure(self):
        parent=self.deferred();job=self.s.job(fixtures.A,parent['result']['evaluation_job'])
        job['payload']['baseline']['data']['evaluator']={'argv':['reviewed-evaluator']}
        report=self.s.user_root(fixtures.A)/'reports/result.txt';report.write_text('report')
        valid={'state':'PASS','job_id':job['id'],'source_job':parent['id'],'local_report':str(report),'report_sha256':'wronghash'}
        outputs=[SimpleNamespace(returncode=1,stdout=''),SimpleNamespace(returncode=0,stdout='not json'),
                 SimpleNamespace(returncode=0,stdout=json.dumps({**valid,'source_job':'other'})),
                 SimpleNamespace(returncode=0,stdout=json.dumps(valid))]
        executor=Executor(self.cfg,self.s)
        for output in outputs:
            with self.subTest(output=output),patch('bench_gateway.executor.subprocess.run',return_value=output):
                with self.assertRaises(EvaluationOutcomeUnknown):executor.evaluate(job)

    def test_unknown_evaluation_receipt_quarantines_only_this_source(self):
        parent=self.deferred()
        class Unknown:
            def execute(self,job):raise EvaluationOutcomeUnknown('receipt identity mismatch')
        postprocess_tick(self.cfg,self.s,Unknown())
        self.assertEqual(self.s.job(fixtures.A,parent['id'])['result']['evaluation_state'],'NEEDS_REVIEW')
        with self.assertRaises(GatewayError):self.service.handle(fixtures.SESSION,'重新评测',{'action':'evaluate','task_id':parent['id']})
        next_job=self.enqueue('next');self.assertEqual(self.s.claim()['id'],next_job['id'])

    def test_missing_evaluator_is_known_pre_dispatch_failure(self):
        parent=self.deferred();job=self.s.job(fixtures.A,parent['result']['evaluation_job'])
        # Model an old persisted task whose evaluator configuration is absent.
        job['payload']['baseline']['data'].pop('evaluator',None)
        with patch('bench_gateway.executor.subprocess.run') as invoke:
            with self.assertRaises(GatewayError) as error:Executor(self.cfg,self.s).evaluate(job)
            self.assertNotIsInstance(error.exception,EvaluationOutcomeUnknown);invoke.assert_not_called()

    def test_unconfigured_evaluation_is_rejected_before_replay_is_queued(self):
        intent={**self.intent,'baseline':'verified','evaluate_after':True}
        with patch('bench_gateway.executor.subprocess.run') as invoke:
            with self.assertRaisesRegex(GatewayError,'评测入口'):
                self.service.handle(fixtures.SESSION,'回灌后评测',intent)
            invoke.assert_not_called()
        self.assertEqual(self.s.jobs(fixtures.A),[])

    def reviewable_evaluation(self):
        parent=self.deferred();child=self.s.claim('postprocess')
        child['payload']['baseline']['data']['evaluator']={'argv':['must-not-run'],'validity':{'required_tasks':['vision']}}
        with self.s.connect() as c:c.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(child['payload']),child['id']))
        self.s.finish(child,'NEEDS_REVIEW',{'error':'interrupted after adapter submitted'})
        vendor=self.root/'vendor';vendor.mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1]/'vendor/parse_eval_report.py',vendor/'parse_eval_report.py')
        report=self.s.user_root(fixtures.A)/'reports/existing.txt'
        report.write_text('候选预测帧: 10\n至少完成一项任务: 10\n未匹配真值时间戳: 0\nOverall mAP: 0.85\n',encoding='utf-8')
        receipt={'state':'PASS','job_id':child['id'],'source_job':parent['id'],'local_report':str(report),'report_sha256':digest(report)}
        path=self.root/'receipt.json';path.write_text(json.dumps(receipt),encoding='utf-8')
        return parent,child,path,receipt

    def test_recover_evaluation_validates_existing_txt_without_invoking_adapter(self):
        from bench_gateway.cli import main
        parent,child,path,receipt=self.reviewable_evaluation()
        argv=['gateway','recover-evaluation','--user',fixtures.A,'--task',child['id'],'--receipt',str(path)]
        with patch('sys.argv',argv),patch('bench_gateway.cli.load',return_value=self.cfg),patch('bench_gateway.executor.subprocess.run') as invoke,patch('builtins.print'):
            main();invoke.assert_not_called()
        self.assertEqual(self.s.job(fixtures.A,child['id'])['state'],'SUCCEEDED')
        result=self.s.job(fixtures.A,parent['id'])
        self.assertEqual(result['state'],'SUCCEEDED');self.assertEqual(result['result']['task'],parent['result']['task'])
        self.assertEqual(result['result']['local_report'],receipt['local_report'])

    def test_recover_evaluation_invalid_receipt_preserves_quarantine(self):
        from bench_gateway.cli import main
        parent,child,path,receipt=self.reviewable_evaluation()
        argv=['gateway','recover-evaluation','--user',fixtures.A,'--task',child['id'],'--receipt',str(path)]
        for change in ({'source_job':'other-job'},{'report_sha256':'tampered'}):
            path.write_text(json.dumps({**receipt,**change}),encoding='utf-8')
            with patch('sys.argv',argv),patch('bench_gateway.cli.load',return_value=self.cfg),patch('bench_gateway.executor.subprocess.run') as invoke,patch('builtins.print'):
                with self.assertRaises(SystemExit) as error:main()
                self.assertEqual(error.exception.code,2);invoke.assert_not_called()
            self.assertEqual(self.s.job(fixtures.A,child['id'])['state'],'NEEDS_REVIEW')
            self.assertEqual(self.s.job(fixtures.A,parent['id'])['state'],'PARTIAL_SUCCESS')

    def test_recover_evaluation_rejects_foreign_owner(self):
        from bench_gateway.cli import main
        parent,child,path,receipt=self.reviewable_evaluation()
        argv=['gateway','recover-evaluation','--user',fixtures.B,'--task',child['id'],'--receipt',str(path)]
        with patch('sys.argv',argv),patch('bench_gateway.cli.load',return_value=self.cfg),patch('bench_gateway.executor.subprocess.run') as invoke,patch('builtins.print'):
            with self.assertRaises(SystemExit) as error:main()
            self.assertEqual(error.exception.code,2);invoke.assert_not_called()
        self.assertEqual(self.s.job(fixtures.A,child['id'])['state'],'NEEDS_REVIEW')

    def test_repeat_evaluate_request_during_automatic_evaluation_is_not_duplicated(self):
        parent=self.deferred()
        reply=self.service.handle(fixtures.SESSION,'评测',{'action':'evaluate','task_id':parent['id']})
        self.assertIn('已安排评测',reply);self.assertEqual(len(self.s.jobs(fixtures.A)),2)

    def test_simulated_replay_does_not_create_a_real_evaluation_handoff(self):
        self.intent['evaluate_after']=True;job=self.enqueue()
        tick(self.cfg,self.s)
        self.assertEqual(self.s.job(fixtures.A,job['id'])['state'],'SIMULATED')
        self.assertEqual(len(self.s.jobs(fixtures.A)),1)


if __name__=='__main__':unittest.main()
