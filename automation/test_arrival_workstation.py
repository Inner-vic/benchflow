import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import arrival_workstation as runner
from batch_arrival_accuracy import run_batch


BRIDGE = Path(__file__).resolve().parents[1]/'skills/bench-eval-execution/scripts/run_arrival_accuracy.py'
spec = importlib.util.spec_from_file_location('arrival_bridge', BRIDGE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class WorkstationArrivalTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)

    def test_disabled_does_not_load_inputs_or_contact_ssh(self):
        with patch.object(runner,'runtime',side_effect=AssertionError('runtime accessed')), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.main(['--execute']),0)
        self.assertEqual(json.loads(output.getvalue())['state'],'NOT_REQUESTED')
        with patch.object(bridge.subprocess,'run',side_effect=AssertionError('SSH called')), redirect_stdout(io.StringIO()):
            self.assertEqual(bridge.main(['--execute']),0)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_enabled_bridge_requires_inputs_and_quotes_path(self):
        with patch.object(bridge.subprocess,'run') as remote, redirect_stdout(io.StringIO()):
            self.assertEqual(bridge.main(['--enabled','--execute']),2)
            remote.assert_not_called()
        args=type('Args',(),dict(ssh='ssh',task_id='task1',truth_root='/truth/a b;$(touch x)',
                                attempt_id=None,execute=True,check_runtime=False))()
        command=bridge.command(args)
        self.assertIn("'/truth/a b;$(touch x)'",command[-1])
        self.assertIn('--execute',command[-1])
        self.assertNotIn('scp',command)

    def test_split_sources_preserve_task_ids_and_do_not_copy_inputs(self):
        timeline=self.root/'task/scene/sus_preview_timeline.csv'
        timeline.parent.mkdir(parents=True);timeline.write_text('csv')
        truth=self.root/'truth';truth.mkdir()
        jerks={w:truth/(w+'.json') for w in ['fl','fr']}
        for p in jerks.values(): p.write_text('{}')
        source=[{'scene_id':'s018','scene':'scene','timeline':str(timeline),'jerk':{w:str(p) for w,p in jerks.items()}}]
        receipt=self.root/'receipt.json'
        runner.write(receipt,{'run_id':'run1','product_identity':'product','files':[
            {'local':str(p),'sha256':runner.sha(p)} for p in [timeline,*jerks.values()]]})
        core=self.root/'core.py';core.write_text('# core')
        seen=[]
        def analyse(path,sid,loaded,**kwargs):
            seen.append((path,sid,kwargs))
            return {'scene_id':sid,'scene':path.name,'state':'CALCULATED','detail':[],
                    'front_groups':[],'prediction_cycles':[],'data_quality':[],'metrics':{}},{}
        with patch('batch_arrival_accuracy.load_core',return_value=type('Core',(),{'FRONT_IMPACT_PRE_S':1,'FRONT_IMPACT_POST_S':1})()), patch('batch_arrival_accuracy.analyse_scene',side_effect=analyse):
            result=run_batch(timeline.parent,core,self.root/'out',receipt,scene_sources=source,arrays_only=True)
        self.assertEqual(result['scenes'][0]['scene_id'],'s018')
        self.assertEqual(seen[0][2]['jerk_paths'],jerks)
        self.assertFalse((timeline.parent/'fl.json').exists())
        self.assertEqual(timeline.read_text(),'csv')

    def test_changed_snapshot_is_rejected_before_output(self):
        source=self.root/'source';source.write_text('old')
        p={'files':[{'local':str(source),'sha256':runner.sha(source)}],
           'upstream':{'evidence':{}},'computation_identity':{}}
        source.write_text('changed')
        with self.assertRaisesRegex(ValueError,'changed'):
            runner.verify_snapshot(p)

    def test_partial_calculation_records_failure_and_does_not_render(self):
        p={'files':[],'upstream':{'evidence':{}},'computation_identity':{},'output':str(self.root/'out'),
           'task_id':'task1','run_id':'run1','product_identity':'product','input_root':str(self.root),'scenes':[]}
        with patch('batch_arrival_accuracy.run_batch',return_value={'state':'PARTIAL_BATCH'}), patch('render_arrival_workbook.render') as render:
            with self.assertRaisesRegex(ValueError,'scenes failed'):
                runner.execute(p)
            render.assert_not_called()
        self.assertEqual(runner.load(self.root/'out/attempt.json')['state'],'FAILED')

    def test_xlsx_failure_never_reports_success(self):
        p={'files':[],'upstream':{'evidence':{}},'computation_identity':{},'output':str(self.root/'out'),
           'task_id':'task1','run_id':'run1','product_identity':'product','input_root':str(self.root),'scenes':[],
           'execution_location':'workstation'}
        with patch('batch_arrival_accuracy.run_batch',return_value={'state':'CALCULATED'}), patch('render_arrival_workbook.render',side_effect=ValueError('XLSX mismatch')):
            with self.assertRaisesRegex(ValueError,'XLSX mismatch'):
                runner.execute(p)
        self.assertEqual(runner.load(self.root/'out/attempt.json')['state'],'FAILED')


if __name__=='__main__':
    unittest.main()
