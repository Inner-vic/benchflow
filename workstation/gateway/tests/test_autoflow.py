import io
import json
import os
import time
import unittest
from unittest.mock import Mock,patch

import test_bindings as fixtures
from bench_gateway.autoflow import AutoFlow
from bench_gateway.attachments import Attachments
from bench_gateway.core import Store,GatewayError
from bench_gateway.intake import Intake
from bench_gateway.tasks import Tasks
from bench_gateway.service import Service
from bench_gateway import cc_agent


class AutoFlowTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.BindingTests();self.case.setUp();self.addCleanup(self.case.tearDown)
        c=self.case;c.cfg.update(auto_execute_on_complete=True,intake_settle_seconds=0,cc_project='bench')
        self.flow=AutoFlow(c.cfg,c.store)

    def row(self):
        with self.case.store.connect() as c:
            row=c.execute('SELECT * FROM automatic_tasks ORDER BY updated DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def ready(self):
        c=self.case;self.bound();self.flow.arm(c.user,c.session);self.flow.tick()
        self.assertEqual('CHECKING',self.row()['state'])

    def bound(self):
        c=self.case;baseline=c.bind()
        # The real preparation worker writes this after bind_preparation returns.
        c.change_result(lambda result:result.update(adapter_binding={'state':'READY_FOR_GATE','baseline_name':baseline['name']}))

    def add_attachment(self,state):
        c=self.case;Attachments(c.cfg,c.store,None,client=Mock());task=Tasks(c.store).current(c.user)
        with c.store.connect() as db:
            db.execute('INSERT INTO task_attachments VALUES(?,?,?,?,?,?,?,?,?)',
                ('att-'+'a'*16,c.user,task['id'],'om_pending','custom.yaml','{}',state,time.time(),time.time()))

    def test_deployment_does_not_backfill_old_drafts(self):
        self.case.bind()
        for _ in range(3):self.flow.tick()
        self.assertIsNone(self.row());self.assertEqual([],self.case.store.jobs(self.case.user))

    def test_complete_intake_queues_once_without_execute_command_and_freezes_task(self):
        self.ready();c=self.case;task=Tasks(c.store).current(c.user)
        self.flow.tick();self.flow.tick()
        jobs=c.store.jobs(c.user);self.assertEqual(1,len(jobs));job=jobs[0]
        self.assertEqual('QUEUED',job['state']);self.assertEqual('SUBMITTED',self.row()['state'])
        self.assertEqual(job['id'],Tasks(c.store).get(c.user,task['id'])['job_id'])
        self.assertEqual('automatic:'+task['id'],job['request_key'])
        reply=c.service.handle(c.session,'执行回灌',{'action':'replay'})
        self.assertIn('不重复执行',reply);self.assertEqual(1,len(c.store.jobs(c.user)))
        original=json.dumps(job['payload'],sort_keys=True)
        Intake(c.store).update(c.user,{'camera_so':'new-camera'})
        self.flow.arm(c.user,c.session)
        self.assertNotEqual(task['id'],Tasks(c.store).current(c.user)['id'])
        self.assertEqual(original,json.dumps(c.store.job(c.user,job['id'])['payload'],sort_keys=True))

    def test_missing_material_does_not_start_check_and_supplement_starts_it(self):
        c=self.case;values={k:v for k,v in c.draft.items() if k!='camera_so'}
        Intake(c.store).update(c.user,values,new=True);self.flow.arm(c.user,c.session);self.flow.tick()
        self.assertEqual('WAITING',self.row()['state']);self.assertIsNone(c.queue.latest(c.user))
        Intake(c.store).update(c.user,{'camera_so':'camera'});self.flow.arm(c.user,c.session);self.flow.tick()
        self.assertEqual('CHECKING',self.row()['state']);self.assertEqual('QUEUED',c.queue.latest(c.user)['state'])
        self.assertEqual([],c.store.jobs(c.user))

    def test_missing_m100_can_enter_supported_build_preparation(self):
        c=self.case;c.cfg['m100_build_enabled']=True
        values={k:v for k,v in c.draft.items() if k not in ('m100_so','m100_mode')};values['m100_mode']='build_if_missing'
        Intake(c.store).update(c.user,values,new=True);self.flow.arm(c.user,c.session);self.flow.tick()
        self.assertEqual('CHECKING',self.row()['state'])

    def test_unresolved_or_failed_attachments_block_even_with_other_sources_present(self):
        for state in ('RECEIVED','STORED','NEEDS_ROLE','NEEDS_TARGET','FAILED'):
            with self.subTest(state=state):
                self.add_attachment(state);self.flow.arm(self.case.user,self.case.session);self.flow.tick()
                self.assertEqual('WAITING',self.row()['state']);self.assertEqual([],self.case.store.jobs(self.case.user))
                with self.case.store.connect() as c:c.execute('DELETE FROM task_attachments')

    def test_pause_persists_across_new_material_and_resume_allows_queue(self):
        c=self.case;c.bind();self.flow.control(c.user,c.session,True)
        Intake(c.store).update(c.user,{'software_name':'new-label'});self.flow.arm(c.user,c.session);self.flow.tick()
        self.assertEqual('PAUSED',self.row()['state']);self.assertEqual([],c.store.jobs(c.user))
        reply=c.service.material_ack(c.user,{'software_name':'new-label'})
        self.assertIn('自动提交已暂停',reply);self.assertNotIn('将自动检查并排队',reply)
        # Restoring the original material values needs a fresh matching check.
        Intake(c.store).update(c.user,{'software_name':''})
        self.flow.control(c.user,c.session,False);self.flow.tick()
        self.assertEqual('CHECKING',self.row()['state'])

    def test_attachment_receipt_lists_missing_fields_without_requesting_optional_m100(self):
        c=self.case;c.cfg['m100_build_enabled']=True
        Intake(c.store).update(c.user,{'package':'A','camera_so':'camera','lidar_so':'lidar'},new=True)
        self.flow.arm(c.user,c.session);reply=self.flow.receipt(c.user)
        self.assertEqual('共享18场景',Intake(c.store).get(c.user)['dataset'])
        self.assertEqual('全部',Intake(c.store).get(c.user)['scene_scope'])
        self.assertIn('材料齐备',reply);self.assertNotIn('M100',reply)

    def test_checks_do_not_loop_on_unchanged_failure(self):
        c=self.case;self.flow.arm(c.user,c.session);self.flow.tick()
        with c.store.connect() as db:db.execute("UPDATE preparations SET state='NEEDS_INPUT',result=? WHERE id=?",(json.dumps({'issues':[{'field':'m100_so','message':'版本不匹配'}]}),self.row()['preparation_id']))
        self.flow.tick();self.assertEqual('BLOCKED',self.row()['state'])
        with c.store.connect() as db:count=db.execute('SELECT count(*) FROM preparations').fetchone()[0]
        for _ in range(4):self.flow.tick()
        with c.store.connect() as db:self.assertEqual(count,db.execute('SELECT count(*) FROM preparations').fetchone()[0])
        self.assertEqual([],c.store.jobs(c.user))

    def test_revision_changed_after_validation_is_rejected_at_atomic_commit(self):
        self.ready();c=self.case;original=Store.submit
        def changing(store,*args,**kwargs):
            Intake(c.store).update(c.user,{'scene_scope':'3'});self.flow.arm(c.user,c.session)
            return original(store,*args,**kwargs)
        with patch.object(Store,'submit',changing):self.flow.tick()
        self.assertEqual([],c.store.jobs(c.user));self.assertIsNone(Tasks(c.store).current(c.user)['job_id'])
        self.assertEqual('WAITING',self.row()['state'])

    def test_pause_after_validation_is_rejected_at_atomic_commit(self):
        self.ready();c=self.case;original=Store.submit
        def pausing(store,*args,**kwargs):
            self.flow.control(c.user,c.session,True)
            return original(store,*args,**kwargs)
        with patch.object(Store,'submit',pausing):self.flow.tick()
        self.assertEqual([],c.store.jobs(c.user));self.assertEqual('PAUSED',self.row()['state'])

    def test_new_attachment_after_validation_blocks_atomic_admission(self):
        self.ready();c=self.case;original=Store.submit
        def receiving(store,*args,**kwargs):
            self.add_attachment('RECEIVED')
            return original(store,*args,**kwargs)
        with patch.object(Store,'submit',receiving):self.flow.tick()
        self.assertEqual([],c.store.jobs(c.user));self.assertIn('附件',self.row()['error'])

    def test_restart_after_commit_does_not_create_a_second_job(self):
        self.ready();c=self.case;original=self.flow.update
        def interrupted(row,state,**values):
            if state=='SUBMITTED':raise RuntimeError('facade interrupted after atomic admission')
            return original(row,state,**values)
        with patch.object(self.flow,'update',interrupted),self.assertRaises(RuntimeError):self.flow.tick()
        self.assertEqual(1,len(c.store.jobs(c.user)));AutoFlow(c.cfg,c.store).tick()
        self.assertEqual(1,len(c.store.jobs(c.user)));self.assertEqual('SUBMITTED',self.row()['state'])

    def test_settle_period_combines_messages_without_requiring_user_confirmation(self):
        c=self.case;c.cfg['intake_settle_seconds']=5;self.bound();self.flow.arm(c.user,c.session);self.flow.tick()
        self.assertEqual('WAITING',self.row()['state'])
        with c.store.connect() as db:db.execute('UPDATE automatic_tasks SET not_before=0')
        self.flow.tick();self.flow.tick();self.assertEqual(1,len(c.store.jobs(c.user)))

    def test_unavailable_requested_delivery_is_reported_before_queue(self):
        c=self.case;Intake(c.store).update(c.user,{'lpai_target':'team'});self.flow.arm(c.user,c.session)
        self.flow.tick();self.assertEqual('BLOCKED',self.row()['state']);self.assertIn('LPAI',self.row()['error'])
        self.assertEqual([],c.store.jobs(c.user))

    def test_cross_user_session_cannot_arm_automatic_execution(self):
        with self.assertRaises(GatewayError):self.flow.arm(self.case.user,'feishu:oc_other:'+self.case.other)
        self.assertIsNone(self.row())

    def run_entry(self,text):
        c=self.case;output=io.StringIO()
        with patch('bench_gateway.cc_agent.load',return_value=c.cfg),patch.dict(os.environ,{'CC_PROJECT':'bench','CC_SESSION_KEY':c.session}),patch('sys.stdin',io.StringIO(text)),patch('sys.stdout',output),patch('bench_gateway.cc_agent.parse') as model:
            cc_agent.main();model.assert_not_called()
        return output.getvalue()

    def test_trusted_entry_arms_on_material_changes_but_not_greetings_or_reads(self):
        self.run_entry('你好');self.assertIsNone(self.row())
        self.run_entry('帮助');self.assertIsNone(self.row())
        self.run_entry('补充任务材料\n相机SO：new-camera')
        self.assertEqual('WAITING',self.row()['state']);self.assertEqual(self.case.user,self.row()['owner'])




    def test_adapter_update_rechecks_unsubmitted_preparation_without_resetting_materials(self):
        from unittest.mock import patch
        c=self.case;self.bound();old=c.queue.latest(c.user)
        before=Tasks(c.store).current(c.user)
        self.flow.arm(c.user,c.session)
        with patch('bench_gateway.bindings.preparation_needs_rebind',return_value=True):self.flow.tick()
        latest=c.queue.latest(c.user)
        self.assertNotEqual(old['id'],latest['id'])
        self.assertEqual('QUEUED',latest['state'])
        self.assertEqual(before['data'],Tasks(c.store).current(c.user)['data'])
        self.assertEqual([],c.store.jobs(c.user))