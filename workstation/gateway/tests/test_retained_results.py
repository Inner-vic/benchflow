"""Only explicitly registered owners can consume an existing verified product."""
import concurrent.futures
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bench_gateway.core import GatewayError, Store
from bench_gateway.retained_results import register_result


class RetainedResultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cfg = {'state_root': str(Path(self.temporary.name) / 'state')}
        self.store = Store(self.cfg['state_root'])
        self.user, self.other = 'ou_retained_alice', 'ou_retained_bob'
        self.store.allow(self.user)
        self.store.allow(self.other)
        self.task = '/mnt/data/bench-eval/tasks/retained-18'
        self.prediction = '/mnt/data/bench-eval/cache/predictions/retained-18/predictions'
        self.source = {'producer_run_id': 'retained-18', 'source_task': self.task, 'task': self.task,
            'prediction_path': self.prediction, 'product': self.prediction,
            'product_receipt': self.prediction + '.bench-eval-product.json',
            'product_receipt_sha256': '1' * 64, 'source_identity': '2' * 64,
            'product_identity': '3' * 64, 'index_sha256': '4' * 64,
            'job_sha256': '5' * 64, 'prepared_manifest_sha256': '6' * 64,
            'scene_names': ['scene-01', 'scene-02'], 'scene_count': 2,
            'discovery': {'required_tasks': ['trajectory', 'vision'], 'warnings': ['VISION_GT_MISSING']},
            'hardware_actions': 'NOT_RUN'}
        self.evaluator = {'provider': 'workstation-offline-v1', 'contract_sha256': '7' * 64}
        self.provider = SimpleNamespace(
            pin_contract=Mock(return_value={'discovery': {'tasks_root': '/mnt/data/bench-eval/tasks'}}),
            inspect_task=Mock(side_effect=lambda *args, **kwargs: copy.deepcopy(self.source)),
            descriptor=Mock(side_effect=lambda cfg: copy.deepcopy(self.evaluator)))
        provider_patch = patch('bench_gateway.retained_results._provider', return_value=self.provider)
        self.mock_provider = provider_patch.start()
        self.addCleanup(provider_patch.stop)

    def register(self, user=None, **kwargs):
        return register_result(self.cfg, self.store, user or self.user, self.task, **kwargs)

    def counts(self):
        with self.store.connect() as connection:
            return {name: connection.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                    for name in ('jobs', 'baselines', 'outbox', 'queue_outbox', 'assets')}

    def test_registration_records_verified_source_without_new_hardware_or_notification(self):
        job = self.register(label='已授权的历史18场景')
        self.provider.inspect_task.assert_called_once_with(self.cfg, self.task, expected_run='retained-18')
        self.assertEqual('SUCCEEDED', job['state'])
        self.assertIs(True, job['payload']['retained_result'])
        self.assertIs(True, job['result']['registration_only'])
        self.assertEqual('PASS', job['result']['state'])
        self.assertEqual('retained-18', job['result']['run_id'])
        self.assertEqual(self.task, job['result']['task'])
        self.assertEqual(self.prediction, job['result']['product'])
        self.assertEqual(self.source['product_receipt'], job['result']['receipt'])
        self.assertEqual(self.source['discovery'], job['result']['discovery'])
        self.assertEqual(self.source['source_identity'], job['payload']['source_identity'])
        self.assertIsNone(self.store.claim(), 'a registered result must never be dispatched as hardware work')
        self.assertEqual({'jobs': 1, 'baselines': 1, 'outbox': 0, 'queue_outbox': 0, 'assets': 0}, self.counts())
        self.assertIn('本次未刷包、未回灌、未执行评测', job['result']['registration_message'])

    def test_retained_baseline_is_owner_scoped_and_is_not_a_replay_baseline(self):
        job = self.register()
        baseline = self.store.baseline(self.user, job['payload']['baseline']['name'])
        self.assertEqual(self.user, baseline['owner'])
        self.assertEqual('retained-result-v1', baseline['data']['kind'])
        self.assertIs(False, baseline['data']['gate_verified'])
        self.assertEqual({}, baseline['data']['asset_ids'])
        self.assertEqual(self.evaluator, baseline['data']['evaluator'])
        self.assertNotIn('assets', job['payload'])
        with self.assertRaises(GatewayError):
            self.store.baseline(self.other, baseline['name'])
        with self.assertRaises(GatewayError):
            self.store.job(self.other, job['id'])
        self.assertEqual([], self.store.jobs(self.other))

    def test_same_owner_source_identity_is_idempotent_even_if_label_changes(self):
        first = self.register(label='首次名称')
        second = self.register(label='新的显示名称')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual('首次名称', second['payload']['label'])
        self.assertEqual(1, self.counts()['jobs'])
        self.assertEqual(1, self.counts()['baselines'])
        self.assertEqual(2, self.provider.inspect_task.call_count, 'repeat registration must recheck source identity')

    def test_explicit_second_owner_registration_is_separate_not_global_admission(self):
        first = self.register()
        self.assertEqual([], self.store.jobs(self.other))
        second = self.register(user=self.other)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.other, second['owner'])
        self.assertNotEqual(first['payload']['baseline']['id'], second['payload']['baseline']['id'])
        self.assertEqual(1, len(self.store.jobs(self.user)))
        self.assertEqual(1, len(self.store.jobs(self.other)))

    def test_content_change_creates_new_source_without_overwriting_old_identity(self):
        first = self.register()
        original = copy.deepcopy(first)
        self.source['source_identity'] = '8' * 64
        self.source['product_identity'] = '9' * 64
        self.source['product_receipt_sha256'] = 'a' * 64
        second = self.register()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(original, self.store.job(self.user, first['id']))
        self.assertEqual('8' * 64, second['payload']['source_identity'])
        self.assertEqual(2, self.counts()['baselines'])
        self.assertEqual(0, self.counts()['outbox'])

    def test_tool_change_cannot_silently_rewrite_existing_registration(self):
        first = self.register()
        self.evaluator['contract_sha256'] = '9' * 64
        with self.assertRaisesRegex(GatewayError, '工具身份已变化'):
            self.register()
        self.assertEqual(first, self.store.job(self.user, first['id']))
        self.assertEqual(1, self.counts()['jobs'])

    def test_path_must_be_one_safe_task_below_fixed_root(self):
        invalid = ['/mnt/data/bench-eval/tasks', '/mnt/data/bench-eval/tasks/retained-18/scenes',
                   '/mnt/data/bench-eval/tasks/../other/run', '/somewhere/retained-18',
                   '/mnt/data/bench-eval/tasks/.hidden', '/mnt/data/bench-eval/tasks/retained-18/',
                   '/mnt/data/bench-eval/tasks//retained-18', 'C:\\tasks\\run',
                   '/mnt/data/bench-eval/tasks/unsafe;command']
        for task in invalid:
            with self.subTest(task=task), self.assertRaises(GatewayError):
                register_result(self.cfg, self.store, self.user, task)
        self.provider.inspect_task.assert_not_called()
        self.assertEqual(0, self.counts()['jobs'])

    def test_disabled_or_unknown_owner_is_rejected_before_inspecting_source(self):
        with self.assertRaises(GatewayError):
            self.register(user='ou_not_provisioned')
        with self.store.connect() as connection:
            connection.execute('UPDATE users SET enabled=0 WHERE id=?', (self.user,))
        with self.assertRaises(GatewayError):
            self.register()
        self.mock_provider.assert_not_called()
        self.assertEqual(0, self.counts()['jobs'])

    def test_access_disabled_during_inspection_does_not_commit_source(self):
        def inspect(*args, **kwargs):
            with self.store.connect() as connection:
                connection.execute('UPDATE users SET enabled=0 WHERE id=?', (self.user,))
            return self.source
        self.provider.inspect_task.side_effect = inspect
        with self.assertRaisesRegex(GatewayError, '当前不可用'):
            self.register()
        self.assertEqual(0, self.counts()['jobs'])
        self.assertEqual(0, self.counts()['baselines'])

    def test_failed_inspection_never_records_success_or_baseline(self):
        self.provider.inspect_task.side_effect = GatewayError('产品收据缺失')
        with self.assertRaisesRegex(GatewayError, '产品收据缺失'):
            self.register()
        self.provider.descriptor.assert_not_called()
        self.assertEqual(0, self.counts()['jobs'])
        self.assertEqual(0, self.counts()['baselines'])

    def test_malformed_source_receipt_is_rejected_before_transaction(self):
        original = copy.deepcopy(self.source)
        updates = [{'producer_run_id': 'other-run'}, {'task': '/other/tasks/run'},
                   {'hardware_actions': 'EXECUTED'}, {'source_identity': 'not-a-hash'},
                   {'product': '/different/predictions'}, {'product_receipt': '/different/receipt.json'},
                   {'scene_names': ['scene-01', 'scene-01']}, {'scene_count': 3}, {'scene_count': True}]
        for update in updates:
            with self.subTest(update=update):
                self.source = {**copy.deepcopy(original), **update}
                with self.assertRaises(GatewayError):
                    self.register()
        self.assertEqual(0, self.counts()['jobs'])
        self.assertEqual(0, self.counts()['baselines'])

    def test_parallel_registration_is_idempotent_and_never_enqueues_notifications(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = list(pool.map(lambda _: self.register(), range(2)))
        self.assertEqual(jobs[0]['id'], jobs[1]['id'])
        self.assertEqual({'jobs': 1, 'baselines': 1, 'outbox': 0, 'queue_outbox': 0, 'assets': 0}, self.counts())


if __name__ == '__main__':
    unittest.main()
