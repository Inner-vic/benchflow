import copy
import json
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock,patch

import test_assemblies as fixtures
from bench_gateway.archive_queue import tick,recover
from bench_gateway.core import GatewayError
from bench_gateway.executor import Executor


class ArchiveQueueTests(unittest.TestCase):
    example=fixtures.AssemblyCatalogTests.example

    def setUp(self):
        fixtures.AssemblyCatalogTests.setUp(self)
        self.archive=copy.deepcopy(self.result['assembly_archive']);self.result.pop('assembly_archive')
        self.result['smoke']['task']='/workstation/tasks/'+self.job['id']+'-smoke'
        self.result.update(assembly_pending=True,assembly={'state':'PENDING'})

    def status(self):
        with self.store.connect() as c:return dict(c.execute('SELECT * FROM archive_jobs WHERE job=?',(self.job['id'],)).fetchone())

    def test_result_and_archive_request_commit_together_and_next_hardware_is_free(self):
        self.store.finish(self.job,'SUCCEEDED',self.result)
        self.assertEqual('QUEUED',self.status()['state'])
        next_job=self.store.submit(self.user,self.job['session'],'next',{'action':'replay'})[0]
        entered=threading.Event();release=threading.Event()
        def slow(*args,**kwargs):entered.set();release.wait(5);return {'state':'ASSEMBLY_PENDING'}
        with patch('bench_gateway.assembly_recovery.recover_assembly',side_effect=slow):
            thread=threading.Thread(target=tick,args=(self.cfg,self.store));thread.start();self.addCleanup(release.set)
            self.assertTrue(entered.wait(2));self.assertEqual('RUNNING',self.status()['state'])
            self.assertEqual(next_job['id'],self.store.claim()['id'])
            release.set();thread.join(5)
        self.assertEqual('SUCCEEDED',self.store.job(self.user,self.job['id'])['state'])

    def test_archive_reuses_recovery_merge_and_does_not_reissue_completion(self):
        self.store.finish(self.job,'SUCCEEDED',self.result)
        with self.store.connect() as c:before=dict(c.execute('SELECT * FROM outbox WHERE job=?',(self.job['id'],)).fetchone())
        executor=SimpleNamespace(save_assembly=Mock(side_effect=lambda job,result,**kw:{**result,'assembly_archive':self.archive}))
        tick(self.cfg,self.store,executor)
        self.assertEqual('DONE',self.status()['state'])
        current=self.store.job(self.user,self.job['id'])
        self.assertEqual('SAVED',current['result']['assembly']['state']);self.assertFalse(current['result']['assembly_pending'])
        with self.store.connect() as c:self.assertEqual(before,dict(c.execute('SELECT * FROM outbox WHERE job=?',(self.job['id'],)).fetchone()))
        self.assertIsNone(tick(self.cfg,self.store,executor));self.assertEqual(1,executor.save_assembly.call_count)

    def test_failure_keeps_replay_success_and_interruption_does_not_retry(self):
        self.store.finish(self.job,'SUCCEEDED',self.result)
        with self.store.connect() as c:c.execute("UPDATE archive_jobs SET state='RUNNING'")
        recover(self.store)
        self.assertEqual('NEEDS_REVIEW',self.status()['state']);self.assertIsNone(tick(self.cfg,self.store))
        self.assertEqual('SUCCEEDED',self.store.job(self.user,self.job['id'])['state'])

    def test_bad_receipt_rolls_back_terminal_state_and_archive_enqueue(self):
        result={**self.result,'input_digest':'a'*64}
        with self.assertRaises(GatewayError):self.store.finish(self.job,'SUCCEEDED',result)
        self.assertEqual('RUNNING',self.store.job(self.user,self.job['id'])['state'])
        with self.store.connect() as c:self.assertIsNone(c.execute('SELECT * FROM archive_jobs').fetchone())

    def test_pipeline_returns_before_archiving_when_enabled(self):
        worker=object.__new__(Executor);worker.cfg={**self.cfg,'defer_assembly_archive':True};worker.store=self.store
        worker.request=lambda owner,operation,**kw:{'operation':operation,**kw}
        self.job['payload']['scenes']=[1]
        worker.remote=Mock(side_effect=[self.result['smoke'],{k:v for k,v in self.result.items() if k!='smoke'}])
        worker.save_assembly=Mock(side_effect=AssertionError('must archive later'))
        result=worker.replay_pipeline(self.job)
        self.assertTrue(result['assembly_pending']);worker.save_assembly.assert_not_called()
