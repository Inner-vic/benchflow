import base64,copy,hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import Mock
from bench_gateway.core import Store,GatewayError
from bench_gateway import arrival_analysis as a
from bench_gateway.task_cards import snapshot,render

class ArrivalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=Store(self.tmp.name)
        self.user='ou_arrival';self.other='ou_other';self.store.allow(self.user);self.store.allow(self.other)
        self.cfg={'project_root':self.tmp.name,'state_root':self.tmp.name,'feishu_app_id':'cli_test','notifications_enabled':True}
        self.session='feishu:oc_arrival:'+self.user
        self.job=self.store.submit(self.user,self.session,'source',{'action':'replay','scenes':[1],
            'assets':{'dataset':{'owner':'shared','data':{'remote_path':'/datasets/truth'}}}})[0]
        self.store.claim();self.result={'state':'PASS','run_id':self.job['id'],'task':'/mnt/data/bench-eval/tasks/'+self.job['id']}
        self.store.finish(self.job,'SUCCEEDED',self.result);self.job=self.store.job(self.user,self.job['id'])
        self.original=copy.deepcopy(self.job)
        with self.store.connect() as c:c.execute('UPDATE task_cards SET message_id=? WHERE job=?',('om_original',self.job['id']))
    def row(self):
        with self.store.connect() as c:return dict(c.execute('SELECT * FROM arrival_analyses WHERE job=?',(self.job['id'],)).fetchone())
    def success(self,cfg,row):
        req=json.loads(row['request']);raw=b'generated report'
        return {'state':'SUCCEEDED','output':req['source_task']+'/evaluations/arrival-accuracy/'+req['attempt'],
            'scene_count':1,'files':{n:{'data':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)} for n in a.NAMES}}
    def client(self):
        client=Mock();client.message.return_value={'chat_id':'oc_arrival','sender':{'id':'cli_test'}}
        client.upload_report.side_effect=['file_xlsx','file_plan'];client.reply_report.side_effect=['om_xlsx','om_plan'];return client
    def test_viewing_finished_task_never_queues_analysis(self):
        self.assertEqual({'state':'AVAILABLE'},a.view(self.store,self.job))
        a.schema(self.store)
        with self.store.connect() as c:self.assertEqual(0,c.execute('SELECT COUNT(*) FROM arrival_analyses').fetchone()[0])
    def test_owner_and_delivered_source_are_required(self):
        with self.assertRaises(GatewayError):a.enqueue(self.store,self.other,self.job)
        broken=copy.deepcopy(self.job);broken['state']='RUNNING'
        with self.assertRaises(GatewayError):a.enqueue(self.store,self.user,broken)
        broken=copy.deepcopy(self.job);broken['payload']['assets']['dataset']['owner']=self.other
        with self.assertRaises(GatewayError):a.enqueue(self.store,self.user,broken)
    def test_duplicate_click_never_recomputes_or_changes_replay(self):
        first=a.enqueue(self.store,self.user,self.job);second=a.enqueue(self.store,self.user,self.job)
        self.assertEqual(first['id'],second['id'])
        runner=Mock(side_effect=self.success);a.tick(self.cfg,self.store,runner);self.assertIsNone(a.tick(self.cfg,self.store,runner))
        self.assertEqual(1,runner.call_count);self.assertEqual(self.original,self.store.job(self.user,self.job['id']))
        self.assertEqual('SUCCEEDED',self.row()['state'])
    def test_missing_distance_csv_is_separate_from_replay_success(self):
        a.enqueue(self.store,self.user,self.job);a.tick(self.cfg,self.store,lambda *x:{'state':'NEEDS_INPUT','code':'MISSING_DISTANCE_CSV'})
        self.assertIn('未生成距离CSV',a.view(self.store,self.job)['detail'])
        self.assertEqual(self.original,self.store.job(self.user,self.job['id']))
        self.assertIsNone(a.tick(self.cfg,self.store,Mock()))
    def test_unknown_remote_outcome_is_not_retried(self):
        a.enqueue(self.store,self.user,self.job);runner=Mock(side_effect=ConnectionError())
        a.tick(self.cfg,self.store,runner);self.assertEqual('NEEDS_REVIEW',self.row()['state'])
        self.assertIsNone(a.tick(self.cfg,self.store,runner));self.assertEqual(1,runner.call_count)
    def test_interruption_marks_review_without_restarting(self):
        a.enqueue(self.store,self.user,self.job)
        with self.store.connect() as c:c.execute("UPDATE arrival_analyses SET state='RUNNING'")
        a.recover(self.store);self.assertEqual('NEEDS_REVIEW',self.row()['state']);self.assertIsNone(a.tick(self.cfg,self.store))
    def test_reports_reply_to_original_card_and_are_not_resent(self):
        a.enqueue(self.store,self.user,self.job);a.tick(self.cfg,self.store,self.success);client=self.client()
        self.assertEqual(2,a.deliver(self.cfg,self.store,client=client));self.assertEqual(0,a.deliver(self.cfg,self.store,client=client))
        self.assertEqual(2,client.reply_report.call_count)
        for call in client.reply_report.call_args_list:self.assertEqual('om_original',call.args[0])
        self.assertEqual(self.original,self.store.job(self.user,self.job['id']))
    def test_uncertain_attachment_reply_is_not_resent_or_recomputed(self):
        a.enqueue(self.store,self.user,self.job);a.tick(self.cfg,self.store,self.success);client=self.client()
        client.reply_report.side_effect=TimeoutError()
        a.deliver(self.cfg,self.store,client=client);calls=client.reply_report.call_count
        a.recover(self.store);a.deliver(self.cfg,self.store,client=client)
        self.assertEqual(calls,client.reply_report.call_count);self.assertEqual('SUCCEEDED',self.row()['state'])
        self.assertIn('发送待核验',a.view(self.store,self.job)['detail'])
    def test_wrong_original_chat_never_receives_files(self):
        a.enqueue(self.store,self.user,self.job);a.tick(self.cfg,self.store,self.success);client=self.client()
        client.message.return_value={'chat_id':'oc_other','sender':{'id':'cli_test'}}
        self.assertEqual(0,a.deliver(self.cfg,self.store,client=client));client.upload_report.assert_not_called()
    def test_bad_report_hash_is_not_delivered(self):
        a.enqueue(self.store,self.user,self.job)
        def bad(cfg,row):
            r=self.success(cfg,row);r['files'][a.NAMES[0]]['sha256']='0'*64;return r
        a.tick(self.cfg,self.store,bad);self.assertEqual('NEEDS_REVIEW',self.row()['state'])
        client=self.client();self.assertEqual(0,a.deliver(self.cfg,self.store,client=client));client.upload_report.assert_not_called()
    def test_old_sent_reports_do_not_starve_new_delivery(self):
        a.enqueue(self.store,self.user,self.job);a.tick(self.cfg,self.store,self.success)
        for n in range(10):
            old=self.store.submit(self.user,self.session,'older-'+str(n),{'action':'replay','scenes':[1]})[0]
            with self.store.connect() as c:
                c.execute('INSERT INTO arrival_analyses(id,job,owner,session,state,request,reports,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                    ('arr-old-'+str(n),old['id'],self.user,self.session,'SUCCEEDED','{}',
                     json.dumps({name:{'state':'SENT','message_id':'om_old'} for name in a.NAMES}),0,0))
        client=self.client();self.assertEqual(2,a.deliver(self.cfg,self.store,client=client))
        self.assertEqual(2,client.reply_report.call_count)

