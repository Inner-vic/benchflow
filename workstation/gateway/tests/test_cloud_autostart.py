import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from bench_gateway.cloud_provider import ready
from bench_gateway.core import GatewayError


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.cfg={'project_root':'isolated','cloud_browser_autostart':True}
        self.profile={'instance':'bench-eval-offline','profile_sha256':'a'*64,'volume_prefix':'fixed/'}
        self.good={'instance':'bench-eval-offline','profile_sha256':'a'*64,'state':'READY','checked_at':time.time()}
        self.old=dict(self.good,checked_at=time.time()-300)

    def test_live_receiver_does_not_launch_browser(self):
        with patch('bench_gateway.cloud_provider.transport',return_value=self.good),patch('bench_gateway.cloud_bootstrap.EnvironmentTool') as tool:
            self.assertEqual(ready(self.cfg,self.profile),self.good)
            tool.assert_not_called()

    def test_disabled_feature_preserves_previous_behavior(self):
        with patch('bench_gateway.cloud_provider.transport',return_value=self.old),patch('bench_gateway.cloud_bootstrap.EnvironmentTool') as tool:
            with self.assertRaises(GatewayError):ready(dict(self.cfg,cloud_browser_autostart=False),self.profile)
            tool.assert_not_called()

    def test_recovery_rechecks_heartbeat_and_does_not_recurse(self):
        with patch('bench_gateway.cloud_provider.transport',side_effect=[self.old,self.good]) as transport,patch('bench_gateway.cloud_bootstrap.EnvironmentTool') as tool:
            tool.return_value.ensure.return_value={'state':'READY'}
            self.assertEqual(ready(self.cfg,self.profile),self.good)
            self.assertEqual(transport.call_count,2)
            tool.return_value.ensure.assert_called_once_with(allow_start=True,wait_seconds=120)
            self.assertEqual(tool.call_args.kwargs['backend'],'browser')

    def test_unknown_or_denied_is_reported_without_repeated_starts(self):
        for state in ('START_RESULT_UNKNOWN','START_REJECTED','RECEIVER_NOT_READY','INSTANCE_ID_CHANGED'):
            with patch('bench_gateway.cloud_provider.transport',return_value=self.old),patch('bench_gateway.cloud_bootstrap.EnvironmentTool') as tool:
                tool.return_value.ensure.return_value={'state':state}
                with self.assertRaisesRegex(GatewayError,state):ready(self.cfg,self.profile)
                tool.return_value.ensure.assert_called_once()

    def test_model_success_cannot_override_stale_receiver(self):
        with patch('bench_gateway.cloud_provider.transport',return_value=self.old),patch('bench_gateway.cloud_bootstrap.EnvironmentTool') as tool:
            tool.return_value.ensure.return_value={'state':'READY'}
            with self.assertRaises(GatewayError):ready(self.cfg,self.profile)
            tool.return_value.ensure.assert_called_once()

    def test_storage_auth_error_is_not_diagnosed_as_stopped(self):
        with patch('bench_gateway.cloud_provider.transport',side_effect=GatewayError('auth failed')),patch('bench_gateway.cloud_bootstrap.EnvironmentTool') as tool:
            with self.assertRaisesRegex(GatewayError,'auth failed'):ready(self.cfg,self.profile)
            tool.assert_not_called()

    def test_delayed_start_completion_reconciles_pending_without_new_request(self):
        with TemporaryDirectory() as root:
            path=Path(root)/'runtime/cloud-environment-control/operation.json'
            path.parent.mkdir(parents=True);path.write_text('{"state":"STARTING"}')
            with patch('bench_gateway.cloud_provider.transport',return_value=self.good),patch('bench_gateway.cloud_bootstrap.EnvironmentTool') as tool:
                tool.return_value.ensure.return_value={'state':'READY'}
                self.assertEqual(ready(dict(self.cfg,project_root=root),self.profile),self.good)
                tool.return_value.ensure.assert_called_once_with(allow_start=False,wait_seconds=0)

    def test_corrupt_pending_operation_is_not_overwritten(self):
        with TemporaryDirectory() as root:
            path=Path(root)/'runtime/cloud-environment-control/operation.json'
            path.parent.mkdir(parents=True);path.write_text('broken')
            with patch('bench_gateway.cloud_provider.transport',return_value=self.good),patch('bench_gateway.cloud_bootstrap.EnvironmentTool') as tool:
                with self.assertRaises(GatewayError):ready(dict(self.cfg,project_root=root),self.profile)
                tool.assert_not_called()
            self.assertEqual(path.read_text(),'broken')


if __name__=='__main__':unittest.main()
