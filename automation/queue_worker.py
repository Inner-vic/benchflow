#!/usr/bin/env python3
"""Persistent coordination worker for the shared workstation."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from bench_transaction import execute_profile_transaction, selected_command_set
from full_transaction import execute_full_evaluation, validate_full_job
from target_workflow import execute_target_workflow, validate_target_job
from runner_state import (
    RunnerError,
    append_event,
    claim_bench,
    queue_snapshot,
    read_json,
    release_bench,
    set_state,
    transition_lock,
    inspect_bench_lock,
)


SUPPORTED_EXECUTORS = (
    "coordination-smoke",
    "profile-transaction",
    "full-evaluation",
    "target-workflow",
)


def validate_target_plan(
    job: dict[str, Any],
    profile_path: Path,
    run_dir: Path,
) -> dict[str, Any]:
    """Recompute bench use from the immutable job and compare it with the plan."""
    resolved = validate_target_job(job, profile_path, run_dir)
    plan = read_json(run_dir / "plan.json")
    workflow = plan.get("workflow")
    planned = workflow.get("requires_bench") if isinstance(workflow, dict) else None
    computed = resolved["requires_bench"]
    if not isinstance(planned, bool) or planned != computed:
        raise RunnerError(
            "plan.workflow.requires_bench does not match the validated target job "
            f"(plan={planned!r}, validated={computed!r})"
        )
    return resolved


def process_next(
    runs_dir: Path,
    locks_dir: Path,
    executor: str,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Quarantine invalid ready jobs before trying the next eligible job."""
    if profile_path is not None:
        try:
            read_json(profile_path.expanduser().resolve())
        except RunnerError as exc:
            return {"status": "BLOCKED", "reason": "INVALID_WORKER_PROFILE", "message": str(exc)}
    blocked = None
    while True:
        result = _process_next(runs_dir, locks_dir, executor, profile_path)
        if result.get("reason") not in ("INVALID_EXECUTION_PROFILE", "INVALID_JOB"):
            return blocked if blocked and result.get("status") == "IDLE" else result
        run_dir = runs_dir.expanduser().resolve() / result["run_id"]
        with transition_lock(run_dir):
            lock = inspect_bench_lock(locks_dir.expanduser().resolve() / "bench.lock")
            if lock and (lock.get("safe_to_claim") is False or lock.get("run_id") == result["run_id"]):
                return result
            if read_json(run_dir / "state.json").get("state") != "READY":
                return result
            details = {"reason": result["reason"], "message": result.get("message", ""), "executor": executor}
            append_event(run_dir, {"event": "RUN_QUARANTINED", "state": "BLOCKED_VALIDATION", **details})
            set_state(run_dir, "BLOCKED_VALIDATION", **details)
        blocked = result


