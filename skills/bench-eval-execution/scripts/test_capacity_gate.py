"""Capacity gate budgets the post-stage Runner reserve before any mutation."""

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).with_name("capacity_gate.py")
SPEC = importlib.util.spec_from_file_location("capacity_gate", SOURCE)
GATE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(GATE)


class CapacityGateTests(unittest.TestCase):
    def test_bench_query_uses_empty_keyboard_interactive_auth(self) -> None:
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps({"stage": {}, "run": {}}), stderr="")
        with mock.patch.object(GATE.subprocess, "run", return_value=response) as run:
            result = GATE.bench_stats("bench-avm", "/home/user/ssh_config", "/app", "/map")
        self.assertIn("stage", result)
        command = run.call_args.args[0]
        self.assertIn("BatchMode=no", command)
        self.assertIn("PreferredAuthentications=keyboard-interactive", command)
        self.assertNotIn("BatchMode=yes", command)
        self.assertEqual(run.call_args.kwargs["env"]["SSH_ASKPASS"], "/bin/true")

    def setUp(self) -> None:
        gib = 1024 ** 3
        self.plan = {
            "bench": {
                "runner_free_floor_bytes": 2 * gib,
                "output_budget_bytes": gib // 2,
                "stage_safety_bytes": gib // 2,
                "inode_floor": 10000,
                "memory_floor_bytes": 2 * gib,
                "stage_filesystem_path": "/app",
                "run_filesystem_path": "/app",
            },
            "workstation": {
                "system_free_floor_bytes": 5 * gib,
                "data_free_floor_bytes": 10 * gib,
                "memory_floor_bytes": 4 * gib,
            },
        }
        self.archive = {"package_bytes": gib // 3,
                        "unpacked_bytes": gib, "archive_members": 500}
        self.local = {
            "system": {"available_bytes": 30 * gib},
            "data": {"available_bytes": 533 * gib},
            "memory_available_bytes": 37 * gib,
        }
        self.bench = {
            "stage": {"available_bytes": 5 * gib, "available_inodes": 700000, "device_id": 1},
            "run": {"available_bytes": 5 * gib, "available_inodes": 700000, "device_id": 1},
            "memory_available_bytes": 15 * gib,
        }

    def test_ready_when_archive_and_post_stage_floor_fit(self) -> None:
        self.assertEqual(GATE.evaluate(self.plan, self.archive, self.local, self.bench)["state"], "READY")

    def test_blocks_before_staging_when_runner_floor_would_fail(self) -> None:
        self.bench["stage"]["available_bytes"] = 3 * 1024 ** 3
        report = GATE.evaluate(self.plan, self.archive, self.local, self.bench)
        self.assertEqual(report["state"], "BLOCKED_CAPACITY")
        self.assertGreater(report["checks"]["bench_stage_and_run_bytes"]["shortfall"], 0)

    def test_blocks_for_inode_and_memory_shortage(self) -> None:
        self.bench["stage"]["available_inodes"] = 10000
        self.local["memory_available_bytes"] = 1024 ** 3
        report = GATE.evaluate(self.plan, self.archive, self.local, self.bench)
        self.assertFalse(report["checks"]["bench_inodes"]["ok"])
        self.assertFalse(report["checks"]["workstation_memory_bytes"]["ok"])

    def test_external_stage_has_separate_storage_and_runtime_budgets(self) -> None:
        gib = 1024 ** 3
        self.plan["bench"]["stage_filesystem_path"] = "/map"
        self.bench["stage"]["available_bytes"] = 48 * gib
        self.bench["stage"]["device_id"] = 2
        self.bench["run"]["available_bytes"] = 5 * gib
        report = GATE.evaluate(self.plan, self.archive, self.local, self.bench)
        self.assertEqual(report["state"], "READY")
        self.bench["run"]["available_bytes"] = 2 * gib
        report = GATE.evaluate(self.plan, self.archive, self.local, self.bench)
        self.assertFalse(report["checks"]["bench_run_bytes"]["ok"])

    def test_refuses_zero_floor_as_disabled_gate(self) -> None:
        self.plan["bench"]["runner_free_floor_bytes"] = 0
        with self.assertRaises(GATE.CapacityError):
            GATE.evaluate(self.plan, self.archive, self.local, self.bench)

    def test_plan_rejects_missing_paths_and_thresholds_before_query(self) -> None:
        plan = {
            "schema_version": 1, "package": "/data/candidate.app.tar.gz",
            "bench": {**self.plan["bench"], "ssh_alias": "bench-avm",
                      "ssh_config": "/home/user/ssh_config"},
            "workstation": {**self.plan["workstation"], "system_path": "/",
                            "data_path": "/mnt/data"},
        }
        GATE.validate_plan(plan)
        plan["bench"]["stage_filesystem_path"] = "../map"
        with self.assertRaises(GATE.CapacityError):
            GATE.validate_plan(plan)


if __name__ == "__main__":
    unittest.main()
