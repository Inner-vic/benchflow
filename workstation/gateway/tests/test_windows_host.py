import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch

from bench_gateway.config import save
from bench_gateway.core import GatewayError
from bench_gateway import windows_host as host


class WindowsHostTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cfg={'project_root':str(self.root),'state_root':str(self.root/'state'),'mode':'simulate','live_enabled':False}
        self.registration={'schema_version':1,'enabled':True,'project_root':str(self.root),'task_name':host.task_name(self.cfg)}

    def install(self):save(host.manifest_path(self.cfg),self.registration)

    def test_registration_is_project_bound_and_missing_retains_old_start_path(self):
        self.assertIsNone(host.manifest(self.cfg));self.install()
        self.assertEqual(host.manifest(self.cfg),self.registration)
        self.registration['task_name']='another-service';self.install()
        with self.assertRaises(GatewayError):host.manifest(self.cfg)

    def test_malformed_registration_does_not_fall_back_to_tool_child(self):
        self.install();host.manifest_path(self.cfg).write_text('bad',encoding='utf-8')
        from bench_gateway.deployment import start
        with patch('bench_gateway.deployment.status',return_value={'active':False}),patch('bench_gateway.deployment.subprocess.Popen') as process:
            with self.assertRaises(GatewayError):start(self.cfg)
            process.assert_not_called()

    def test_normal_start_routes_registered_project_through_windows(self):
        self.install()
        from bench_gateway.deployment import start
        with patch('bench_gateway.deployment.status',return_value={'active':False}),patch.object(host,'start_registered',return_value={'phase':'RUNNING'}) as native,patch('bench_gateway.deployment.subprocess.Popen') as process:
            self.assertEqual(start(self.cfg)['phase'],'RUNNING');native.assert_called_once_with(self.cfg,self.registration);process.assert_not_called()

    def test_native_request_waits_for_real_health_and_does_not_spawn_directly(self):
        self.install();reply=Mock(returncode=0)
        with patch.object(host.subprocess,'run',return_value=reply) as run,patch('bench_gateway.deployment.status',return_value={'active':True,'phase':'RUNNING'}):
            self.assertEqual(host.start_registered(self.cfg,self.registration)['phase'],'RUNNING')
        self.assertEqual(run.call_args.args[0][-2:],['/TN',self.registration['task_name']])

    def test_explicit_start_resumes_only_a_confirmed_normal_stop(self):
        self.install();stop_path=self.root/'runtime/service/stop.json';save(stop_path,{'instance':'old'})
        with patch.object(host.subprocess,'run',return_value=Mock(returncode=0)),patch('bench_gateway.deployment.status',side_effect=[{'phase':'STOPPED','instance':'old'},{'active':True,'phase':'RUNNING'}]):
            self.assertEqual(host.start_registered(self.cfg,self.registration)['phase'],'RUNNING')
        self.assertEqual(json.loads(stop_path.read_text(encoding='utf-8')),{'instance':''})
        save(stop_path,{'instance':'unknown'})
        with patch('bench_gateway.deployment.status',return_value={'phase':'STALE','instance':'unknown'}),patch.object(host.subprocess,'run') as native:
            with self.assertRaises(GatewayError):host.start_registered(self.cfg,self.registration)
            native.assert_not_called()
        self.assertEqual(json.loads(stop_path.read_text(encoding='utf-8')),{'instance':'unknown'})

    def test_native_request_failure_is_not_hidden_or_duplicated(self):
        self.install()
        with patch.object(host.subprocess,'run',return_value=Mock(returncode=1)):
            with self.assertRaises(GatewayError):host.start_registered(self.cfg,self.registration)
        with patch.object(host.subprocess,'run',side_effect=subprocess.TimeoutExpired('task',15)):
            with self.assertRaises(GatewayError):host.start_registered(self.cfg,self.registration)

    def test_already_running_host_does_not_claim_or_stop_another_supervisor(self):
        with patch('bench_gateway.deployment.status',return_value={'active':True,'instance':'original'}),patch.object(host.subprocess,'Popen') as child:
            self.assertEqual(host.run_host(self.cfg),0);child.assert_not_called()

    def test_periodic_native_start_never_resumes_an_intentional_stop(self):
        save(self.root/'runtime/service/stop.json',{'instance':'paused'})
        with patch('bench_gateway.deployment.status',return_value={'active':False,'phase':'STOPPED','instance':'paused'}),patch.object(host.subprocess,'Popen') as child:
            self.assertEqual(host.run_host(self.cfg),0);child.assert_not_called()
        self.assertEqual(json.loads((self.root/'runtime/service/stop.json').read_text()),{'instance':'paused'})

    def test_host_propagates_failure_but_normal_drain_exits_cleanly(self):
        for code,expected in ((0,0),(17,1)):
            child=Mock(pid=123);child.wait.return_value=code
            with patch('bench_gateway.deployment.status',return_value={'active':False}),patch.object(host.subprocess,'Popen',return_value=child) as spawn:
                self.assertEqual(host.run_host(self.cfg),expected)
            self.assertEqual(spawn.call_args.args[0][-1],'run')
            self.assertNotIn('DETACHED_PROCESS',str(spawn.call_args))

    def test_pythonw_host_launches_console_python_for_gateway_stdio(self):
        interpreter=self.root/'pythonw.exe';interpreter.touch();(self.root/'python.exe').touch()
        child=Mock(pid=123);child.wait.return_value=0
        with patch('bench_gateway.deployment.status',return_value={'active':False}),patch.object(host.sys,'executable',str(interpreter)),patch.object(host.subprocess,'Popen',return_value=child) as spawn:
            self.assertEqual(host.run_host(self.cfg),0)
        self.assertEqual(spawn.call_args.args[0][0],str(self.root/'python.exe'))

    def test_host_failure_log_records_type_without_exception_secret(self):
        with patch('bench_gateway.deployment.status',side_effect=RuntimeError('secret-password')):
            self.assertEqual(host.run_host(self.cfg),1)
        raw=(self.root/'runtime/service/windows-host.log').read_text(encoding='utf-8')
        self.assertIn('RuntimeError',raw);self.assertNotIn('secret-password',raw)
