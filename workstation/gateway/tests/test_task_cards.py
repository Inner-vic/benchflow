import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from bench_gateway.core import Store
from bench_gateway.progress import event,deliver as progress_deliver
from bench_gateway.task_cards import deliver,snapshot,render


class TaskCardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name));self.owner='ou_card_alice';self.store.allow(self.owner)
        self.cfg={'task_cards_enabled':True,'notifications_enabled':True,'feishu_app_id':'cli_test','project_root':self.temp.name}
        self.job=self.store.submit(self.owner,'feishu:oc_card:'+self.owner,'one',{'action':'replay','scenes':list(range(1,19)),
            'business_contract':{'software_name':'软件包A'},'assets':{'dataset':{'name':'共享18场景'}}})[0]
        self.client=Mock();self.client._json.return_value={'data':{'message_id':'om_one_card'}}

    def emit(self,kind,**kw):
        event(self.store,self.owner,self.job['id'],self.job['id'],{'schema_version':1,'run_id':self.job['id'],'kind':kind,**kw})

    def test_create_then_patch_same_card_with_owner_private_chat(self):
        self.assertEqual(1,deliver(self.cfg,self.store,client=self.client))
        request=json.loads(self.client._json.call_args.kwargs['body'])
        self.assertEqual('oc_card',request['receive_id']);self.assertEqual('interactive',request['msg_type'])
        self.store.claim();self.store.stage(self.job,'正式回灌中');self.emit('BATCH',completed=3,total=18)
        self.assertEqual(1,deliver(self.cfg,self.store,client=self.client))
        self.assertEqual('/im/v1/messages/om_one_card',self.client._json.call_args.args[0])
        self.assertEqual('PATCH',self.client._json.call_args.kwargs['method'])
        creates=[c for c in self.client._json.call_args_list if c.args[0].startswith('/im/v1/messages?')]
        self.assertEqual(1,len(creates))

    def test_late_batch_updates_count_but_does_not_regress_restore_phase(self):
        self.store.claim();self.store.stage(self.job,'正式回灌中')
        self.emit('PHASE',phase='收集工作站预测');self.emit('BATCH',completed=18,total=18)
        value=snapshot(self.store,self.store.job(self.owner,self.job['id']))
        self.assertEqual('回灌收尾',value['label']);self.assertIn('收集结果',value['detail']);self.assertEqual(18,value['completed'])
        self.emit('BATCH',completed=2,total=18)
        self.assertEqual(18,snapshot(self.store,self.store.job(self.owner,self.job['id']))['completed'])

    def test_regular_progress_is_coalesced_and_critical_milestones_remain(self):
        self.store.claim();self.store.stage(self.job,'小场景预检中');self.store.stage(self.job,'正式回灌中')
        for i in range(1,19):self.emit('BATCH',completed=i,total=18)
        for phase in ('恢复正式服务','核验恢复','收集工作站预测'):self.emit('PHASE',phase=phase)
        texts=[];progress_deliver(self.store,lambda r:texts.append(r['text']) or True,cards=True)
        self.assertEqual(2,len(texts));self.assertTrue(any('小场景测试通过' in t for t in texts))
        self.assertFalse(any('仍在执行' in t or '收集工作站预测中' in t for t in texts))

    def test_terminal_change_during_send_is_not_consumed_by_old_ack(self):
        self.store.claim();self.store.stage(self.job,'正式回灌中')
        def finish(*args,**kwargs):
            self.store.finish(self.job,'SUCCEEDED',{'task':'/data/tasks/'+self.job['id']})
            return {'data':{'message_id':'om_one_card'}}
        self.client._json.side_effect=finish
        deliver(self.cfg,self.store,client=self.client)
        self.client._json.side_effect=None
        deliver(self.cfg,self.store,client=self.client)
        body=json.loads(self.client._json.call_args.kwargs['body'])
        self.assertIn('回灌完成',body['content']);self.assertEqual('PATCH',self.client._json.call_args.kwargs['method'])

    def test_failed_updates_fall_back_to_text_without_touching_job(self):
        self.client._json.side_effect=ConnectionError('offline');before=self.store.job(self.owner,self.job['id']);sent=[]
        for _ in range(3):
            with self.store.connect() as c:c.execute('UPDATE task_cards SET next_attempt=0')
            deliver(self.cfg,self.store,client=self.client,fallback=lambda r:sent.append(r) or True)
        self.assertEqual(1,len(sent));self.assertEqual(before,self.store.job(self.owner,self.job['id']))
        uuids=[json.loads(c.kwargs['body'])['uuid'] for c in self.client._json.call_args_list]
        self.assertEqual(1,len(set(uuids)))

    def test_identity_mismatch_never_calls_message_api(self):
        with self.store.connect() as c:c.execute('UPDATE jobs SET session=? WHERE id=?',('feishu:oc_other:ou_someone_else',self.job['id']))
        deliver(self.cfg,self.store,client=self.client);self.client._json.assert_not_called()

    def test_disconnect_updates_original_card_and_recovery_keeps_job_queued(self):
        from bench_gateway.queue_status import QueueStatus
        job=self.store.job(self.owner,self.job['id']);deliver(self.cfg,self.store,client=self.client)
        queue=QueueStatus(self.store)
        queue.resource({'state':'BENCH_UNREACHABLE','checked_at':time.time()})
        value=snapshot(self.store,job)
        self.assertEqual(value['label'],'等待台架连接');self.assertEqual(value['tone'],'orange')
        self.assertIn('现场同事',value['action']);self.assertIn('物理连接',value['action'])
        with patch('bench_gateway.task_cards.time.time',return_value=time.time()+31):
            deliver(self.cfg,self.store,client=self.client)
        self.assertEqual(self.client._json.call_args.args[0],'/im/v1/messages/om_one_card')
        self.assertEqual(self.client._json.call_args.kwargs['method'],'PATCH')
        queue.resource({'probe_kind':'ssh-readonly-v1','state':'READY','workstation_ssh':'PASS','bench_ssh':'PASS','checked_at':time.time()})
        value=snapshot(self.store,self.store.job(self.owner,job['id']))
        self.assertEqual(value['label'],'就绪排队');self.assertEqual(value['tone'],'blue')
        self.assertEqual(self.store.job(self.owner,job['id']),job)

    def test_workstation_auth_and_unknown_runner_are_distinct_wait_states(self):
        from bench_gateway.queue_status import QueueStatus
        for state,label in [('WORKSTATION_UNAVAILABLE','等待工作站连接'),('BENCH_SSH_FAILED','台架连接待处理'),('STATUS_UNAVAILABLE','台架状态待核验')]:
            QueueStatus(self.store).resource({'state':state,'checked_at':time.time()})
            value=snapshot(self.store,self.store.job(self.owner,self.job['id']))
            self.assertEqual(value['label'],label)
            if state=='WORKSTATION_UNAVAILABLE':
                self.assertIn('尚不能判断',value['action']);self.assertNotIn('请联系现场同事',value['action'])

    def test_queue_card_shows_only_waiting_ahead_count_and_current_occupant(self):
        # Existing job is first and runs; two later owners wait in FIFO order.
        self.store.claim();other='ou_card_waiter';self.store.allow(other)
        self.store.submit(other,'feishu:oc_other:'+other,'second',{'action':'replay','secret':'other-private'})
        own,_=self.store.submit(self.owner,'feishu:oc_card:'+self.owner,'third',{'action':'replay','scenes':[1]})
        value=snapshot(self.store,own)
        self.assertEqual('前方 1 项等待。',value['action'])
        self.assertIn('当前使用：',value['queue_text'])
        self.assertNotIn('排队第',value['action']);self.assertNotIn('项执行',value['action'])
        self.assertNotIn('other-private',json.dumps(render(value),ensure_ascii=False))

    def test_stale_or_invalid_connection_evidence_does_not_claim_current_disconnect(self):
        for stamp in (time.time()-120,time.time()+120,'invalid',float('nan')):
            with self.store.connect() as c:
                c.execute('INSERT OR REPLACE INTO resource_status VALUES(1,?)',(json.dumps({'state':'BENCH_UNREACHABLE','checked_at':stamp}),))
            value=snapshot(self.store,self.store.job(self.owner,self.job['id']))
            self.assertEqual(value['label'],'就绪排队');self.assertEqual(value['tone'],'blue')

    def test_dispatched_ssh_loss_explains_review_without_automatic_resume(self):
        from bench_gateway.queue_status import QueueStatus
        self.store.claim()
        self.store.finish(self.job,'NEEDS_REVIEW',{'error':'SSH 已断开，远端执行结果未知；请核验后续接，不自动重跑'})
        QueueStatus(self.store).resource({'probe_kind':'ssh-readonly-v1','state':'READY','workstation_ssh':'PASS','bench_ssh':'PASS','checked_at':time.time()})
        job=self.store.job(self.owner,self.job['id']);value=snapshot(self.store,job)
        self.assertEqual(value['label'],'执行连接中断，待核验');self.assertEqual(value['tone'],'red')
        self.assertIn('不会自动重跑',value['action']);self.assertEqual(job['state'],'NEEDS_REVIEW')

    def test_card_has_five_compact_blocks_and_escapes_material_names(self):
        job=self.store.job(self.owner,self.job['id']);job['payload']['business_contract']['software_name']='<at id=all>**fake**'
        value=snapshot(self.store,job);value['dataset']='<at id=all>bad'
        card=render(value);raw=json.dumps(card,ensure_ascii=False)
        self.assertEqual('2.0',card['schema']);self.assertEqual(5,len(card['body']['elements']))
        self.assertEqual('plain_text',card['header']['title']['tag'])
        self.assertNotIn('<at id=all>',json.dumps(card['body'],ensure_ascii=False));self.assertLess(len(raw.encode()),30000)

    def test_elapsed_updates_are_throttled_and_terminal_card_is_stable(self):
        deliver(self.cfg,self.store,client=self.client)
        self.assertEqual(0,deliver(self.cfg,self.store,client=self.client))
        self.store.finish(self.job,'SUCCEEDED',{'task':'/data/tasks/'+self.job['id'],'assembly_pending':False})
        deliver(self.cfg,self.store,client=self.client);calls=self.client._json.call_count
        self.assertEqual(0,deliver(self.cfg,self.store,client=self.client));self.assertEqual(calls,self.client._json.call_count)
