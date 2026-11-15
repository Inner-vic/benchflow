import io,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from bench_gateway.core import GatewayError
from bench_gateway.service import Service
from bench_gateway.tasks import Tasks
from bench_gateway.intake import Intake
from bench_gateway.autoflow import AutoFlow
from bench_gateway import card_ui,input_cards,confirmations
from bench_gateway.preparation import PreparationQueue,fingerprint
from bench_gateway.incremental import InputBatch
from bench_gateway.task_cards import render

class ConfirmationFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg={'project_root':self.tmp.name,'state_root':str(Path(self.tmp.name)/'state'),'mode':'simulate','live_enabled':False,
                  'require_material_confirmation':True,'auto_execute_on_complete':True,'card_callbacks_enabled':True,
                  'unified_status_cards':True,'task_cards_enabled':True,'notifications_enabled':True,'feishu_app_id':'cli_test','cc_project':'bench'}
        self.svc=Service(self.cfg);self.store=self.svc.store;self.user='ou_confirm_flow';self.session='feishu:oc_test:'+self.user
        self.store.allow(self.user);self.tasks=Tasks(self.store);self.intake=Intake(self.store)
        self.intake.update(self.user,{'package':'A','camera_so':'C','lidar_so':'L','m100_so':'M','scene_scope':'1','goal':'replay'})
        self.flow=AutoFlow(self.cfg,self.store);self.flow.arm(self.user,self.session)
        self.task=self.tasks.current(self.user);self.ui=card_ui.ensure(self.store,self.task)
        with self.store.connect() as c:c.execute("UPDATE input_cards SET message_id='om_flow_card' WHERE task_id=?",(self.task['id'],))
    def event(self,revision=None):return {'event_id':'ev-'+str(time.time_ns()),'message_id':'om_flow_card','action':'confirm_execution','value':{'nonce':self.ui['nonce'],'revision':self.task['revision'] if revision is None else revision},'form_value':{}}
    def count(self,table):
        with self.store.connect() as c:return c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    def test_new_material_and_start_text_only_show_card_never_prepare_or_execute(self):
        self.flow.tick();self.assertEqual(0,self.count('preparations'));self.assertEqual(0,self.count('jobs'))
        self.svc.handle(self.session,'执行回灌',{'action':'replay'});self.flow.tick()
        self.assertEqual(0,self.count('preparations'));self.assertEqual(0,self.count('jobs'))
        self.assertTrue(self.svc.material_card)
    def test_direct_check_and_direct_store_submit_cannot_bypass_button(self):
        with self.assertRaisesRegex(GatewayError,'确认执行'):PreparationQueue(self.cfg,self.store).submit(self.user,self.session)
        with self.assertRaisesRegex(GatewayError,'确认执行'):self.store.submit(self.user,self.session,'raw',{'action':'replay'})
        self.svc.handle(self.session,'检查材料',{'action':'check_materials'});self.assertEqual(0,self.count('preparations'))
    def test_callback_excludes_itself_but_worker_waits_for_its_completion(self):
        batch=InputBatch(self.store,self.user);self.svc.input_batch_token=batch.token
        answer=card_ui.handle(self.svc,self.user,self.session,self.event());self.assertIn('已确认',answer)
        self.flow.tick();self.assertEqual(0,self.count('preparations'))
        batch.finish(True);self.flow.tick();self.assertEqual(1,self.count('preparations'));self.assertEqual(0,self.count('jobs'))
    def test_new_file_after_confirmation_requires_new_confirmation(self):
        card_ui.handle(self.svc,self.user,self.session,self.event());self.flow.tick()
        self.intake.update(self.user,{'lidar_so':'L2'});self.flow.arm(self.user,self.session)
        self.flow.tick();self.assertEqual(1,self.count('preparations'));self.assertEqual(0,self.count('jobs'))
        with self.store.connect() as c:self.assertIsNone(confirmations.active(c,self.user,self.tasks.current(self.user),self.session))
        answer=card_ui.handle(self.svc,self.user,self.session,self.event())
        self.assertIn('旧卡',answer);self.assertEqual(0,self.count('jobs'))
    def test_preparation_does_not_invalidate_confirmation_and_queue_consumes_it_once(self):
        card_ui.handle(self.svc,self.user,self.session,self.event());self.flow.tick()
        task=self.tasks.current(self.user)
        with self.store.connect() as c:
            self.assertIsNotNone(confirmations.active(c,self.user,task,self.session))
            c.execute("UPDATE preparations SET state='STATIC_PASSED',result=? WHERE id=?",(json.dumps({'adapter_binding':{'state':'READY_FOR_GATE'}}),task['prep_id']))
        expected={'task_id':task['id'],'revision':task['revision'],'fingerprint':fingerprint(task['data']),'preparation_id':task['prep_id']}
        job,_=self.store.submit(self.user,self.session,'one-confirmed',{'action':'replay','scenes':[1]},expected_task=expected)
        again,duplicate=self.store.submit(self.user,self.session,'one-confirmed',{'action':'replay','scenes':[1]},expected_task=expected)
        self.assertEqual(job['id'],again['id']);self.assertTrue(duplicate);self.assertEqual(1,self.count('jobs'))
        with self.store.connect() as c:self.assertEqual('CONSUMED',c.execute('SELECT state FROM material_confirmations').fetchone()[0])
    def test_material_card_is_short_and_confirmation_is_primary_action(self):
        with self.store.connect() as c:row=dict(c.execute('SELECT * FROM input_cards WHERE task_id=?',(self.task['id'],)).fetchone())
        value=input_cards.snapshot(self.cfg,self.store,row);card=render(value);raw=json.dumps(card,ensure_ascii=False)
        self.assertTrue(value['confirm_ready']);self.assertIn('确认执行',raw);self.assertNotIn('软件部署',raw)
        self.assertLessEqual(len(card['body']['elements']),5);self.assertEqual('台架评测',card['header']['title']['content'])
        self.assertNotIn('pending_uploads',json.dumps(card_ui.material_form({**value['card_ui'],'editing':1})))

if __name__=='__main__':unittest.main()
