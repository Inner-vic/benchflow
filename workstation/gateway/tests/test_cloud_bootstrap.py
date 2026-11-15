import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.cloud_bootstrap import EnvironmentTool, request_start, TARGET, EXPECTED_UID
from bench_gateway.core import GatewayError


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile=dict(zip(('instance','namespace','cluster'),TARGET),volume_prefix='fixed-output',profile_sha256='a'*64)
        self.tool=EnvironmentTool({},self.profile,binary='lpai-agent',control_root=Path(self.temp.name))

    def observation(self,state='STOPPED'):
        return ({'state':state,'browser_action':'NONE'}, {'uid':EXPECTED_UID}, {'generation':'old'})

    def test_running_or_busy_instance_never_gets_start_request(self):
        for busy in (False,True):
            with patch.object(self.tool,'inspect',return_value=({'state':'READY','busy':busy,'generation':'fresh'}, {}, {})), patch('bench_gateway.cloud_bootstrap.request_start') as start:
                self.assertFalse(self.tool.ensure(allow_start=True)['start_submitted'])
                start.assert_not_called()

    def test_checking_stopped_does_not_start_without_explicit_flag(self):
        with patch.object(self.tool,'inspect',return_value=self.observation()), patch('bench_gateway.cloud_bootstrap.request_start') as start:
            self.assertEqual(self.tool.ensure()['state'],'STOPPED')
            start.assert_not_called()

    def test_approval_unknown_and_rejected_are_not_resubmitted(self):
        for state in ('WAITING_APPROVAL','START_RESULT_UNKNOWN','START_REJECTED','STARTING'):
            with self.subTest(state=state):
                self.tool.path.unlink(missing_ok=True)
                def submit(*args):
                    saved=json.loads(self.tool.path.read_text())
                    self.assertEqual(saved['state'],'REQUESTING')
                    self.assertEqual(saved['target'],list(TARGET))
                    return {'state':state}
                with patch.object(self.tool,'inspect',return_value=self.observation()),patch('bench_gateway.cloud_bootstrap.request_start',side_effect=submit) as start:
                    first=self.tool.ensure(allow_start=True)
                    second=self.tool.ensure(allow_start=True)
                    self.assertTrue(first['start_submitted'])
                    self.assertFalse(second['start_submitted'])
                    self.assertEqual(second['state'],state)
                    self.assertEqual(start.call_count,1)

    def test_crash_after_durable_intent_does_not_duplicate_request(self):
        with patch.object(self.tool,'inspect',return_value=self.observation()),patch('bench_gateway.cloud_bootstrap.request_start',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.tool.ensure(allow_start=True)
        with patch.object(self.tool,'inspect',return_value=self.observation()),patch('bench_gateway.cloud_bootstrap.request_start') as start:
            self.assertEqual(self.tool.ensure(allow_start=True)['state'],'REQUESTING')
            start.assert_not_called()

    def test_start_wait_requires_new_receiver_generation(self):
        with patch.object(self.tool,'inspect',side_effect=[self.observation(),self.observation('RECEIVER_NOT_READY'),({'state':'READY','generation':'new'}, {}, {})]) as inspect,patch('bench_gateway.cloud_bootstrap.request_start',return_value={'state':'STARTING'}),patch('bench_gateway.cloud_bootstrap.time.sleep'):
            result=self.tool.ensure(allow_start=True,wait_seconds=5)
        self.assertEqual(result['state'],'READY')
        self.assertTrue(result['start_submitted'])
        self.assertEqual(inspect.call_args.args,('old',))
        self.assertEqual(json.loads(self.tool.path.read_text())['ready_generation'],'new')

    def test_wrong_fixed_target_and_operation_identity_block(self):
        with self.assertRaises(GatewayError):EnvironmentTool({},dict(self.profile,instance='other'),binary='x',control_root=self.temp.name)
        self.tool.path.write_text(json.dumps({'target':list(TARGET),'uid':'different'}))
        with patch.object(self.tool,'inspect') as inspect:
            with self.assertRaises(GatewayError):self.tool.ensure(allow_start=True)
            inspect.assert_not_called()

    def test_unknown_startup_phase_is_only_polled_never_restarted(self):
        with patch.object(self.tool,'inspect',side_effect=[self.observation(),self.observation('UNKNOWN_PLATFORM_STATE'),({'state':'READY','generation':'new'}, {}, {})]),patch('bench_gateway.cloud_bootstrap.request_start',return_value={'state':'STARTING'}) as start,patch('bench_gateway.cloud_bootstrap.time.sleep'):
            self.assertEqual(self.tool.ensure(allow_start=True,wait_seconds=5)['state'],'READY')
            start.assert_called_once()

    def test_platform_unknown_and_receiver_errors_do_not_start(self):
        for state in ('PLATFORM_UNAVAILABLE','PLATFORM_STALE','INSTANCE_FAILED','INSTANCE_ID_CHANGED','RECEIVER_NOT_READY','STOPPING'):
            with patch.object(self.tool,'inspect',return_value=self.observation(state)),patch('bench_gateway.cloud_bootstrap.request_start') as start:
                self.assertFalse(self.tool.ensure(allow_start=True)['start_submitted'])
                start.assert_not_called()

    def test_recreated_instance_is_rejected_before_receiving_heartbeat(self):
        with patch('bench_gateway.cloud_bootstrap.inspect_instance',return_value={'state':'OBSERVED','uid':'different'}),patch('bench_gateway.cloud_bootstrap.transport') as transport:
            self.assertEqual(self.tool.check()['state'],'INSTANCE_ID_CHANGED')
            transport.assert_not_called()

    def test_official_cli_pending_is_not_ready_and_raw_errors_are_redacted(self):
        samples=[({'status':'pending_approval','approval_id':'safe-id'},0,'WAITING_APPROVAL'),
                 ({'status':'ok','data':{'status':'pending_approval'}},0,'WAITING_APPROVAL'),
                 ({'status':'ok'},0,'STARTING'), ({'status':'failed','secret':'DO_NOT_SHOW'},1,'START_REJECTED'),
                 ({'unexpected':'DO_NOT_SHOW'},0,'START_RESULT_UNKNOWN')]
        for body,code,state in samples:
            with patch('bench_gateway.cloud_bootstrap.subprocess.run',return_value=SimpleNamespace(stdout=json.dumps(body),returncode=code)) as run:
                result=request_start('lpai-agent',self.profile)
                self.assertEqual(result['state'],state)
                self.assertNotIn('DO_NOT_SHOW',str(result))
                self.assertEqual(run.call_args.args[0][1:3],['devspaces','start'])
                self.assertNotIn('stop',run.call_args.args[0])


if __name__=='__main__':unittest.main()
