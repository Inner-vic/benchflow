#!/usr/bin/env python3
"""Compose bench transaction, verified transfer, and LPAI evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bench_transaction import execute_profile_transaction, selected_command_set
from evaluation_runner import EvaluationError, resolve_evaluation, run_evaluation
from prediction_transfer import TransferError, resolve_transfer, transfer_predictions
from runner_state import RunnerError, append_event, read_json, set_state
from workflow_planner import LEGACY_PROTOCOL


SUCCESS_EVALUATION_STATES = {"EVALUATION_COMPLETED"}


def load_profile(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"cannot read full-evaluation profile {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerError("full-evaluation profile must contain a JSON object")
    return value


def validate_full_job(job: dict[str, Any], profile_path: Path, run_dir: Path) -> dict[str, Any]:
    protocol = str(job.get("runner_protocol") or LEGACY_PROTOCOL)
    if protocol != LEGACY_PROTOCOL:
        raise RunnerError(f"full-evaluation requires runner_protocol {LEGACY_PROTOCOL}")
    selected_command_set(job, profile_path)
    profile = load_profile(profile_path)
    transfer = resolve_transfer(job, profile, run_dir)
    evaluation = resolve_evaluation(job, profile, run_dir)
    if transfer["destination"] != evaluation["context"]["prediction_path"]:
        raise RunnerError("prediction transfer destination must equal evaluation prediction_path")
    return {"profile": profile, "transfer": transfer, "evaluation": evaluation}


def execute_full_evaluation(
    *, runs_dir: Path, locks_dir: Path, run_id: str, profile_path: Path,
    command_runner: Any | None = None, transfer_runner: Any | None = None,
    evaluation_runner: Any | None = None,
) -> dict[str, Any]:
    run_dir = runs_dir.expanduser().resolve() / run_id
    job = read_json(run_dir / "job.json")
    resolved = validate_full_job(job, profile_path, run_dir)
    append_event(run_dir, {"event": "FULL_EVALUATION_STARTED", "state": "RUNNING"})
    transaction = execute_profile_transaction(
        runs_dir=runs_dir, locks_dir=locks_dir, run_id=run_id,
        profile_path=profile_path, command_runner=command_runner,
    )
    if transaction.get("status") != "PROCESSED":
        return transaction

    set_state(run_dir, "TRANSFERRING", executor="full-evaluation", restore_verified=True)
    append_event(run_dir, {"event": "PREDICTION_TRANSFER_STARTED", "state": "TRANSFERRING"})
    try:
        transfer = transfer_predictions(
            job, resolved["profile"], run_dir, execute=True, runner=transfer_runner,
        )
    except (TransferError, OSError) as exc:
        set_state(run_dir, "TRANSFER_FAILED", executor="full-evaluation", restore_verified=True, message=str(exc))
        append_event(run_dir, {"event": "PREDICTION_TRANSFER_FAILED", "state": "TRANSFER_FAILED", "message": str(exc)})
        return {"status": "FAILED", "run_id": run_id, "state": "TRANSFER_FAILED", "message": str(exc)}

    set_state(run_dir, "EVALUATING", executor="full-evaluation", restore_verified=True)
    append_event(run_dir, {"event": "EVALUATION_STARTED", "state": "EVALUATING"})
    try:
        evaluation = run_evaluation(
            job, resolved["profile"], run_dir, execute=True, runner=evaluation_runner,
            allow_reuse=False,
        )
    except (EvaluationError, OSError) as exc:
        set_state(run_dir, "EVALUATION_FAILED", executor="full-evaluation", restore_verified=True, message=str(exc))
        append_event(run_dir, {"event": "EVALUATION_FAILED", "state": "EVALUATION_FAILED", "message": str(exc)})
        return {"status": "FAILED", "run_id": run_id, "state": "EVALUATION_FAILED", "message": str(exc)}

    if evaluation.get("state") not in SUCCESS_EVALUATION_STATES:
        state = str(evaluation.get("state") or "EVALUATION_FAILED")
        set_state(run_dir, state, executor="full-evaluation", restore_verified=True)
        append_event(run_dir, {"event": "FULL_EVALUATION_FAILED", "state": state})
        return {"status": "FAILED", "run_id": run_id, "state": state, "evaluation": evaluation}

    set_state(
        run_dir, "FULL_COMPLETED", executor="full-evaluation", restore_verified=True,
        transfer_inventory_sha256=transfer.get("inventory_sha256"),
        evaluation_decision=evaluation.get("acceptance", {}).get("decision"),
    )
    append_event(run_dir, {"event": "FULL_EVALUATION_COMPLETED", "state": "FULL_COMPLETED"})
    return {
        "status": "PROCESSED", "run_id": run_id, "state": "FULL_COMPLETED",
        "transfer": transfer, "evaluation": evaluation,
    }
