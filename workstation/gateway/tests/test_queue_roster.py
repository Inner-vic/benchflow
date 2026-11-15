"""Display-only shared queue tests; no real messages or bench operations."""
import copy
import json
import time
import unittest
from unittest.mock import Mock

import test_gateway as fixtures
from bench_gateway.queue_status import QueueStatus
from bench_gateway.task_cards import snapshot,render,deliver


class QueueRosterTests(unittest.TestCase):
    setUp=fixtures.GatewayTests.setUp
    tearDown=fixtures.GatewayTests.tearDown

    def job(self,user,key):
        return self.s.submit(user,'feishu:oc_'+user+':'+user,key,{
            'action':'replay','scenes':[1],'package_secret':'private-package',
            'dataset':'/private/dataset','software_url':'https://private.invalid/package'})[0]

    def test_running_then_fifo_waiters_show_people_not_private_inputs(self):
        first=self.job(fixtures.B,'bob-secret');self.s.claim()
        own=self.job(fixtures.A,'alice');later=self.job(fixtures.B,'bob-later')
        before=[self.s.job(j['owner'],j['id']) for j in (first,own,later)]
        q=QueueStatus(self.s);rows=q.roster(fixtures.A,own['id']);text=q.roster_text(fixtures.A,own['id'],card=True)
        self.assertEqual([r['state'] for r in rows],['RUNNING','QUEUED','QUEUED'])
        self.assertEqual([r['position'] for r in rows],[None,1,2])
        self.assertIn("<person id='"+fixtures.B+"'",text)
        self.assertIn('当前使用：',text);self.assertNotIn('等待 1：',text)
        self.assertEqual(text.count('<person '),1)
        self.assertNotIn(fixtures.A,text)
        for secret in ('bob-secret',first['id'],later['id'],'private-package','/private/dataset','private.invalid'):
            self.assertNotIn(secret,text)
        self.assertEqual(before,[self.s.job(j['owner'],j['id']) for j in (first,own,later)])
        self.assertEqual(q.head(),None)

    def test_public_business_reference_is_read_not_allocated(self):
        own=self.job(fixtures.A,'one')
        from bench_gateway.tasks import Tasks
        tasks=Tasks(self.s);tasks.ensure(fixtures.A,fixtures.SESSION);tasks.link(fixtures.A,own)
        with self.s.connect() as c:
            before=[tuple(r) for r in c.execute('SELECT * FROM task_references')]
        queue=QueueStatus(self.s)
        self.assertTrue(queue.roster(fixtures.A,own['id'])[0]['reference'].startswith('TJ-'))
        self.assertEqual(queue.roster_text(fixtures.A,own['id'],card=True),'')
        with self.s.connect() as c:self.assertEqual(before,[tuple(r) for r in c.execute('SELECT * FROM task_references')])

    def test_cancel_completion_and_review_refresh_without_changing_dispatch(self):
        first=self.job(fixtures.B,'one');self.s.claim()
        cancelled=self.job(fixtures.B,'two');own=self.job(fixtures.A,'three');q=QueueStatus(self.s)
        self.assertEqual(q.roster(fixtures.A,own['id'])[-1]['position'],2)
        self.s.cancel(fixtures.B,cancelled['id'])
        self.assertEqual(q.roster(fixtures.A,own['id'])[-1]['position'],1)
        self.s.finish(first,'NEEDS_REVIEW',{'error':'unknown'})
        self.assertIn('待核验：',q.roster_text(fixtures.A,own['id'],card=True))
        self.assertIsNone(q.head())
        self.s.finish(first,'SUCCEEDED',{'task':'/existing/result'})
        self.assertNotIn('待核验：',q.roster_text(fixtures.A,own['id'],card=True))
        self.assertEqual(q.head()['id'],own['id'])

    def test_large_queue_keeps_exact_own_position_and_bounded_rows(self):
        for i in range(12):self.job(fixtures.B,'other-'+str(i))
        own=self.job(fixtures.A,'last');q=QueueStatus(self.s)
        text=q.roster_text(fixtures.A,own['id'],card=True)
        self.assertEqual(q.roster(fixtures.A,own['id'])[-1]['position'],13)
        self.assertEqual(text,'')
        self.assertEqual(snapshot(self.s,own)['action'],'前方 12 项等待。')
        self.assertEqual(q.head()['owner'],fixtures.B)

    def test_postprocessing_drafts_and_other_users_task_are_not_hardware_waiters(self):
        own=self.job(fixtures.A,'one');other=self.job(fixtures.B,'two')
        baseline={'data':{'evaluator':{'kind':'workstation-offline-v1'}}}
        evaluation=self.s.submit(fixtures.B,'feishu:oc_b:'+fixtures.B,'eval',{
            'action':'evaluate','source_job':other['id'],'source_task':'/existing','baseline':baseline})[0]
        q=QueueStatus(self.s)
        self.assertEqual(len(q.roster(fixtures.A,own['id'])),2)
        self.assertEqual(q.roster(fixtures.A,other['id']),[])
        self.assertEqual(q.roster(fixtures.A,evaluation['id']),[])

    def test_external_occupancy_does_not_invent_person_or_show_stale_state(self):
        own=self.job(fixtures.A,'one');q=QueueStatus(self.s)
        q.resource({'state':'RESOURCE_BUSY','checked_at':time.time()})
        self.assertIn('其他入口任务（人员未登记）',q.roster_text(fixtures.A,own['id'],card=True))
        q.resource({'state':'RESOURCE_BUSY','checked_at':time.time()-120})
        self.assertNotIn('其他入口任务',q.roster_text(fixtures.A,own['id'],card=True))

    def test_plain_fallback_uses_registered_names_without_ids_or_markup(self):
        self.s.allow(fixtures.B,'李同事<测试>')
        other=self.job(fixtures.B,'first');own=self.job(fixtures.A,'second')
        text=QueueStatus(self.s).text(fixtures.A,own['id'])
        self.assertIn('李同事&lt;测试&gt;',text)
        self.assertNotIn(fixtures.B,text);self.assertNotIn(other['id'],text)

    def test_same_card_refreshes_when_other_tasks_change(self):
        other=self.job(fixtures.B,'first');self.s.claim();own=self.job(fixtures.A,'second')
        cfg={**self.cfg,'notifications_enabled':True,'task_cards_enabled':True}
        client=Mock();client._json.return_value={'data':{'message_id':'om_unchanged_card'}}
        deliver(cfg,self.s,client=client,job_id=own['id'])
        initial=json.dumps(snapshot(self.s,self.s.job(fixtures.A,own['id'])),ensure_ascii=False)
        self.assertIn(fixtures.B,initial)
        self.s.finish(other,'SUCCEEDED',{'task':'/existing'})
        # Existing 30-second coalescing cycle; no new timer or core update.
        with self.s.connect() as c:c.execute('UPDATE task_cards SET last_sent=? WHERE job=?',(time.time()-31,own['id']))
        deliver(cfg,self.s,client=client,job_id=own['id'])
        self.assertEqual(client._json.call_args.args[0],'/im/v1/messages/om_unchanged_card')
        self.assertEqual(client._json.call_args.kwargs['method'],'PATCH')
        body=json.loads(client._json.call_args.kwargs['body'])
        self.assertNotIn(fixtures.B,body['content'])
        self.assertEqual(self.s.job(fixtures.A,own['id'])['state'],'QUEUED')

    def test_finished_cards_remove_roster_and_mobile_layout_keeps_native_person(self):
        self.job(fixtures.B,'first');self.s.claim();own=self.job(fixtures.A,'second')
        value=snapshot(self.s,own);card=render(value)
        self.assertIn('<person ',json.dumps(card))
        self.assertLess(len(json.dumps(card).encode()),28*1024)
        finished=copy.deepcopy(own);finished.update(state='CANCELLED',updated=time.time())
        self.assertEqual(snapshot(self.s,finished)['queue_text'],'')

    def test_card_does_not_name_finished_or_evaluation_jobs_as_current_occupant(self):
        other=self.job(fixtures.B,'first');self.s.claim();own=self.job(fixtures.A,'second')
        self.s.finish(other,'SUCCEEDED',{'task':'/existing'})
        self.s.submit(fixtures.B,'feishu:oc_b:'+fixtures.B,'eval',{'action':'evaluate','source_job':other['id']})
        self.assertEqual(QueueStatus(self.s).roster_text(fixtures.A,own['id'],card=True),'')
        self.assertEqual(snapshot(self.s,own)['action'],'前方 0 项等待。')


if __name__=='__main__':unittest.main()
