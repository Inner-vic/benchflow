"""Foreground queue extension; original queue/lock/restore implementation is reused."""
from __future__ import annotations
import os
from pathlib import Path

from environment_store import EnvironmentError, read, write
from candidate_tasks import promote, resolve


def process_next(store, runs_dir, locks_dir, runtime, legacy_profile=None, execute=None):
    import sys
    sys.path.insert(0, str(runtime))
    from runner_state import queue_snapshot, claim_bench, RunnerError
    from queue_worker import process_next as legacy_next, validate_target_plan
    from target_workflow import execute_target_workflow
    snapshot = queue_snapshot(Path(runs_dir), Path(locks_dir))
    if not snapshot["queued"]: return {"status": "IDLE", "reason": "QUEUE_EMPTY"}
    # All bound tasks require bench execution; never claim while another user owns it.
    if snapshot["active"]: return {"status": "IDLE", "reason": "BENCH_BUSY"}
    item = snapshot["queued"][0]
    run_id = item["run_id"]
    run_dir = Path(runs_dir) / run_id
    job = read(run_dir / "job.json")
    if "candidate_binding" not in job:
        return legacy_next(Path(runs_dir), Path(locks_dir), "target-workflow", legacy_profile)
    try:
        profile, meta = resolve(job, store)
        if str(Path(runtime).resolve()) != meta["runtime"]["root"]:
            raise EnvironmentError("worker runtime differs from frozen runtime")
        if read(run_dir / "manifest.json").get("submitted_by") != meta["binding"]["actor"]:
            raise EnvironmentError("submitted_by differs from candidate owner")
        resolved = validate_target_plan(job, profile, run_dir)
        if not resolved["requires_bench"]:
            raise EnvironmentError("candidate validation must execute on the bench")
    except (EnvironmentError, RunnerError, OSError, KeyError, ValueError) as exc:
        return {"status": "BLOCKED", "run_id": run_id, "reason": "FROZEN_INPUT_INVALID", "message": str(exc)}
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    claim_bench(Path(runs_dir), Path(locks_dir), run_id)
    # No installation controller takes/releases a second lock between deploy and replay.
    engine = execute or execute_target_workflow
    result = engine(runs_dir=Path(runs_dir), locks_dir=Path(locks_dir), run_id=run_id, profile_path=profile)
    result["frozen_task_sha256"] = job["candidate_binding"]["sha256"]
    write(run_dir / "candidate-execution-result.json", result)
    if result.get("status") == "PROCESSED":
        try:
            promote(store, runs_dir, run_id)
            result["candidate_promotion"] = "VERIFIED"
        except Exception as exc:
            result["candidate_promotion"] = "NOT_PROMOTED"
            result["promotion_error"] = str(exc)
        write(run_dir / "candidate-execution-result.json", result)
        if result.get("candidate_promotion") == "VERIFIED":
            try:
                from candidate_steps import cleanup_after_delivery
                result["candidate_cleanup"] = cleanup_after_delivery(profile.parent, run_dir)
            except Exception as exc:
                result["candidate_cleanup"] = {"state": "DELIVERY_COMPLETE_CLEANUP_PENDING", "error": str(exc)}
            write(run_dir / "candidate-execution-result.json", result)
    return result
