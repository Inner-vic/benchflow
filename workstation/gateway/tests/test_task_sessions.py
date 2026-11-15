import tempfile
from pathlib import Path
import unittest
from bench_gateway.core import Store,GatewayError
from bench_gateway.intake import Intake
from bench_gateway.tasks import Tasks
from bench_gateway.preparation import PreparationQueue


class TaskSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'state');self.user='ou_task_session_owner';self.other='ou_task_session_other'
        for user in (self.user,self.other):self.store.allow(user)
        self.tasks=Tasks(self.store);self.intake=Intake(self.store)

    def tearDown(self):self.tmp.cleanup()

    def test_new_task_registers_before_execution_and_switch_restores_draft(self):
        self.intake.update(self.user,{'software_name':'A','package':'A'},new=True)
        a=self.tasks.current(self.user)
        self.intake.update(self.user,{'software_name':'B'},new=True);b=self.tasks.current(self.user)
        self.assertNotEqual(a['id'],b['id'])
        self.assertNotIn('package',self.intake.get(self.user))
        self.tasks.select(self.user,a['id'])
        self.assertEqual(self.intake.get(self.user)['package'],'A')
        self.assertIn('B',self.tasks.text(self.user))
        with self.assertRaises(GatewayError):self.tasks.select(self.other,a['id'])

    def test_frozen_job_cannot_be_mutated_but_another_draft_can_be_edited(self):
        self.intake.update(self.user,{'package':'A'},new=True);a=self.tasks.current(self.user)
        job,_=self.store.submit(self.user,'session','one',{'action':'replay'})
        self.tasks.link(self.user,job)
        self.intake.update(self.user,{'camera_so':'next-camera'})
        following=self.tasks.current(self.user)
        self.assertNotEqual(a['id'],following['id'])
        self.assertEqual(following['data']['package'],'A')
        self.assertEqual(following['data']['camera_so'],'next-camera')
        with self.assertRaises(GatewayError):self.tasks.update(self.user,a['id'],{'package':'B'})
        self.intake.update(self.user,{'package':'C'},new=True)
        self.assertEqual(self.tasks.get(self.user,a['id'])['data']['package'],'A')
        self.assertEqual(self.store.job(self.user,job['id'])['payload'],{'action':'replay'})

    def test_conversation_history_follows_task_and_owner(self):
        self.intake.update(self.user,{'package':'A'},new=True);a=self.tasks.current(self.user)
        self.store.message(self.user,'chat','user','material A')
        self.intake.update(self.user,{'package':'B'},new=True)
        self.assertEqual(self.store.history(self.user,'chat'),[])
        self.store.message(self.user,'chat','user','material B')
        self.tasks.select(self.user,a['id'])
        self.assertEqual(self.store.history(self.user,'chat'),[{'role':'user','content':'material A'}])
        self.assertEqual(self.store.history(self.other,'chat'),[])

    def test_preparation_lookup_does_not_borrow_another_task(self):
        cfg={'state_root':str(self.store.root)};q=PreparationQueue(cfg,self.store)
        self.intake.update(self.user,{'package':'A'},new=True);a=self.tasks.current(self.user)
        q.submit(self.user,'chat');prep=q.latest(self.user)
        self.intake.update(self.user,{'package':'B'},new=True)
        self.assertIsNone(q.latest(self.user))
        self.tasks.select(self.user,a['id'])
        self.assertEqual(q.latest(self.user)['id'],prep['id'])

    def test_background_attachment_update_stays_on_its_original_task(self):
        self.intake.update(self.user,{'package':'A'},new=True);a=self.tasks.current(self.user)
        self.intake.update(self.user,{'package':'B'},new=True)
        self.tasks.update(self.user,a['id'],{'camera_so':'a-camera'})
        self.assertNotIn('camera_so',self.intake.get(self.user))
        self.assertEqual(self.tasks.get(self.user,a['id'])['data']['camera_so'],'a-camera')

    def test_failed_task_stays_frozen_and_corrections_start_next_request(self):
        self.intake.update(self.user,{'package':'A'},new=True);task=self.tasks.current(self.user)
        job,_=self.store.submit(self.user,'chat','failed',{'action':'replay'})
        self.tasks.link(self.user,job);self.store.finish(job,'FAILED',{'diagnostics':{'safe_to_continue':True}})
        with self.assertRaises(GatewayError):self.tasks.update(self.user,task['id'],{'camera_so':'new-camera'})
        self.intake.update(self.user,{'camera_so':'new-camera'})
        current=self.tasks.current(self.user)
        self.assertNotEqual(current['id'],task['id']);self.assertIsNone(current['job_id'])
        self.assertEqual(self.tasks.get(self.user,task['id'])['job_id'],job['id'])
        self.assertNotIn('camera_so',self.tasks.get(self.user,task['id'])['data'])
        self.assertEqual(self.store.job(self.user,job['id'])['state'],'FAILED')
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM task_attempts').fetchone()[0],1)

    def test_logical_task_query_and_legacy_jobs_remain_owner_scoped(self):
        job,_=self.store.submit(self.user,'chat','legacy',{'action':'replay'})
        self.intake.update(self.user,{'software_name':'A'},new=True);task=self.tasks.current(self.user)
        self.assertIn(job['id'],self.tasks.text(self.user))
        self.assertIn('A',self.tasks.command(self.user,'查看任务 '+task['id']))
        with self.assertRaises(GatewayError):self.tasks.command(self.other,'查看任务 '+task['id'])


if __name__=='__main__':unittest.main()
