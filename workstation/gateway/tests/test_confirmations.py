import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from bench_gateway import confirmations
from bench_gateway.autoflow import AutoFlow
from bench_gateway.core import GatewayError,Store
from bench_gateway.intake import Intake
from bench_gateway.tasks import Tasks


class MaterialConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg={'project_root':self.tmp.name,'state_root':str(Path(self.tmp.name)/'state'),
                  'mode':'simulate','live_enabled':False,'require_material_confirmation':True,
                  'auto_execute_on_complete':True,'m100_build_enabled':False,'intake_settle_seconds':15}
        self.store=Store(self.cfg['state_root']);self.user='ou_confirmation_owner';self.other='ou_confirmation_other'
        self.session='feishu:oc_confirmation:'+self.user
        self.store.allow(self.user);self.store.allow(self.other)
        self.tasks=Tasks(self.store);self.intake=Intake(self.store)
        self.intake.update(self.user,{'package':'A','camera_so':'C','lidar_so':'L','m100_so':'M','scene_scope':'1','goal':'replay'})
        confirmations.configure(self.cfg,self.store)

    def task(self):return self.tasks.current(self.user)

    def confirm(self,**kwargs):
        t=self.task()
        return confirmations.confirm(self.cfg,self.store,self.user,self.session,kwargs.pop('tid',t['id']),kwargs.pop('revision',t['revision']),**kwargs)

    def active(self,task=None,session=None):
        with self.store.connect() as c:return confirmations.active(c,self.user,task or self.task(),session)

    def attachment(self,state='RECEIVED'):
        from bench_gateway.attachments import Attachments
        Attachments(self.cfg,self.store,Mock())
        t=self.task()
        with self.store.connect() as c:
            c.execute('INSERT INTO task_attachments VALUES(?,?,?,?,?,?,?,?,?)',
                      ('att-confirmation-test',self.user,t['id'],'om_confirmation_file','libxcamera.so','{}',state,time.time(),time.time()))

    def test_policy_is_persisted_and_absent_config_does_not_clear_it(self):
        self.assertTrue(confirmations.enabled(self.cfg));self.assertFalse(confirmations.enabled({}))
        confirmations.configure({},self.store)
        with self.store.connect() as c:self.assertTrue(confirmations.required_in(c))
        confirmations.configure({'require_material_confirmation':False},self.store)
        with self.store.connect() as c:self.assertFalse(confirmations.required_in(c))

    def test_confirm_freezes_exact_snapshot_and_arms_only_the_same_revision(self):
        task=self.task();record=self.confirm()
        self.assertEqual(task['data'],json.loads(record['snapshot']));self.assertEqual('ACTIVE',record['state'])
        self.assertEqual(record,self.active(session=self.session))
        with self.store.connect() as c:row=dict(c.execute('SELECT * FROM automatic_tasks WHERE task_id=?',(task['id'],)).fetchone())
        self.assertEqual('WAITING',row['state']);self.assertEqual(task['revision'],row['revision'])
        self.assertEqual(record['fingerprint'],row['fingerprint']);self.assertLessEqual(row['not_before'],time.time())
        self.assertEqual([],self.store.jobs(self.user))

    def test_double_click_reuses_one_confirmation_and_creates_no_job(self):
        first=self.confirm();second=self.confirm();self.assertEqual(first,second)
        with self.store.connect() as c:self.assertEqual(1,c.execute("SELECT COUNT(*) FROM material_confirmations WHERE state='ACTIVE'").fetchone()[0])
        self.assertEqual([],self.store.jobs(self.user))

    def test_duplicate_confirmation_does_not_reset_an_active_check(self):
        record=self.confirm()
        with self.store.connect() as c:c.execute("UPDATE automatic_tasks SET state='CHECKING'")
        # AutoFlow integration separately guarantees arm() preserves an active
        # confirmation. This module must not reset CHECKING after that call.
        with patch.object(AutoFlow,'arm',return_value=None) as arm:self.assertEqual(record,self.confirm())
        arm.assert_called_once()
        with self.store.connect() as c:self.assertEqual('CHECKING',c.execute('SELECT state FROM automatic_tasks').fetchone()[0])

    def test_concurrent_clicks_have_one_active_snapshot(self):
        gate=threading.Barrier(2)
        def click():gate.wait();return self.confirm()['id']
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            a=pool.submit(click);b=pool.submit(click);self.assertEqual(a.result(),b.result())
        with self.store.connect() as c:self.assertEqual(1,c.execute("SELECT COUNT(*) FROM material_confirmations WHERE state='ACTIVE'").fetchone()[0])

    def test_material_change_makes_old_confirmation_inactive(self):
        first=self.confirm();self.intake.update(self.user,{'scene_scope':'2'})
        self.assertIsNone(self.active())
        second=self.confirm();self.assertNotEqual(first['id'],second['id'])
        with self.store.connect() as c:self.assertEqual('INVALIDATED',c.execute('SELECT state FROM material_confirmations WHERE id=?',(first['id'],)).fetchone()[0])

    def test_material_hash_is_checked_even_when_revision_is_falsely_unchanged(self):
        self.confirm();task=self.task();data=dict(task['data']);data['camera_so']='tampered'
        task['data']=data;self.assertIsNone(self.active(task))

    def test_snapshot_integrity_and_session_are_checked(self):
        record=self.confirm();self.assertIsNone(self.active(session='feishu:oc_wrong:'+self.user))
        with self.store.connect() as c:c.execute('UPDATE material_confirmations SET snapshot=? WHERE id=?',('{invalid',record['id']))
        self.assertIsNone(self.active())

    def test_active_accepts_sql_task_rows_and_ignores_preparation_timestamp(self):
        record=self.confirm();task=self.task()
        with self.store.connect() as c:
            c.execute("UPDATE task_requests SET prep_id='prep-nonmaterial',updated=? WHERE id=?",(time.time()+10,task['id']))
            row=c.execute('SELECT * FROM task_requests WHERE id=?',(task['id'],)).fetchone()
            self.assertEqual(record,confirmations.active(c,self.user,row,self.session))

    def test_invalidate_preserves_frozen_snapshot_and_cannot_invalidate_other_owner(self):
        record=self.confirm()
        with self.store.connect() as c:
            self.assertEqual(0,confirmations.invalidate(c,self.other,record['task_id']))
            self.assertEqual(1,confirmations.invalidate(c,self.user,record['task_id'],'MATERIAL_CHANGED'))
            after=dict(c.execute('SELECT * FROM material_confirmations WHERE id=?',(record['id'],)).fetchone())
        self.assertEqual('INVALIDATED',after['state']);self.assertEqual(record['snapshot'],after['snapshot'])

    def test_stale_card_revision_and_non_integer_revision_are_refused(self):
        self.intake.update(self.user,{'scene_scope':'2'})
        for revision in (1,'2',True):
            with self.assertRaises(GatewayError):self.confirm(revision=revision)
        self.assertIsNone(self.active())

    def test_foreign_task_non_current_task_and_foreign_session_are_refused(self):
        old=self.task();self.intake.update(self.user,{'package':'B'},new=True)
        with self.assertRaisesRegex(GatewayError,'切换'):self.confirm(tid=old['id'],revision=old['revision'])
        with self.assertRaises(GatewayError):confirmations.confirm(self.cfg,self.store,self.other,'feishu:oc_other:'+self.other,old['id'],old['revision'])
        with self.assertRaisesRegex(GatewayError,'身份'):confirmations.confirm(self.cfg,self.store,self.user,'feishu:oc_other:'+self.other,self.task()['id'],self.task()['revision'])

    def test_submitted_task_cannot_be_confirmed_again(self):
        t=self.task()
        with self.store.connect() as c:c.execute("UPDATE task_requests SET job_id='gw-existing' WHERE id=?",(t['id'],))
        with self.assertRaisesRegex(GatewayError,'已提交'):self.confirm()

    def test_editing_material_and_unfinished_attachments_are_refused(self):
        from bench_gateway.card_ui import ensure
        t=self.task();ensure(self.store,t)
        with self.store.connect() as c:c.execute('UPDATE card_ui SET editing=1 WHERE task_id=?',(t['id'],))
        with self.assertRaisesRegex(GatewayError,'编辑'):self.confirm()
        with self.store.connect() as c:c.execute('UPDATE card_ui SET editing=0 WHERE task_id=?',(t['id'],))
        self.attachment()
        with self.assertRaisesRegex(GatewayError,'附件'):self.confirm()
        with self.store.connect() as c:c.execute("UPDATE task_attachments SET state='ASSOCIATED'")
        self.assertEqual('ACTIVE',self.confirm()['state'])

    def test_only_the_confirm_callbacks_own_batch_may_be_excluded(self):
        from bench_gateway.incremental import InputBatch
        own=InputBatch(self.store,self.user,'message')
        with self.assertRaisesRegex(GatewayError,'接收'):self.confirm()
        other=InputBatch(self.store,self.user,'files:pending.so')
        with self.assertRaisesRegex(GatewayError,'接收'):self.confirm(exclude_batch=own.token)
        other.finish(True)
        self.assertEqual('ACTIVE',self.confirm(exclude_batch=own.token)['state'])
        own.finish(True)

    def test_aborted_previous_material_message_blocks_confirmation(self):
        from bench_gateway.incremental import InputBatch
        InputBatch(self.store,self.user,'files:missing.so').finish(False)
        with self.assertRaisesRegex(GatewayError,'接收'):self.confirm()

    def test_declared_pending_upload_is_not_silently_ignored(self):
        from bench_gateway.card_ui import ensure
        t=self.task();ensure(self.store,t)
        with self.store.connect() as c:c.execute('INSERT INTO card_expected_uploads VALUES(?,?,?,?)',(t['id'],self.user,'camera',time.time()))
        with self.assertRaisesRegex(GatewayError,'声明'):self.confirm()

    def test_missing_sensor_is_refused_and_m100_automatic_build_is_optional(self):
        self.intake.update(self.user,{'package':'B'},new=True)
        with self.assertRaisesRegex(GatewayError,'相机'):self.confirm()
        self.intake.update(self.user,{'camera_so':'C','lidar_so':'L','m100_mode':'build_if_missing'})
        with self.assertRaisesRegex(GatewayError,'M100'):self.confirm()
        self.cfg['m100_build_enabled']=True
        self.assertEqual('ACTIVE',self.confirm()['state'])

    def test_provided_m100_mode_requires_artifact_even_if_build_available(self):
        self.cfg['m100_build_enabled']=True
        self.intake.update(self.user,{'package':'B','camera_so':'C','lidar_so':'L','m100_mode':'provided'},new=True)
        with self.assertRaisesRegex(GatewayError,'M100'):self.confirm()

    def test_feature_or_scheduler_disabled_is_refused(self):
        for name in ('require_material_confirmation','auto_execute_on_complete'):
            cfg={**self.cfg,name:False};t=self.task()
            with self.assertRaises(GatewayError):confirmations.confirm(cfg,self.store,self.user,self.session,t['id'],t['revision'])


if __name__=='__main__':unittest.main()
