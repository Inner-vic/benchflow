from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bench_transaction import execute_profile_transaction, load_command_set
from runner_state import RunnerError, approve_stage, claim_bench, runner_status, submit_job


def ready_plan() -> dict:
    return {"preflight": {"status": "READY", "blockers": []}}


def profile(root: Path, strategy: str, command_set_id: str = "test") -> Path:
    steps = {
        "use_current_app": ("precheck", "health_current", "replay"),
        "isolated_candidate": (
            "precheck", "capture_baseline", "stop_active", "start_candidate",
            "health_candidate", "replay", "restore", "verify_restore",
        ),
    }[strategy]
    path = root / "device.json"
    path.write_text(json.dumps({
        "execution": {
            "command_sets": {
                command_set_id: {
                    "strategy": strategy,
                    "commands": {step: {"transport": "local", "argv": ["/usr/bin/true"]} for step in steps},
                }
            }
        }
    }), encoding="utf-8")
    return path


def claimed_run(root: Path, run_id: str, command_set_id: str = "test") -> None:
    submit_job(
        job={"run_id": run_id, "mode": "offline", "execution": {"command_set_id": command_set_id}},
        source=root / "job.json",
        plan=ready_plan(),
        runs_dir=root / "runs",
        submitted_by="alice",
        client="test",
    )
    approve_stage(root / "runs", run_id, "ACTIVATE", "alice")
    approve_stage(root / "runs", run_id, "REPLAY", "alice")
    claim_bench(root / "runs", root / "locks", run_id)


class FakeRunner:
    def __init__(self, failures=()):
        self.failures = set(failures)
        self.steps: list[str] = []

    def run(self, step, _spec, _execution, _context):
        self.steps.append(step)
        ok = step not in self.failures
        return {"step": step, "ok": ok, "exit_code": 0 if ok else 1, "timed_out": False}


class BenchTransactionTests(unittest.TestCase):
    def test_isolated_success_restores_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = profile(root, "isolated_candidate")
            claimed_run(root, "success")
            fake = FakeRunner()
            result = execute_profile_transaction(
                runs_dir=root / "runs", locks_dir=root / "locks", run_id="success",
                profile_path=path, command_runner=fake,
            )
            self.assertEqual("PROCESSED", result["status"])
            self.assertEqual(
                ["precheck", "capture_baseline", "stop_active", "start_candidate", "health_candidate", "replay", "restore", "verify_restore"],
                fake.steps,
            )
            self.assertEqual("COMPLETED", runner_status(root / "runs", root / "locks", "success")["run"]["state"])
            self.assertFalse((root / "locks" / "bench.lock").exists())
            self.assertTrue((root / "runs" / "success" / "restore-verification.json").is_file())

    def test_failure_after_activation_restores_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = profile(root, "isolated_candidate")
            claimed_run(root, "replay-fails")
            fake = FakeRunner({"replay"})
            result = execute_profile_transaction(
                runs_dir=root / "runs", locks_dir=root / "locks", run_id="replay-fails",
                profile_path=path, command_runner=fake,
            )
            self.assertEqual("FAILED", result["status"])
            self.assertEqual(1, fake.steps.count("restore"))
            self.assertEqual(1, fake.steps.count("verify_restore"))
            self.assertEqual("FAILED_EXECUTION", runner_status(root / "runs", root / "locks", "replay-fails")["run"]["state"])
            self.assertFalse((root / "locks" / "bench.lock").exists())

    def test_stop_failure_also_restores(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = profile(root, "isolated_candidate")
            claimed_run(root, "stop-fails")
            fake = FakeRunner({"stop_active"})
            execute_profile_transaction(
                runs_dir=root / "runs", locks_dir=root / "locks", run_id="stop-fails",
                profile_path=path, command_runner=fake,
            )
            self.assertEqual(["precheck", "capture_baseline", "stop_active", "restore", "verify_restore"], fake.steps)

    def test_restore_failure_retains_bench_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = profile(root, "isolated_candidate")
            claimed_run(root, "unsafe")
            fake = FakeRunner({"restore"})
            result = execute_profile_transaction(
                runs_dir=root / "runs", locks_dir=root / "locks", run_id="unsafe",
                profile_path=path, command_runner=fake,
            )
            self.assertEqual("UNSAFE", result["status"])
            status = runner_status(root / "runs", root / "locks", "unsafe")
            self.assertEqual("RESTORE_FAILED_UNSAFE", status["run"]["state"])
            self.assertTrue(status["owns_bench"])

    def test_current_app_never_stops_or_restores(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = profile(root, "use_current_app")
            claimed_run(root, "current")
            fake = FakeRunner()
            execute_profile_transaction(
                runs_dir=root / "runs", locks_dir=root / "locks", run_id="current",
                profile_path=path, command_runner=fake,
            )
            self.assertEqual(["precheck", "health_current", "replay"], fake.steps)
            self.assertEqual("COMPLETED", runner_status(root / "runs", root / "locks", "current")["run"]["state"])

    def test_missing_command_set_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = profile(root, "use_current_app")
            with self.assertRaises(RunnerError):
                load_command_set(path, "missing")


if __name__ == "__main__":
    unittest.main()
