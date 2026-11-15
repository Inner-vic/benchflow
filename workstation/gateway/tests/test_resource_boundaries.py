"""Real JSON lock primitives in temporary directories, never the production lock."""
import importlib.util,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from bench_gateway.core import Store
from bench_gateway.roadmodel_queue import HardwareQueue

class ResourceBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    def test_native_json_ownership_excludes_second_run_and_wrong_release(self):
        source=Path(os.environ.get('BENCH_RUNNER_AUTOMATION','/home/lixiang/bench-eval/current/automation'))/'runner_state.py'
        if not source.is_file():self.skipTest('run this test on Linux with the reviewed Runner')
        spec=importlib.util.spec_from_file_location('reviewed_runner_state',source);runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
        runs=self.root/'runs';locks=self.root/'locks'
        for name in ('pcm-test','other-test'):
            p=runs/name;p.mkdir(parents=True)
            (p/'state.json').write_text(json.dumps({'state':'READY'}));(p/'manifest.json').write_text(json.dumps({'submitted_by':'test'}))
        runner.claim_bench(runs,locks,'pcm-test')
        lock=locks/'bench.lock';before=lock.read_bytes()
        self.assertEqual('pcm-test',json.loads(before)['run_id']);self.assertEqual(0o600,lock.stat().st_mode&0o777)
        with self.assertRaises(runner.RunnerError):runner.claim_bench(runs,locks,'other-test')
        with self.assertRaises(runner.RunnerError):runner.release_bench(runs,locks,'other-test','NO_ACTIVATION',None)
        with self.assertRaises(runner.RunnerError):runner.release_bench(runs,locks,'pcm-test','RESTORE_VERIFIED',None)
        self.assertEqual(before,lock.read_bytes())
        runner.release_bench(runs,locks,'pcm-test','NO_ACTIVATION',None)
        runner.claim_bench(runs,locks,'other-test')
    def queue_fixture(self):
        cfg={'state_root':str(self.root/'state')};q=HardwareQueue(cfg);q.store.allow('ou_test')
        task={'id':'rm-fixed','owner':'ou_test','state':'RUNNING','session':'feishu:oc_test:ou_test','event_id':'original-confirmation','inputs':{'model':'fixed'}}
        q.runtime=lambda:SimpleNamespace(get=lambda tid,user:task)
        return q,task
    def test_pcm_then_roadmodel_share_fifo_and_deduplicate_dispatch_identity(self):
        q,task=self.queue_fixture()
        pcm,_=q.store.submit(task['owner'],task['session'],'pcm-first',{'action':'replay'})
        with patch('bench_gateway.roadmodel_queue.modules',return_value=(None,None,None,None)):
            first=q.submit(task,{'kind':'compile_alignment'},q.request_path(task['id']).parent)
            duplicate=q.submit(task,{'kind':'compile_alignment'},q.request_path(task['id']).parent)
        self.assertEqual(first['hardware_job'],duplicate['hardware_job'])
        self.assertEqual(pcm['id'],q.store.claim()['id']);self.assertIsNone(q.store.claim())
        q.store.finish(pcm,'SUCCEEDED',{})
        self.assertEqual(first['hardware_job'],q.store.claim()['id'])
    def test_roadmodel_unknown_blocks_pcm_without_repeating_frames(self):
        q,task=self.queue_fixture()
        with patch('bench_gateway.roadmodel_queue.modules',return_value=(None,None,None,None)):
            rm=q.submit(task,{'kind':'compile_alignment'},q.request_path(task['id']).parent)
        q.store.submit(task['owner'],task['session'],'pcm-second',{'action':'replay'})
        claimed=q.store.claim();self.assertEqual(rm['hardware_job'],claimed['id'])
        q.store.finish(claimed,'NEEDS_REVIEW',{'completed_frames':10})
        self.assertIsNone(q.store.claim())
