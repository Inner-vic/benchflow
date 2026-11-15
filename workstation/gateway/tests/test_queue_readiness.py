import ast
import json
import os
from pathlib import Path
import threading
import time
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_gateway as fixtures
from bench_gateway.availability import REMOTE_PROBE,probe
from bench_gateway.transfer import SSH
from bench_gateway.queue_status import QueueStatus
from bench_gateway.worker import tick,deliver,run
from bench_gateway.setup import cc_config


class ReadyExecutor:
    def __init__(self,state='READY'):self.state=state;self.executed=[]
    def availability(self,job):return {'state':self.state,'checked_at':time.time()}
    def execute(self,job):
        self.executed.append(job['id'])
        return {'task':'/tasks/'+job['id']}


class QueueTests(unittest.TestCase):
    setUp=fixtures.GatewayTests.setUp
    tearDown=fixtures.GatewayTests.tearDown
    enqueue=fixtures.GatewayTests.enqueue

    def test_global_position_reveals_no_other_user_details(self):
        other,_=self.s.submit(fixtures.B,'feishu:oc_b:'+fixtures.B,'bob-secret',{'action':'replay','secret':'private'})
        own=self.enqueue()
        text=QueueStatus(self.s).text(fixtures.A,own['id'])
        self.assertIn('第 2 位',text);self.assertIn('前面 1 个',text)
        for secret in [fixtures.B,other['id'],'bob-secret','private']:self.assertNotIn(secret,text)

    def test_disconnect_waits_without_claim_and_recovers_in_fifo_order(self):
        a=self.enqueue('one');b=self.enqueue('two');fake=ReadyExecutor('BENCH_UNREACHABLE')
        for _ in range(3):self.assertEqual(tick(self.cfg,self.s,fake),'WAITING_RESOURCE')
        self.assertEqual(fake.executed,[])
        self.assertEqual(self.s.job(fixtures.A,a['id'])['state'],'QUEUED')
        text=QueueStatus(self.s).text(fixtures.A)
        self.assertIn('现场同事',text);self.assertIn('物理连接',text)
        with self.s.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM queue_outbox').fetchone()[0],2)
            self.assertTrue(all(r['version']==1 for r in c.execute('SELECT version FROM queue_outbox')))
        fake.state='READY';tick(self.cfg,self.s,fake);tick(self.cfg,self.s,fake)
        self.assertEqual(fake.executed,[a['id'],b['id']])

    def test_workstation_failure_does_not_claim_physical_bench_disconnect(self):
        self.enqueue();tick(self.cfg,self.s,ReadyExecutor('WORKSTATION_UNAVAILABLE'))
        self.assertIn('尚不能判断',QueueStatus(self.s).text(fixtures.A))

    def test_busy_then_cancel_never_dispatches_cancelled_task(self):
        a=self.enqueue();fake=ReadyExecutor('RESOURCE_BUSY');tick(self.cfg,self.s,fake)
        self.s.cancel(fixtures.A,a['id']);fake.state='READY';tick(self.cfg,self.s,fake)
        self.assertEqual(fake.executed,[])

    def test_unknown_previous_outcome_blocks_reconnection_auto_replay(self):
        a=self.enqueue();self.s.claim();self.s.finish(a,'NEEDS_REVIEW',{'error':'lost during replay'})
        self.enqueue('next');fake=ReadyExecutor();tick(self.cfg,self.s,fake)
        self.assertEqual(fake.executed,[]);self.assertIn('待核验',QueueStatus(self.s).text(fixtures.A))

    def test_running_and_review_do_not_hide_connection_failure(self):
        job=self.enqueue();job=self.s.claim()
        q=QueueStatus(self.s);q.resource({'state':'BENCH_UNREACHABLE','checked_at':time.time()})
        for state in ('RUNNING','NEEDS_REVIEW'):
            if state=='NEEDS_REVIEW':self.s.finish(job,state,{'error':'lost during replay'})
            self.assertIn('无法连接台架',q.text(fixtures.A))

    def test_status_in_simulation_probes_each_query_despite_recent_cache(self):
        self.cfg['resource_probe_enabled']=True
        value={'probe_kind':'ssh-readonly-v1','state':'READY','workstation_ssh':'PASS',
               'bench_ssh':'PASS','runner_state':'IDLE','checked_at':time.time()}
        with patch('bench_gateway.availability.probe',return_value=value) as check:
            for _ in range(2):
                reply=self.service.handle(fixtures.SESSION,'台架状态',{'action':'queue'})
                self.assertIn('本次真实 SSH 只读检查',reply)
                self.assertIn('工作站 → 台架：正常',reply)
                self.assertRegex(reply,r'真实刷包/(?:回放|回灌)尚未启用')
                self.assertNotIn('【模拟模式',reply)
            self.assertEqual(check.call_count,2)

    def test_disabled_probe_does_not_present_cached_result_as_current(self):
        self.cfg['resource_probe_enabled']=False
        with patch('bench_gateway.availability.probe') as check:
            reply=self.service.handle(fixtures.SESSION,'台架状态',{'action':'queue'})
            check.assert_not_called()
        self.assertIn('检查未启用',reply)
        self.assertNotIn('本次真实 SSH',reply)

    def test_background_status_update_cannot_replace_current_measurement(self):
        self.cfg['resource_probe_enabled']=True
        value={'probe_kind':'ssh-readonly-v1','state':'BENCH_SSH_FAILED','workstation_ssh':'PASS',
               'bench_ssh':'HOST_KEY_CHANGED','runner_state':'IDLE','checked_at':time.time()}
        def resource(_self,value=None):return {'state':'READY','checked_at':time.time()}
        with patch('bench_gateway.availability.probe',return_value=value),patch.object(QueueStatus,'resource',resource):
            reply=self.service.handle(fixtures.SESSION,'台架状态',{'action':'queue'})
        self.assertIn('设备指纹与历史记录不一致',reply)
        self.assertNotIn('两段 SSH 链路已通过',reply)

    def test_queue_notice_retry_does_not_execute_task(self):
        a=self.enqueue();q=QueueStatus(self.s);q.publish();self.cfg['notifications_enabled']=True
        self.assertEqual(deliver(self.cfg,self.s,lambda row:False),0)
        sent=[];self.assertEqual(deliver(self.cfg,self.s,lambda row:sent.append(row) or True),1)
        self.assertEqual(sent[0]['session'],fixtures.SESSION)
        q.publish();self.assertEqual(deliver(self.cfg,self.s,lambda row:True),0)
        self.assertEqual(self.s.job(fixtures.A,a['id'])['state'],'QUEUED')

    def test_ack_of_old_queue_notice_does_not_hide_new_update(self):
        a=self.enqueue();q=QueueStatus(self.s);q.notice(a,'old');self.cfg['notifications_enabled']=True
        def sender(row):q.notice(a,'new');return True
        deliver(self.cfg,self.s,sender)
        with self.s.connect() as c:row=c.execute('SELECT * FROM queue_outbox').fetchone()
        self.assertEqual(row['text'],'new');self.assertEqual(row['sent'],0)

    def test_private_only_config_blocks_group_messages_without_user_whitelist(self):
        self.cfg.update(cc_project='test',feishu_app_id='cli_test');(self.root/'config').mkdir()
        text=cc_config(self.cfg).read_text(encoding='utf-8')
        self.assertIn('allow_chat = "__gateway_private_only__"',text)
        self.assertIn('allow_from = "*"',text)

    def test_notice_delivered_while_execution_is_still_running(self):
        self.enqueue();self.cfg.update(notifications_enabled=True,poll_seconds=.01)
        delivered=threading.Event();errors=[];drain=self.root/'runtime/service/drain.json'
        cfg=self.cfg;store=self.s
        class Blocking(ReadyExecutor):
            def __init__(self,*args):super().__init__()
            def execute(self,job):
                ok=delivered.wait(4)
                drain.parent.mkdir(parents=True,exist_ok=True);drain.write_text('{}')
                if not ok:raise AssertionError('start notice was delayed until execution completed')
                return {'task':'/tasks/test'}
        def sender(row):
            if '轮到任务' in row['text']:delivered.set()
            return True
        def runner():
            try:run(cfg,store)
            except Exception as exc:errors.append(exc)
        with patch.dict(os.environ,{'BENCH_GATEWAY_MANAGED':'1'}),patch('bench_gateway.worker.Executor',Blocking),patch('bench_gateway.worker.deliver',lambda cfg,store:deliver(cfg,store,sender)):
            thread=threading.Thread(target=runner);thread.start();thread.join(8)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[]);self.assertTrue(delivered.is_set())


