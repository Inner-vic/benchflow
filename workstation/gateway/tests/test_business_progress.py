import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from bench_gateway.core import Store
from bench_gateway.progress import event,stage,deliver,Follow,PREFIX,notify_input


class BusinessProgressTests(unittest.TestCase):
    def test_periodic_business_notice_is_not_suppressed_by_internal_progress_updates(self):
        with patch('bench_gateway.progress.time.time',return_value=100):
            self.store.stage(self.job,'正式回灌中')
            deliver(self.store,lambda row:True)
        with patch('bench_gateway.progress.time.time',return_value=190):self.emit('BATCH',completed=1,total=18)
        sent=[]
        with patch('bench_gateway.progress.time.time',return_value=191):deliver(self.store,lambda row:sent.append(row['text']) or True)
        self.assertEqual(1,len(sent));self.assertIn('1/18',sent[0]);self.assertIn('仍在执行',sent[0])
        with patch('bench_gateway.progress.time.time',return_value=192):deliver(self.store,lambda row:sent.append(row['text']) or True)
        self.assertEqual(1,len(sent))

    def test_completed_preparation_drops_queued_old_phase_notice(self):
        from bench_gateway.intake import Intake
        from bench_gateway.preparation import PreparationQueue,_record_progress
        Intake(self.store).update(self.user,{'package':'A'},new=True)
        queue=PreparationQueue({},self.store);queue.submit(self.user,'feishu:private:'+self.user)
        prep=queue.latest(self.user)
        with self.store.connect() as c:c.execute("UPDATE preparations SET state='RUNNING' WHERE id=?",(prep['id'],))
        _record_progress(self.store,prep,'准备M100编译产物')
        with self.store.connect() as c:c.execute("UPDATE preparations SET state='STATIC_PASSED',result='{}' WHERE id=?",(prep['id'],))
        sent=[];deliver(self.store,lambda row:sent.append(row['text']) or True)
        self.assertEqual([],sent)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'state');self.user='ou_progress_owner';self.store.allow(self.user)
        self.job,_=self.store.submit(self.user,'feishu:private:'+self.user,'first',{'action':'replay','scenes':list(range(1,19))})
        self.job=self.store.claim();self.run=self.job['id']

    def tearDown(self):self.tmp.cleanup()

    def emit(self,kind,run=None,**values):
        event(self.store,self.user,self.run,run or self.run,{'schema_version':1,'run_id':run or self.run,'kind':kind,**values})

    def test_verified_software_and_gate_milestones_are_idempotent(self):
        for _ in range(2):self.emit('SOFTWARE_INSTALLED',run=self.run+'-smoke')
        self.store.stage(self.job,'正式回灌中')
        sent=[];deliver(self.store,lambda row:sent.append(row['text']) or True)
        self.assertEqual(len(sent),2)
        self.assertIn('刷包完成',sent[0]);self.assertIn('小场景测试通过',sent[1])
        self.assertFalse(any('命令' in s or 'thinking' in s for s in sent))

    def test_scene_updates_are_throttled_and_cannot_go_backwards(self):
        for done in range(1,7):self.emit('BATCH',completed=done,total=18)
        self.emit('BATCH',completed=2,total=18)
        self.emit('BATCH',completed=17,total=19)
        sent=[];deliver(self.store,lambda row:sent.append(row['text']) or True)
        self.assertEqual(len(sent),2)
        self.assertIn('3/18',sent[0]);self.assertIn('6/18',sent[1])
        self.assertEqual(self.store.job(self.user,self.run)['result']['execution_progress']['completed'],6)

    def test_stage_update_preserves_progress_and_smoke_details(self):
        self.emit('BATCH',completed=3,total=18)
        self.store.stage(self.job,'正式回灌中',smoke={'state':'PASS'})
        result=self.store.job(self.user,self.run)['result']
        self.assertEqual(result['execution_progress']['completed'],3)
        self.assertEqual(result['smoke']['state'],'PASS')

    def test_failure_does_not_fabricate_success_and_terminal_suppresses_stale_updates(self):
        self.store.stage(self.job,'预检失败，检查软件包及依赖')
        sent=[];deliver(self.store,lambda r:sent.append(r['text']) or True)
        self.assertIn('全量回灌尚未开始',sent[0])
        self.emit('BATCH',completed=3,total=18)
        self.store.finish(self.job,'FAILED',{'error':'failed'})
        sent=[];deliver(self.store,lambda r:sent.append(r['text']) or True)
        self.assertEqual(sent,[])

    def test_bad_events_raw_logs_and_old_log_bytes_are_ignored(self):
        path=self.root/'ssh.log';path.write_text(PREFIX+json.dumps({'schema_version':1,'run_id':self.run,'kind':'BATCH','completed':18,'total':18})+'\n')
        follower=Follow(self.store,self.user,self.run,path)
        with path.open('a') as f:
            f.write('arbitrary shell command\n'+PREFIX+'not json\n')
            f.write(PREFIX+json.dumps({'schema_version':1,'run_id':'other-run','kind':'BATCH','completed':9,'total':18})+'\n')
        follower.poll()
        self.assertIsNone(self.store.job(self.user,self.run)['result'])

    def test_notification_failure_retries_without_touching_job_or_hardware(self):
        self.emit('SOFTWARE_READY',run=self.run+'-smoke')
        deliver(self.store,lambda row:False)
        self.assertEqual(self.store.job(self.user,self.run)['state'],'RUNNING')
        with self.store.connect() as c:c.execute('UPDATE progress_outbox SET next_attempt_at=0')
        sent=[];deliver(self.store,lambda r:sent.append(r['text']) or True)
        self.assertEqual(len(sent),1)
        deliver(self.store,lambda r:sent.append(r['text']) or True)
        self.assertEqual(len(sent),1)


if __name__=='__main__':unittest.main()
