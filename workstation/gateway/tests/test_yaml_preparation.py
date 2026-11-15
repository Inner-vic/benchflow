"""Integration boundaries for optional YAML task copies and replay admission."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.core import GatewayError, Store
from bench_gateway.intake import Intake
from bench_gateway.preparation import MaterialCheck, PreparationQueue, fingerprint
from bench_gateway.service import Service


class YamlPreparationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(__file__).resolve().parents[1]
        self.cfg = {'state_root': str(Path(self.tmp.name) / 'state'),
                    'project_root': str(self.project), 'mode': 'simulate',
                    'live_enabled': False, 'notifications_enabled': False,
                    'remote_users_root': '/data/users'}
        self.store = Store(self.cfg['state_root'])
        self.user = 'ou_yaml_alice'
        self.store.allow(self.user)
        self.owner_root = self.store.remote_root(self.user, '/data/users')
        self.preparation_id = 'prep-yaml-integration'
        self.package_sha = 'a' * 64
        self.draft = {'package': 'package-A', 'camera_so': 'camera-A',
                      'lidar_so': 'lidar-A', 'm100_so': 'm100-A',
                      'dataset': '共享18场景', 'scene_scope': '1-3',
                      'yaml_request': '将 config/replay.yaml 的 camera.fps 改为 10'}
        self.plan = {'status': 'READY', 'question': '', 'operations': [
            {'operation': 'set', 'target': 'config/replay.yaml',
             'path': ['camera', 'fps'], 'value_json': '10', 'source': ''}]}
        task_dir = self.owner_root + '/preparations/' + self.preparation_id + '/yaml/batch'
        self.receipt = {'schema_version': 1, 'status': 'PREPARED',
                        'scope': 'task-copy-only', 'package_modified': False,
                        'bench_modified': False, 'preparation_id': self.preparation_id,
                        'package_sha256': self.package_sha, 'receipt_path': task_dir + '/receipt.json',
                        'changes': [{'target': 'config/replay.yaml', 'operation': 'set',
                                     'path': ['camera', 'fps']}], 'issues': [],
                        'outputs': [{'target': 'config/replay.yaml', 'path': task_dir + '/candidate-00.yaml',
                                     'sha256': 'b' * 64, 'before_sha256': 'c' * 64,
                                     'diff_path': task_dir + '/candidate-00.diff',
                                     'diff_sha256': 'd' * 64, 'size_bytes': 32}]}

    def fake_service(self, missing_package=False):
        assets = []
        for field, name in [('package', 'package-A'), ('camera_so', 'camera-A'),
                            ('lidar_so', 'lidar-A'), ('m100_so', 'm100-A')]:
            if field == 'package' and missing_package:
                continue
            target = {'camera_so': 'lib/libxcamera.so', 'lidar_so': 'lib/libxlidar.so',
                      'm100_so': 'lib/liburoad_m100.so'}.get(field)
            data = {'remote_path': self.owner_root + '/' + field + '/asset.bin',
                    'sha256': self.package_sha if field == 'package' else 'e' * 64}
            if target:
                data['target'] = target
            assets.append({'id': field, 'kind': 'package' if field == 'package' else 'dependency',
                           'name': name, 'owner': self.user, 'version': 1, 'data': data})
        # The real inspector is covered separately. This transport deliberately
        # has no bench API and only supplies its bounded static-check receipt.
        ssh = SimpleNamespace(python=lambda *args, **kwargs: json.dumps(
            {'schema_version': 1, 'status': 'PASS', 'checks': [], 'issues': []}))
        registry = SimpleNamespace(ssh=ssh, list=lambda user: assets)
        dataset = {'origin': 'dataset_catalog', 'data': {'scene_count': 18}}
        return SimpleNamespace(registry=registry, datasets=SimpleNamespace(ssh=ssh),
                               asset=lambda *args: dataset)

    def run_check(self, draft=None, planned=None, receipt=None, missing_package=False):
        with patch('bench_gateway.service.Service', return_value=self.fake_service(missing_package)), \
             patch('bench_gateway.yaml_plan.plan', return_value=planned or self.plan) as planner, \
             patch('bench_gateway.yaml_runtime.prepare', return_value=copy.deepcopy(self.receipt if receipt is None else receipt)) as prepare:
            result = MaterialCheck(self.cfg, self.store).run(
                {'id': self.preparation_id, 'owner': self.user,
                 'payload': self.draft if draft is None else draft})
        return result, planner, prepare

    def test_prepared_candidate_is_bound_to_owner_task_and_does_not_enqueue_hardware(self):
        result, planner, prepare = self.run_check()
        self.assertEqual(result['status'], 'STATIC_PASSED')
        self.assertEqual(result['hardware_actions'], 'NOT_RUN')
        self.assertEqual(result['runtime_compatibility'], 'NOT_VERIFIED')
        self.assertEqual(self.store.jobs(self.user), [])
        planner.assert_called_once()
        prepare.assert_called_once()
        request = prepare.call_args.args[2]
        self.assertEqual(request['owner_root'], self.owner_root)
        self.assertEqual(request['preparation_id'], self.preparation_id)
        self.assertEqual(request['package']['sha256'], self.package_sha)
        self.assertEqual(request['plan'], self.plan)

    def test_no_yaml_request_preserves_static_material_path(self):
        draft = dict(self.draft)
        draft.pop('yaml_request')
        result, planner, prepare = self.run_check(draft=draft)
        self.assertEqual(result['status'], 'STATIC_PASSED')
        self.assertEqual(result['yaml_preparation'], {})
        planner.assert_not_called()
        prepare.assert_not_called()

    def test_unique_filename_plan_accepts_worker_resolved_package_target(self):
        planned = copy.deepcopy(self.plan)
        planned['operations'][0]['target'] = 'replay.yaml'
        draft = {**self.draft, 'yaml_request': '将 replay.yaml 的 camera.fps 改为 10'}
        result, _, _ = self.run_check(draft=draft, planned=planned)
        self.assertEqual(result['status'], 'STATIC_PASSED')
        self.assertEqual(result['yaml_preparation']['outputs'][0]['target'], 'config/replay.yaml')

    def test_missing_package_cannot_create_configuration_candidate(self):
        result, planner, prepare = self.run_check(missing_package=True)
        self.assertEqual(result['status'], 'NEEDS_INPUT')
        self.assertIn('YAML_PACKAGE_REQUIRED', {issue['code'] for issue in result['issues']})
        planner.assert_not_called()
        prepare.assert_not_called()

    def test_ambiguous_yaml_request_returns_actionable_question_without_remote_work(self):
        planned = {'status': 'NEEDS_INPUT', 'question': '请提供完整字段路径。', 'operations': []}
        result, _, prepare = self.run_check(planned=planned)
        self.assertEqual(result['status'], 'NEEDS_INPUT')
        self.assertTrue(any('完整字段路径' in issue['message'] for issue in result['issues']))
        prepare.assert_not_called()

    def test_worker_missing_field_is_needs_input_not_static_pass(self):
        receipt = {'schema_version': 1, 'status': 'NEEDS_INPUT', 'outputs': [], 'issues': [
            {'code': 'FIELD_MISSING', 'message': 'camera.fps 不存在。', 'next_action': '请核对字段路径。'}]}
        result, _, prepare = self.run_check(receipt=receipt)
        self.assertEqual(result['status'], 'NEEDS_INPUT')
        self.assertIn('FIELD_MISSING', {issue['code'] for issue in result['issues']})
        prepare.assert_called_once()

    def test_empty_success_receipt_is_not_a_prepared_configuration(self):
        receipt = copy.deepcopy(self.receipt)
        receipt['outputs'] = []
        result, _, _ = self.run_check(receipt=receipt)
        self.assertEqual(result['status'], 'NEEDS_INPUT')
        self.assertTrue(any(issue['field'] == 'yaml_request' for issue in result['issues']))

    def test_other_owner_output_path_is_rejected(self):
        receipt = copy.deepcopy(self.receipt)
        receipt['outputs'][0]['path'] = '/data/users/somebody-else/candidate.yaml'
        result, _, _ = self.run_check(receipt=receipt)
        self.assertEqual(result['status'], 'NEEDS_INPUT')
        summary = PreparationQueue(self.cfg, self.store).summary(self.user, {
            'id': self.preparation_id, 'owner': self.user, 'state': 'NEEDS_INPUT',
            'fingerprint': fingerprint(self.draft), 'result': result})
        self.assertNotIn('YAML 任务副本已准备', summary)
        self.assertNotIn('/data/users/somebody-else', summary)

    def test_success_receipt_from_another_task_or_package_is_rejected(self):
        for field, value in [('preparation_id', 'prep-other'), ('package_sha256', 'f' * 64),
                             ('scope', 'live'), ('package_modified', True), ('bench_modified', True)]:
            with self.subTest(field=field):
                receipt = copy.deepcopy(self.receipt)
                receipt[field] = value
                result, _, _ = self.run_check(receipt=receipt)
                self.assertEqual(result['status'], 'NEEDS_INPUT')

    def test_wrong_yaml_target_or_malformed_diff_receipt_is_rejected(self):
        for field, value in [('target', 'config/unrequested.yaml'), ('diff_sha256', 'not-a-sha')]:
            with self.subTest(field=field):
                receipt = copy.deepcopy(self.receipt)
                receipt['outputs'][0][field] = value
                result, _, _ = self.run_check(receipt=receipt)
                self.assertEqual(result['status'], 'NEEDS_INPUT')

    def test_planner_consuming_budget_cannot_start_unbounded_remote_preparation(self):
        current = [0.0]
        def slow_plan(*args):
            current[0] = 590.0
            return copy.deepcopy(self.plan)
        with patch('bench_gateway.service.Service', return_value=self.fake_service()), \
             patch('bench_gateway.preparation.time.monotonic', side_effect=lambda: current[0]), \
             patch('bench_gateway.yaml_plan.plan', side_effect=slow_plan), \
             patch('bench_gateway.yaml_runtime.prepare', return_value=self.receipt) as prepare:
            result = MaterialCheck(self.cfg, self.store).run(
                {'id': self.preparation_id, 'owner': self.user, 'payload': self.draft})
        if prepare.called:
            request = prepare.call_args.args[2]
            self.assertIn('limits', request)
            self.assertLess(request['limits']['duration_seconds'], 10.0)
            self.assertLessEqual(prepare.call_args.kwargs['timeout'], 10.0)
        else:
            self.assertEqual(result['status'], 'NEEDS_INPUT')


class YamlReplayAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = 'ou_yaml_replay_alice'
        self.session = 'feishu:oc_yaml:' + self.user
        self.cfg = {'state_root': str(Path(self.tmp.name) / 'state'),
                    'project_root': self.tmp.name, 'remote_users_root': '/data/users',
                    'mode': 'simulate', 'live_enabled': False, 'notifications_enabled': False}
        self.service = Service(self.cfg)
        self.store = self.service.store
        self.store.allow(self.user)
        package = self.store.add_asset(self.user, 'package', 'A', {'sha256': 'a' * 64})
        dataset = self.store.add_asset(self.user, 'dataset', 'D', {'sha256': 'b' * 64})
        ids = {'package': package['id'], 'dataset': dataset['id'], 'dependencies': []}
        self.store.add_baseline(self.user, 'old-verified', {'gate_verified': True, 'asset_ids': ids})
        self.intent = {'action': 'replay', 'package': 'A', 'dataset': 'D',
                       'dependencies': [], 'baseline': 'old-verified', 'scenes': [1]}

    def test_existing_yaml_draft_blocks_old_baseline(self):
        Intake(self.store).update(self.user, {'yaml_request': '将 config/replay.yaml 的 fps 改为 10'})
        with self.assertRaisesRegex(GatewayError, 'YAML'):
            self.service.handle(self.session, '执行回灌', self.intent)
        self.assertEqual(self.store.jobs(self.user), [])

    def test_yaml_in_same_replay_message_cannot_bypass_draft_guard(self):
        intent = {**self.intent, 'inputs': {'yaml_request': '将 config/replay.yaml 的 fps 改为 10'}}
        with self.assertRaisesRegex(GatewayError, 'YAML'):
            self.service.handle(self.session, '执行回灌并修改 YAML', intent)
        self.assertEqual(self.store.jobs(self.user), [])
        self.assertEqual((Intake(self.store).get(self.user) or {}).get('yaml_request'),
                         intent['inputs']['yaml_request'])
        # The next message must not lose the newly supplied change and run the
        # old package simply because that message no longer repeats the YAML.
        with self.assertRaisesRegex(GatewayError, 'YAML'):
            self.service.handle(self.session, '那就执行回灌', self.intent)

    def test_plain_replay_and_explicit_yaml_cancellation_preserve_old_path(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                if cancel:
                    Intake(self.store).update(self.user, {'yaml_request': '修改 config/replay.yaml'})
                    self.service.handle(self.session, '取消 YAML 修改',
                                        {'action': 'prepare', 'inputs': {'goal':'replay','yaml_request': '保留原配置'}})
                reply = self.service.handle(self.session, '执行回灌 ' + str(cancel), self.intent)
                self.assertIn('队列', reply)
        self.assertEqual(len(self.store.jobs(self.user)), 2)

    def test_new_material_in_replay_is_saved_instead_of_silently_ignored(self):
        intent = {**self.intent, 'inputs': {'camera_so': '/data/users/alice/new-camera.so'}}
        with self.assertRaisesRegex(GatewayError, '新增材料已保存'):
            self.service.handle(self.session, '回灌并更换相机 SO', intent)
        self.assertEqual(self.store.jobs(self.user), [])
        self.assertEqual(Intake(self.store).get(self.user)['camera_so'], intent['inputs']['camera_so'])


if __name__ == '__main__':
    unittest.main()