class ArrivalCallbackTests(unittest.TestCase):
    def setUp(self):
        from test_confirmation_flow import ConfirmationFlowTests
        ConfirmationFlowTests.setUp(self)
        from bench_gateway import confirmations
        confirmations.configure({'require_material_confirmation':False},self.store)
        self.job=self.store.submit(self.user,self.session,'delivered-source',{'action':'replay','scenes':[1],
            'assets':{'dataset':{'owner':'shared','data':{'remote_path':'/datasets/truth'}}}})[0]
        self.tasks.link(self.user,self.job);self.store.claim()
        self.store.finish(self.job,'SUCCEEDED',{'state':'PASS','run_id':self.job['id'],'task':'/mnt/data/bench-eval/tasks/'+self.job['id']})
        self.job=self.store.job(self.user,self.job['id']);self.original=copy.deepcopy(self.job)
        confirmations.configure(self.cfg,self.store)
    def event(self):return {'event_id':'arrival-event-'+str(time.time_ns()),'message_id':'om_flow_card',
        'action':'arrival_accuracy','value':{'nonce':self.ui['nonce']},'form_value':{}}
    def test_real_handler_route_and_duplicate_event_keep_parent_unchanged(self):
        from bench_gateway import card_ui
        event=self.event();text=card_ui.handle(self.svc,self.user,self.session,event)
        self.assertIn('到点精度',text)
        card_ui.handle(self.svc,self.user,self.session,event)
        with self.store.connect() as c:self.assertEqual(1,c.execute('SELECT count(*) FROM arrival_analyses').fetchone()[0])
        self.assertEqual(self.original,self.store.job(self.user,self.job['id']))
    def test_button_only_appears_on_completed_replay_card(self):
        value=snapshot(self.store,self.job,cfg=self.cfg);text=json.dumps(render(value),ensure_ascii=False)
        self.assertIn('计算到点精度',text);self.assertIn('bench:arrival_accuracy',text)
        current=copy.deepcopy(self.job);current.update(state='RUNNING',result={})
        self.assertNotIn('计算到点精度',json.dumps(render(snapshot(self.store,current,cfg=self.cfg)),ensure_ascii=False))
    def test_missing_input_is_prominent_on_original_card(self):
        a.enqueue(self.store,self.user,self.job)
        a.tick(self.cfg,self.store,lambda *x:{'state':'NEEDS_INPUT','code':'MISSING_DISTANCE_CSV'})
        card=render(snapshot(self.store,self.job,cfg=self.cfg))
        panel=card['body']['elements'][0]['columns'][0]
        self.assertIn('到点精度 · 无法计算',panel['elements'][0]['content'])
        self.assertIn('未生成距离CSV',panel['elements'][1]['content'])
        self.assertEqual('yellow-50',panel['background_style'])
        self.assertEqual(self.original,self.store.job(self.user,self.job['id']))

if __name__=='__main__':unittest.main()
