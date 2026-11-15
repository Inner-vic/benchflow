"""Dispatch ordering and early failure, with synthetic inputs and no bench."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from bench_gateway.candidate_adapter import STARTUP_MONITOR,render


class DispatchTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[1]/'vendor/run_stream_batch.py'
        spec=importlib.util.spec_from_file_location('lean_batch_test',path)
        self.batch=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.batch)
        self.batch.progress=Mock()
        self.batch.command=Mock(side_effect=[{'status':'READY'},{'active':[],'queued':[],'waiting':[]}])
        self.batch.bench_capacity=Mock()
        self.batch.offline_validate=Mock()
        self.batch.prepare=Mock(side_effect=RuntimeError('stop before submission'))

    def test_one_preflight_precedes_activation(self):
        sequence=[]
        self.batch.bench_capacity.side_effect=lambda:sequence.append('capacity')
        self.batch.offline_validate.side_effect=lambda p:sequence.append('offline')
        before=Mock(side_effect=lambda:sequence.append('activate'))
        with self.assertRaisesRegex(RuntimeError,'stop before submission'):
            self.batch.execute({},before_submit=before)
        self.assertEqual(['capacity','offline','activate'],sequence)
        self.batch.bench_capacity.assert_called_once()
        self.assertEqual([('doctor',),('queue',)],[c.args for c in self.batch.command.call_args_list])

    def test_busy_or_invalid_plan_cannot_install(self):
        before=Mock()
        self.batch.command.side_effect=[{'status':'READY'},{'active':[{'run_id':'other'}]}]
        self.assertEqual({'state':'RESOURCE_BUSY'},self.batch.execute({},before_submit=before))
        before.assert_not_called();self.batch.prepare.assert_not_called()
        self.batch.command.side_effect=[{'status':'READY'},{'active':[],'queued':[],'waiting':[]}]
        self.batch.offline_validate.side_effect=ValueError('invalid generated plan')
        with self.assertRaises(ValueError):self.batch.execute({},before_submit=before)
        before.assert_not_called();self.batch.prepare.assert_not_called()

    def test_post_submit_race_is_still_blocked(self):
        self.batch.prepare=Mock(return_value=(Path('job.json'),Path('profile.json')))
        self.batch.command.side_effect=[{'status':'READY'},{'active':[]}, {},{},{},
                                       {'active':[{'run_id':'other'}],'queued':[]}]
        with self.assertRaisesRegex(RuntimeError,'sole READY'),patch.object(self.batch.subprocess,'Popen') as worker:
            self.batch.execute({'actor':'owner','run_id':'new'},before_submit=Mock())
        worker.assert_not_called()


class StartupMonitorTests(unittest.TestCase):
    def run_monitor(self, initial, chunks, *, ready=False, baseline_exists=True):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name)/'startup.log';path.write_text(initial,encoding='utf-8')
            s=path.stat()
            baseline={'device':s.st_dev,'inode':s.st_ino,'offset':s.st_size} if baseline_exists else {'inode':None,'offset':0}
            clock=[0];changes=list(chunks)
            def sleep(seconds):
                clock[0]+=seconds
                if changes:
                    change=changes.pop(0)
                    if callable(change):change(path)
                    else:
                        with path.open('a',encoding='utf-8') as f:f.write(change)
            out=io.StringIO()
            source=STARTUP_MONITOR.replace('/app/eval-LC/logs/infer_expert_pcm_replay.stdout.log',path.as_posix())
            connection=Mock()
            connection.__enter__=Mock(return_value=connection);connection.__exit__=Mock(return_value=False)
            with patch.object(sys,'argv',['monitor',json.dumps(baseline)]), \
                 patch('time.monotonic',side_effect=lambda:clock[0]),patch('time.sleep',side_effect=sleep), \
                 patch.object(os,'O_NOFOLLOW',getattr(os,'O_NOFOLLOW',0),create=True), \
                 patch.object(os,'O_NONBLOCK',getattr(os,'O_NONBLOCK',0),create=True), \
                 patch.object(socket,'create_connection',return_value=connection,
                              side_effect=None if ready else OSError('synthetic closed port')),contextlib.redirect_stdout(out):
                exec(compile(source,'isolated-startup-monitor','exec'),{})
            return json.loads(out.getvalue()),clock[0]

    def test_current_restart_stops_in_seconds_instead_of_timeout(self):
        report,seconds=self.run_monitor('',[
            'formal_replay_wrapper_started pid=101\n',
            'formal_replay_wrapper_started pid=102\n'])
        self.assertEqual('RESTART_LOOP',report['state']);self.assertLess(seconds,5)

    def test_old_crash_is_ignored_and_healthy_ports_succeed(self):
        report,seconds=self.run_monitor('formal_replay_wrapper_started pid=1\nSIGABRT\n',[],ready=True)
        self.assertEqual('READY',report['state']);self.assertEqual(0,seconds)
        self.assertEqual(0,report['observed_starts'])

    def test_current_fatal_is_reported_without_repair_or_retry(self):
        report,seconds=self.run_monitor('',[
            'formal_replay_wrapper_started pid=103\n__stack_chk_fail\n'])
        self.assertEqual('APPLICATION_FATAL',report['state']);self.assertLess(seconds,3)

    def test_both_ports_share_one_180_second_deadline(self):
        report,seconds=self.run_monitor('',[])
        self.assertEqual('ENDPOINT_UNAVAILABLE',report['state']);self.assertEqual(180,seconds)

    def test_log_created_for_this_launch_is_accepted(self):
        report,_=self.run_monitor('',[
            'formal_replay_wrapper_started pid=103\nSIGABRT\n'],baseline_exists=False)
        self.assertEqual('APPLICATION_FATAL',report['state'])

    def test_replaced_log_is_rejected_instead_of_reading_stale_bytes(self):
        def replace(path):
            other=path.with_name('replacement.log');other.write_text('unrelated\n',encoding='utf-8');os.replace(other,path)
        report,_=self.run_monitor('old log\n',[replace])
        self.assertEqual('STARTUP_LOG_CHANGED',report['state'])

    def test_generated_health_persists_failure_before_runner_recovers(self):
        import ast
        from test_candidate_adapter import fixture
        template,candidate=fixture();source=render(template,candidate,'health-test','owner',[1])['adapter']
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='health_candidate')
        function='from __future__ import annotations\n'+''.join(source.splitlines(keepends=True)[node.lineno-1:node.end_lineno])
        case=Mock();case.ProbeError=RuntimeError;case.shlex=__import__('shlex')
        case.bench.return_value='{"state":"RESTART_LOOP","observed_starts":2}'
        scope={'case':case,'identity':Mock(),'require_runner_lock':Mock(),'read':Mock(return_value={'inode':None,'offset':0}),'json':json}
        exec(compile(function,'generated-health','exec'),scope)
        with self.assertRaisesRegex(RuntimeError,'RESTART_LOOP'):scope['health_candidate'](Path('/runs/health-test'),'scene-one')
        self.assertEqual('RESTART_LOOP',case.write_json.call_args.args[1]['state'])
        case.bench.assert_called_once();scope['require_runner_lock'].assert_called_once_with('health-test')


if __name__=='__main__':unittest.main()
