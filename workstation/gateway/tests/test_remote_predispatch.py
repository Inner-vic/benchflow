import base64
import hashlib
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from bench_gateway import candidate_remote as remote,remote_agent


class RemotePredispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.m=SimpleNamespace(BASE=self.root/'runner',ROOT=self.root/'data',SAFE=re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}'))
        self.m.command=Mock(return_value={'active':None,'queued':[],'waiting':[]})
        self.req={'operation':'smoke','run_id':'job-current-smoke','baseline':{'kind':'candidate-v1'}}

    def test_positive_absence_proof_only_for_fresh_unactivated_precheck(self):
        proof=remote.no_previous_attempt(self.req,self.m)
        self.assertEqual(proof,{'schema_version':1,'code':'PREDISPATCH_REJECTED','run_id':'job-current-smoke',
                               'hardware_actions':'NOT_RUN','no_previous_attempt':True,'runner_idle':True})
        self.m.command.assert_called_once_with('queue')

    def test_any_own_run_install_adapter_or_task_evidence_prevents_proof(self):
        for path in (self.m.ROOT/'runs/job-current-smoke',self.m.BASE/'runs/job-current-smoke-install',
                     self.m.BASE/'adapters/job-current',self.m.ROOT/'tasks/job-current-smoke'):
            with self.subTest(path=path):
                path.mkdir(parents=True)
                self.assertIsNone(remote.no_previous_attempt(self.req,self.m))
                path.rmdir()

    def test_busy_unknown_or_locked_runner_never_proves_absence(self):
        for queue in ({},{'active':{'run_id':'other'},'queued':[],'waiting':[]},
                      {'active':None,'queued':[{'run_id':'other'}],'waiting':[]}):
            self.m.command.return_value=queue
            self.assertIsNone(remote.no_previous_attempt(self.req,self.m))
        self.m.command.return_value={'active':None,'queued':[],'waiting':[]}
        lock=self.m.BASE/'locks/bench.lock';lock.parent.mkdir(parents=True);lock.write_text('{}')
        self.assertIsNone(remote.no_previous_attempt(self.req,self.m))

    def test_activation_marker_full_replay_and_ambiguous_run_do_not_emit_proof(self):
        for change in ({'_dispatch_started':True},{'operation':'replay'},{'run_id':'../job-smoke'},
                       {'run_id':'some-run'},{'run_id':'-smoke'}):
            self.assertIsNone(remote.no_previous_attempt({**self.req,**change},self.m))

    def dispatch_error(self,started=False):
        raw=b'# harmless fixed module'
        request={**self.req,'owner_root':'/fixed/owner','batch_code':base64.b64encode(raw).decode(),
                 'candidate_implementation':{'batch':hashlib.sha256(raw).hexdigest()}}
        def execute(req,*args):
            if started:req['_dispatch_started']=True
            raise ValueError('frozen input differs')
        helper=SimpleNamespace(execute=execute,no_previous_attempt=remote.no_previous_attempt)
        modules={'candidate_remote':helper,'candidate_files':object(),'candidate_adapter':object()}
        with patch.object(remote_agent,'batch_module',return_value=self.m),patch.object(remote_agent,'candidate_modules',return_value=modules):
            return remote_agent.main(request)

    def test_remote_entry_preserves_typed_proof_only_before_activation(self):
        result=self.dispatch_error()
        self.assertEqual(result['state'],'BLOCKED')
        self.assertEqual(result['rejection']['run_id'],'job-current-smoke')
        with self.assertRaisesRegex(ValueError,'frozen input differs'):self.dispatch_error(started=True)

    def test_unknown_queue_does_not_convert_an_error_into_safe_failure(self):
        self.m.command.side_effect=OSError('Runner unavailable')
        with self.assertRaisesRegex(ValueError,'frozen input differs'):self.dispatch_error()


if __name__=='__main__':unittest.main()
