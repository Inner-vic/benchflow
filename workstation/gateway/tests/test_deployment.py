import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from bench_gateway.config import save
from bench_gateway.core import Store
from bench_gateway.deployment import active,status,stop,stop_matches,_HeartbeatWriter
from bench_gateway.worker import run
from bench_gateway.executor import Executor
from bench_gateway.core import GatewayError,digest


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cfg={'project_root':str(self.root),'state_root':str(self.root/'state'),'mode':'simulate','live_enabled':False,
                  'notifications_enabled':False,'poll_seconds':.1}
        (self.root/'config').mkdir();save(self.root/'config/gateway.json',self.cfg)

    def tearDown(self):self.tmp.cleanup()

    def test_stale_or_future_heartbeat_is_not_running(self):
        for heartbeat in (1,112):self.assertFalse(active({'phase':'RUNNING','heartbeat':heartbeat},now=100))
        self.assertTrue(active({'phase':'RUNNING','heartbeat':99},now=100))

    def test_old_stop_request_cannot_stop_new_instance(self):
        self.assertFalse(stop_matches({'instance':'old'},{'instance':'new'}))
        self.assertFalse(stop_matches({},{}))
        self.assertTrue(stop_matches({'instance':'new'},{'instance':'new'}))

    def test_status_exposes_counts_without_secret(self):
        self.cfg['app_secret']='do-not-publish'
        result=status(self.cfg)
        self.assertEqual(result['phase'],'STOPPED')
        self.assertNotIn('do-not-publish',json.dumps(result))
        self.assertFalse(result['live_enabled'])

    def test_status_write_failure_keeps_stale_health_and_recovers_without_payload_logging(self):
        from io import StringIO
        path=self.root/'status.json'
        save(path,{'phase':'RUNNING','heartbeat':1})
        writer=_HeartbeatWriter(path);output=StringIO()
        with patch('bench_gateway.deployment.save',side_effect=PermissionError('secret-must-not-appear')),patch('sys.stdout',output):
            for _ in range(3):self.assertFalse(writer.write({'secret':'never-log-this'}))
        self.assertEqual(json.loads(path.read_text())['heartbeat'],1)
        self.assertFalse(active(json.loads(path.read_text())))
        self.assertEqual(output.getvalue().count('heartbeat_write_failed'),1)
        self.assertNotIn('secret',output.getvalue())
        with patch('sys.stdout',output):self.assertTrue(writer.write({'phase':'RUNNING','heartbeat':time.time()}))
        self.assertEqual(output.getvalue().count('heartbeat_write_recovered'),1)
        self.assertTrue(active(json.loads(path.read_text())))

    def test_persistent_write_error_is_rate_limited(self):
        from io import StringIO
        output=StringIO();writer=_HeartbeatWriter(self.root/'status.json')
        with patch('bench_gateway.deployment.save',side_effect=OSError('private')),patch('bench_gateway.deployment.time.monotonic',side_effect=[10,20,40]),patch('sys.stdout',output):
            for _ in range(3):self.assertFalse(writer.write({}))
        self.assertEqual(output.getvalue().count('heartbeat_write_failed'),2)

    def test_non_io_programming_error_is_not_hidden(self):
        with patch('bench_gateway.deployment.save',side_effect=TypeError('invalid state')):
            with self.assertRaises(TypeError):_HeartbeatWriter(self.root/'status.json').write({})

    def test_unavailable_diagnostic_log_does_not_crash_heartbeat_recovery(self):
        writer=_HeartbeatWriter(self.root/'status.json')
        with patch('bench_gateway.deployment.save',side_effect=PermissionError()),patch('builtins.print',side_effect=OSError()):
            self.assertFalse(writer.write({}))
        with patch('builtins.print',side_effect=OSError()):self.assertTrue(writer.write({'recovered':True}))

    def test_draining_worker_does_not_claim_new_task(self):
        store=Store(self.cfg['state_root']);store.allow('ou_deploy_test')
        job,_=store.submit('ou_deploy_test','test','first',{'action':'replay'})
        drain=self.root/'runtime/service/drain.json';save(drain,{'instance':'test'})
        with patch.dict(os.environ,{'BENCH_GATEWAY_MANAGED':'1'}):run(self.cfg,store,once=True)
        self.assertEqual(store.job('ou_deploy_test',job['id'])['state'],'QUEUED')

    def test_probe_uses_worker_without_dispatching_hardware(self):
        store=Store(self.cfg['state_root']);owner='ou_deploy_test';store.allow(owner)
        report=store.user_root(owner)/'reports/check.txt';report.write_text('DEPLOYMENT ONLY',encoding='utf-8')
        job,_=store.submit(owner,'test','probe',{'action':'deployment_check','local_report':str(report),'report_sha256':digest(report)})
        with patch.object(Executor,'remote',side_effect=AssertionError('hardware must not run')):
            run(self.cfg,store,once=True)
        self.assertEqual(store.job(owner,job['id'])['state'],'SIMULATED')
        with store.connect() as c:self.assertEqual(c.execute('SELECT attachment FROM outbox').fetchone()[0],str(report))

    def test_probe_is_refused_in_live_mode(self):
        self.cfg.update(mode='live',live_enabled=True)
        with self.assertRaises(GatewayError):
            Executor(self.cfg,Store(self.cfg['state_root'])).execute({'payload':{'action':'deployment_check'}})

    def test_supervisor_restarts_bridge_and_stops_children(self):
        # Use a fake local bridge and a real isolated worker; no Feishu, SSH, or hardware.
        repo=Path(__file__).resolve().parents[1]
        fake="import pathlib,time; p=pathlib.Path('bridge-once'); first=not p.exists(); p.write_text('started'); print('[connected to wss://local-test]',flush=True); time.sleep(.3 if first else 120)"
        code="from bench_gateway import deployment as d;import os,sys;d.launch_command=lambda cfg:([sys.executable,'-u','-c',"+repr(fake)+"],os.environ.copy());d.supervise(d.load(sys.argv[1]))"
        env={**os.environ,'PYTHONPATH':str(repo),'PYTHONUTF8':'1'}
        with (self.root/'test-supervisor.log').open('wb') as output:
            process=subprocess.Popen([sys.executable,'-c',code,str(self.root)],cwd=self.root,env=env,stdout=output,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+18;current={}
            while time.monotonic()<deadline:
                current=status(self.cfg)
                if current['phase']=='RUNNING' and current.get('children',{}).get('bridge',{}).get('restarts',0)>=2:break
                if process.poll() is not None:self.fail((self.root/'test-supervisor.log').read_text(encoding='utf-8'))
                time.sleep(.2)
            self.assertEqual(current['phase'],'RUNNING')
            self.assertGreaterEqual(current['children']['bridge']['restarts'],2)
            self.assertEqual(current['children']['worker']['restarts'],1)
            result=stop(self.cfg)
            self.assertEqual(result['phase'],'STOPPED')
            self.assertTrue(all(not c['running'] for c in result['children'].values()))
            self.assertEqual(process.wait(timeout=5),0)
        finally:
            if process.poll() is None:
                stop(self.cfg)
                process.wait(timeout=15)


    def test_status_sharing_fault_does_not_restart_or_orphan_children(self):
        repo=Path(__file__).resolve().parents[1]
        fake="import time; print('[connected to wss://local-test]',flush=True); time.sleep(120)"
        code='''from bench_gateway import deployment as d
import os,sys,json,pathlib
d.launch_command=lambda cfg:([sys.executable,'-u','-c',FAKE],os.environ.copy())
original=d.save
anchor=pathlib.Path('first-healthy.json')
fault=pathlib.Path('fault-count')
failures=0
def sharing_fault(path,data):
 global failures
 if pathlib.Path(path).name=='status.json' and data.get('phase')=='RUNNING':
  if not anchor.exists(): original(anchor,data)
  elif failures<3:
   failures+=1;fault.write_text(str(failures))
   raise PermissionError('injected-sharing-error')
 return original(path,data)
d.save=sharing_fault
d.supervise(d.load(sys.argv[1]))
'''.replace('FAKE',repr(fake))
        env={**os.environ,'PYTHONPATH':str(repo),'PYTHONUTF8':'1'}
        log=self.root/'sharing-fault-supervisor.log'
        with log.open('wb') as output:
            process=subprocess.Popen([sys.executable,'-c',code,str(self.root)],cwd=self.root,env=env,stdout=output,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if process.poll() is not None:self.fail(log.read_text(encoding='utf-8'))
                if b'heartbeat_write_recovered' in log.read_bytes():break
                time.sleep(.2)
            else:self.fail('Status did not recover: '+log.read_text(encoding='utf-8'))
            first=json.loads((self.root/'first-healthy.json').read_text())
            current=status(self.cfg)
            self.assertEqual(current['instance'],first['instance'])
            self.assertEqual(current['phase'],'RUNNING')
            self.assertEqual((self.root/'fault-count').read_text(),'3')
            for role in ('bridge','worker'):
                self.assertEqual(current['children'][role]['pid'],first['children'][role]['pid'])
                self.assertEqual(current['children'][role]['restarts'],1)
            self.assertEqual(stop(self.cfg)['phase'],'STOPPED')
            self.assertEqual(process.wait(timeout=5),0)
        finally:
            if process.poll() is None:
                stop(self.cfg)
                process.wait(timeout=15)


if __name__=='__main__':unittest.main()