class ProbeTests(unittest.TestCase):
    def remote_probe(self,busy=False,error='',invalid_receipt=False,queue_failed=False,timeout=False,exit_code=255,queue_payload=None):
        calls=[]
        def run(argv,**kw):
            calls.append(argv)
            if argv[0]=='ssh':
                self.assertIn('StrictHostKeyChecking=yes',argv)
                self.assertIn('ControlMaster=no',argv);self.assertIn('ControlPath=none',argv)
                self.assertIn('bench-avm',argv);self.assertEqual(argv[-1],'python3 -')
                if timeout:raise subprocess.TimeoutExpired(argv,12)
                if error:return SimpleNamespace(returncode=exit_code,stdout='',stderr=error)
                tree=ast.parse(kw['input'])
                receipt=tree.body[1].value.args[0].args[0]
                token=ast.literal_eval(receipt.values[0])
                payload={'challenge':'wrong' if invalid_receipt else token,'hostname':'bench-test','system':'Linux'}
                return SimpleNamespace(returncode=0,stdout=json.dumps(payload),stderr='')
            queue={'schema_version':1,'active':{'run_id':'external'} if busy else None,'queued':[],'waiting':[]}
            return SimpleNamespace(returncode=1 if queue_failed else 0,stdout=json.dumps(queue if queue_payload is None else queue_payload),stderr='')
        modes=[]
        def command(binary,args,mode):modes.append(mode);return [binary,*args]
        helper=SimpleNamespace(command=command,environment=lambda mode:{})
        with patch.dict('sys.modules',{'ssh_transport':helper}),patch('subprocess.run',side_effect=run),patch('pathlib.Path.exists',return_value=False),patch('builtins.print') as output:
            exec(REMOTE_PROBE,{})
            result=json.loads(output.call_args.args[0])
        self.assertEqual(modes,['keyboard-interactive-empty'])
        self.assertEqual(len(calls),2)
        return result

    def test_real_ssh_receipt_and_idle_runner_are_required(self):
        value=self.remote_probe()
        self.assertEqual(value['state'],'READY');self.assertEqual(value['bench_ssh'],'PASS')
        self.assertEqual(value['bench_host'],'bench-test')

    def test_partial_or_malformed_queue_never_means_idle(self):
        for queue in ({},{'active':None},{'schema_version':1,'active':False,'queued':[],'waiting':[]},
                      {'schema_version':1,'active':None,'queued':['invalid'],'waiting':[]},
                      {'schema_version':True,'active':None,'queued':[],'waiting':[]}):
            with self.subTest(queue=queue):
                result=self.remote_probe(queue_payload=queue)
                self.assertEqual(result['state'],'STATUS_UNAVAILABLE');self.assertEqual(result['bench_ssh'],'PASS')

    def test_an_empty_active_lock_object_is_conservatively_busy(self):
        self.assertEqual(self.remote_probe(queue_payload={'schema_version':1,'active':{},'queued':[],'waiting':[]})['state'],'RESOURCE_BUSY')

    def test_invalid_lock_is_not_presented_as_confirmed_active_work(self):
        from bench_gateway.availability import connection_text
        value=self.remote_probe(queue_payload={'schema_version':1,'active':{'state':'INVALID_LOCK','safe_to_claim':False},'queued':[],'waiting':[]})
        self.assertEqual('STATUS_UNAVAILABLE',value['state']);self.assertEqual('INVALID_LOCK',value['runner_state'])
        self.assertEqual('PASS',value['bench_ssh'])
        value['checked_at']=time.time()
        self.assertIn('锁记录损坏',connection_text(value));self.assertNotIn('被任务或锁占用',connection_text(value))

    def test_busy_runner_does_not_skip_bench_ssh(self):
        self.assertEqual(self.remote_probe(busy=True)['state'],'RESOURCE_BUSY')
        value=self.remote_probe(busy=True,error='Connection refused')
        self.assertEqual(value['state'],'BENCH_UNREACHABLE');self.assertEqual(value['runner_state'],'BUSY')

    def test_auth_and_host_key_failure_are_not_reported_as_physical_disconnect(self):
        for error,reason in [('Permission denied','AUTH_FAILED'),('Host key verification failed','HOST_KEY_FAILED'),('REMOTE HOST IDENTIFICATION HAS CHANGED!','HOST_KEY_CHANGED')]:
            value=self.remote_probe(error=error)
            self.assertEqual(value['state'],'BENCH_SSH_FAILED');self.assertEqual(value['bench_ssh'],reason)

    def test_remote_command_failure_is_not_reported_as_network_failure(self):
        value=self.remote_probe(error='python3: not found',exit_code=127)
        self.assertEqual(value['state'],'BENCH_SSH_FAILED');self.assertEqual(value['bench_ssh'],'CHECK_FAILED')

    def test_zero_exit_without_correct_receipt_never_counts_as_connected(self):
        value=self.remote_probe(invalid_receipt=True)
        self.assertEqual(value['state'],'BENCH_SSH_FAILED');self.assertEqual(value['bench_ssh'],'CHECK_FAILED')

    def test_bench_timeout_keeps_runner_result_separate(self):
        value=self.remote_probe(timeout=True)
        self.assertEqual(value['state'],'BENCH_UNREACHABLE');self.assertEqual(value['bench_ssh'],'TIMEOUT')
        self.assertEqual(value['runner_state'],'IDLE')

    def test_runner_failure_does_not_erase_verified_ssh(self):
        value=self.remote_probe(queue_failed=True)
        self.assertEqual(value['state'],'STATUS_UNAVAILABLE');self.assertEqual(value['bench_ssh'],'PASS')

    def test_stale_port_only_probe_envelope_is_rejected(self):
        with patch('bench_gateway.availability.SSH.python',return_value='{"state":"READY"}'):
            self.assertEqual(probe({})['state'],'STATUS_UNAVAILABLE')

    def test_first_hop_disables_connection_reuse(self):
        with patch('bench_gateway.transfer.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{}')) as run:
            SSH({'ssh_binary':'ssh','workstation':'test-workstation'}).python('print(1)',fresh=True)
            argv=run.call_args.args[0]
            for flag in ('ControlMaster=no','ControlPath=none'):
                self.assertLess(argv.index(flag),argv.index('test-workstation'))

    def test_probe_requests_fresh_workstation_session(self):
        value=self.remote_probe()
        with patch('bench_gateway.availability.SSH.python',return_value=json.dumps(value)) as run:
            self.assertEqual(probe({})['state'],'READY')
            self.assertTrue(run.call_args.kwargs['fresh'])

    def test_workstation_ssh_failure_keeps_pre_dispatch_waiting(self):
        with patch('bench_gateway.availability.SSH.python',side_effect=TimeoutError()):
            self.assertEqual(probe({})['state'],'WORKSTATION_UNAVAILABLE')


if __name__=='__main__':unittest.main()
