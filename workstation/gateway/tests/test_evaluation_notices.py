import unittest
from types import SimpleNamespace
import test_gateway as fixtures
from bench_gateway.queue_status import QueueStatus
from bench_gateway.worker import postprocess_tick


class EvaluationNoticeTests(unittest.TestCase):
    setUp=fixtures.GatewayTests.setUp
    tearDown=fixtures.GatewayTests.tearDown

    def enqueue(self,user,key):
        return self.s.submit(user,'feishu:oc_'+key+':'+user,key,{'action':'evaluate','source_job':key})[0]

    def notice(self,jid):
        with self.s.connect() as c:
            row=c.execute('SELECT * FROM queue_outbox WHERE job=?',(jid,)).fetchone()
            return dict(row) if row else None

    def test_evaluation_position_changes_are_private_and_deduplicated(self):
        first=self.enqueue(fixtures.B,'secret-source');own=self.enqueue(fixtures.A,'own-source');queue=QueueStatus(self.s)
        queue.publish();before=self.notice(own['id'])
        self.assertIn('等待评测第 2 位',before['text'])
        for private in (fixtures.B,first['id'],'secret-source'):self.assertNotIn(private,before['text'])
        queue.publish();self.assertEqual(self.notice(own['id'])['version'],before['version'])
        active=self.s.claim('postprocess');queue.publish()
        self.assertIn('已开始工作站离线评测',self.notice(active['id'])['text'])
        after=self.notice(own['id']);self.assertIn('等待评测第 1 位',after['text']);self.assertGreater(after['version'],before['version'])

    def test_start_notice_is_persisted_before_evaluator_runs(self):
        job=self.enqueue(fixtures.A,'source')
        def execute(active):
            self.assertIn('已开始工作站离线评测',self.notice(job['id'])['text'])
            return {'report':'raw.txt','local_report':'raw.txt','task':'/retained'}
        postprocess_tick(self.cfg,self.s,SimpleNamespace(execute=execute))
        self.assertEqual(self.s.job(fixtures.A,job['id'])['state'],'SUCCEEDED')
        self.assertIsNone(self.notice(job['id']))

    def test_unknown_evaluation_state_is_visible_without_other_owner_details(self):
        other=self.enqueue(fixtures.B,'secret');own=self.enqueue(fixtures.A,'own')
        self.s.finish(other,'NEEDS_REVIEW',{'error':'private'})
        self.s.finish(own,'NEEDS_REVIEW',{'error':'lost connection'})
        text=QueueStatus(self.s).text(fixtures.A)
        self.assertIn('2 个待核验',text);self.assertIn(own['id'],text);self.assertIn('不自动重跑',text)
        self.assertNotIn(other['id'],text);self.assertNotIn('private',text)


if __name__=='__main__':unittest.main()
