import concurrent.futures
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bench_gateway.core import GatewayError, Store
from bench_gateway.intake import Intake
from bench_gateway.preparation import PreparationQueue


class PreparationBindingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cfg = {"project_root": str(self.root), "state_root": str(self.root / "state"),
                    "remote_users_root": "/data/users", "mode": "simulate", "live_enabled": False,
                    "default_adapter_template": "reviewed", "notifications_enabled": True,
                    "notification_retry_seconds": 5}
        self.store = Store(self.cfg["state_root"])
        self.user, self.other = "ou_binding_lifecycle", "ou_binding_other"
        for user in (self.user, self.other):
            self.store.allow(user)
            Intake(self.store).update(user, {"goal":"replay","package": "package-A", "camera_so": "camera-A",
                "lidar_so": "lidar-A", "m100_so": "M100-A", "m100_mode": "provided",
                "dataset": "shared18", "scene_scope": "全部"}, new=True)
        self.queue = PreparationQueue(self.cfg, self.store)
        self.session = "feishu:oc_test:" + self.user
        self.queue.submit(self.user, self.session)
        self.job = self.queue.latest(self.user)
        owner_root = self.store.remote_root(self.user, self.cfg['remote_users_root'])
        self.materials = {'package': self.store.add_asset(self.user, 'package', 'package-A', {
            'sha256': '1' * 64, 'remote_path': owner_root + '/packages/A.tgz'})}
        for index, (role, name, target) in enumerate((('camera_so', 'camera-A', 'libxcamera.so'),
                ('lidar_so', 'lidar-A', 'libxlidar.so'), ('m100_so', 'M100-A', 'liburoad_m100.so')), 2):
            self.materials[role] = self.store.add_asset(self.user, 'dependency', name, {
                'sha256': str(index) * 64, 'remote_path': owner_root + '/dependencies/' + target,
                'target': 'lib/' + target})
        self.dataset = self.store.add_asset(self.user, 'dataset', 'shared18', {
            'sha256': '5' * 64, 'scene_count': 18, 'manifest': owner_root + '/datasets/manifest.json'})
        self.baseline_data = {'kind': 'candidate-v1', 'preparation_id': self.job['id'],
            'readiness': 'READY_FOR_GATE', 'gate_verified': False,
            'binding': {'input_digest': 'a' * 64}, 'scenes': list(range(1, 19)),
            'assets': {'package': self.materials['package'], 'dataset': self.dataset,
                       'dependencies': [self.materials[role] for role in ('camera_so', 'lidar_so', 'm100_so')]}}
        self.baseline = None
        # These tests isolate preparation persistence and notification lifecycle.
        # Remote binding/identity validation has its own integration tests; the
        # view stub still requires a stored candidate for this owner/preparation.
        validator = patch('bench_gateway.bindings.validate_candidate', side_effect=self.validate_display_candidate)
        validator.start()
        self.addCleanup(validator.stop)

    def validate_display_candidate(self, cfg, store, user, baseline,allow_other_task=False):
        self.assertEqual(self.user, user)
        self.assertEqual(user, baseline['owner'])
        self.assertEqual('candidate-v1', baseline['data']['kind'])
        self.assertEqual(self.job['id'], baseline['data']['preparation_id'])
        self.assertEqual('READY_FOR_GATE', baseline['data']['readiness'])
        return baseline['data']['assets'], baseline['data']['scenes']

    def persist_baseline(self, *args, **kwargs):
        if self.baseline is None:
            self.baseline = self.store.add_baseline(self.user, 'prepared-' + self.job['id'], self.baseline_data)
        return self.baseline

    def checker(self, result=None):
        result = {"status": "STATIC_PASSED", "issues": [], "checks": [],
                  "assets": self.materials, "dataset": self.dataset, "scenes": list(range(1, 19)),
                  "adapter_binding": "REQUIRED_BEFORE_HARDWARE_QUEUE", **(result or {})}
        return SimpleNamespace(run=Mock(side_effect=lambda job: copy.deepcopy(result)))

    def count(self):
        with self.store.connect() as connection:
            return connection.execute("SELECT count(*) FROM preparations").fetchone()[0]

    def test_success_binds_then_sends_one_ready_message_without_creating_hardware_job(self):
        sender = Mock(return_value=True)
        def binding(*args, **kwargs):
            saved = self.queue.latest(self.user)
            self.assertEqual("STATIC_PASSED", saved["state"])
            self.assertEqual("BINDING", saved["result"]["adapter_binding"]["state"])
            self.assertEqual(0, self.queue.deliver(sender))
            self.assertIn("正在离线绑定候选", self.queue.summary(self.user))
            return self.persist_baseline()
        with patch("bench_gateway.bindings.bind_preparation", side_effect=binding) as binder:
            self.assertEqual(self.job["id"], self.queue.tick(self.checker()))
        binder.assert_called_once()
        saved = self.queue.latest(self.user)
        self.assertEqual("READY_FOR_GATE", saved["result"]["adapter_binding"]["state"])
        self.assertEqual("a" * 64, saved["result"]["adapter_binding"]["input_digest"])
        self.assertEqual(1, self.queue.deliver(sender))
        self.assertEqual(0, self.queue.deliver(sender))
        self.assertIn("执行回放", sender.call_args.args[0]["text"])
        self.assertIn("待本次小场景预检", sender.call_args.args[0]["text"])
        self.assertNotIn("预检已通过", sender.call_args.args[0]["text"])
        self.assertLess(len(sender.call_args.args[0]["text"]),300)
        self.assertEqual([], self.store.jobs(self.user))

    def test_queue_sets_binding_before_persisting_a_checker_result(self):
        sender = Mock(return_value=True)
        def binding(*args, **kwargs):
            self.assertEqual(0, self.queue.deliver(sender), "must not deliver an intermediate STATIC_PASSED result")
            self.assertEqual("BINDING", self.queue.latest(self.user)["result"]["adapter_binding"]["state"])
            return self.persist_baseline()
        with patch("bench_gateway.bindings.bind_preparation", side_effect=binding):
            self.queue.tick(self.checker({"status": "STATIC_PASSED", "issues": []}))
        self.assertEqual("READY_FOR_GATE", self.queue.latest(self.user)["result"]["adapter_binding"]["state"])
        sender.assert_not_called()

    def test_expected_binding_failure_reports_input_action_without_auto_retry(self):
        with patch("bench_gateway.bindings.bind_preparation", side_effect=GatewayError("YAML 字段与固定协议冲突")) as binder:
            self.queue.tick(self.checker())
            self.assertIsNone(self.queue.tick(self.checker()))
        binder.assert_called_once()
        saved = self.queue.latest(self.user)
        self.assertEqual("STATIC_PASSED", saved["state"])
        self.assertEqual("NEEDS_INPUT", saved["result"]["adapter_binding"]["state"])
        self.assertIn("YAML 字段", self.queue.summary(self.user))
        self.assertIn("适配器绑定未完成", self.queue.summary(self.user))
        self.assertIn("下一步：按绑定原因修正材料", self.queue.summary(self.user))
        sender = Mock(return_value=True)
        self.assertEqual(1, self.queue.deliver(sender))
        self.assertEqual([], self.store.jobs(self.user))

    def test_unexpected_binding_error_is_sanitized(self):
        with patch("bench_gateway.bindings.bind_preparation", side_effect=RuntimeError("signed-url=private-secret")):
            self.queue.tick(self.checker())
        result = self.queue.latest(self.user)["result"]["adapter_binding"]
        self.assertEqual("NEEDS_INPUT", result["state"])
        self.assertNotIn("private-secret", json.dumps(result))
        self.assertIn("不会自动重试", result["message"])

    def test_interrupted_binding_is_recovered_to_action_needed_without_rebinding(self):
        with patch("bench_gateway.bindings.bind_preparation", side_effect=KeyboardInterrupt("process stopped")):
            with self.assertRaises(KeyboardInterrupt):
                self.queue.tick(self.checker())
        saved = self.queue.latest(self.user)
        self.assertEqual("BINDING", saved["result"]["adapter_binding"]["state"])
        before = copy.deepcopy(saved["payload"])
        sender = Mock(return_value=True)
        self.assertEqual(0, self.queue.deliver(sender))
        recovered = PreparationQueue(self.cfg, self.store)
        with patch("bench_gateway.bindings.bind_preparation") as binder:
            recovered.recover()
            recovered.recover()
            self.assertIsNone(recovered.tick(self.checker()))
        binder.assert_not_called()
        saved = recovered.latest(self.user)
        self.assertEqual(before, saved["payload"])
        self.assertEqual("NEEDS_INPUT", saved["result"]["adapter_binding"]["state"])
        self.assertIn("进程中断", saved["result"]["adapter_binding"]["message"])
        self.assertEqual(1, recovered.deliver(sender))
        self.assertEqual(0, recovered.deliver(sender))

    def test_restart_preserves_successful_binding_without_rebinding(self):
        with patch("bench_gateway.bindings.bind_preparation", side_effect=self.persist_baseline):
            self.queue.tick(self.checker())
        original = copy.deepcopy(self.queue.latest(self.user)["result"])
        recovered = PreparationQueue(self.cfg, self.store)
        with patch("bench_gateway.bindings.bind_preparation") as binder:
            recovered.recover()
            self.assertIsNone(recovered.tick(self.checker()))
        binder.assert_not_called()
        self.assertEqual(original, recovered.latest(self.user)["result"])

    def test_repeated_check_during_binding_deduplicates_active_preparation(self):
        def binding(*args, **kwargs):
            message = self.queue.submit(self.user, self.session)
            self.assertIn(self.job["id"], message)
            self.assertEqual(1, self.count())
            self.assertEqual(self.job["id"], self.queue.latest(self.user)["id"])
            return self.persist_baseline()
        with patch("bench_gateway.bindings.bind_preparation", side_effect=binding):
            self.queue.tick(self.checker())
        self.assertEqual("READY_FOR_GATE", self.queue.latest(self.user)["result"]["adapter_binding"]["state"])

    def test_parallel_tick_cannot_claim_next_check_while_first_binding_is_running(self):
        self.queue.submit(self.other, "feishu:oc_other:" + self.other)
        entered, release = threading.Event(), threading.Event()
        def binding(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test release did not arrive")
            return self.persist_baseline()
        second_checker = self.checker()
        with patch("bench_gateway.bindings.bind_preparation", side_effect=binding):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                future = pool.submit(self.queue.tick, self.checker())
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertIsNone(self.queue.tick(second_checker))
                    second_checker.run.assert_not_called()
                    self.assertEqual("QUEUED", self.queue.latest(self.other)["state"])
                finally:
                    release.set()
                self.assertEqual(self.job["id"], future.result(timeout=5))

    def test_failed_static_check_and_missing_template_do_not_bind(self):
        with patch("bench_gateway.bindings.bind_preparation") as binder:
            self.queue.tick(self.checker({"status": "NEEDS_INPUT", "issues": []}))
        binder.assert_not_called()
        self.queue.submit(self.user, self.session)
        self.cfg.pop("default_adapter_template")
        with patch("bench_gateway.bindings.bind_preparation") as binder:
            self.queue.tick(self.checker({"status": "STATIC_PASSED", "issues": []}))
        binder.assert_not_called()
        self.assertIn("待绑定适配器", self.queue.summary(self.user))
        self.assertIn("下一步：请管理员完成本次候选绑定", self.queue.summary(self.user))

    def test_failed_notification_retries_delivery_without_repeating_binding(self):
        with patch("bench_gateway.bindings.bind_preparation", side_effect=self.persist_baseline) as binder:
            self.queue.tick(self.checker())
            self.assertEqual(0, self.queue.deliver(Mock(return_value=False)))
            self.assertIsNone(self.queue.tick(self.checker()))
            with self.store.connect() as connection:
                connection.execute("UPDATE preparations SET next_attempt=0 WHERE id=?", (self.job["id"],))
            sender = Mock(return_value=True)
            self.assertEqual(1, self.queue.deliver(sender))
        binder.assert_called_once()
        self.assertIn("执行回放", sender.call_args.args[0]["text"])


if __name__ == "__main__":
    unittest.main()
