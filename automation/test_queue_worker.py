from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prediction_transfer import local_inventory, write_workstation_product_receipt
from queue_worker import process_next, stop_background_worker
from runner_state import (
    RunnerError,
    approve_stage,
    cancel_run,
    claim_bench,
    queue_snapshot,
    requeue_run,
    runner_status,
    submit_job,
)


def ready_plan() -> dict:
    return {
        "preflight": {"status": "READY", "blockers": []},
        "steps": [
            {"state": "ACTIVATE", "explicit_approval_required": True},
            {"state": "REPLAY", "explicit_approval_required": True},
        ],
    }


def submit_ready(root: Path, run_id: str, owner: str, outcome: str = "success") -> None:
    submit_job(
        job={"run_id": run_id, "mode": "coordination_smoke", "coordination_smoke": {"outcome": outcome}},
        source=root / f"{run_id}.json",
        plan=ready_plan(),
        runs_dir=root / "runs",
        submitted_by=owner,
        client="test",
    )
    approve_stage(root / "runs", run_id, "ACTIVATE", owner)
    approve_stage(root / "runs", run_id, "REPLAY", owner)


def target_profile(root: Path) -> Path:
    profile = root / "profile.json"
    profile.write_text(json.dumps({
        "execution": {"command_sets": {"unused-in-lock-test": {
            "strategy": "use_current_app",
            "commands": {
                step: {"transport": "local", "argv": [sys.executable, "-c", "pass"]}
                for step in ("precheck", "health_current", "replay")
            },
        }}},
        "prediction_transfer": {"sets": {"local": {
            "source": {"transport": "local", "allowed_root": str(root / "bench")},
            "workstation_cache_root": str(root / "shared-products"),
            "destination": {"transport": "local", "allowed_root": str(root / "lpai")},
        }}},
    }), encoding="utf-8")
    return profile


def target_job(root: Path, run_id: str, *, requires_bench: bool) -> dict:
    job = {
        "schema_version": 1,
        "runner_protocol": "1.1",
        "run_id": run_id,
        "mode": "target_workflow",
        "workflow": {"targets": ["workstation_predictions" if requires_bench else "lpai_predictions"]},
        "predictions": {
            "transfer_set_id": "local",
            "destination_path": str(root / "lpai" / run_id),
        },
    }
    if requires_bench:
        source = root / "bench" / "predictions" / run_id
        source.mkdir(parents=True, exist_ok=True)
        (source / "pred.bin").write_bytes(b"prediction")
        job["predictions"]["source_path"] = str(source)
        job["execution"] = {"command_set_id": "unused-in-lock-test"}
    else:
        shared = root / "shared-products" / run_id
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "pred.bin").write_bytes(b"prediction")
        write_workstation_product_receipt(
            shared.resolve(),
            local_inventory(shared),
            producer_run_id="producer-run",
            source="test-fixture",
        )
        job["workflow"]["sources"] = {"workstation_predictions": str(shared)}
    return job


def submit_target(root: Path, job: dict, *, planned_requires_bench: bool) -> None:
    plan = {
        "runner_protocol": "1.1",
        "preflight": {"status": "READY", "blockers": []},
        "required_approvals": [],
        "workflow": {"requires_bench": planned_requires_bench},
        "steps": [],
    }
    submit_job(
        job=job,
        source=root / f"{job['run_id']}.json",
        plan=plan,
        runs_dir=root / "runs",
        submitted_by="bob",
        client="test",
    )


