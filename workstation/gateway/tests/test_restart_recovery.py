import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from bench_gateway.core import Store,GatewayError
from bench_gateway.restart_recovery import completed
from bench_gateway.reconciliation import PROOF_FLAGS


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name));self.user='ou_restart';self.store.allow(self.user)
        payload={'action':'replay','scenes':[1,2],'assets':{},'baseline':{'data':{'kind':'candidate-v1','binding':{'input_digest':'a'*64}}}}
        self.job=self.store.submit(self.user,'private','request',payload)[0]
        self.store.finish(self.job,'NEEDS_REVIEW',{'error':'interrupted'})
        self.job=self.store.job(self.user,self.job['id'])
        self.executor=Mock();self.executor.request.side_effect=lambda u,op,**kw:{'operation':op,**kw}
        def receipt(req,*args,**kwargs):
            return {'state':'PASS','run_id':req['run_id'],'task':'/data/tasks/'+req['run_id'],'input_digest':'a'*64,
                    'hardware_actions':'NOT_RUN',**dict.fromkeys(PROOF_FLAGS,True)}
        self.executor.remote.side_effect=receipt

    def test_positive_full_and_precheck_receipts_complete_without_dispatch(self):
        self.assertTrue(completed({},self.store,self.job,self.executor))
        job=self.store.job(self.user,self.job['id']);self.assertEqual('SUCCEEDED',job['state'])
        self.assertEqual(self.job['payload'],job['payload']);self.assertEqual(1,len(self.store.jobs(self.user)))
        self.assertEqual(['reconcile_replay','reconcile_precheck'],[c.args[0]['operation'] for c in self.executor.remote.call_args_list])
        self.assertTrue(all(c.kwargs['read_only'] for c in self.executor.remote.call_args_list));self.executor.execute.assert_not_called()
        self.assertFalse(completed({},self.store,job,self.executor))

    def test_missing_or_wrong_proof_keeps_review_barrier(self):
        for change in ({'state':'FAIL'},{'input_digest':'b'*64},{'cleanup_verified':False},{'hardware_actions':'RAN'}):
            self.executor.remote.side_effect=None
            self.executor.remote.return_value={'state':'PASS','run_id':self.job['id'],'task':'/data/task','input_digest':'a'*64,'hardware_actions':'NOT_RUN',**dict.fromkeys(PROOF_FLAGS,True),**change}
            with self.assertRaises(GatewayError):completed({},self.store,self.job,self.executor)
            self.assertEqual('NEEDS_REVIEW',self.store.job(self.user,self.job['id'])['state'])

    def test_concurrent_job_change_is_not_overwritten(self):
        original=self.executor.remote.side_effect
        def mutate(*args,**kwargs):
            self.store.finish(self.job,'FAILED',{'error':'operator action'})
            return original(*args,**kwargs)
        self.executor.remote.side_effect=mutate
        with self.assertRaises(GatewayError):completed({},self.store,self.job,self.executor)
        self.assertEqual('FAILED',self.store.job(self.user,self.job['id'])['state'])

    def evaluated_job(self):
        payload=copy.deepcopy(self.job['payload']);payload['evaluate_after']=True
        job=self.store.submit(self.user,'private','evaluation-recovery',payload)[0]
        self.store.finish(job,'NEEDS_REVIEW',{'error':'interrupted before evaluation handoff'})
        return self.store.job(self.user,job['id'])

    def test_default_evaluation_recovery_hands_off_once_without_replay(self):
        job=self.evaluated_job()
        self.assertTrue(completed({},self.store,job,self.executor))
        parent=self.store.job(self.user,job['id']);self.assertEqual('POSTPROCESSING',parent['state'])
        child=self.store.job(self.user,job['id']+'-eval')
        self.assertEqual('QUEUED',child['state']);self.assertEqual('postprocess',child['lane'])
        self.assertEqual(job['id'],child['payload']['source_job']);self.assertEqual(job['payload'],parent['payload'])
        self.assertFalse(completed({},self.store,parent,self.executor))
        self.assertEqual(1,len([j for j in self.store.jobs(self.user) if j['payload'].get('source_job')==job['id']]))
        self.executor.execute.assert_not_called()
        self.assertTrue(all(c.kwargs['read_only'] for c in self.executor.remote.call_args_list))

    def test_latest_skip_choice_is_honored_during_recovery(self):
        from bench_gateway.execution_controls import schema
        job=self.evaluated_job()
        with self.store.connect() as c:
            schema(c)
            c.execute('INSERT INTO execution_controls(job,owner,evaluate_after,version,session,updated) VALUES(?,?,?,?,?,?)',(job['id'],self.user,0,1,'private',1))
        self.assertTrue(completed({},self.store,job,self.executor))
        self.assertEqual('SUCCEEDED',self.store.job(self.user,job['id'])['state'])
        self.assertEqual([], [j for j in self.store.jobs(self.user) if j['payload'].get('source_job')==job['id']])

    def test_evaluation_recovery_with_incomplete_proof_never_queues_evaluation(self):
        job=self.evaluated_job();self.executor.remote.side_effect=None
        self.executor.remote.return_value={'state':'PASS','run_id':job['id'],'input_digest':'a'*64,'task':'/data/task','hardware_actions':'NOT_RUN',**dict.fromkeys(PROOF_FLAGS,True),'restore_verified':False}
        with self.assertRaises(GatewayError):completed({},self.store,job,self.executor)
        self.assertEqual('NEEDS_REVIEW',self.store.job(self.user,job['id'])['state'])
        self.assertEqual([], [j for j in self.store.jobs(self.user) if j['payload'].get('source_job')==job['id']])

    def test_concurrent_receipt_change_is_not_overwritten_on_default_evaluation(self):
        job=self.evaluated_job();original=self.executor.remote.side_effect;changed=[]
        def mutate(*args,**kwargs):
            if not changed:
                changed.append(True);self.store.finish(job,'NEEDS_REVIEW',{'error':'new receipt received'})
            return original(*args,**kwargs)
        self.executor.remote.side_effect=mutate
        with self.assertRaises(GatewayError):completed({},self.store,job,self.executor)
        current=self.store.job(self.user,job['id']);self.assertEqual('NEEDS_REVIEW',current['state'])
        self.assertEqual('new receipt received',current['result']['error'])
        self.assertEqual([], [j for j in self.store.jobs(self.user) if j['payload'].get('source_job')==job['id']])
