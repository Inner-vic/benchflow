import ast,copy,json,shutil,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from test_candidate_adapter import fixture
from bench_gateway.candidate_adapter import render
from bench_gateway.intake import validate
from bench_gateway.core import GatewayError

REFERENCE=Path(__file__).resolve().parents[1]/'research/cloud-dataset-reference'

class ReplayModeTests(unittest.TestCase):
    def test_stream_path_is_preserved_and_new_mode_is_frozen_into_the_job(self):
        template,candidate=fixture();first=render(template,candidate,'mode-first','actor_owner',[1])
        self.assertNotIn('gateway_replay_tools',first['adapter'])
        candidate['replay_mode']='frame_by_frame';second=render(template,candidate,'mode-second','actor_owner',[1])
        self.assertEqual('frame_by_frame',second['job']['batch_stream']['mode'])
        self.assertIn('gateway_replay_tools',second['adapter'])
        self.assertIn("batch.get(\"mode\") != 'frame_by_frame'",second['adapter'])
        self.assertEqual('frame_by_frame',second['identity']['replay_mode'])
    def test_unknown_mode_is_rejected_without_running_templates(self):
        with self.assertRaises(GatewayError):validate({'replay_mode':'arbitrary'})
        template,candidate=fixture();candidate['replay_mode']='arbitrary'
        with self.assertRaises(ValueError):render(template,candidate,'invalid','actor_owner',[1])
    def derive(self,folder,mode='frame_by_frame',raw=True):
        template,candidate=fixture();candidate['replay_mode']=mode
        if raw:candidate['dataset']['prepared_scenes'][0].update(replay_input_format='cloud-original-v1',lidar_frames=120)
        result=render(template,candidate,'derived-own-run','actor_owner',[1])
        node=next(x for x in ast.parse(result['adapter']).body if isinstance(x,ast.FunctionDef) and x.name=='gateway_replay_tools')
        refs=folder/'shared';refs.mkdir()
        for name in ('run_controlled_sweep.py','native_stream_replay.py'):
            (refs/name).write_text((REFERENCE/name).read_text(),encoding='utf-8')
        namespace={'Path':Path,'TERMINAL_ADAPTER':refs,'case':SimpleNamespace(ProbeError=RuntimeError)}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'derived-test','exec'),namespace)
        return namespace['gateway_replay_tools'],refs
    def test_derived_copy_passes_selected_mode_and_raw_count_without_changing_shared_clients(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);derive,refs=self.derive(root);before={p.name:p.read_bytes() for p in refs.iterdir()}
            sweep=derive(root/'run');native=sweep.with_name('native_stream_replay.py').read_text()
            self.assertIn('{"mode": \'frame_by_frame\', "speed": args.speed}',native)
            self.assertEqual(before,{p.name:p.read_bytes() for p in refs.iterdir()})
            node=next(x for x in ast.parse(sweep.read_text()).body if isinstance(x,ast.FunctionDef) and x.name=='recorded_lidar_frame_count')
            namespace={'Path':Path,'json':json}
            exec(compile(ast.Module(body=[node],type_ignores=[]),'count-test','exec'),namespace)
            self.assertEqual(120,namespace['recorded_lidar_frame_count']({'replay_input_format':'cloud-original-v1','lidar_frames':120}))
            self.assertEqual(sweep,derive(root/'run'))
            sweep.chmod(0o600);sweep.write_text('modified')
            with self.assertRaises(RuntimeError):derive(root/'run')
    def test_unrecognized_shared_tool_is_refused_before_candidate_activation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);derive,refs=self.derive(root);(refs/'native_stream_replay.py').write_text('unexpected source')
            with self.assertRaisesRegex(RuntimeError,'changed'):derive(root/'run')

if __name__=='__main__':unittest.main()
