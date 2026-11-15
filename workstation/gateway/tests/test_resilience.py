from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.core import Store
from bench_gateway.queue_status import QueueStatus
from bench_gateway.worker import deliver,run


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'state');self.owner='ou_test_resilience'
        self.store.allow(self.owner)
        self.cfg={'project_root':str(self.root),'mode':'simulate','live_enabled':False,
                  'notifications_enabled':True,'cc_binary':'fake-cc','cc_project':'test'}
        self.job=self.store.submit(self.owner,'feishu:oc_test:'+self.owner,'request',{'action':'replay'})[0]

    def tearDown(self):self.tmp.cleanup()

    def result(self,**kwargs):
        self.store.finish(self.job,'SUCCEEDED',{'task':'/tasks/evidence',**kwargs})

    def test_restart_preserves_receipts_and_never_replays_unknown_outcome(self):
        claimed=self.store.claim()
        receipt={'run_id':self.job['id']+'-smoke','state':'PASS'}
        self.store.stage(claimed,'正式回灌中',smoke=receipt,task='/tasks/partial')
        self.store.submit(self.owner,self.job['session'],'next',{'action':'replay'})
        self.cfg['notifications_enabled']=False
        with patch('bench_gateway.worker.Executor.execute') as execute:
            run(self.cfg,self.store,once=True)
        execute.assert_not_called()
        saved=self.store.job(self.owner,self.job['id'])
        self.assertEqual(saved['state'],'NEEDS_REVIEW')
        self.assertEqual(saved['result']['smoke'],receipt)
        self.assertEqual(saved['result']['stage'],'正式回灌中')
        self.assertEqual(saved['result']['task'],'/tasks/partial')
        self.assertIsNone(self.store.claim())

    def test_old_result_acknowledgement_cannot_hide_reconciled_result(self):
        self.result()
        def sender(row):
            self.store.finish(self.job,'NEEDS_REVIEW',{'error':'new evidence requires review'})
            return True
        self.assertEqual(deliver(self.cfg,self.store,sender),0)
        with self.store.connect() as c:row=dict(c.execute('SELECT * FROM outbox').fetchone())
        self.assertEqual(row['sent'],0);self.assertEqual(row['version'],2)
        self.assertIn('new evidence',row['text'])

    def test_attachment_retry_does_not_duplicate_successful_body(self):
        self.result(local_report=str(self.root/'report.txt'))
        calls=[]
        def send(argv,**kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=int('--file' in argv and len(calls)==2))
        with patch('bench_gateway.worker.subprocess.run',side_effect=send):
            self.assertEqual(deliver(self.cfg,self.store),0)
            self.assertEqual(deliver(self.cfg,self.store),1)
            self.assertEqual(deliver(self.cfg,self.store),0)
        self.assertEqual(sum('--stdin' in argv for argv in calls),1)
        self.assertEqual(sum('--file' in argv for argv in calls),2)

    def test_failed_delivery_retries_after_backoff_without_reexecuting_job(self):
        self.result();self.cfg['notification_retry_seconds']=5
        calls=[]
        def sender(row):calls.append(row);raise TimeoutError('transport unavailable')
        with patch('bench_gateway.worker.time.time',return_value=100):
            self.assertEqual(deliver(self.cfg,self.store,sender),0)
            self.assertEqual(deliver(self.cfg,self.store,sender),0)
        self.assertEqual(len(calls),1)
        with self.store.connect() as c:
            row=dict(c.execute('SELECT * FROM outbox').fetchone())
        self.assertEqual(row['next_attempt_at'],105)
        with patch('bench_gateway.worker.time.time',return_value=105):
            self.assertEqual(deliver(self.cfg,self.store,sender),0)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT next_attempt_at FROM outbox').fetchone()[0],115)
        self.assertEqual(self.store.job(self.owner,self.job['id'])['state'],'SUCCEEDED')

    def test_new_queue_position_resets_retry_delay(self):
        queue=QueueStatus(self.store);queue.notice(self.job,'position 2')
        self.cfg['notification_retry_seconds']=5
        with patch('bench_gateway.worker.time.time',return_value=100):
            deliver(self.cfg,self.store,lambda row:False)
            queue.notice(self.job,'position 1')
            sent=[];self.assertEqual(deliver(self.cfg,self.store,lambda row:sent.append(row) or True),1)
        self.assertEqual(sent[0]['text'],'position 1')

    def test_completed_results_are_delivered_before_queue_positions(self):
        other=self.store.submit(self.owner,self.job['session'],'next',{'action':'replay'})[0]
        QueueStatus(self.store).notice(other,'position')
        self.result();sent=[]
        deliver(self.cfg,self.store,lambda row:sent.append(row) or True)
        self.assertEqual([row['job'] for row in sent],[self.job['id'],other['id']])

    def test_dispatch_ready_without_ssh_receipt_is_not_reported_as_real_connection(self):
        queue=QueueStatus(self.store)
        queue.resource({'state':'READY','checked_at':time.time()})
        self.assertEqual(queue.resource()['state'],'UNKNOWN')
        self.assertNotIn('两段 SSH 链路已通过',queue.text(self.owner))
        queue.resource({'state':'READY','checked_at':time.time(),'probe_kind':'ssh-readonly-v1',
                        'workstation_ssh':'PASS','bench_ssh':'PASS'})
        self.assertIn('两段 SSH 链路已通过',queue.text(self.owner))

    def test_schema_upgrade_preserves_pending_legacy_notification(self):
        legacy=self.root/'legacy';legacy.mkdir()
        with closing(sqlite3.connect(legacy/'gateway.sqlite3')) as c:
            c.executescript('''CREATE TABLE outbox(id INTEGER PRIMARY KEY,job TEXT NOT NULL UNIQUE,
                session TEXT NOT NULL,text TEXT NOT NULL,attachment TEXT,sent INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0);
                INSERT INTO outbox(job,session,text) VALUES('legacy-job','legacy-session','pending result');''')
        upgraded=Store(legacy)
        with upgraded.connect() as c:row=dict(c.execute('SELECT * FROM outbox').fetchone())
        self.assertEqual(row['text'],'pending result');self.assertEqual(row['sent'],0)
        self.assertEqual(row['version'],1);self.assertEqual(row['text_sent'],0)
        self.assertEqual(row['next_attempt_at'],0)


if __name__=='__main__':unittest.main()
