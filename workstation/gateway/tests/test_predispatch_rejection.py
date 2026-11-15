"""Only verified no-attempt receipts release a rejected job's hardware queue."""
import ast
import base64
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.core import GatewayError, PreDispatchRejected, Store
from bench_gateway.executor import Executor
from bench_gateway.worker import tick


class PreDispatchRejectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'bench_gateway').mkdir()
        (self.root / 'bench_gateway/remote_agent.py').write_text('print("unused")', encoding='utf-8')
        self.store = Store(self.root / 'state')
        self.users = ('ou_rejection_alice', 'ou_rejection_bob')
        for user in self.users:
            self.store.allow(user)
        self.cfg = {'project_root': str(self.root), 'state_root': str(self.store.root),
                    'mode': 'live', 'live_enabled': True, 'notifications_enabled': False,
                    'resource_probe_enabled': False, 'workstation': 'unused'}
        self.executor = Executor(self.cfg, self.store)
        self.executor.request = lambda owner, operation, **values: dict(owner=owner, operation=operation, **values)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def proof(run_id):
        return {'schema_version': 1, 'code': 'PREDISPATCH_REJECTED', 'run_id': run_id,
                'hardware_actions': 'NOT_RUN', 'no_previous_attempt': True, 'runner_idle': True}

    def blocked(self, request, proof=True, error='queued input changed; rebind materials'):
        result = {'state': 'BLOCKED', 'error': error}
        if proof:
            result['rejection'] = self.proof(request['run_id'])
        return SimpleNamespace(returncode=2, stdout=json.dumps(result))

    def request(self, **values):
        return {'owner': self.users[0], 'operation': 'smoke', 'run_id': 'job-a-smoke',
                'baseline': {'kind': 'candidate-v1'}, **values}

    def queue(self, user, key):
        payload = {'action': 'replay', 'assets': {'package': {'owner': user}, 'dependencies': [],
                   'dataset': {'owner': user}}, 'scenes': [1, 2],
                   'baseline': {'data': {'kind': 'candidate-v1', 'binding': {'input_digest': 'a' * 64}}}}
        return self.store.submit(user, 'feishu:oc_' + key + ':' + user, key, payload)[0]

    def transport(self, **options):
        def run(argv, **kwargs):
            tree = ast.parse(kwargs['input'])
            encoded = ast.literal_eval(tree.body[1].value)[1]
            request = json.loads(base64.b64decode(encoded))
            self.calls.append((request['owner'], request['operation']))
            if request['owner'] == self.users[0]:
                if request['operation'] == 'diagnose':
                    return SimpleNamespace(returncode=0, stdout=json.dumps({
                        'state': 'DIAGNOSED', 'safe_to_continue': False, 'summary': 'outcome unknown'}))
                if options.get('lost'):
                    return SimpleNamespace(returncode=255, stdout=json.dumps({
                        'state': 'BLOCKED', 'error': 'lost', 'rejection': self.proof(request['run_id'])}))
                return self.blocked(request, proof=options.get('proof', True),
                                    error=options.get('error', 'queued input changed; rebind materials'))
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                'state': 'PASS', 'run_id': request['run_id'], 'input_digest': 'a' * 64,
                'task': '/tasks/' + request['run_id']}))
        return run

    def test_changed_input_or_stale_implementation_only_fails_its_owner_job(self):
        for reason in ('queued SO hash changed', 'candidate implementation changed; rebind materials'):
            with self.subTest(reason=reason):
                first = self.queue(self.users[0], reason)
                following = self.queue(self.users[1], reason + '-next')
                with patch('bench_gateway.executor.subprocess.run', side_effect=self.transport(error=reason)):
                    self.assertEqual(first['id'], tick(self.cfg, self.store, self.executor))
                    rejected = self.store.job(self.users[0], first['id'])
                    self.assertEqual('FAILED', rejected['state'])
                    self.assertEqual('NOT_RUN', rejected['result']['rejection']['hardware_actions'])
                    self.assertIn('尚未刷包或回灌', rejected['result']['error'])
                    self.assertEqual(following['id'], tick(self.cfg, self.store, self.executor))
                self.assertEqual('SUCCEEDED', self.store.job(self.users[1], following['id'])['state'])
        self.assertNotIn((self.users[0], 'diagnose'), self.calls)
        self.assertEqual(2, self.calls.count((self.users[0], 'smoke')))
        self.assertEqual(2, self.calls.count((self.users[1], 'replay')))

    def test_blocked_without_proof_keeps_the_global_recovery_barrier(self):
        first = self.queue(self.users[0], 'unknown')
        following = self.queue(self.users[1], 'next')
        with patch('bench_gateway.executor.subprocess.run', side_effect=self.transport(proof=False)):
            tick(self.cfg, self.store, self.executor)
            self.assertIsNone(tick(self.cfg, self.store, self.executor))
        self.assertEqual('NEEDS_REVIEW', self.store.job(self.users[0], first['id'])['state'])
        self.assertEqual('QUEUED', self.store.job(self.users[1], following['id'])['state'])
        self.assertEqual([(self.users[0], 'smoke'), (self.users[0], 'diagnose')], self.calls)

    def test_ssh_loss_cannot_release_queue_even_with_proof_in_partial_output(self):
        first = self.queue(self.users[0], 'lost')
        following = self.queue(self.users[1], 'next')
        with patch('bench_gateway.executor.subprocess.run', side_effect=self.transport(lost=True)):
            tick(self.cfg, self.store, self.executor)
            self.assertIsNone(tick(self.cfg, self.store, self.executor))
        self.assertEqual('NEEDS_REVIEW', self.store.job(self.users[0], first['id'])['state'])
        self.assertEqual('QUEUED', self.store.job(self.users[1], following['id'])['state'])

    def test_complete_matching_proof_is_typed_and_preserved(self):
        request = self.request()
        with patch('bench_gateway.executor.subprocess.run', return_value=self.blocked(request)):
            with self.assertRaises(PreDispatchRejected) as caught:
                self.executor.remote(request, request['run_id'])
        self.assertEqual(self.proof(request['run_id']), caught.exception.rejection)

    def test_partial_forged_or_unexpected_proof_does_not_become_safe_rejection(self):
        request = self.request()
        original = json.loads(self.blocked(request).stdout)
        mutations = [
            lambda p: p['rejection'].pop('runner_idle'),
            lambda p: p['rejection'].update(schema_version=True),
            lambda p: p['rejection'].update(schema_version=2),
            lambda p: p['rejection'].update(code='INPUT_ERROR'),
            lambda p: p['rejection'].update(run_id='other-smoke'),
            lambda p: p['rejection'].update(hardware_actions='UNKNOWN'),
            lambda p: p['rejection'].update(no_previous_attempt=False),
            lambda p: p['rejection'].update(no_previous_attempt=1),
            lambda p: p['rejection'].update(runner_idle='true'),
            lambda p: p['rejection'].update(runner_idle=False),
            lambda p: p['rejection'].update(extra='unreviewed'),
        ]
        for index, mutate in enumerate(mutations):
            payload = copy.deepcopy(original)
            mutate(payload)
            response = SimpleNamespace(returncode=2, stdout=json.dumps(payload))
            with self.subTest(index=index), patch('bench_gateway.executor.subprocess.run', return_value=response):
                with self.assertRaises(GatewayError) as caught:
                    self.executor.remote(request, request['run_id'])
                self.assertNotIsInstance(caught.exception, PreDispatchRejected)

    def test_full_replay_legacy_or_started_dispatch_cannot_claim_no_hardware_action(self):
        requests = [self.request(operation='replay'), self.request(operation='diagnose'),
                    self.request(baseline={'kind': 'legacy'}), self.request(_dispatch_started=True)]
        for request in requests:
            with self.subTest(request=request), patch('bench_gateway.executor.subprocess.run', return_value=self.blocked(request)):
                with self.assertRaises(GatewayError) as caught:
                    self.executor.remote(request, request['run_id'])
                self.assertNotIsInstance(caught.exception, PreDispatchRejected)

    def test_proof_for_different_local_run_and_abnormal_exit_are_untrusted(self):
        request = self.request()
        response = self.blocked(request)
        with patch('bench_gateway.executor.subprocess.run', return_value=response):
            with self.assertRaises(GatewayError) as caught:
                self.executor.remote(request, 'other-local-run')
            self.assertNotIsInstance(caught.exception, PreDispatchRejected)
        response.returncode = 1
        with patch('bench_gateway.executor.subprocess.run', return_value=response):
            with self.assertRaises(GatewayError) as caught:
                self.executor.remote(request, request['run_id'])
            self.assertNotIsInstance(caught.exception, PreDispatchRejected)


if __name__ == '__main__':
    unittest.main()
