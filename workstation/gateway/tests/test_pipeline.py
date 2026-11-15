import unittest

from bench_gateway.core import GatewayError,PrecheckFailed
from bench_gateway.executor import Executor
from bench_gateway.worker import tick
import test_gateway as fixtures


class Adapter(Executor):
    def __init__(self,cfg,store,fail_smoke=False,busy_full=False):
        super().__init__(cfg,store);self.calls=[];self.fail_smoke=fail_smoke;self.busy_full=busy_full
    def request(self,owner,operation,**kw):return {'operation':operation,**kw}
    def remote(self,request,job_id,read_only=False):
        op=request['operation'];self.calls.append(op)
        if op=='smoke' and self.fail_smoke:raise GatewayError('replay failed')
        if op=='diagnose':return {'state':'DIAGNOSED','summary':'依赖内容不一致','safe_to_continue':True}
        if op=='replay' and self.busy_full:return {'state':'RESOURCE_BUSY'}
        return {'state':'PASS','run_id':request['run_id'],'task':'/tasks/'+request['run_id']}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        fixtures.GatewayTests.setUp(self);self.cfg.update(mode='live',live_enabled=True)
    tearDown=fixtures.GatewayTests.tearDown
    enqueue=fixtures.GatewayTests.enqueue

    def test_precheck_must_precede_formal_replay(self):
        j=self.enqueue();adapter=Adapter(self.cfg,self.s)
        tick(self.cfg,self.s,adapter)
        self.assertEqual(adapter.calls,['smoke','replay'])
        result=self.s.job('ou_test_alice',j['id'])
        self.assertEqual(result['state'],'SUCCEEDED')
        self.assertEqual(result['result']['smoke']['run_id'],j['id']+'-smoke')

    def test_precheck_failure_diagnoses_and_never_runs_full(self):
        j=self.enqueue();adapter=Adapter(self.cfg,self.s,fail_smoke=True)
        tick(self.cfg,self.s,adapter)
        self.assertEqual(adapter.calls,['smoke','diagnose'])
        self.assertEqual(self.s.job('ou_test_alice',j['id'])['state'],'FAILED')

    def test_wait_after_successful_precheck_does_not_repeat_it(self):
        j=self.enqueue();adapter=Adapter(self.cfg,self.s,busy_full=True)
        tick(self.cfg,self.s,adapter)
        self.assertEqual(self.s.job('ou_test_alice',j['id'])['state'],'QUEUED')
        adapter.busy_full=False;tick(self.cfg,self.s,adapter)
        self.assertEqual(adapter.calls,['smoke','replay','replay'])

    def test_next_task_has_its_own_precheck(self):
        self.enqueue('first');self.enqueue('second');adapter=Adapter(self.cfg,self.s)
        tick(self.cfg,self.s,adapter);tick(self.cfg,self.s,adapter)
        self.assertEqual(adapter.calls,['smoke','replay','smoke','replay'])


if __name__=='__main__':unittest.main()
