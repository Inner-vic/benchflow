#!/usr/bin/env python3
"""Execute protocol 1.1 workflows to their explicitly requested targets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bench_transaction import execute_profile_transaction, selected_command_set
from evaluation_runner import EvaluationError, resolve_evaluation, run_evaluation
from prediction_transfer import (
    TransferError,
    collect_predictions_to_workstation,
    publish_predictions_to_lpai,
    resolve_collection,
    resolve_publication,
)
from runner_state import RunnerError, append_event, read_json, set_state
from workflow_planner import TARGET_PROTOCOL, requested_targets, requires_bench
from workflow_receipts import write_receipt


SUCCESS_EVALUATION_STATES = {"EVALUATION_COMPLETED", "EVALUATION_REUSED"}


def _load_profile(path: Path) -> dict[str, Any]:
    try:
        value = read_json(path.expanduser().resolve())
    except RunnerError as exc:
        raise RunnerError(f"cannot read target-workflow profile {path}: {exc}") from exc
    return value


def _workflow_sources(job: dict[str, Any]) -> dict[str, str]:
    workflow = job.get("workflow")
    sources = workflow.get("sources") if isinstance(workflow, dict) else None
    if not isinstance(sources, dict):
        return {}
    return {str(key): str(value) for key, value in sources.items()}


def validate_target_job(job: dict[str, Any], profile_path: Path, run_dir: Path) -> dict[str, Any]:
    if job.get("runner_protocol") != TARGET_PROTOCOL:
        raise RunnerError(f"target-workflow requires runner_protocol {TARGET_PROTOCOL}")
    profile = _load_profile(profile_path)
    targets = set(requested_targets(job))
    sources = _workflow_sources(job)
    bench = requires_bench(job)

    needs_workstation = "workstation_predictions" in targets or (
        "lpai_predictions" in targets and "workstation_predictions" not in sources
    ) or (
        "evaluation_report" in targets
        and "lpai_predictions" not in sources
        and "workstation_predictions" not in sources
    )
    needs_publication = "lpai_predictions" in targets or (
        "evaluation_report" in targets and "lpai_predictions" not in sources
    )

    collection = None
    publication = None
    try:
        if needs_workstation:
            collection = resolve_collection(job, profile, run_dir)
        if needs_publication:
            publication = resolve_publication(job, profile, run_dir)
    except TransferError as exc:
        raise RunnerError(str(exc)) from exc

    if "workstation_predictions" in sources:
        expected_cache = publication["cache"] if publication else None
        if expected_cache is None:
            raise RunnerError("workstation prediction source is not used by the requested targets")
        if Path(sources["workstation_predictions"]).expanduser().resolve() != expected_cache:
            raise RunnerError("workflow workstation source must equal the resolved shared workstation product")

    evaluation = None
    if "evaluation_report" in targets:
        try:
            evaluation = resolve_evaluation(job, profile, run_dir)
        except EvaluationError as exc:
            raise RunnerError(str(exc)) from exc
        lpai_source = sources.get("lpai_predictions")
        expected_prediction = lpai_source or (
            publication["destination"] if publication else None
        )
        if expected_prediction != evaluation["context"]["prediction_path"]:
            raise RunnerError("resolved LPAI prediction product must equal evaluation prediction_path")

    if bench:
        _execution, command_set = selected_command_set(job, profile_path)
        command_strategy = command_set["strategy"]
        workflow = job.get("workflow")
        declared_strategy = (
            workflow.get("bench_strategy") if isinstance(workflow, dict) else None
        )
        if declared_strategy is not None and declared_strategy != command_strategy:
            raise RunnerError(
                "workflow.bench_strategy must match the selected Profile command set "
                f"(workflow={declared_strategy!r}, profile={command_strategy!r})"
            )
    return {
        "profile": profile,
        "targets": targets,
        "sources": sources,
        "requires_bench": bench,
        "collection": collection,
        "publication": publication,
        "evaluation": evaluation,
    }


def execute_target_workflow(
    *,
    runs_dir: Path,
    locks_dir: Path,
    run_id: str,
    profile_path: Path,
    command_runner: Any | None = None,
    transfer_runner: Any | None = None,
    evaluation_runner: Any | None = None,
) -> dict[str, Any]:
    run_dir = runs_dir.expanduser().resolve() / run_id
    job = read_json(run_dir / "job.json")
    resolved = validate_target_job(job, profile_path, run_dir)
    append_event(run_dir, {"event": "TARGET_WORKFLOW_STARTED", "state": "RUNNING"})

    transaction = None
    if resolved["requires_bench"]:
        transaction = execute_profile_transaction(
            runs_dir=runs_dir,
            locks_dir=locks_dir,
            run_id=run_id,
            profile_path=profile_path,
            command_runner=command_runner,
        )
        if transaction.get("status") != "PROCESSED":
            return transaction
    else:
        set_state(run_dir, "RUNNING", executor="target-workflow", requires_bench=False)

    targets = resolved["targets"]
    sources = resolved["sources"]
    workstation = None
    publication = None
    evaluation = None

    needs_workstation = "workstation_predictions" in targets or (
        "lpai_predictions" in targets and "workstation_predictions" not in sources
    ) or (
        "evaluation_report" in targets
        and "lpai_predictions" not in sources
        and "workstation_predictions" not in sources
    )
    if needs_workstation:
        set_state(run_dir, "COLLECTING_WORKSTATION", executor="target-workflow")
        append_event(run_dir, {"event": "WORKSTATION_COLLECTION_STARTED", "state": "COLLECTING_WORKSTATION"})
        try:
            workstation = collect_predictions_to_workstation(
                job, resolved["profile"], run_dir, execute=True, runner=transfer_runner,
            )
        except (TransferError, OSError) as exc:
            write_receipt(
                run_dir, "COLLECT_WORKSTATION", "FAILED", targets=targets,
                details={"message": str(exc)},
            )
            set_state(run_dir, "WORKSTATION_COLLECTION_FAILED", executor="target-workflow", message=str(exc))
            append_event(run_dir, {"event": "WORKSTATION_COLLECTION_FAILED", "state": "WORKSTATION_COLLECTION_FAILED", "message": str(exc)})
            return {"status": "FAILED", "run_id": run_id, "state": "WORKSTATION_COLLECTION_FAILED", "message": str(exc)}

    needs_publication = "lpai_predictions" in targets or (
        "evaluation_report" in targets and "lpai_predictions" not in sources
    )
    if needs_publication:
        set_state(run_dir, "PUBLISHING_LPAI", executor="target-workflow")
        append_event(run_dir, {"event": "LPAI_PUBLICATION_STARTED", "state": "PUBLISHING_LPAI"})
        try:
            publication = publish_predictions_to_lpai(
                job, resolved["profile"], run_dir, execute=True, runner=transfer_runner,
            )
        except (TransferError, OSError) as exc:
            write_receipt(
                run_dir, "PUBLISH_LPAI", "FAILED", targets=targets,
                details={"message": str(exc)},
            )
            set_state(run_dir, "LPAI_PUBLICATION_FAILED", executor="target-workflow", message=str(exc))
            append_event(run_dir, {"event": "LPAI_PUBLICATION_FAILED", "state": "LPAI_PUBLICATION_FAILED", "message": str(exc)})
            return {"status": "FAILED", "run_id": run_id, "state": "LPAI_PUBLICATION_FAILED", "message": str(exc)}

    if "evaluation_report" in targets:
        set_state(run_dir, "EVALUATING", executor="target-workflow")
        append_event(run_dir, {"event": "EVALUATION_STARTED", "state": "EVALUATING"})
        try:
            evaluation = run_evaluation(
                job, resolved["profile"], run_dir, execute=True, runner=evaluation_runner,
            )
        except (EvaluationError, OSError) as exc:
            write_receipt(
                run_dir, "EVALUATE", "FAILED", targets=targets,
                details={"message": str(exc)},
            )
            set_state(run_dir, "EVALUATION_FAILED", executor="target-workflow", message=str(exc))
            append_event(run_dir, {"event": "EVALUATION_FAILED", "state": "EVALUATION_FAILED", "message": str(exc)})
            return {"status": "FAILED", "run_id": run_id, "state": "EVALUATION_FAILED", "message": str(exc)}
        if evaluation.get("state") not in SUCCESS_EVALUATION_STATES:
            state = str(evaluation.get("state") or "EVALUATION_FAILED")
            set_state(run_dir, state, executor="target-workflow")
            append_event(run_dir, {"event": "TARGET_WORKFLOW_FAILED", "state": state})
            return {"status": "FAILED", "run_id": run_id, "state": state, "evaluation": evaluation}

    completed = {
        "status": "PROCESSED",
        "run_id": run_id,
        "state": "COMPLETED_TO_REQUESTED_GOAL",
        "requested_targets": sorted(targets),
        "transaction": transaction,
        "workstation_predictions": workstation,
        "lpai_predictions": publication,
        "evaluation": evaluation,
    }
    write_receipt(
        run_dir,
        "REPORT",
        "SUCCEEDED",
        targets=targets,
        details={"completion_state": completed["state"]},
    )
    set_state(
        run_dir,
        completed["state"],
        executor="target-workflow",
        requested_targets=completed["requested_targets"],
    )
    append_event(run_dir, {"event": "TARGET_WORKFLOW_COMPLETED", "state": completed["state"]})
    return completed
