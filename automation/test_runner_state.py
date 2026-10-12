from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runner_state import RunnerError, approve_stage, claim_bench, queue_snapshot, release_bench, runner_status, submit_job


def ready_plan() -> dict:
    return {
        "preflight": {"status": "READY", "blockers": []},
        "steps": [
            {"state": "ACTIVATE", "explicit_approval_required": True},
            {"state": "REPLAY", "explicit_approval_required": True},
        ],
    }


class RunnerStateTests(unittest.TestCase):
    def test_submit_approve_claim_and_safe_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            locks = root / "locks"
            result = submit_job(
                job={"run_id": "run-001"},
                source=root / "job.json",
                plan=ready_plan(),
                runs_dir=runs,
                submitted_by="张三",
                client="codex-windows",
            )
            self.assertEqual("WAITING_APPROVAL", result["state"])
            self.assertEqual("PARTIALLY_APPROVED", approve_stage(runs, "run-001", "ACTIVATE", "张三")["state"])
            self.assertEqual("READY", approve_stage(runs, "run-001", "REPLAY", "张三")["state"])
            self.assertEqual("BENCH_CLAIMED", claim_bench(runs, locks, "run-001")["state"])
            with self.assertRaises(RunnerError):
                release_bench(runs, locks, "run-001", "RESTORE_VERIFIED", None)
            evidence = runs / "run-001" / "restore.json"
            evidence.write_text('{"verified": true}\n', encoding="utf-8")
            self.assertEqual(
                "RELEASED",
                release_bench(runs, locks, "run-001", "RESTORE_VERIFIED", evidence)["state"],
            )
            self.assertFalse(runner_status(runs, locks, "run-001")["owns_bench"])

    def test_global_lock_blocks_another_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            locks = root / "locks"
            for run_id in ("run-a", "run-b"):
                submit_job(
                    job={"run_id": run_id},
                    source=root / f"{run_id}.json",
                    plan=ready_plan(),
                    runs_dir=runs,
                    submitted_by=run_id,
                    client="test",
                )
                approve_stage(runs, run_id, "ACTIVATE", run_id)
                approve_stage(runs, run_id, "REPLAY", run_id)
            claim_bench(runs, locks, "run-a")
            with self.assertRaises(RunnerError):
                claim_bench(runs, locks, "run-b")

    def test_formal_update_can_release_only_with_matching_verified_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            locks = root / "locks"
            plan = ready_plan()
            plan["safety"] = {"active_path_write_allowed": True}
            submit_job(
                job={
                    "run_id": "formal-update",
                    "workflow": {"bench_strategy": "formal_update"},
                },
                source=root / "job.json",
                plan=plan,
                runs_dir=runs,
                submitted_by="alice",
                client="test",
            )
            approve_stage(runs, "formal-update", "ACTIVATE", "alice")
            approve_stage(runs, "formal-update", "REPLAY", "alice")
            claim_bench(runs, locks, "formal-update")
            evidence = runs / "formal-update" / "formal-activation.json"
            evidence.write_text(json.dumps({
                "state": "FORMAL_ACTIVATED_VERIFIED",
                "formal_inputs": {"/app/livc/lib/liburoad_m100.so": "a" * 64},
            }), encoding="utf-8")
            result = release_bench(
                runs,
                locks,
                "formal-update",
                "FORMAL_ACTIVATED_VERIFIED",
                evidence,
            )
            self.assertEqual("RELEASED", result["state"])
            self.assertEqual("FORMAL_ACTIVATED_VERIFIED", result["reason"])

    def test_non_formal_job_cannot_claim_formal_release_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            locks = root / "locks"
            submit_job(
                job={"run_id": "temporary-candidate"},
                source=root / "job.json",
                plan=ready_plan(),
                runs_dir=runs,
                submitted_by="alice",
                client="test",
            )
            approve_stage(runs, "temporary-candidate", "ACTIVATE", "alice")
            approve_stage(runs, "temporary-candidate", "REPLAY", "alice")
            claim_bench(runs, locks, "temporary-candidate")
            evidence = runs / "temporary-candidate" / "formal-activation.json"
            evidence.write_text(json.dumps({
                "state": "FORMAL_ACTIVATED_VERIFIED",
                "formal_inputs": {"x": "y"},
            }), encoding="utf-8")
            with self.assertRaises(RunnerError):
                release_bench(
                    runs,
                    locks,
                    "temporary-candidate",
                    "FORMAL_ACTIVATED_VERIFIED",
                    evidence,
                )

    def test_blocked_job_cannot_be_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submit_job(
                job={"run_id": "run-blocked"},
                source=root / "job.json",
                plan={"preflight": {"status": "BLOCKED", "blockers": [{"message": "missing"}]}, "steps": []},
                runs_dir=root / "runs",
                submitted_by="user",
                client="test",
            )
            with self.assertRaises(RunnerError):
                approve_stage(root / "runs", "run-blocked", "ACTIVATE", "user")

    def test_target_job_without_approvals_is_ready_on_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = submit_job(
                job={"run_id": "upload-only"},
                source=root / "job.json",
                plan={
                    "preflight": {"status": "READY", "blockers": []},
                    "required_approvals": [],
                    "steps": [],
                },
                runs_dir=root / "runs",
                submitted_by="user",
                client="test",
            )
            self.assertEqual("READY", result["state"])
            with self.assertRaises(RunnerError):
                approve_stage(root / "runs", "upload-only", "REPLAY", "user")

    def test_empty_bench_lock_is_reported_but_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            locks = root / "locks"
            locks.mkdir()
            (locks / "bench.lock").write_bytes(b"")
            snapshot = queue_snapshot(runs, locks)
            self.assertEqual("INVALID_LOCK", snapshot["active"]["state"])
            self.assertFalse(snapshot["active"]["safe_to_claim"])

            submit_job(
                job={"run_id": "run-corrupt-lock"},
                source=root / "job.json",
                plan=ready_plan(),
                runs_dir=runs,
                submitted_by="user",
                client="test",
            )
            approve_stage(runs, "run-corrupt-lock", "ACTIVATE", "user")
            approve_stage(runs, "run-corrupt-lock", "REPLAY", "user")
            with self.assertRaises(RunnerError):
                claim_bench(runs, locks, "run-corrupt-lock")


if __name__ == "__main__":
    unittest.main()
