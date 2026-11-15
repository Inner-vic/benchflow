import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from bench_gateway import candidate_remote
from bench_gateway.reconciliation import PROOF_FLAGS


class RemotePrecheckReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gateway-remote-reconcile-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        source = Path(__file__).resolve().parents[1] / 'vendor/run_stream_batch.py'
        spec = importlib.util.spec_from_file_location('isolated_reconciliation_batch', source)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.m.BASE, self.m.ROOT = root / 'workstation', root / 'data'
        self.parent, self.run_id = 'gw-precheck-test', 'gw-precheck-test-smoke'
        self.run = self.m.ROOT / 'runs' / self.run_id
        self.product = self.m.ROOT / 'cache/predictions' / self.run_id / 'predictions'
        self.product_receipt = self.product.parent / 'predictions.bench-eval-product.json'
        self.task = self.m.ROOT / 'tasks' / self.run_id
        self.cleanup = self.m.ROOT / 'maintenance' / (self.run_id + '-cleanup.json')
        self.evidence = self.m.BASE / 'adapters' / self.run_id / 'evidence' / self.run_id
        self.plan = {'run_id': self.run_id, 'task_id': self.run_id, 'source_run': 'historical-template',
                     'scenes': [{'index': 2, 'scene': 'scene-two'}],
                     'required_output_types': ['pred_traj', 'traj_xy'], 'resume_delivery': False}
        self.req = {'operation': 'reconcile_precheck', 'parent_run_id': self.parent,
                    'run_id': self.run_id, 'scenes': [2], 'actor': 'operator-test'}
        self.binding = {'scenes': [2, 4], 'frozen': {'input_digest': 'a' * 64}, 'candidate': {}}
        for kind in ('pred_traj', 'traj_xy'):
            path = self.product / 'scene-two' / (kind + '_0001.txt')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic product ' + kind, encoding='utf-8')
        self.write(self.run / 'state.json', {'state': 'COMPLETED_TO_REQUESTED_GOAL'})
        self.write(self.run / 'restore-verification.json', {'restore_ok': True})
        self.write(self.run / 'sw0058-profile-precheck.json', {'run_id': self.run_id,
                   'required_output_types': ['pred_traj', 'traj_xy']})
        self.write(self.evidence / 'batch-result.json', {'expected': 1,
                   'completed': [{'scene': 'scene-two', 'state': 'REPLAY_COMPLETE'}]})
        for name in ('job.json', 'profile.json'):
            self.write(self.m.BASE / 'adapters' / self.run_id / name, {'run_id': self.run_id})
        for name in ('configured-inputs/common_param.probe.yaml', 'logs/worker.log', 'probe.log', 'events.jsonl'):
            path = self.run / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic evidence\n', encoding='utf-8')
        (self.run / 'logs/worker-exit-code.txt').write_text('0\n', encoding='utf-8')
        files = [{'path': path.relative_to(self.product).as_posix(), 'size': path.stat().st_size,
                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in sorted(self.product.rglob('*')) if path.is_file()]
        inventory = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()
        self.producer = {'state': 'SUCCEEDED', 'producer_run_id': self.run_id,
                         'workstation_product': str(self.product), 'inventory_sha256': inventory,
                         'product_identity': 'synthetic-precheck-product', 'files': files,
                         'file_count': len(files), 'source': '/app/livc-test/' + self.run_id + '/outputs/eval_pred_result'}
        self.write(self.product_receipt, self.producer)
        self.write(self.run / 'workstation-predictions.json', {'state': 'WORKSTATION_PREDICTIONS_COLLECTED',
                   'inventory_sha256': inventory, 'product_identity': self.producer['product_identity'], 'file_count': len(files)})
        self.task.parent.mkdir(parents=True)
        # Build a real retained product using the actual batch validator, with
        # synthetic files only. Recovery itself must never create this task.
        self.m.validate_and_retain(self.plan, 0)
        self.plan['resume_delivery'] = True
        self.cleanup_data = {'batch_id': self.run_id + '-cleanup', 'accepted': [{
            'run_id': self.run_id, 'source': self.producer['source'], 'workstation_product': str(self.product),
            'product_receipt': str(self.product_receipt), 'inventory_sha256': inventory}],
            'removed': [self.producer['source']]}
        self.publish_cleanup()
        self.m.command = Mock(return_value={'active': [], 'queued': [], 'waiting': []})
        self.m.execute = Mock(side_effect=AssertionError('recovery cannot replay'))
        self.m.finish_delivery = Mock(side_effect=AssertionError('recovery cannot run cleanup'))
        self.m.bench_capacity = Mock(side_effect=AssertionError('recovery cannot inspect a bench'))

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding='utf-8')

    def publish_cleanup(self):
        self.write(self.cleanup, self.cleanup_data)
        self.write(self.task / 'logs/bench-output-cleanup.json', self.cleanup_data)

    def recover(self):
        with patch('subprocess.run', side_effect=AssertionError('hardware subprocess forbidden')):
            return candidate_remote.recovered_precheck(self.req, self.m, self.plan, self.binding)

    def test_complete_retained_precheck_passes_without_hardware_or_file_changes(self):
        before = {path: path.read_bytes() for path in self.m.ROOT.rglob('*') if path.is_file()}
        result = self.recover()
        self.assertEqual('PASS', result['state'])
        self.assertEqual(self.run_id, result['run_id'])
        self.assertEqual('a' * 64, result['input_digest'])
        self.assertEqual('NOT_RUN', result['hardware_actions'])
        self.assertTrue(all(result[flag] is True for flag in PROOF_FLAGS))
        self.assertEqual(before, {path: path.read_bytes() for path in self.m.ROOT.rglob('*') if path.is_file()})
        self.assertEqual([('queue',), ('queue',)], [call.args for call in self.m.command.call_args_list])
        self.m.execute.assert_not_called()
        self.m.finish_delivery.assert_not_called()
        self.m.bench_capacity.assert_not_called()

    def test_missing_or_changed_cleanup_proof_does_not_clean_or_resume(self):
        for path in (self.cleanup, self.task / 'logs/bench-output-cleanup.json'):
            raw = path.read_bytes()
            path.unlink()
            with self.assertRaisesRegex(ValueError, 'cleanup'):
                self.recover()
            path.write_bytes(raw)
        (self.task / 'logs/bench-output-cleanup.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'cleanup'):
            self.recover()
        self.m.finish_delivery.assert_not_called()

    def test_other_task_wrong_scene_and_cleanup_inventory_are_rejected(self):
        for changes in ({'parent_run_id': 'old-parent'}, {'run_id': 'old-smoke'}, {'scenes': [4]}):
            original = self.req
            self.req = {**original, **changes}
            with self.assertRaisesRegex(ValueError, 'this task'):
                self.recover()
            self.req = original
        for changes in ({'run_id': 'old-smoke'}, {'inventory_sha256': 'b' * 64}, {'product_receipt': '/other/receipt'}):
            original = copy.deepcopy(self.cleanup_data)
            self.cleanup_data['accepted'][0].update(changes)
            self.publish_cleanup()
            with self.assertRaisesRegex(ValueError, 'differs'):
                self.recover()
            self.cleanup_data = original

    def test_changed_output_or_failed_restoration_cannot_use_success_receipt(self):
        output = self.product / 'scene-two/pred_traj_0001.txt'
        original = output.read_bytes()
        output.write_bytes(b'x' * len(original))
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            self.recover()
        output.write_bytes(original)
        self.write(self.run / 'restore-verification.json', {'restore_ok': False})
        with self.assertRaisesRegex(RuntimeError, 'restoration'):
            self.recover()

    def test_missing_task_and_nonzero_worker_are_not_recreated_or_retried(self):
        index = self.task / 'index.json'
        raw = index.read_bytes()
        index.unlink()
        with self.assertRaisesRegex(ValueError, 'retained task'):
            self.recover()
        self.assertFalse(index.exists())
        index.write_bytes(raw)
        exit_code = self.run / 'logs/worker-exit-code.txt'
        exit_code.write_text('1\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'worker completion'):
            self.recover()

    def test_busy_incomplete_unknown_queue_or_lock_keeps_recovery_pending(self):
        for queue in ({'active': [{'run_id': 'colleague'}], 'queued': [], 'waiting': []},
                      {'active': [], 'queued': ['other'], 'waiting': []},
                      {'active': [], 'queued': [], 'waiting': ['other']}, {}, None):
            self.m.command.return_value = queue
            with self.subTest(queue=queue), self.assertRaisesRegex(ValueError, 'verified idle'):
                self.recover()
        self.m.command.return_value = {'active': [], 'queued': [], 'waiting': []}
        lock = self.m.BASE / 'locks/bench.lock'
        lock.parent.mkdir(parents=True)
        lock.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'verified idle'):
            self.recover()

    def test_resource_change_during_hash_validation_is_rejected(self):
        self.m.command.side_effect = [
            {'active': [], 'queued': [], 'waiting': []},
            {'active': [{'run_id': 'other'}], 'queued': [], 'waiting': []}]
        with self.assertRaisesRegex(ValueError, 'changed during'):
            self.recover()

    def test_execute_dispatch_uses_receipt_recovery_branch_without_install_or_replay(self):
        install, matches = Mock(), Mock()
        with patch.object(candidate_remote, 'load_binding', return_value=(self.binding, {})), \
             patch.object(candidate_remote, 'make_plan', return_value=self.plan) as make_plan, \
             patch('subprocess.run', side_effect=AssertionError('hardware subprocess forbidden')):
            result = candidate_remote.execute(self.req, self.m, None, Mock(), None, install, matches)
        self.assertEqual('PASS', result['state'])
        self.assertTrue(make_plan.call_args.args[-1])
        install.assert_not_called()
        matches.assert_not_called()
        self.m.execute.assert_not_called()
        self.m.finish_delivery.assert_not_called()


if __name__ == '__main__':
    unittest.main()
