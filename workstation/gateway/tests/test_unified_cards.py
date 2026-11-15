import io
import json
import os
import time
import unittest
from unittest.mock import Mock,patch

import test_bindings as fixtures
from bench_gateway import input_cards,task_cards,cc_agent
from bench_gateway.autoflow import AutoFlow
from bench_gateway.core import GatewayError
from bench_gateway.intake import Intake
from bench_gateway.tasks import Tasks
from bench_gateway.worker import deliver as deliver_all
from bench_gateway.progress import notify_input,notice


class UnifiedCardTests(unittest.TestCase):
    def setUp(self):
        self.c=fixtures.BindingTests();self.c.setUp();self.addCleanup(self.c.tearDown)
        c=self.c;c.cfg.update(unified_status_cards=True,task_cards_enabled=True,notifications_enabled=True,
            auto_execute_on_complete=True,intake_settle_seconds=0,cc_project='bench',feishu_app_id='cli_cards_test')
        self.flow=AutoFlow(c.cfg,c.store);self.tasks=Tasks(c.store);self.intake=Intake(c.store)
        self.client=Mock();self.number=0
        def api(path,**kwargs):
            if path.startswith('/im/v1/messages?'):self.number+=1
            return {'data':{'message_id':'om_card_'+str(self.number)}}
        self.client._json.side_effect=api
        self.client_patch=patch('bench_gateway.task_cards._client',return_value=self.client);self.client_patch.start();self.addCleanup(self.client_patch.stop)

    def arm(self):self.flow.arm(self.c.user,self.c.session)

    def row(self):
        with self.c.store.connect() as c:return dict(c.execute('SELECT * FROM input_cards ORDER BY rowid DESC LIMIT 1').fetchone())

    def publish(self):return input_cards.deliver(self.c.cfg,self.c.store,client=self.client)

    def expire(self):
        with self.c.store.connect() as c:c.execute('UPDATE input_cards SET last_sent=0,next_attempt=0')

    def bind(self):
        c=self.c;b=c.bind();c.change_result(lambda r:r.update(adapter_binding={'state':'READY_FOR_GATE','baseline_name':b['name']}));return b

    def requests(self,method):return [x for x in self.client._json.call_args_list if x.kwargs.get('method','POST')==method]

    def test_collecting_and_checking_patch_one_card(self):
        c=self.c;self.arm();self.publish();first=self.row()['message_id']
        self.flow.tick();self.publish()
        self.assertEqual(first,self.row()['message_id']);self.assertEqual(1,self.number)
        self.assertEqual('CHECKING',self.row()['state'])
        card=json.loads(self.client._json.call_args.kwargs['body'])
        self.assertIn('检查材料',card['content']);self.assertNotIn('0/0',json.loads(card['content'])['body']['elements'][0]['columns'][0]['elements'][1]['content'])

    def test_missing_inputs_are_named_without_requiring_new_task_or_scope(self):
        c=self.c;self.intake.update(c.user,{'package':'https://example.test/A.app.tar.gz'},new=True)
        self.arm();self.publish();raw=json.loads(self.client._json.call_args.kwargs['body'])['content']
        self.assertIn('待补充',raw);self.assertIn('libxcamera.so',raw);self.assertIn('libxlidar.so',raw)
        self.assertNotIn('新建任务',raw);self.assertNotIn('task-',raw)

    def test_failure_then_complete_materials_starts_a_new_card(self):
        c=self.c;self.arm()
        with c.store.connect() as db:db.execute("UPDATE automatic_tasks SET state='BLOCKED',error='雷达 SO 不匹配，请提供配套版本。'")
        self.publish();old=self.row()['message_id'];self.assertEqual(1,self.row()['reopen'])
        self.intake.update(c.user,{'lidar_so':'replacement-lidar'});self.arm();self.publish()
        row=self.row();self.assertNotEqual(old,row['message_id']);self.assertEqual(2,self.number)
        self.assertEqual(old,json.loads(row['history'])[-1]['message_id'])

    def test_partial_supplement_does_not_lose_the_reopen_requirement(self):
        c=self.c;self.intake.update(c.user,{'package':'A'},new=True);self.arm()
        with c.store.connect() as db:db.execute("UPDATE automatic_tasks SET state='BLOCKED',error='缺少配套依赖。'")
        self.publish();old=self.row()['message_id']
        self.intake.update(c.user,{'camera_so':'camera'});self.arm();self.publish()
        self.assertEqual(old,self.row()['message_id']);self.assertEqual(1,self.row()['reopen'])
        self.intake.update(c.user,{'lidar_so':'lidar','m100_so':'M100'});self.arm();self.publish()
        self.assertNotEqual(old,self.row()['message_id']);self.assertEqual(2,self.number)

    def test_admission_hands_same_message_to_execution_and_normal_notices_are_silent(self):
        c=self.c;self.bind();self.arm();self.flow.tick();self.publish();mid=self.row()['message_id']
        self.flow.tick();task=self.tasks.current(c.user);self.assertTrue(task['job_id'])
        sent=[];deliver_all(c.cfg,c.store,sender=lambda r:sent.append(r) or True)
        with c.store.connect() as db:card=db.execute('SELECT message_id FROM task_cards WHERE job=?',(task['job_id'],)).fetchone()
        self.assertEqual(mid,card['message_id']);self.assertEqual(1,self.number);self.assertEqual([],sent)
        job=c.store.claim();c.store.stage(job,'小场景预检中');notice(c.store,job,'software-ready','刷包完成')
        deliver_all(c.cfg,c.store,sender=lambda r:sent.append(r) or True)
        c.store.finish(job,'SUCCEEDED',{'task':'/data/tasks/test','state':'PASS'})
        deliver_all(c.cfg,c.store,sender=lambda r:sent.append(r) or True)
        self.assertEqual([],sent);self.assertEqual(1,self.number)
        self.assertIn('结果目录',json.loads(self.client._json.call_args.kwargs['body'])['content'])

    def test_inflight_creation_is_not_duplicated_when_admission_races(self):
        c=self.c;self.bind();self.arm();self.flow.tick();api=self.client._json.side_effect
        def admit(path,**kwargs):
            self.flow.tick();return api(path,**kwargs)
        self.client._json.side_effect=admit;self.publish()
        self.client._json.side_effect=api;task=self.tasks.current(c.user)
        task_cards.deliver(c.cfg,c.store,client=self.client)
        self.assertEqual(1,self.number)
        with c.store.connect() as db:self.assertEqual(self.row()['message_id'],db.execute('SELECT message_id FROM task_cards WHERE job=?',(task['job_id'],)).fetchone()[0])

    def test_execution_waits_for_an_inflight_material_patch(self):
        c=self.c;self.bind();self.arm();self.flow.tick();self.publish();self.flow.tick();task=self.tasks.current(c.user)
        with c.store.connect() as db:db.execute('UPDATE input_cards SET lease_until=?',(time.time()+20,))
        before=self.client._json.call_count;task_cards.deliver(c.cfg,c.store,client=self.client)
        self.assertEqual(before,self.client._json.call_count)
        with c.store.connect() as db:db.execute('UPDATE input_cards SET lease_until=0')
        task_cards.deliver(c.cfg,c.store,client=self.client);self.assertEqual(1,self.number)

    def test_failed_card_calls_reuse_uuid_and_fall_back_once(self):
        c=self.c;self.arm();self.client._json.side_effect=ConnectionError('offline');sent=[]
        for _ in range(3):
            self.expire();input_cards.deliver(c.cfg,c.store,client=self.client,fallback=lambda r:sent.append(r) or True)
        self.assertEqual(1,len(sent));self.assertEqual([],c.store.jobs(c.user))
        keys=[json.loads(call.kwargs['body'])['uuid'] for call in self.client._json.call_args_list]
        self.assertEqual(1,len(set(keys)))

    def test_owner_and_private_session_must_match(self):
        c=self.c
        with self.assertRaises(GatewayError):input_cards.ensure(c.cfg,c.store,c.other,c.session)
        self.client._json.assert_not_called()

    def test_facade_sends_no_reply_only_after_card_acknowledgement(self):
        c=self.c;output=io.StringIO()
        with patch('bench_gateway.cc_agent.load',return_value=c.cfg),patch.dict(os.environ,{'CC_PROJECT':'bench','CC_SESSION_KEY':c.session}),patch('sys.stdin',io.StringIO('软件包：https://example.test/new.app.tar.gz')),patch('sys.stdout',output):cc_agent.main()
        answer=json.loads(output.getvalue().splitlines()[-2])['item']['text']
        self.assertEqual('NO_REPLY',answer);self.assertEqual(1,self.number);self.assertEqual([],c.store.jobs(c.user))

    def test_failed_card_keeps_readable_facade_reply(self):
        c=self.c;output=io.StringIO();self.client._json.side_effect=ConnectionError('offline')
        with patch('bench_gateway.cc_agent.load',return_value=c.cfg),patch.dict(os.environ,{'CC_PROJECT':'bench','CC_SESSION_KEY':c.session}),patch('sys.stdin',io.StringIO('软件包：https://example.test/new.app.tar.gz')),patch('sys.stdout',output):cc_agent.main()
        answer=json.loads(output.getvalue().splitlines()[-2])['item']['text']
        self.assertNotEqual('NO_REPLY',answer);self.assertIn('软件',answer)

    def test_idle_missing_material_card_does_not_refresh_a_timer_forever(self):
        c=self.c;self.intake.update(c.user,{'package':'A'},new=True);self.arm();self.publish();before=self.client._json.call_count
        self.expire();self.publish()
        self.assertEqual(before,self.client._json.call_count)

    def test_automatic_evaluation_file_is_sent_without_duplicate_completion_text(self):
        c=self.c;c.cfg.update(workstation_evaluation_enabled=True,default_evaluator={'argv':['reviewed-test-evaluator']})
        self.intake.update(c.user,{'goal':'replay_and_evaluate'});old=c.preparation['id'];c.preparation=c.make_preparation()
        c.receipt['path']=c.receipt['path'].replace(old,c.preparation['id'])
        with patch('bench_gateway.workstation_evaluation.validate_descriptor'):
            self.bind();self.arm();self.flow.tick();self.publish();self.flow.tick();job=c.store.claim()
        c.store.complete_replay(job,{'state':'PASS','task':'/data/tasks/test'})
        child=c.store.claim('postprocess');c.store.finish(child,'SUCCEEDED',{'state':'PASS','task':'/data/tasks/test','report':'/data/report.txt','local_report':'C:/test/report.txt'})
        sent=[];deliver_all(c.cfg,c.store,sender=lambda r:sent.append(r) or True)
        attachments=[x for x in sent if x.get('attachment')]
        self.assertEqual(1,len(attachments));self.assertEqual(1,attachments[0]['text_sent'])
        self.assertFalse(any(not x.get('attachment') for x in sent))

    def test_archiving_status_cannot_retrigger_a_completed_job(self):
        c=self.c;self.bind();self.arm();self.flow.tick();self.publish();self.flow.tick();job=c.store.claim()
        digest=job['payload']['baseline']['data']['binding']['input_digest']
        c.store.finish(job,'SUCCEEDED',{'state':'PASS','task':'/data/tasks/test','assembly_pending':True,'run_id':job['id'],'input_digest':digest,
            'smoke':{'state':'PASS','run_id':job['id']+'-smoke','input_digest':digest}})
        deliver_all(c.cfg,c.store,sender=Mock(return_value=True))
        current=c.store.job(c.user,job['id']);before=json.dumps(current['payload'],sort_keys=True)
        for _ in range(3):deliver_all(c.cfg,c.store,sender=Mock(return_value=True))
        self.assertEqual(1,len(c.store.jobs(c.user)));self.assertEqual(before,json.dumps(c.store.job(c.user,job['id'])['payload'],sort_keys=True))

    def test_input_card_escapes_untrusted_display_text(self):
        c=self.c;self.intake.update(c.user,{'software_name':'<at id=all>**bad**'});self.arm();self.publish()
        request=json.loads(self.client._json.call_args.kwargs['body']);card=json.loads(request['content'])
        self.assertLessEqual(len(card['body']['elements']),5);self.assertNotIn('<at id=all>',json.dumps(card['body'],ensure_ascii=False))
        self.assertIn('1,2',json.dumps(card['body'],ensure_ascii=False))

    def test_notification_budget_does_not_count_as_card_api_failure(self):
        c=self.c;self.arm()
        with patch('bench_gateway.input_cards.time.monotonic',side_effect=[0,0,1]):
            input_cards.deliver(c.cfg,c.store,client=self.client,deadline=.5)
        self.assertEqual(0,self.row()['attempts']);self.assertEqual(0,self.row()['lease_until'])
        self.client._json.assert_not_called()

    def test_terminal_api_outage_sends_one_complete_fallback_result(self):
        c=self.c;self.bind();self.arm();self.flow.tick();self.publish();self.flow.tick();job=c.store.claim()
        c.store.finish(job,'SUCCEEDED',{'state':'PASS','task':'/data/tasks/test'})
        self.client._json.side_effect=ConnectionError('offline');sent=[]
        for _ in range(4):
            with c.store.connect() as db:db.execute('UPDATE task_cards SET next_attempt=0')
            deliver_all(c.cfg,c.store,sender=lambda r:sent.append(r) or True)
        self.assertEqual(1,len(sent));self.assertIn('/data/tasks/test',sent[0]['text'])


if __name__=='__main__':unittest.main()