def _process_next(
    runs_dir: Path,
    locks_dir: Path,
    executor: str,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    if executor not in SUPPORTED_EXECUTORS:
        raise RunnerError(f"executor must be one of: {', '.join(SUPPORTED_EXECUTORS)}")
    snapshot = queue_snapshot(runs_dir, locks_dir)
    if not snapshot["queued"]:
        return {"status": "IDLE", "reason": "QUEUE_EMPTY"}

    target_resolution: dict[str, Any] | None = None
    if snapshot["active"] and executor == "target-workflow":
        if profile_path is None:
            return {
                "status": "BLOCKED",
                "reason": "EXECUTION_PROFILE_REQUIRED",
                "run_id": snapshot["queued"][0]["run_id"],
            }
        item = None
        for candidate in snapshot["queued"]:
            candidate_run_dir = runs_dir.expanduser().resolve() / candidate["run_id"]
            try:
                candidate_job = read_json(candidate_run_dir / "job.json")
                resolved = validate_target_plan(
                    candidate_job, profile_path, candidate_run_dir,
                )
            except RunnerError as exc:
                return {
                    "status": "BLOCKED",
                    "reason": "INVALID_EXECUTION_PROFILE",
                    "run_id": candidate["run_id"],
                    "message": str(exc),
                }
            if not resolved["requires_bench"]:
                item = candidate
                target_resolution = resolved
                break
        if item is None:
            return {"status": "IDLE", "reason": "BENCH_BUSY", "active": snapshot["active"]}
    else:
        if snapshot["active"]:
            return {"status": "IDLE", "reason": "BENCH_BUSY", "active": snapshot["active"]}
        item = snapshot["queued"][0]

    run_id = item["run_id"]
    run_dir = runs_dir.expanduser().resolve() / run_id
    try:
        job = read_json(run_dir / "job.json")
    except RunnerError as exc:
        return {"status": "BLOCKED", "reason": "INVALID_JOB", "run_id": run_id, "message": str(exc)}
    if executor == "coordination-smoke" and job.get("mode") != "coordination_smoke":
        return {
            "status": "BLOCKED",
            "reason": "EXECUTOR_NOT_IMPLEMENTED_FOR_JOB",
            "run_id": run_id,
            "mode": job.get("mode"),
        }

    if executor in ("profile-transaction", "full-evaluation", "target-workflow"):
        if profile_path is None:
            return {"status": "BLOCKED", "reason": "EXECUTION_PROFILE_REQUIRED", "run_id": run_id}
        try:
            # Validate the complete selected set before the bench is claimed.
            if executor == "full-evaluation":
                validate_full_job(job, profile_path, run_dir)
            elif executor == "target-workflow":
                if target_resolution is None:
                    target_resolution = validate_target_plan(job, profile_path, run_dir)
            else:
                selected_command_set(job, profile_path)
        except RunnerError as exc:
            return {"status": "BLOCKED", "reason": "INVALID_EXECUTION_PROFILE", "run_id": run_id, "message": str(exc)}

    needs_bench = (
        bool(target_resolution["requires_bench"])
        if executor == "target-workflow" and target_resolution is not None
        else True
    )
    if needs_bench:
        if snapshot["active"]:
            return {"status": "IDLE", "reason": "BENCH_BUSY", "active": snapshot["active"]}
        claim_bench(runs_dir, locks_dir, run_id)
    if executor == "target-workflow":
        return execute_target_workflow(
            runs_dir=runs_dir,
            locks_dir=locks_dir,
            run_id=run_id,
            profile_path=profile_path,
        )
    if executor == "full-evaluation":
        return execute_full_evaluation(
            runs_dir=runs_dir,
            locks_dir=locks_dir,
            run_id=run_id,
            profile_path=profile_path,
        )
    if executor == "profile-transaction":
        return execute_profile_transaction(
            runs_dir=runs_dir,
            locks_dir=locks_dir,
            run_id=run_id,
            profile_path=profile_path,
        )

    set_state(run_dir, "RUNNING", executor=executor)
    append_event(run_dir, {"event": "WORKER_STARTED", "state": "RUNNING", "executor": executor})
    smoke = job.get("coordination_smoke", {})
    outcome = smoke.get("outcome", "success") if isinstance(smoke, dict) else "success"
    if outcome not in ("success", "fail"):
        outcome = "fail"

    if outcome == "success":
        append_event(run_dir, {"event": "COORDINATION_SMOKE_COMPLETED", "state": "RUNNING"})
        released = release_bench(runs_dir, locks_dir, run_id, "NO_ACTIVATION", None)
        return {"status": "PROCESSED", "run_id": run_id, "result": released}

    append_event(run_dir, {"event": "COORDINATION_SMOKE_FAILED", "state": "RUNNING"})
    release_bench(runs_dir, locks_dir, run_id, "NO_ACTIVATION", None)
    set_state(run_dir, "FAILED_COORDINATION", executor=executor, retryable=True)
    append_event(
        run_dir,
        {"event": "WORKER_FAILED", "state": "FAILED_COORDINATION", "executor": executor, "retryable": True},
    )
    return {"status": "FAILED", "run_id": run_id, "retryable": True}


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_matches_record(record: dict[str, Any]) -> bool:
    """Avoid signalling an unrelated process if a stale PID was reused."""
    pid = int(record.get("pid") or 0)
    if not process_alive(pid):
        return False
    if os.name == "nt":
        # The production worker runs on Ubuntu. Windows lacks a dependency-free
        # equivalent of /proc cmdline inspection, so retain the PID-only check.
        return True
    try:
        command = (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return False
    token = str(record.get("token") or "")
    return bool(token and "_worker-loop" in command and token in command)


def worker_record_path(locks_dir: Path) -> Path:
    return locks_dir.expanduser().resolve() / "worker.json"


def worker_status(locks_dir: Path) -> dict[str, Any]:
    path = worker_record_path(locks_dir)
    if not path.is_file():
        return {"status": "STOPPED", "record": None}
    record = read_json(path)
    alive = process_alive(int(record.get("pid") or 0))
    matches = process_matches_record(record) if alive else False
    return {"status": "RUNNING" if matches else "STALE", "record": record}


def start_background_worker(
    *,
    script: Path,
    runs_dir: Path,
    locks_dir: Path,
    executor: str,
    profile_path: Path | None,
    poll_seconds: float,
    log_path: Path | None,
) -> dict[str, Any]:
    if executor not in SUPPORTED_EXECUTORS:
        raise RunnerError(f"executor must be one of: {', '.join(SUPPORTED_EXECUTORS)}")
    if poll_seconds < 0.2:
        raise RunnerError("poll-seconds must be at least 0.2")
    if executor in ("profile-transaction", "full-evaluation", "target-workflow") and profile_path is None:
        raise RunnerError(f"{executor} requires --profile")
    if executor in ("full-evaluation", "target-workflow"):
        raise RunnerError(
            f"{executor} is foreground-only; use worker once inside the submitter's forwarded SSH session"
        )
    record_path = worker_record_path(locks_dir)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    try:
        descriptor = os.open(str(record_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        status = worker_status(locks_dir)
        raise RunnerError(f"worker record already exists with status {status['status']}; run worker stop to clean it") from exc

    effective_log = (log_path or record_path.parent / "worker.log").expanduser().resolve()
    effective_log.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(script),
        "_worker-loop",
        "--runs-dir",
        str(runs_dir.expanduser().resolve()),
        "--locks-dir",
        str(locks_dir.expanduser().resolve()),
        "--executor",
        executor,
        "--poll-seconds",
        str(poll_seconds),
        "--token",
        token,
    ]
    if profile_path is not None:
        command.extend(["--profile", str(profile_path.expanduser().resolve())])
    log_stream = effective_log.open("a", encoding="utf-8")
    process: subprocess.Popen[Any] | None = None
    descriptor_open = True
    try:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_stream,
            "stderr": subprocess.STDOUT,
            "cwd": str(script.resolve().parent),
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        record = {
            "schema_version": 1,
            "pid": process.pid,
            "token": token,
            "started_at": time.time(),
            "executor": executor,
            "profile_path": str(profile_path.expanduser().resolve()) if profile_path else None,
            "runs_dir": str(runs_dir.expanduser().resolve()),
            "locks_dir": str(locks_dir.expanduser().resolve()),
            "log_path": str(effective_log),
        }
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor_open = False
            stream.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return {"status": "STARTED", "record": record}
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
        if descriptor_open:
            os.close(descriptor)
        record_path.unlink(missing_ok=True)
        raise
    finally:
        log_stream.close()


def stop_background_worker(locks_dir: Path) -> dict[str, Any]:
    path = worker_record_path(locks_dir)
    if not path.is_file():
        return {"status": "STOPPED", "idempotent": True}
    record = read_json(path)
    pid = int(record.get("pid") or 0)
    if not process_alive(pid) or not process_matches_record(record):
        path.unlink()
        return {"status": "STOPPED", "stale_record_removed": True}
    os.kill(pid, signal.SIGTERM)
    return {"status": "STOPPING", "pid": pid}


def run_worker_loop(
    *,
    runs_dir: Path,
    locks_dir: Path,
    executor: str,
    profile_path: Path | None,
    poll_seconds: float,
    token: str,
) -> int:
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    record_path = worker_record_path(locks_dir)
    try:
        while not stopping:
            try:
                result = process_next(runs_dir, locks_dir, executor, profile_path)
                if result.get("status") in ("BLOCKED", "FAILED", "UNSAFE"):
                    time.sleep(poll_seconds)
                elif result.get("status") == "IDLE":
                    time.sleep(poll_seconds)
            except Exception as exc:
                print(json.dumps({"status": "WORKER_ERROR", "message": str(exc)}, ensure_ascii=False), flush=True)
                time.sleep(poll_seconds)
    finally:
        if record_path.is_file():
            try:
                record = read_json(record_path)
                if record.get("token") == token:
                    record_path.unlink()
            except RunnerError:
                pass
    return 0
