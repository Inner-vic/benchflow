"""Real preparation phase transitions over isolated DB and mocked transports."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bench_gateway.core import GatewayError, Store
from bench_gateway.intake import Intake
from bench_gateway.preparation import MaterialCheck, PreparationQueue, _record_progress


class PreparationProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state')
        self.user, self.other = 'ou_progress_alice', 'ou_progress_bob'
        for user in (self.user, self.other):
            self.store.allow(user)
        self.cfg = {'project_root': str(Path(__file__).resolve().parents[1]), 'state_root': str(self.store.root),
                    'remote_users_root': '/data/users', 'mode': 'simulate', 'live_enabled': False,
                    'notifications_enabled': False}
        self.draft = {'package': 'https://private.example/software?secret=not-for-progress',
                      'camera_so': '/data/camera.so', 'lidar_so': 'lidar-A',
                      'm100_so': 'https://private.example/m100.so', 'm100_mode': 'provided',
                      'dataset': 'shared18', 'scene_scope': '全部'}
        Intake(self.store).update(self.user, self.draft, new=True)
        self.draft = Intake(self.store).get(self.user)
        self.queue = None
        self.job = {'id': 'direct-no-preparation', 'owner': self.user, 'payload': copy.deepcopy(self.draft)}

    def running(self, **draft):
        if draft:
            Intake(self.store).update(self.user, draft)
        self.queue = PreparationQueue(self.cfg, self.store)
        self.queue.submit(self.user, 'feishu:oc_progress:' + self.user)
        self.job = self.queue.latest(self.user)
        with self.store.connect() as connection:
            connection.execute("UPDATE preparations SET state='RUNNING',updated=100 WHERE id=?", (self.job['id'],))
        return self.job

    def progress(self):
        if self.queue is None:
            return None
        return (self.queue.latest(self.user).get('result') or {}).get('progress')

    def assert_phase(self, expected):
        progress = self.progress()
        self.assertIsNotNone(progress)
        self.assertEqual(expected, progress['phase'])
        self.assertEqual({'phase', 'phase_started_at', 'started_at'}, set(progress))
        self.assertNotIn('https:', json.dumps(progress))
        self.assertNotIn('secret', json.dumps(progress))

    def asset(self, role):
        target = {'camera_so': 'lib/libxcamera.so', 'lidar_so': 'lib/libxlidar.so', 'm100_so': 'lib/liburoad_m100.so'}.get(role)
        return {'id': 'asset-' + role, 'name': 'lidar-A' if role == 'lidar_so' else role,
                'kind': 'package' if role == 'package' else 'dependency', 'owner': self.user, 'version': 1,
                'data': {'sha256': 'a' * 64, 'remote_path': '/data/users/private/' + role, **({'target': target} if target else {})}}

    def material_service(self, verify_phases=True, mutate_draft=False):
        def phase(name):
            if verify_phases:
                self.assert_phase(name)
        def listed(user):
            self.assertEqual(self.user, user)
            phase('核验本人工作站目录')
            return [self.asset('lidar_so')]
        def remote_file(user, kind, name, source, target):
            phase('登记相机 SO工作站文件')
            return self.asset('camera_so')
        def fetch(registry, user, kind, name, source, target, **kwargs):
            role = 'package' if kind == 'package' else 'm100_so'
            phase('准备软件包工作站文件' if role == 'package' else '下载M100 SO到工作站')
            if mutate_draft and role == 'package':
                Intake(self.store).update(self.user, {'camera_so': 'new-draft-camera'})
            return self.asset(role)
        dataset = {'id': 'shared18', 'name': 'shared18', 'kind': 'dataset', 'owner': 'shared',
                   'origin': 'dataset_catalog', 'data': {'sha256': 'd' * 64, 'scene_count': 18}}
        def asset(user, kind, name):
            phase('核验数据源与本次场景范围')
            return dataset
        def python(code, args=(), **kwargs):
            phase('检查软件包与 SO 静态兼容')
            return json.dumps({'schema_version': 1, 'status': 'PASS', 'checks': [], 'issues': []})
        transport = SimpleNamespace(python=python)
        registry = SimpleNamespace(ssh=transport, list=listed, remote_file=remote_file)
        service = SimpleNamespace(registry=registry, datasets=SimpleNamespace(ssh=transport), asset=asset)
        return service, fetch

    def check(self, **kwargs):
        service, fetch = self.material_service(**kwargs)
        with patch('bench_gateway.service.Service', return_value=service), patch('bench_gateway.asset_fetch.fetch', side_effect=fetch):
            return MaterialCheck(self.cfg, self.store).run(self.job)

    def test_material_operations_persist_their_actual_stage_before_transport(self):
        self.running()
        result = self.check()
        self.assertEqual('STATIC_PASSED', result['status'])
        self.assertNotIn('progress', result, 'transient progress must not alter final material receipts')
        self.assert_phase('检查软件包与 SO 静态兼容')
        self.assertEqual([], self.store.jobs(self.user))

    def test_all_four_roles_have_source_resolution_stage(self):
        self.running()
        observed = []
        import bench_gateway.preparation as preparation
        original = preparation._record_progress
        def record(store, job, phase):
            observed.append(phase)
            return original(store, job, phase)
        with patch.object(preparation, '_record_progress', side_effect=record):
            self.check()
        for label in ('软件包', '相机 SO', '雷达 SO', 'M100 SO'):
            self.assertIn('解析' + label + '来源', observed)

    def test_direct_checker_without_persisted_preparation_never_creates_one(self):
        result = self.check(verify_phases=False)
        self.assertEqual('STATIC_PASSED', result['status'])
        with self.store.connect() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='preparations'").fetchone())
        self.assertEqual([], self.store.jobs(self.user))

    def test_direct_checker_without_job_id_still_checks_materials_without_progress_record(self):
        self.running()
        self.job = {'owner': self.user, 'payload': copy.deepcopy(self.draft)}
        self.assertEqual('STATIC_PASSED', self.check(verify_phases=False)['status'])
        self.assertIsNone(self.progress())

    def test_nonexistent_id_or_foreign_owner_cannot_write_progress(self):
        self.running()
        self.assertIsNone(_record_progress(self.store, {**self.job, 'owner': self.other}, '核验本人工作站目录'))
        self.assertIsNone(_record_progress(self.store, {**self.job, 'id': 'absent'}, '核验本人工作站目录'))
        self.assertIsNone(self.progress())
        with self.assertRaises(GatewayError):
            self.queue.summary(self.other, self.queue.latest(self.user))

    def test_changed_submitted_payload_is_rejected_but_new_draft_does_not_relabel_old_check(self):
        self.running()
        forged = {**self.job, 'payload': {**self.job['payload'], 'package': 'forged'}}
        self.assertIsNone(_record_progress(self.store, forged, '核验本人工作站目录'))
        before = copy.deepcopy(self.job['payload'])
        result = self.check(mutate_draft=True)
        self.assertEqual('STATIC_PASSED', result['status'])
        self.assertEqual(before, self.queue.latest(self.user)['payload'])
        summary = self.queue.summary(self.user)
        self.assertIn('当前实际阶段：检查软件包与 SO 静态兼容', summary)
        self.assertIn('上述进度属于已提交的材料快照', summary)
        self.assertIn('草稿已变化', summary)

    def test_progress_has_phase_and_total_elapsed_without_percent_or_fake_eta(self):
        self.running()
        with patch('bench_gateway.preparation.time.time', return_value=120):
            first = _record_progress(self.store, self.job, '核验本人工作站目录')
        with patch('bench_gateway.preparation.time.time', return_value=135):
            second = _record_progress(self.store, self.job, '检查软件包与 SO 静态兼容')
        with patch('bench_gateway.preparation.time.time', return_value=140):
            repeated = _record_progress(self.store, self.job, '检查软件包与 SO 静态兼容')
        self.assertEqual(100, first['started_at'])
        self.assertEqual(100, second['started_at'])
        self.assertEqual(135, repeated['phase_started_at'])
        with patch('bench_gateway.preparation.time.time', return_value=200):
            summary = self.queue.summary(self.user)
        self.assertIn('本阶段已用 1分5秒', summary)
        self.assertIn('本轮累计 1分40秒', summary)
        self.assertNotIn('%', summary)
        self.assertNotIn('预计', summary)

    def test_unapproved_phase_cannot_persist_url_or_secret(self):
        self.running()
        with self.assertRaises(GatewayError):
            _record_progress(self.store, self.job, 'https://host/path?token=secret')
        self.assertIsNone(self.progress())

    def test_yaml_plan_and_generation_stages_precede_their_real_calls(self):
        self.running(yaml_request='set camera flag')
        service, fetch = self.material_service()
        def plan(cfg, text):
            self.assert_phase('整理本次 YAML 修改要求')
            return {'status': 'READY', 'operations': []}
        def prepare(*args, **kwargs):
            self.assert_phase('生成本次 YAML 候选与差异')
            return {'status': 'NEEDS_INPUT', 'issues': [{'code': 'YAML_FIELD_MISSING', 'message': 'field missing', 'next_action': 'clarify'}]}
        with patch('bench_gateway.service.Service', return_value=service), patch('bench_gateway.asset_fetch.fetch', side_effect=fetch), \
                patch('bench_gateway.yaml_plan.plan', side_effect=plan), patch('bench_gateway.yaml_runtime.prepare', side_effect=prepare):
            result = MaterialCheck(self.cfg, self.store).run(self.job)
        self.assertEqual('NEEDS_INPUT', result['status'])
        self.assertEqual('YAML_FIELD_MISSING', result['issues'][0]['code'])
        self.assertNotIn('progress', result)

    def test_finish_stops_progress_updates_and_drops_transient_state(self):
        self.running()
        _record_progress(self.store, self.job, '检查软件包与 SO 静态兼容')
        result = {'status': 'NEEDS_INPUT', 'issues': [{'field': 'package', 'code': 'fixture', 'message': 'needs input', 'next_action': 'supply'}]}
        self.queue.finish(self.job, result)
        self.assertIsNone(_record_progress(self.store, self.job, '核验本人工作站目录'))
        self.assertEqual(result, self.queue.latest(self.user)['result'])
        self.assertNotIn('当前实际阶段', self.queue.summary(self.user))

    def test_binding_phase_is_written_before_static_pass_and_stays_frozen_during_binding(self):
        self.cfg['default_adapter_template'] = 'reviewed'
        self.queue = PreparationQueue(self.cfg, self.store)
        self.queue.submit(self.user, 'feishu:oc_progress:' + self.user)
        job = self.queue.latest(self.user)
        snapshots = []
        def binding(*args, **kwargs):
            first = self.queue.latest(self.user)
            self.assertEqual('STATIC_PASSED', first['state'])
            self.assertEqual('BINDING', first['result']['adapter_binding']['state'])
            self.assertEqual('绑定本次回放适配器', first['result']['progress']['phase'])
            snapshots.append(copy.deepcopy(first['result']))
            self.assertIn('当前实际阶段：绑定本次回放适配器', self.queue.summary(self.user))
            self.assertEqual(snapshots[0], self.queue.latest(self.user)['result'])
            return {'name': 'prepared-example', 'data': {'binding': {'input_digest': 'a' * 64}}}
        with patch('bench_gateway.bindings.bind_preparation', side_effect=binding):
            self.queue.tick(SimpleNamespace(run=lambda _: {'status': 'STATIC_PASSED', 'checks': []}))
        saved = self.queue.latest(self.user)
        self.assertEqual(job['id'], saved['id'])
        self.assertEqual('READY_FOR_GATE', saved['result']['adapter_binding']['state'])
        self.assertNotIn('progress', saved['result'])

    def test_binding_failure_and_restart_end_progress_without_claiming_gate_success(self):
        self.cfg['default_adapter_template'] = 'reviewed'
        self.queue = PreparationQueue(self.cfg, self.store)
        self.queue.submit(self.user, 'feishu:oc_progress:' + self.user)
        checker = SimpleNamespace(run=lambda _: {'status': 'STATIC_PASSED'})
        with patch('bench_gateway.bindings.bind_preparation', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.queue.tick(checker)
        self.assertEqual('绑定本次回放适配器', self.progress()['phase'])
        self.queue.recover()
        saved = self.queue.latest(self.user)
        self.assertEqual('NEEDS_INPUT', saved['result']['adapter_binding']['state'])
        self.assertNotIn('progress', saved['result'])
        self.assertNotIn('当前实际阶段', self.queue.summary(self.user))


if __name__ == '__main__':
    unittest.main()
