"""Isolated direct-evaluation tests; no real executable, SSH or bench."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway import evaluation_direct as direct


class DirectInstallTests(unittest.TestCase):
    def test_tool_pin_checked_once_and_collect_never_rechecks_data(self):
        calls=[]
        worker=SimpleNamespace(verify_pin=lambda p:calls.append('pin') or ('package','worker'),
            collect=lambda r,p,plan,recheck:calls.append(('collect',recheck)) or 'saved',
            execute=lambda *args:'original')
        direct.install(worker,use_direct=False)
        pin={'worker':{'sha256':'a'},'evaluator':{'sha256':'b'}}
        self.assertEqual(worker.verify_pin(pin),('package','worker'));worker.verify_pin(pin)
        self.assertEqual(worker.collect({},pin,{}),'saved')
        self.assertEqual(calls,['pin',('collect',False)])
        self.assertEqual(worker.execute(), 'original')


@unittest.skipIf(os.name=='nt','Real symlink view cases run on Linux, without Windows administrator dependency')
class DirectExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.package=self.root/'package';(self.package/'etc').mkdir(parents=True);(self.package/'build').mkdir()
        for name in ('run.sh','build/uroad_offline_eval'):
            p=self.package/name;p.write_bytes(b'not executable fixture');p.chmod(0o755)
        (self.package/'etc/evaluation_param.yaml').write_text('dataset:\n  labeled_data_root: old\n  pred_data_root: old\n  scene_folder_prefix: old\n  max_scenes_for_debug: 1\neval_tasks:\n  eval_traj: ON\n  eval_vision: ON\n  eval_road: OFF\n')
        self.runs=self.root/'runs';self.runs.mkdir();self.truth=self.root/'truth';self.pred=self.root/'pred'
        frame=self.truth/'scene-a/dataset/1';frame.mkdir(parents=True)
        (frame/'dense_labeled_1_frame_1787194173354676_x.npy').write_bytes(b'fixture must never be read or hashed')
        pred=self.pred/'scene-a';pred.mkdir(parents=True);(pred/'large.npy').write_bytes(b'also never read by wrapper')
        self.pin={'runs_root':str(self.runs),'worker':{},'evaluator':{'root':str(self.package),'manifest_sha256':'a'*64}}
        self.plan={'scenes':['scene-a'],'scene_count':1,'scene_inputs':[{'scene':'scene-a','truth_frames':1}],
            'truth_root':str(self.truth),'prediction_path':str(self.pred),'prepared_manifest_sha256':'b'*64}
        spec=importlib.util.spec_from_file_location('direct_scope',Path(__file__).resolve().parents[1]/'vendor/offline_eval/scripts/report_scope.py')
        scope=importlib.util.module_from_spec(spec);spec.loader.exec_module(scope)
        self.events=[];self.hashes=[];self.collects=[]
        def inside(path,root):
            path,root=Path(path).resolve(),Path(root).resolve()
            if root not in path.parents:raise ValueError('outside root')
            return path
        def require(value,message):
            if not value:raise ValueError(message)
        def sha(path):
            path=Path(path);self.hashes.append(str(path))
            if self.truth in path.parents or self.pred in path.parents:raise AssertionError('raw input hash forbidden')
            return hashlib.sha256(path.read_bytes()).hexdigest()
        def write(path,value):path.write_text(json.dumps(value))
        def report(root):
            require((root/'exit_code.txt').read_text().strip()=='0','bad exit')
            reports=list((root/'eval_report').glob('*.txt'));require(len(reports)==1,'missing report')
            return reports[0]
        self.worker=SimpleNamespace(inside=inside,require=require,SAFE=__import__('re').compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z'),
            sha=sha,write=write,verify_pin=lambda p:(self.package,None),emit=lambda phase,**kw:self.events.append(phase),
            native_report=report,parse_scene_coverage=scope.parse_scene_coverage)
        def collect(r,p,plan,recheck):
            self.collects.append(recheck)
            return json.loads((self.runs/r['run_id']/'directory-receipt.json').read_text())
        self.collect=collect;self.request={'run_id':'direct-fixture'}
        self.report_text='评测场景数: 1\n[1] scene-a: 1/1\n候选预测帧: 1\n至少完成一项任务: 1\n未匹配真值时间戳: 0\nOverall mAP: N/A\n'

    def native(self,*args,**kwargs):
        root=Path(kwargs['cwd']);reports=root/'eval_report';reports.mkdir()
        (reports/'eval_report_fixture.txt').write_text(self.report_text,encoding='utf-8')
        return SimpleNamespace(returncode=0)

    def run_direct(self):
        with patch.object(direct.subprocess,'run',side_effect=self.native):
            return direct.execute(self.worker,self.collect,self.request,self.pin,self.plan)

    def test_direct_path_does_not_hash_raw_inputs_and_preserves_switches_and_scope(self):
        receipt=self.run_direct()
        self.assertEqual(receipt['input_verification'],direct.POLICY)
        self.assertFalse(receipt['input_content_verified']);self.assertFalse(receipt['source_postchecks_passed'])
        self.assertEqual(receipt['source_data_hash_passes'],0);self.assertTrue(receipt['path_checks_passed'])
        self.assertEqual(self.events,['prepared','evaluating']);self.assertEqual(self.collects,[False])
        self.assertTrue(all(Path(p).name in ('evaluation_param.yaml','eval_report_fixture.txt') for p in self.hashes))
        cfg=(self.runs/'direct-fixture/evaluation_param.yaml').read_text()
        for text in ('eval_traj: ON','eval_vision: ON','eval_road: OFF','max_scenes_for_debug: 0'):self.assertIn(text,cfg)
        self.assertTrue((self.runs/'direct-fixture/inputs/truth/scene-a/dataset/1_1787194173354').is_symlink())

    def test_incomplete_existing_attempt_never_reruns(self):
        (self.runs/'direct-fixture').mkdir()
        with patch.object(direct.subprocess,'run') as native:
            with self.assertRaisesRegex(ValueError,'never overwrite'):self.run_direct()
            native.assert_not_called()

    def test_completed_attempt_only_collects(self):
        self.run_direct()
        with patch.object(direct.subprocess,'run') as native:
            receipt=direct.execute(self.worker,self.collect,self.request,self.pin,self.plan)
            native.assert_not_called()
        self.assertEqual(receipt['input_verification'],direct.POLICY);self.assertEqual(self.collects,[False,False])

    def test_wrong_report_denominator_is_rejected(self):
        self.report_text=self.report_text.replace('scene-a: 1/1','scene-a: 1/2')
        with self.assertRaisesRegex(ValueError,'denominator'):self.run_direct()
        self.assertFalse((self.runs/'direct-fixture/directory-receipt.json').exists())

    def test_native_failure_preserves_logs_and_does_not_create_receipt(self):
        with patch.object(direct.subprocess,'run',return_value=SimpleNamespace(returncode=7)):
            with self.assertRaisesRegex(ValueError,'native evaluator failed'):
                direct.execute(self.worker,self.collect,self.request,self.pin,self.plan)
        root=self.runs/'direct-fixture'
        self.assertEqual((root/'exit_code.txt').read_text().strip(),'7')
        self.assertTrue((root/'stderr.log').is_file());self.assertFalse((root/'directory-receipt.json').exists())

    def test_missing_truth_file_stops_before_native(self):
        (self.truth/'scene-a/dataset/1/dense_labeled_1_frame_1787194173354676_x.npy').unlink()
        with patch.object(direct.subprocess,'run') as native:
            with self.assertRaisesRegex(ValueError,'one dense truth'):direct.execute(self.worker,self.collect,self.request,self.pin,self.plan)
            native.assert_not_called()


if __name__=='__main__':unittest.main()
