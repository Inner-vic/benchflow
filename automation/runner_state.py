#!/usr/bin/env python3
"""Local state and locking for the shared workstation runner.

This module never connects to a remote host.  It provides the durable job,
approval, ownership and exclusive-bench primitives used by the CLI.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
APPROVAL_STAGES = ("ACTIVATE", "REPLAY")
RELEASE_REASONS = ("NO_ACTIVATION", "RESTORE_VERIFIED", "FORMAL_ACTIVATED_VERIFIED")
CANCELLABLE_STATES = ("WAITING_INPUT", "WAITING_APPROVAL", "PARTIALLY_APPROVED", "READY", "BLOCKED_VALIDATION")
REQUEUEABLE_STATES = ("FAILED_COORDINATION", "BLOCKED_VALIDATION")


class RunnerError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_run_id(run_id: Any) -> str:
    value = str(run_id or "")
    if not SAFE_RUN_ID.fullmatch(value):
        raise RunnerError("run_id must use 1-128 ASCII letters, digits, dot, underscore or hyphen")
    return value


def validate_identity(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result or len(result) > 128 or any(ord(character) < 32 for character in result):
        raise RunnerError(f"{field} must be a non-empty printable value of at most 128 characters")
    return result


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"cannot read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerError(f"JSON file must contain an object: {path}")
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(pending), str(path))


def inspect_bench_lock(path: Path) -> dict[str, Any] | None:
    """Return lock ownership or a fail-closed diagnostic for a malformed lock.

    Read-only status commands must remain usable when an interrupted external
    tool leaves an empty file at the coordination-lock path.  Mutating commands
    continue to call ``read_json`` directly and therefore refuse to claim,
    release, cancel, or requeue around an invalid lock.
    """
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except RunnerError as exc:
        return {
            "schema_version": 1,
            "state": "INVALID_LOCK",
            "path": str(path),
            "safe_to_claim": False,
            "message": str(exc),
        }


@contextmanager
def transition_lock(run_dir: Path):
    """Serialize state transitions and release the OS lock after process exit."""
    lock_path = run_dir / ".transition.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def append_event(run_dir: Path, event: dict[str, Any]) -> None:
    value = {"at": utc_now(), **event}
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def set_state(run_dir: Path, state: str, **details: Any) -> dict[str, Any]:
    value = {"schema_version": 1, "run_id": run_dir.name, "state": state, "updated_at": utc_now(), **details}
    atomic_write_json(run_dir / "state.json", value)
    return value


def required_approval_stages(plan: dict[str, Any]) -> tuple[str, ...]:
    configured = plan.get("required_approvals")
    if configured is None:
        return APPROVAL_STAGES
    if not isinstance(configured, list) or any(stage not in APPROVAL_STAGES for stage in configured):
        raise RunnerError("plan.required_approvals must contain only ACTIVATE and REPLAY")
    return tuple(stage for stage in APPROVAL_STAGES if stage in configured)


def submit_job(
    *,
    job: dict[str, Any],
    source: Path,
    plan: dict[str, Any],
    runs_dir: Path,
    submitted_by: str,
    client: str,
) -> dict[str, Any]:
    run_id = validate_run_id(job.get("run_id"))
    owner = validate_identity(submitted_by, "submitted_by")
    client_name = validate_identity(client, "client")
    run_dir = runs_dir.expanduser().resolve() / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise RunnerError(f"run already exists; refusing to overwrite: {run_dir}") from exc

    preflight = plan.get("preflight", {})
    required_approvals = required_approval_stages(plan)
    if preflight.get("status") != "READY":
        state = "WAITING_INPUT"
    elif required_approvals:
        state = "WAITING_APPROVAL"
    else:
        state = "READY"
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "submitted_at": utc_now(),
        "submitted_order_ns": time.time_ns(),
        "submitted_by": owner,
        "client": client_name,
        "runner_protocol": plan.get("runner_protocol"),
        "source_job": str(source),
        "logical_isolation_only": True,
        "shared_workstation_account": True,
        "state": state,
    }
    atomic_write_json(run_dir / "job.json", job)
    atomic_write_json(run_dir / "plan.json", plan)
    atomic_write_json(run_dir / "manifest.json", manifest)
    current = set_state(run_dir, state, submitted_by=owner, client=client_name)
    append_event(run_dir, {"event": "JOB_SUBMITTED", "state": state, "submitted_by": owner, "client": client_name})
    return {**current, "run_dir": str(run_dir), "preflight": preflight}


def approvals_for(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "approvals.jsonl"
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"invalid approval log at line {number}: {path}") from exc
        if isinstance(item, dict):
            result.append(item)
    return result


def approve_stage(runs_dir: Path, run_id: str, stage: str, approved_by: str) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    stage = stage.upper()
    if stage not in APPROVAL_STAGES:
        raise RunnerError(f"stage must be one of: {', '.join(APPROVAL_STAGES)}")
    approver = validate_identity(approved_by, "approved_by")
    run_dir = runs_dir.expanduser().resolve() / run_id
    if not run_dir.is_dir():
        raise RunnerError(f"run does not exist: {run_dir}")
    with transition_lock(run_dir):
        current_state = read_json(run_dir / "state.json").get("state")
        if current_state in ("CANCELLED", "BENCH_CLAIMED", "RUNNING", "RELEASED", "FAILED_COORDINATION"):
            raise RunnerError(f"run in state {current_state} cannot accept another approval")
        plan = read_json(run_dir / "plan.json")
        if plan.get("preflight", {}).get("status") != "READY":
            raise RunnerError("run has unresolved preflight blockers and cannot be approved")
        required = required_approval_stages(plan)
        if stage not in required:
            raise RunnerError(f"stage {stage} is not required by this run's plan")

        approvals = approvals_for(run_dir)
        existing = next((item for item in approvals if item.get("stage") == stage), None)
        if existing:
            return {"run_id": run_id, "state": current_state, "approval": existing, "idempotent": True}

        approval = {"at": utc_now(), "stage": stage, "approved_by": approver}
        with (run_dir / "approvals.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(approval, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        approvals.append(approval)
        approved_stages = {item.get("stage") for item in approvals}
        state = "READY" if set(required).issubset(approved_stages) else "PARTIALLY_APPROVED"
        set_state(run_dir, state, approved_stages=sorted(approved_stages))
        append_event(run_dir, {"event": "STAGE_APPROVED", "state": state, **approval})
        return {"run_id": run_id, "state": state, "approval": approval, "idempotent": False}


def claim_bench(runs_dir: Path, locks_dir: Path, run_id: str) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    run_dir = runs_dir.expanduser().resolve() / run_id
    if not run_dir.is_dir():
        raise RunnerError(f"run does not exist: {run_dir}")
    with transition_lock(run_dir):
        state = read_json(run_dir / "state.json")
        if state.get("state") == "BENCH_CLAIMED":
            lock_path = locks_dir.expanduser().resolve() / "bench.lock"
            if lock_path.is_file():
                current = read_json(lock_path)
                if current.get("run_id") == run_id:
                    return {"run_id": run_id, "state": "BENCH_CLAIMED", "lock": current, "idempotent": True}
        if state.get("state") != "READY":
            raise RunnerError("run must be READY in the queue before it can claim the shared bench")

        manifest = read_json(run_dir / "manifest.json")
        lock_path = locks_dir.expanduser().resolve() / "bench.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = {
            "schema_version": 1,
            "run_id": run_id,
            "submitted_by": manifest.get("submitted_by"),
            "claimed_at": utc_now(),
            "pid": os.getpid(),
        }
        try:
            descriptor = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            current = read_json(lock_path)
            raise RunnerError(f"bench is already claimed by run {current.get('run_id')} ({current.get('submitted_by')})") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(lock, ensure_ascii=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            lock_path.unlink(missing_ok=True)
            raise

        set_state(run_dir, "BENCH_CLAIMED", lock=str(lock_path))
        append_event(run_dir, {"event": "BENCH_CLAIMED", "state": "BENCH_CLAIMED", "lock": str(lock_path)})
        return {"run_id": run_id, "state": "BENCH_CLAIMED", "lock": lock, "idempotent": False}


def release_bench(
    runs_dir: Path,
    locks_dir: Path,
    run_id: str,
    reason: str,
    evidence: Path | None,
) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    reason = reason.upper()
    if reason not in RELEASE_REASONS:
        raise RunnerError(f"reason must be one of: {', '.join(RELEASE_REASONS)}")
    run_dir = runs_dir.expanduser().resolve() / run_id
    lock_path = locks_dir.expanduser().resolve() / "bench.lock"
    if not run_dir.is_dir() or not lock_path.is_file():
        raise RunnerError("run or bench lock does not exist")
    with transition_lock(run_dir):
        lock = read_json(lock_path)
        if lock.get("run_id") != run_id:
            raise RunnerError(f"refusing to release a lock owned by run {lock.get('run_id')}")

        evidence_relative = None
        if reason in {"RESTORE_VERIFIED", "FORMAL_ACTIVATED_VERIFIED"}:
            if evidence is None:
                raise RunnerError(f"{reason} requires --evidence")
            resolved = evidence.expanduser().resolve()
            try:
                evidence_relative = str(resolved.relative_to(run_dir))
            except ValueError as exc:
                raise RunnerError("release evidence must be inside the run directory") from exc
            if not resolved.is_file() or resolved.stat().st_size == 0:
                raise RunnerError("release evidence must be an existing non-empty file")
        if reason == "FORMAL_ACTIVATED_VERIFIED":
            job = read_json(run_dir / "job.json")
            workflow = job.get("workflow", {})
            if not isinstance(workflow, dict) or workflow.get("bench_strategy") != "formal_update":
                raise RunnerError("FORMAL_ACTIVATED_VERIFIED is allowed only for workflow.bench_strategy=formal_update")
            plan = read_json(run_dir / "plan.json")
            safety = plan.get("safety", {})
            if not isinstance(safety, dict) or safety.get("active_path_write_allowed") is not True:
                raise RunnerError("formal activation is not authorized by the immutable plan")
            activation = read_json(resolved)
            if activation.get("state") != "FORMAL_ACTIVATED_VERIFIED":
                raise RunnerError("formal activation evidence has the wrong state")
            if not isinstance(activation.get("formal_inputs"), dict) or not activation["formal_inputs"]:
                raise RunnerError("formal activation evidence must identify the active inputs")

        released = {"event": "BENCH_RELEASED", "state": "RELEASED", "reason": reason, "evidence": evidence_relative}
        append_event(run_dir, released)
        set_state(
            run_dir,
            "RELEASED",
            release_reason=reason,
            release_evidence=evidence_relative,
            restore_evidence=evidence_relative if reason == "RESTORE_VERIFIED" else None,
        )
        lock_path.unlink()
        return {"run_id": run_id, "state": "RELEASED", "reason": reason, "evidence": evidence_relative}


def allowed_actors(run_dir: Path) -> set[str]:
    manifest = read_json(run_dir / "manifest.json")
    result = {str(manifest.get("submitted_by") or "")}
    job = read_json(run_dir / "job.json")
    coordination = job.get("coordination", {})
    if isinstance(coordination, dict):
        configured = coordination.get("allowed_actors", [])
        if isinstance(configured, list):
            result.update(str(item).strip() for item in configured if str(item).strip())
    return {item for item in result if item}


def cancel_run(runs_dir: Path, locks_dir: Path, run_id: str, cancelled_by: str, reason: str) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    actor = validate_identity(cancelled_by, "cancelled_by")
    explanation = validate_identity(reason, "reason")
    run_dir = runs_dir.expanduser().resolve() / run_id
    if not run_dir.is_dir():
        raise RunnerError(f"run does not exist: {run_dir}")
    with transition_lock(run_dir):
        if actor not in allowed_actors(run_dir):
            raise RunnerError("cancelled_by must be the submitter or an allowed coordination actor")
        state = read_json(run_dir / "state.json").get("state")
        if state == "CANCELLED":
            return {"run_id": run_id, "state": "CANCELLED", "idempotent": True}
        if state not in CANCELLABLE_STATES:
            raise RunnerError(f"run in state {state} cannot be cancelled without an execution-specific recovery")
        lock_path = locks_dir.expanduser().resolve() / "bench.lock"
        if lock_path.is_file() and read_json(lock_path).get("run_id") == run_id:
            raise RunnerError("run already owns the bench; use the execution-specific stop and restore workflow")
        set_state(run_dir, "CANCELLED", cancelled_by=actor, reason=explanation)
        append_event(
            run_dir,
            {"event": "RUN_CANCELLED", "state": "CANCELLED", "cancelled_by": actor, "reason": explanation},
        )
        return {"run_id": run_id, "state": "CANCELLED", "cancelled_by": actor, "reason": explanation, "idempotent": False}


def requeue_run(runs_dir: Path, locks_dir: Path, run_id: str, requeued_by: str) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    actor = validate_identity(requeued_by, "requeued_by")
    run_dir = runs_dir.expanduser().resolve() / run_id
    if not run_dir.is_dir():
        raise RunnerError(f"run does not exist: {run_dir}")
    with transition_lock(run_dir):
        if actor not in allowed_actors(run_dir):
            raise RunnerError("requeued_by must be the submitter or an allowed coordination actor")
        state = read_json(run_dir / "state.json").get("state")
        if state not in REQUEUEABLE_STATES:
            raise RunnerError(f"run in state {state} is not eligible for coordination requeue")
        lock_path = locks_dir.expanduser().resolve() / "bench.lock"
        if lock_path.is_file() and read_json(lock_path).get("run_id") == run_id:
            raise RunnerError("cannot requeue a run that still owns the bench")
        plan = read_json(run_dir / "plan.json")
        approvals = {item.get("stage") for item in approvals_for(run_dir)}
        required = required_approval_stages(plan)
        if plan.get("preflight", {}).get("status") != "READY" or not set(required).issubset(approvals):
            raise RunnerError("run no longer has a ready preflight and complete approvals")
        set_state(run_dir, "READY", requeued_by=actor)
        append_event(run_dir, {"event": "RUN_REQUEUED", "state": "READY", "requeued_by": actor})
        return {"run_id": run_id, "state": "READY", "requeued_by": actor}


def queue_snapshot(runs_dir: Path, locks_dir: Path) -> dict[str, Any]:
    root = runs_dir.expanduser().resolve()
    queued: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    if root.is_dir():
        for run_dir in root.iterdir():
            if not run_dir.is_dir() or not (run_dir / "state.json").is_file() or not (run_dir / "manifest.json").is_file():
                continue
            state = read_json(run_dir / "state.json")
            manifest = read_json(run_dir / "manifest.json")
            item = {
                "run_id": run_dir.name,
                "state": state.get("state"),
                "submitted_by": manifest.get("submitted_by"),
                "submitted_at": manifest.get("submitted_at"),
                "updated_at": state.get("updated_at"),
            }
            if state.get("state") == "READY":
                queued.append(item)
            elif state.get("state") == "BLOCKED_VALIDATION":
                blocked.append({**item, "reason": state.get("reason"), "message": state.get("message")})
            elif state.get("state") in ("WAITING_INPUT", "WAITING_APPROVAL", "PARTIALLY_APPROVED"):
                waiting.append(item)
    def queue_key(item: dict[str, Any]) -> tuple[Any, ...]:
        manifest = read_json(root / str(item["run_id"]) / "manifest.json")
        order = manifest.get("submitted_order_ns")
        if isinstance(order, int):
            return (0, order, str(item["run_id"]))
        return (1, str(item.get("submitted_at") or ""), str(item["run_id"]))

    queued.sort(key=queue_key)
    for position, item in enumerate(queued, start=1):
        item["queue_position"] = position
    waiting.sort(key=lambda item: (str(item.get("submitted_at") or ""), str(item["run_id"])))
    lock_path = locks_dir.expanduser().resolve() / "bench.lock"
    active = inspect_bench_lock(lock_path)
    return {
        "schema_version": 1,
        "active": active,
        "queued": queued,
        "waiting": waiting,
        "blocked": sorted(blocked, key=lambda item: item["run_id"]),
        "blocked_count": len(blocked),
        "queued_count": len(queued),
        "waiting_count": len(waiting),
    }


def runner_status(runs_dir: Path, locks_dir: Path, run_id: str) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    run_dir = runs_dir.expanduser().resolve() / run_id
    if not run_dir.is_dir():
        raise RunnerError(f"run does not exist: {run_dir}")
    lock_path = locks_dir.expanduser().resolve() / "bench.lock"
    lock = inspect_bench_lock(lock_path)
    queue = queue_snapshot(runs_dir, locks_dir)
    queue_item = next((item for item in queue["queued"] if item["run_id"] == run_id), None)
    return {
        "run": read_json(run_dir / "state.json"),
        "manifest": read_json(run_dir / "manifest.json"),
        "approvals": approvals_for(run_dir),
        "bench_lock": lock,
        "owns_bench": bool(lock and lock.get("run_id") == run_id),
        "queue_position": queue_item.get("queue_position") if queue_item else None,
    }
