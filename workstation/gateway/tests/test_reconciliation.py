import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from bench_gateway.core import GatewayError, Store
from bench_gateway.executor import Executor
from bench_gateway.reconciliation import PROOF_FLAGS, recover_precheck


class PrecheckReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gateway-reconciliation-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.owner = 'ou_reconciliation_test'
        self.store.allow(self.owner)
        self.payload = {'action': 'replay', 'scenes': [2, 4], 'assets': {},
                        'baseline': {'data': {'kind': 'candidate-v1',
                                     'binding': {'input_digest': 'a' * 64}}}}
        job = self.store.submit(self.owner, 'private-session', 'test-request', self.payload)[0]
        self.job_id = job['id']
        self.store.finish(job, 'NEEDS_REVIEW', {'stage': '小场景预检中', 'error': 'SSH interrupted'})
        self.receipt = {'state': 'PASS', 'run_id': self.job_id + '-smoke',
                        'input_digest': 'a' * 64, **dict.fromkeys(PROOF_FLAGS, True)}
        self.executor = Mock()
        self.executor.request.side_effect = lambda owner, operation, **kw: {'owner': owner, 'operation': operation, **kw}
        self.executor.remote.return_value = self.receipt

    def recover(self):
        return recover_precheck({}, self.store, self.owner, self.job_id, self.executor)

    def job(self):
        return self.store.job(self.owner, self.job_id)

    def test_completed_smoke_requeues_original_job_and_only_full_will_run(self):
        original = self.job()
        out = self.recover()
        self.assertEqual('PRECHECK_RECOVERED', out['state'])
        self.assertEqual('FULL_REPLAY_ONLY', out['next_step'])
        job = self.job()
        self.assertEqual('QUEUED', job['state'])
        self.assertEqual(original['payload'], job['payload'])
        self.assertEqual(self.receipt, job['result']['smoke'])
        self.assertNotIn('error', job['result'])
        self.assertEqual('SSH interrupted', job['result']['precheck_reconciliation']['previous_error'])
        request = self.executor.remote.call_args.args[0]
        self.assertEqual('reconcile_precheck', request['operation'])
        self.assertEqual(self.job_id + '-smoke', request['run_id'])
        self.assertEqual(self.job_id, request['parent_run_id'])
        self.assertEqual([2], request['scenes'])
        self.assertIs(True, self.executor.remote.call_args.kwargs['read_only'])
        self.executor.execute.assert_not_called()
        # Run the real local pipeline using a fake remote. Its saved, verified
        # precheck must skip smoke and dispatch only this task's full run.
        worker = object.__new__(Executor)
        worker.cfg = {}
        worker.store = self.store
        worker.request = lambda owner, operation, **kw: {'operation': operation, **kw}
        worker.remote = Mock(return_value={'state': 'PASS', 'run_id': self.job_id, 'input_digest': 'a' * 64})
        worker.replay_pipeline(self.store.claim())
        self.assertEqual(['replay'], [call.args[0]['operation'] for call in worker.remote.call_args_list])
        self.assertEqual(1, len(self.store.jobs(self.owner)))

    def test_repeated_recovery_is_idempotent_without_remote_or_new_job(self):
        self.recover()
        before = self.job()
        self.executor.remote.reset_mock()
        out = self.recover()
        self.assertTrue(out['idempotent'])
        self.executor.remote.assert_not_called()
        self.assertEqual(before, self.job())
        self.assertEqual(1, len(self.store.jobs(self.owner)))

    def test_repeated_recovery_after_claim_does_not_requeue_running_task(self):
        self.recover()
        self.store.claim()
        self.executor.remote.reset_mock()
        self.assertEqual('RUNNING', self.recover()['job_state'])
        self.assertEqual('RUNNING', self.job()['state'])
        self.executor.remote.assert_not_called()

    def test_later_full_replay_unknown_outcome_is_never_requeued_by_smoke_recovery(self):
        self.recover()
        job = self.store.claim()
        self.store.finish(job, 'NEEDS_REVIEW', {**job['result'], 'error': 'full replay SSH lost'})
        self.executor.remote.reset_mock()
        out = self.recover()
        self.assertEqual('NEEDS_REVIEW', out['job_state'])
        self.assertEqual('RECONCILE_CURRENT_OUTCOME', out['next_step'])
        self.assertEqual('full replay SSH lost', self.job()['result']['error'])
        self.executor.remote.assert_not_called()

    def test_incomplete_wrong_run_or_wrong_binding_receipt_keeps_barrier(self):
        invalid = [{'state': 'FAIL'}, {'run_id': 'some-other-smoke'}, {'input_digest': 'b' * 64}]
        invalid += [{flag: False} for flag in PROOF_FLAGS]
        invalid += [{flag: 1} for flag in PROOF_FLAGS]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.executor.remote.return_value = {**self.receipt, **changes}
                with self.assertRaises(GatewayError):
                    self.recover()
                self.assertEqual('NEEDS_REVIEW', self.job()['state'])
                self.assertIsNone(self.store.claim())

    def test_missing_receipts_or_ssh_loss_never_requeues(self):
        for error in (GatewayError('cleanup receipt missing'), ConnectionError('SSH lost')):
            with self.subTest(error=type(error).__name__):
                self.executor.remote.side_effect = error
                with self.assertRaises(type(error)):
                    self.recover()
                self.assertEqual('NEEDS_REVIEW', self.job()['state'])

    def test_concurrent_evidence_change_is_not_overwritten(self):
        def change(_request, *_args, **_kwargs):
            self.store.finish(self.job(), 'NEEDS_REVIEW', {'error': 'new evidence needs review'})
            return self.receipt
        self.executor.remote.side_effect = change
        with self.assertRaisesRegex(GatewayError, '发生变化'):
            self.recover()
        self.assertEqual({'error': 'new evidence needs review'}, self.job()['result'])
        self.assertEqual('NEEDS_REVIEW', self.job()['state'])

    def test_concurrent_payload_change_is_not_overwritten(self):
        import json
        def change(_request, *_args, **_kwargs):
            with self.store.connect() as c:
                c.execute('UPDATE jobs SET payload=? WHERE id=?',
                          (json.dumps({**self.payload, 'scenes': [1]}), self.job_id))
            return self.receipt
        self.executor.remote.side_effect = change
        with self.assertRaisesRegex(GatewayError, '发生变化'):
            self.recover()
        self.assertEqual([1], self.job()['payload']['scenes'])
        self.assertEqual('NEEDS_REVIEW', self.job()['state'])

    def test_non_review_legacy_or_foreign_task_is_rejected_before_ssh(self):
        foreign = 'ou_someone_else'
        self.store.allow(foreign)
        with self.assertRaises(GatewayError):
            recover_precheck({}, self.store, foreign, self.job_id, self.executor)
        original = self.job()
        self.store.finish(original, 'FAILED', {'error': 'known failure'})
        with self.assertRaises(GatewayError):
            self.recover()
        import json
        with self.store.connect() as c:
            payload = copy.deepcopy(self.payload)
            payload['baseline']['data']['kind'] = 'legacy'
            c.execute("UPDATE jobs SET state='NEEDS_REVIEW',payload=? WHERE id=?", (json.dumps(payload), self.job_id))
        with self.assertRaises(GatewayError):
            self.recover()
        self.executor.remote.assert_not_called()

    def test_recovery_invalidates_old_notice_without_allowing_stale_ack(self):
        with self.store.connect() as c:
            old = dict(c.execute('SELECT * FROM outbox WHERE job=?', (self.job_id,)).fetchone())
        self.recover()
        with self.store.connect() as c:
            now = dict(c.execute('SELECT * FROM outbox WHERE job=?', (self.job_id,)).fetchone())
            stale_ack = c.execute('UPDATE outbox SET sent=1 WHERE id=? AND version=?', (old['id'], old['version'])).rowcount
        self.assertEqual(1, now['sent'])
        self.assertGreater(now['version'], old['version'])
        self.assertEqual(0, stale_ack)
        self.store.finish(self.job(), 'SUCCEEDED', {'task': '/workstation/real-task'})
        with self.store.connect() as c:
            latest = dict(c.execute('SELECT * FROM outbox WHERE job=?', (self.job_id,)).fetchone())
        self.assertEqual(0, latest['sent'])
        self.assertGreater(latest['version'], now['version'])

    def test_modified_saved_reconciliation_receipt_is_not_accepted_as_idempotent(self):
        import json
        self.recover()
        job = self.job()
        job['result']['smoke']['task'] = 'changed'
        with self.store.connect() as c:
            c.execute('UPDATE jobs SET result=? WHERE id=?', (json.dumps(job['result']), self.job_id))
        with self.assertRaisesRegex(GatewayError, '不一致'):
            self.recover()


if __name__ == '__main__':
    unittest.main()