class QueueWorkerTests(unittest.TestCase):
    def test_two_users_are_processed_in_fifo_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_ready(root, "run-a", "alice")
            submit_ready(root, "run-b", "bob")
            queue = queue_snapshot(root / "runs", root / "locks")
            self.assertEqual(["run-a", "run-b"], [item["run_id"] for item in queue["queued"]])
            self.assertEqual([1, 2], [item["queue_position"] for item in queue["queued"]])

            first = process_next(root / "runs", root / "locks", "coordination-smoke")
            second = process_next(root / "runs", root / "locks", "coordination-smoke")

            self.assertEqual("run-a", first["run_id"])
            self.assertEqual("run-b", second["run_id"])
            self.assertEqual("RELEASED", runner_status(root / "runs", root / "locks", "run-a")["run"]["state"])
            self.assertEqual("RELEASED", runner_status(root / "runs", root / "locks", "run-b")["run"]["state"])

    def test_owner_can_cancel_but_other_user_cannot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_ready(root, "run-a", "alice")
            with self.assertRaises(RunnerError):
                cancel_run(root / "runs", root / "locks", "run-a", "bob", "not mine")
            result = cancel_run(root / "runs", root / "locks", "run-a", "alice", "no longer needed")
            self.assertEqual("CANCELLED", result["state"])

    def test_retryable_coordination_failure_can_be_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_ready(root, "run-fail", "alice", outcome="fail")
            failed = process_next(root / "runs", root / "locks", "coordination-smoke")
            self.assertEqual("FAILED", failed["status"])
            self.assertEqual(
                "FAILED_COORDINATION",
                runner_status(root / "runs", root / "locks", "run-fail")["run"]["state"],
            )
            requeued = requeue_run(root / "runs", root / "locks", "run-fail", "alice")
            self.assertEqual("READY", requeued["state"])

    def test_real_job_is_not_executed_by_smoke_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_job(
                job={"run_id": "live-run", "mode": "offline"},
                source=root / "job.json",
                plan=ready_plan(),
                runs_dir=root / "runs",
                submitted_by="alice",
                client="test",
            )
            approve_stage(root / "runs", "live-run", "ACTIVATE", "alice")
            approve_stage(root / "runs", "live-run", "REPLAY", "alice")
            result = process_next(root / "runs", root / "locks", "coordination-smoke")
            self.assertEqual("BLOCKED", result["status"])
            self.assertEqual("EXECUTOR_NOT_IMPLEMENTED_FOR_JOB", result["reason"])
            self.assertFalse((root / "locks" / "bench.lock").exists())

    def test_profile_transaction_is_validated_before_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_job(
                job={"run_id": "live-run", "mode": "offline", "execution": {"command_set_id": "missing"}},
                source=root / "job.json",
                plan=ready_plan(),
                runs_dir=root / "runs",
                submitted_by="alice",
                client="test",
            )
            approve_stage(root / "runs", "live-run", "ACTIVATE", "alice")
            approve_stage(root / "runs", "live-run", "REPLAY", "alice")
            profile = root / "profile.json"
            profile.write_text('{"execution":{"command_sets":{}}}', encoding="utf-8")
            result = process_next(root / "runs", root / "locks", "profile-transaction", profile)
            self.assertEqual("BLOCKED", result["status"])
            self.assertEqual("INVALID_EXECUTION_PROFILE", result["reason"])
            self.assertFalse((root / "locks" / "bench.lock").exists())
            self.assertEqual("BLOCKED_VALIDATION", runner_status(root / "runs", root / "locks", "live-run")["run"]["state"])

    def test_invalid_job_is_isolated_and_next_job_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "lpai").mkdir()
            bad = target_job(root, "bad", requires_bench=False)
            submit_target(root, bad, planned_requires_bench=True)
            good = target_job(root, "good", requires_bench=False)
            submit_target(root, good, planned_requires_bench=False)
            result = process_next(root / "runs", root / "locks", "target-workflow", target_profile(root))
            self.assertEqual("good", result["run_id"])
            snapshot = queue_snapshot(root / "runs", root / "locks")
            self.assertEqual(["bad"], [item["run_id"] for item in snapshot["blocked"]])
            self.assertEqual(0, snapshot["queued_count"])
            events = (root / "runs" / "bad" / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("RUN_QUARANTINED", events)
            with self.assertRaises(RunnerError):
                requeue_run(root / "runs", root / "locks", "bad", "mallory")
            self.assertEqual("READY", requeue_run(root / "runs", root / "locks", "bad", "bob")["state"])

    def test_missing_worker_profile_does_not_quarantine_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_ready(root, "run-a", "alice")
            result = process_next(root / "runs", root / "locks", "profile-transaction")
            self.assertEqual("EXECUTION_PROFILE_REQUIRED", result["reason"])
            self.assertEqual(1, queue_snapshot(root / "runs", root / "locks")["queued_count"])

    def test_unreadable_worker_profile_does_not_quarantine_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_ready(root, "run-a", "alice")
            result = process_next(root / "runs", root / "locks", "profile-transaction", root / "missing.json")
            self.assertEqual("INVALID_WORKER_PROFILE", result["reason"])
            self.assertEqual(1, queue_snapshot(root / "runs", root / "locks")["queued_count"])

    def test_corrupt_job_does_not_block_following_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_ready(root, "bad", "alice")
            submit_ready(root, "good", "bob")
            (root / "runs" / "bad" / "job.json").write_text("{", encoding="utf-8")
            result = process_next(root / "runs", root / "locks", "coordination-smoke")
            self.assertEqual("good", result["run_id"])
            self.assertEqual("INVALID_JOB", queue_snapshot(root / "runs", root / "locks")["blocked"][0]["reason"])

    def test_active_bench_is_preserved_while_invalid_job_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_ready(root, "bench-owner", "alice")
            claim_bench(root / "runs", root / "locks", "bench-owner")
            (root / "lpai").mkdir()
            submit_target(root, target_job(root, "bad", requires_bench=False), planned_requires_bench=True)
            submit_target(root, target_job(root, "good", requires_bench=False), planned_requires_bench=False)
            result = process_next(root / "runs", root / "locks", "target-workflow", target_profile(root))
            self.assertEqual("good", result["run_id"])
            self.assertEqual("bench-owner", queue_snapshot(root / "runs", root / "locks")["active"]["run_id"])

    def test_corrected_profile_can_be_requeued_and_executed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "lpai").mkdir()
            submit_target(root, target_job(root, "upload", requires_bench=False), planned_requires_bench=False)
            profile = root / "profile.json"
            profile.write_text("{}", encoding="utf-8")
            self.assertEqual("BLOCKED", process_next(root / "runs", root / "locks", "target-workflow", profile)["status"])
            target_profile(root)
            requeue_run(root / "runs", root / "locks", "upload", "bob")
            self.assertEqual("COMPLETED_TO_REQUESTED_GOAL", process_next(root / "runs", root / "locks", "target-workflow", profile)["state"])

    def test_profile_transaction_processes_local_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_job(
                job={"run_id": "transaction-smoke", "mode": "transaction_smoke", "execution": {"command_set_id": "local"}},
                source=root / "job.json",
                plan=ready_plan(),
                runs_dir=root / "runs",
                submitted_by="alice",
                client="test",
            )
            approve_stage(root / "runs", "transaction-smoke", "ACTIVATE", "alice")
            approve_stage(root / "runs", "transaction-smoke", "REPLAY", "alice")
            steps = (
                "precheck", "capture_baseline", "stop_active", "start_candidate",
                "health_candidate", "replay", "restore", "verify_restore",
            )
            profile = root / "profile.json"
            profile.write_text(json.dumps({
                "execution": {"command_sets": {"local": {
                    "strategy": "isolated_candidate",
                    "commands": {
                        step: {"transport": "local", "argv": [sys.executable, "-c", "pass"]}
                        for step in steps
                    },
                }}}
            }), encoding="utf-8")
            result = process_next(root / "runs", root / "locks", "profile-transaction", profile)
            self.assertEqual("PROCESSED", result["status"])
            self.assertEqual("COMPLETED", runner_status(root / "runs", root / "locks", "transaction-smoke")["run"]["state"])
            self.assertTrue((root / "runs" / "transaction-smoke" / "restore-verification.json").is_file())

    def test_non_bench_target_can_run_while_bench_is_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_ready(root, "bench-owner", "alice")
            claim_bench(root / "runs", root / "locks", "bench-owner")
            (root / "lpai").mkdir()
            job = target_job(root, "upload-only", requires_bench=False)
            submit_target(root, job, planned_requires_bench=False)
            profile = target_profile(root)

            result = process_next(
                root / "runs", root / "locks", "target-workflow", profile,
            )

            self.assertEqual("upload-only", result["run_id"])
            self.assertEqual("COMPLETED_TO_REQUESTED_GOAL", result["state"])
            self.assertEqual("bench-owner", queue_snapshot(root / "runs", root / "locks")["active"]["run_id"])

    def test_plan_false_computed_true_mismatch_blocks_before_claim_or_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "lpai").mkdir()
            job = target_job(root, "mismatch-bench", requires_bench=True)
            submit_target(root, job, planned_requires_bench=False)
            profile = target_profile(root)

            with mock.patch("queue_worker.execute_target_workflow") as execute:
                result = process_next(
                    root / "runs", root / "locks", "target-workflow", profile,
                )

            self.assertEqual("BLOCKED", result["status"])
            self.assertIn("does not match", result["message"])
            execute.assert_not_called()
            self.assertFalse((root / "locks" / "bench.lock").exists())

    def test_plan_true_computed_false_mismatch_blocks_before_claim_or_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "lpai").mkdir()
            job = target_job(root, "mismatch-upload", requires_bench=False)
            submit_target(root, job, planned_requires_bench=True)
            profile = target_profile(root)

            with mock.patch("queue_worker.execute_target_workflow") as execute:
                result = process_next(
                    root / "runs", root / "locks", "target-workflow", profile,
                )

            self.assertEqual("BLOCKED", result["status"])
            self.assertIn("does not match", result["message"])
            execute.assert_not_called()
            self.assertFalse((root / "locks" / "bench.lock").exists())

    def test_active_bench_filter_uses_validated_job_not_plan_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_ready(root, "bench-owner", "alice")
            claim_bench(root / "runs", root / "locks", "bench-owner")
            (root / "lpai").mkdir()
            job = target_job(root, "spoofed-non-bench", requires_bench=True)
            submit_target(root, job, planned_requires_bench=False)
            profile = target_profile(root)

            with mock.patch("queue_worker.execute_target_workflow") as execute:
                result = process_next(
                    root / "runs", root / "locks", "target-workflow", profile,
                )

            self.assertEqual("BLOCKED", result["status"])
            self.assertEqual("spoofed-non-bench", result["run_id"])
            self.assertIn("does not match", result["message"])
            execute.assert_not_called()
            self.assertEqual(
                "bench-owner",
                queue_snapshot(root / "runs", root / "locks")["active"]["run_id"],
            )

    def test_stop_does_not_signal_a_reused_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            locks = Path(temp) / "locks"
            locks.mkdir()
            (locks / "worker.json").write_text(
                '{"pid": 12345, "token": "old-worker"}\n',
                encoding="utf-8",
            )
            with mock.patch("queue_worker.process_alive", return_value=True), mock.patch(
                "queue_worker.process_matches_record", return_value=False
            ), mock.patch("queue_worker.os.kill") as kill:
                result = stop_background_worker(locks)
            kill.assert_not_called()
            self.assertTrue(result["stale_record_removed"])
            self.assertFalse((locks / "worker.json").exists())


if __name__ == "__main__":
    unittest.main()
