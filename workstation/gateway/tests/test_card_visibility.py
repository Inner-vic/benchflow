import io,json,os,time,unittest
from unittest.mock import Mock,patch
import test_confirmation_flow as flowfixtures
from bench_gateway import input_cards,card_ui

class CardVisibilityTests(unittest.TestCase):
    event=flowfixtures.ConfirmationFlowTests.event
    def count(self,table):
        with self.store.connect() as c:
            return c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() else 0
    def setUp(self):
        flowfixtures.ConfirmationFlowTests.setUp(self);self.posts=[];self.client=Mock()
        def api(path,**kwargs):
            if path.startswith('/im/v1/messages?'):
                self.posts.append(json.loads(kwargs['body']));return {'data':{'message_id':'om_visible_'+str(len(self.posts))}}
            return {'data':{}}
        self.client._json.side_effect=api
        self.client_patch=patch('bench_gateway.task_cards._client',return_value=self.client);self.client_patch.start();self.addCleanup(self.client_patch.stop)
        with self.store.connect() as c:c.execute("UPDATE input_cards SET message_id=NULL,digest='' WHERE task_id=?",(self.task['id'],))
    def row(self):
        with self.store.connect() as c:return dict(c.execute('SELECT * FROM input_cards WHERE task_id=?',(self.task['id'],)).fetchone())
    def present(self,force=False):return input_cards.present(self.cfg,self.store,self.user,self.session,surface=force)
    def test_explicit_entry_creates_visible_card_without_creating_task_or_job(self):
        self.assertTrue(self.present());old=self.row()['message_id'];self.assertTrue(self.present(True))
        row=self.row();self.assertNotEqual(old,row['message_id']);self.assertEqual(2,len(self.posts));self.assertEqual(1,row['generation'])
        self.assertEqual(old,json.loads(row['history'])[-1]['message_id'])
        self.assertEqual(0,self.count('jobs'));self.assertEqual(0,self.count('preparations'))
        retired=next(json.loads(x.kwargs['body'])['content'] for x in self.client._json.call_args_list if x.args[0].endswith(old))
        self.assertIn('此卡片已停用',retired);self.assertNotIn('behaviors',retired)
    def test_continuous_materials_update_current_card(self):
        self.present();old=self.row()['message_id']
        self.intake.update(self.user,{'lidar_so':'L2'});self.flow.arm(self.user,self.session)
        self.assertTrue(self.present());self.assertEqual(old,self.row()['message_id']);self.assertEqual(1,len(self.posts))
    def test_forwarded_build_card_immediately_surfaces_even_within_idle_window(self):
        from bench_gateway import cc_agent
        self.present();old=self.row()['message_id'];out=io.StringIO()
        packet='@@BENCH_SOURCE@@unit-test'
        with patch('bench_gateway.cc_agent.load',return_value=self.cfg),patch.dict(os.environ,{'CC_PROJECT':'bench','CC_SESSION_KEY':self.session}),patch('sys.stdin',io.StringIO(packet)),patch('sys.stdout',out),patch('bench_gateway.build_messages.decode',return_value={'message_id':'om_forwarded_fixture','content':'LIVC APP: A'}),patch('bench_gateway.build_messages.extract',return_value=({'package':'A','build_reference':'https://ci.example.test/job/app/2737/'},'')),patch('bench_gateway.cc_agent.parse') as model:
            cc_agent.main();model.assert_not_called()
        self.assertNotEqual(old,self.row()['message_id']);self.assertEqual(2,len(self.posts))
        self.assertEqual(0,self.count('jobs'));self.assertEqual(0,self.count('preparations'))
    def test_polling_never_resurfaces_but_material_after_idle_does(self):
        self.present();old=self.row()['message_id']
        with self.store.connect() as c:c.execute('UPDATE input_cards SET last_activity=?,last_sent=0',(time.time()-1801,))
        input_cards.deliver(self.cfg,self.store,client=self.client);self.assertEqual(old,self.row()['message_id'])
        self.assertTrue(self.present());self.assertNotEqual(old,self.row()['message_id']);self.assertEqual(2,len(self.posts))
    def test_legacy_card_is_surfaced_on_first_visit(self):
        self.present();old=self.row()['message_id']
        with self.store.connect() as c:c.execute('UPDATE input_cards SET last_activity=0')
        self.present();self.assertNotEqual(old,self.row()['message_id'])
    def test_failed_new_card_retries_same_generation_and_uuid(self):
        self.present();real=self.client._json.side_effect;failed=[]
        def fail_once(path,**kwargs):
            if path.startswith('/im/v1/messages?') and not failed:
                failed.append(json.loads(kwargs['body'])['uuid']);raise ConnectionError()
            return real(path,**kwargs)
        self.client._json.side_effect=fail_once;self.assertFalse(self.present(True));self.assertEqual(1,self.row()['generation'])
        with self.store.connect() as c:c.execute('UPDATE input_cards SET next_attempt=0')
        self.assertTrue(self.present());self.assertEqual(1,self.row()['generation']);self.assertEqual(failed[0],self.posts[-1]['uuid'])
    def test_replaced_card_callback_cannot_confirm(self):
        self.present();old=self.row()['message_id'];self.present(True)
        event=self.event();event['message_id']=old
        self.assertIn('已被替换',card_ui.handle(self.svc,self.user,self.session,event))
        self.assertEqual(0,self.count('jobs'));self.assertEqual(0,self.count('preparations'))
    def test_confirmed_check_does_not_move_card(self):
        self.present();event=self.event();event['message_id']=self.row()['message_id'];card_ui.handle(self.svc,self.user,self.session,event)
        old=self.row()['message_id'];self.present(True);self.assertEqual(old,self.row()['message_id'])
    def test_duplicate_file_receipt_uses_current_card_instead_of_extra_reply(self):
        from bench_gateway import cc_agent
        self.present();old=self.row()['message_id'];out=io.StringIO()
        with patch('bench_gateway.cc_agent.load',return_value=self.cfg),patch.dict(os.environ,{'CC_PROJECT':'bench','CC_SESSION_KEY':self.session}),patch('sys.stdin',io.StringIO('attachment intake fixture')),patch('sys.stdout',out),patch('bench_gateway.attachments.Attachments.prompt',return_value='已接收，内容一致'),patch('bench_gateway.cc_agent.parse') as model:
            cc_agent.main();model.assert_not_called()
        events=[json.loads(s) for s in out.getvalue().splitlines()]
        answer=next(x['item']['text'] for x in events if x['type']=='item.completed')
        self.assertEqual('NO_REPLY',answer);self.assertEqual(old,self.row()['message_id']);self.assertEqual(1,len(self.posts))
    def test_inflight_card_update_is_awaited_without_duplicate_receipt(self):
        self.present();old=self.row()['message_id'];card_ui.feedback(self.store,self.user,self.task['id'],'已接收 libxlidar.so')
        with self.store.connect() as c:c.execute('UPDATE input_cards SET lease_until=?',(time.time()+20,))
        def finish_update(_):
            import hashlib
            from bench_gateway.task_cards import render
            row=self.row();v=input_cards.snapshot(self.cfg,self.store,row)
            digest=hashlib.sha256(json.dumps(render(v),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            with self.store.connect() as c:c.execute('UPDATE input_cards SET digest=?,lease_until=0,last_sent=?',(digest,time.time()))
        with patch('bench_gateway.input_cards.deliver'),patch('bench_gateway.input_cards.time.sleep',side_effect=finish_update):
            self.assertTrue(self.present())
        self.assertEqual(old,self.row()['message_id']);self.assertEqual(1,len(self.posts))
    def test_unacknowledged_feedback_is_not_silently_suppressed(self):
        self.present();card_ui.feedback(self.store,self.user,self.task['id'],'已接收 libxlidar.so')
        with self.store.connect() as c:c.execute('UPDATE input_cards SET lease_until=?',(time.time()+20,))
        def fail_update(_):
            with self.store.connect() as c:c.execute('UPDATE input_cards SET lease_until=0')
        with patch('bench_gateway.input_cards.deliver'),patch('bench_gateway.input_cards.time.sleep',side_effect=fail_update):
            self.assertFalse(self.present())
    def test_duplicate_file_after_completion_gets_short_visible_receipt(self):
        from bench_gateway import cc_agent,confirmations
        confirmations.configure({'require_material_confirmation':False},self.store)
        job,_=self.store.submit(self.user,self.session,'completed-fixture',{'action':'replay','scenes':[1],'assets':{}})
        self.tasks.link(self.user,job);self.store.finish(job,'SUCCEEDED',{'task':'/mnt/results/test'})
        confirmations.configure(self.cfg,self.store);before=self.tasks.current(self.user);out=io.StringIO()
        with patch('bench_gateway.cc_agent.load',return_value=self.cfg),patch.dict(os.environ,{'CC_PROJECT':'bench','CC_SESSION_KEY':self.session}),patch('sys.stdin',io.StringIO('attachment intake fixture')),patch('sys.stdout',out),patch('bench_gateway.attachments.Attachments.prompt',return_value='已接收 libxlidar.so，内容与当前材料一致，未创建重复变更。\n更多材料详情'),patch('bench_gateway.cc_agent.parse') as model:
            cc_agent.main();model.assert_not_called()
        events=[json.loads(s) for s in out.getvalue().splitlines()]
        answer=next(x['item']['text'] for x in events if x['type']=='item.completed')
        self.assertIn('未新增任务或重复执行',answer);self.assertNotIn('NO_REPLY',answer);self.assertNotIn('更多材料详情',answer)
        self.assertEqual(before,self.tasks.current(self.user));self.assertEqual(1,self.count('jobs'))

if __name__=='__main__':unittest.main()
