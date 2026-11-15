import copy
import unittest
from unittest.mock import Mock

from bench_gateway.core import GatewayError
from bench_gateway.executor import Executor


class CandidatePipelineTests(unittest.TestCase):
    def setUp(self):
        self.job={'id':'job-new','owner':'ou_candidate_test','payload':{'scenes':[2,4],'assets':{},
                  'baseline':{'data':{'kind':'candidate-v1','binding':{'input_digest':'a'*64}}}}}
        self.executor=object.__new__(Executor)
        self.executor.cfg={}
        self.executor.store=Mock()
        self.executor.request=lambda owner,op,**kw:dict(operation=op,owner=owner,**kw)
        self.executor.remote=Mock(side_effect=lambda req,*a,**kw:dict(state='PASS',run_id=req['run_id'],input_digest='a'*64))

    def test_same_binding_first_selected_scene_then_full(self):
        result=self.executor.replay_pipeline(self.job)
        requests=[c.args[0] for c in self.executor.remote.call_args_list]
        self.assertEqual([r['operation'] for r in requests],['smoke','replay'])
        self.assertEqual(requests[0]['scenes'],[2])
        self.assertEqual(requests[1]['scenes'],[2,4])
        self.assertEqual(requests[1]['smoke_run'],'job-new-smoke')
        self.assertEqual(result['smoke']['input_digest'],'a'*64)

    def test_foreign_smoke_input_blocks_full(self):
        self.executor.remote.return_value={'state':'PASS','run_id':'job-new-smoke','input_digest':'b'*64}
        self.executor.remote.side_effect=None
        with self.assertRaises(GatewayError):self.executor.replay_pipeline(self.job)
        self.assertEqual(self.executor.remote.call_count,1)

    def test_persisted_smoke_from_different_input_never_dispatches(self):
        self.job['result']={'smoke':{'state':'PASS','run_id':'job-new-smoke','input_digest':'b'*64}}
        with self.assertRaises(GatewayError):self.executor.replay_pipeline(self.job)
        self.executor.remote.assert_not_called()

    def test_full_result_from_different_input_is_not_success(self):
        self.executor.remote.side_effect=[{'state':'PASS','run_id':'job-new-smoke','input_digest':'a'*64},
                                          {'state':'PASS','run_id':'job-new','input_digest':'b'*64}]
        with self.assertRaises(GatewayError):self.executor.replay_pipeline(self.job)
        self.assertEqual(self.executor.remote.call_count,2)


if __name__=='__main__':unittest.main()
