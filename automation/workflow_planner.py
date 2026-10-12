#!/usr/bin/env python3
"""Plan target-driven bench evaluation workflows without contacting remote hosts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workflow_receipts import receipt_path


LEGACY_PROTOCOL = "1.0"
TARGET_PROTOCOL = "1.1"
SUPPORTED_PROTOCOLS = (LEGACY_PROTOCOL, TARGET_PROTOCOL)
TARGETS = ("workstation_predictions", "lpai_predictions", "evaluation_report")
SO_POLICIES = ("installed", "prebuilt", "build", "auto")
BENCH_STRATEGIES = ("use_current_app", "isolated_candidate", "formal_update")
NODE_STATUSES = (
    "NOT_REQUESTED",
    "REUSED_VERIFIED",
    "SUCCEEDED",
    "FAILED",
    "BLOCKED",
    "INVALID_CACHE",
)

NODE_DEFINITIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ACQUIRE", "Resolve an approved APP/Lockfile source and verify provenance", ()),
    ("BUILD", "Build and archive the evaluation-dump uroad library when required", ("ACQUIRE",)),
    ("INTAKE", "Register APP, Lockfile and replacement libraries by content identity", ("ACQUIRE", "BUILD")),
    ("ASSEMBLE", "Create an isolated candidate package from verified inputs", ("INTAKE",)),
    ("PREFLIGHT", "Validate the requested targets, paths and reusable evidence", ()),
    ("STAGE", "Verify and extract the selected release in an isolated bench path", ("ASSEMBLE", "PREFLIGHT")),
    ("CONFIGURE", "Generate and compare the offline replay configuration", ("STAGE",)),
    ("ACTIVATE", "Activate the approved candidate when the selected strategy requires it", ("CONFIGURE",)),
    ("REPLAY", "Replay the selected scene on the bench", ("PREFLIGHT",)),
    ("RESTORE", "Restore and verify the original App after any activation", ("ACTIVATE", "REPLAY")),
    ("COLLECT_WORKSTATION", "Collect a verified shared prediction product on the workstation", ("REPLAY",)),
    ("PUBLISH_LPAI", "Publish the verified workstation product to the submitter's LPAI space", ("COLLECT_WORKSTATION",)),
    ("EVALUATE", "Run or reuse a valid offline evaluation attempt", ("PUBLISH_LPAI",)),
    ("REPORT", "Record requested-goal completion and all node receipts", ()),
)

LEGACY_STAGE_NAMES = (
    "ACQUIRE", "BUILD", "INTAKE", "ASSEMBLE", "PREFLIGHT", "STAGE", "CONFIGURE",
    "ACTIVATE", "REPLAY", "COLLECT", "EVALUATE", "RESTORE", "REPORT",
)


class WorkflowPlanError(ValueError):
    """Raised when a target workflow declaration is malformed."""


def _workflow(job: dict[str, Any]) -> dict[str, Any]:
    value = job.get("workflow", {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkflowPlanError("workflow must be an object")
    return value


def validate_workflow(job: dict[str, Any]) -> list[dict[str, str]]:
    protocol = job.get("runner_protocol")
    blockers: list[dict[str, str]] = []
    try:
        workflow = _workflow(job)
    except WorkflowPlanError as exc:
        return [{"code": "INVALID_WORKFLOW", "path": "workflow", "message": str(exc)}]

    if protocol != TARGET_PROTOCOL:
        if workflow:
            blockers.append({
                "code": "WORKFLOW_PROTOCOL_REQUIRED",
                "path": "workflow",
                "message": f"workflow targets require runner_protocol {TARGET_PROTOCOL}",
            })
        return blockers

    targets = workflow.get("targets")
    if not isinstance(targets, list) or not targets:
        blockers.append({
            "code": "MISSING_WORKFLOW_TARGETS",
            "path": "workflow.targets",
            "message": "runner_protocol 1.1 requires at least one target",
        })
    else:
        seen: set[str] = set()
        for index, target in enumerate(targets):
            if target not in TARGETS:
                blockers.append({
                    "code": "INVALID_WORKFLOW_TARGET",
                    "path": f"workflow.targets[{index}]",
                    "message": f"target must be one of: {', '.join(TARGETS)}",
                })
            elif target in seen:
                blockers.append({
                    "code": "DUPLICATE_WORKFLOW_TARGET",
                    "path": f"workflow.targets[{index}]",
                    "message": "workflow targets must be unique",
                })
            else:
                seen.add(str(target))

    so_policy = workflow.get("so_policy", "auto")
    if so_policy not in SO_POLICIES:
        blockers.append({
            "code": "INVALID_SO_POLICY",
            "path": "workflow.so_policy",
            "message": f"so_policy must be one of: {', '.join(SO_POLICIES)}",
        })
    if workflow.get("reuse_policy", "verified_only") != "verified_only":
        blockers.append({
            "code": "INVALID_REUSE_POLICY",
            "path": "workflow.reuse_policy",
            "message": "only verified_only reuse is supported",
        })

    bench_strategy = workflow.get("bench_strategy", "isolated_candidate")
    if bench_strategy not in BENCH_STRATEGIES:
        blockers.append({
            "code": "INVALID_BENCH_STRATEGY",
            "path": "workflow.bench_strategy",
            "message": f"bench_strategy must be one of: {', '.join(BENCH_STRATEGIES)}",
        })
    if bench_strategy == "formal_update" and so_policy != "prebuilt":
        blockers.append({
            "code": "FORMAL_UPDATE_REQUIRES_PREBUILT",
            "path": "workflow.so_policy",
            "message": "formal_update requires explicitly identified prebuilt artifacts",
        })

    sources = workflow.get("sources", {})
    if sources is not None and not isinstance(sources, dict):
        blockers.append({
            "code": "INVALID_WORKFLOW_SOURCES",
            "path": "workflow.sources",
            "message": "workflow.sources must be an object",
        })
        sources = {}
    source_names = set(sources) if isinstance(sources, dict) else set()
    target_names = set(targets) if isinstance(targets, list) else set()
    if "workstation_predictions" in source_names and "lpai_predictions" in source_names:
        blockers.append({
            "code": "AMBIGUOUS_PREDICTION_SOURCE",
            "path": "workflow.sources",
            "message": "declare either workstation_predictions or lpai_predictions, not both",
        })
    if "workstation_predictions" in target_names and source_names:
        blockers.append({
            "code": "TARGET_ALREADY_SUPPLIED",
            "path": "workflow.sources",
            "message": "workstation_predictions cannot be both requested and supplied",
        })
    if "lpai_predictions" in target_names and "lpai_predictions" in source_names:
        blockers.append({
            "code": "TARGET_ALREADY_SUPPLIED",
            "path": "workflow.sources.lpai_predictions",
            "message": "lpai_predictions cannot be both requested and supplied",
        })
    return blockers


def requested_targets(job: dict[str, Any]) -> tuple[str, ...]:
    if job.get("runner_protocol") != TARGET_PROTOCOL:
        return TARGETS
    workflow = _workflow(job)
    targets = workflow.get("targets", [])
    return tuple(target for target in TARGETS if target in targets)


def uses_personal_lpai(job: dict[str, Any]) -> bool:
    targets = set(requested_targets(job))
    return bool(targets & {"lpai_predictions", "evaluation_report"})


def requires_bench(job: dict[str, Any]) -> bool:
    if job.get("runner_protocol") != TARGET_PROTOCOL:
        return True
    workflow = _workflow(job)
    if isinstance(workflow.get("sources"), dict):
        sources = workflow["sources"]
        if "workstation_predictions" in sources or "lpai_predictions" in sources:
            return False
    return "workstation_predictions" in requested_targets(job) or bool(
        set(requested_targets(job)) & {"lpai_predictions", "evaluation_report"}
    )


def required_approvals(job: dict[str, Any]) -> list[str]:
    return ["ACTIVATE", "REPLAY"] if requires_bench(job) else []


def _requested_nodes(job: dict[str, Any]) -> set[str]:
    if job.get("runner_protocol") != TARGET_PROTOCOL:
        return set(LEGACY_STAGE_NAMES)

    targets = set(requested_targets(job))
    workflow = _workflow(job)
    sources = workflow.get("sources", {}) if isinstance(workflow.get("sources"), dict) else {}
    nodes = {"PREFLIGHT", "REPORT"}

    lpai_available = "lpai_predictions" in sources
    workstation_available = "workstation_predictions" in sources
    if "evaluation_report" in targets:
        nodes.add("EVALUATE")
        if not lpai_available:
            nodes.add("PUBLISH_LPAI")
    if "lpai_predictions" in targets:
        nodes.add("PUBLISH_LPAI")
    if "PUBLISH_LPAI" in nodes and not workstation_available:
        nodes.add("COLLECT_WORKSTATION")
    if "workstation_predictions" in targets:
        nodes.add("COLLECT_WORKSTATION")

    if "COLLECT_WORKSTATION" in nodes and not workstation_available:
        nodes.add("REPLAY")
        strategy = str(workflow.get("bench_strategy") or "isolated_candidate")
        if strategy == "isolated_candidate":
            nodes.update({"ACQUIRE", "INTAKE", "ASSEMBLE", "STAGE", "CONFIGURE", "ACTIVATE", "RESTORE"})
            if workflow.get("so_policy", "auto") == "build":
                nodes.add("BUILD")
        elif strategy == "use_current_app":
            pass
        elif strategy == "formal_update":
            nodes.update({"CONFIGURE", "ACTIVATE"})
        else:
            nodes.add("ACTIVATE")
    return nodes


def build_target_steps(
    job: dict[str, Any],
    *,
    stage_ready: bool,
    replay_ready: bool,
    receipt_root: Path | None = None,
) -> list[dict[str, Any]]:
    if job.get("runner_protocol") != TARGET_PROTOCOL:
        legacy_description = {name: description for name, description, _deps in NODE_DEFINITIONS}
        legacy_description["COLLECT"] = "Validate and transfer prediction output"
        return [
            {
                "order": index,
                "state": state,
                "description": legacy_description[state],
                "explicit_approval_required": state in {"ACTIVATE", "REPLAY"},
                "eligible": stage_ready if state in {"PREFLIGHT", "STAGE"} else replay_ready,
            }
            for index, state in enumerate(LEGACY_STAGE_NAMES, start=1)
        ]

    selected = _requested_nodes(job)
    approvals = set(required_approvals(job))
    steps: list[dict[str, Any]] = []
    for index, (state, description, dependencies) in enumerate(NODE_DEFINITIONS, start=1):
        requested = state in selected
        receipt = receipt_path(receipt_root, state) if receipt_root else None
        status = "NOT_REQUESTED" if not requested else "PENDING"
        reason = "not required by requested targets" if not requested else "required by requested targets"
        if requested and receipt and receipt.is_file():
            status = "DISCOVERY_REQUIRED"
            reason = "receipt exists and must be identity-verified before reuse"
        steps.append({
            "order": index,
            "state": state,
            "description": description,
            "dependencies": [dependency for dependency in dependencies if dependency in selected],
            "requested": requested,
            "status": status,
            "reason": reason,
            "receipt": str(receipt) if receipt else None,
            "explicit_approval_required": state in approvals,
            "eligible": bool((stage_ready if state in {"PREFLIGHT", "STAGE"} else replay_ready) and requested),
        })
    return steps


def plan_metadata(job: dict[str, Any]) -> dict[str, Any]:
    targets = list(requested_targets(job))
    bench = requires_bench(job)
    return {
        "workflow_schema_version": 1,
        "requested_targets": targets,
        "so_policy": _workflow(job).get("so_policy", "auto") if job.get("runner_protocol") == TARGET_PROTOCOL else "legacy",
        "bench_strategy": _workflow(job).get("bench_strategy", "isolated_candidate") if job.get("runner_protocol") == TARGET_PROTOCOL else "legacy",
        "reuse_policy": _workflow(job).get("reuse_policy", "verified_only") if job.get("runner_protocol") == TARGET_PROTOCOL else "legacy",
        "requires_bench": bench,
        "requires_submitter_lpai_session": uses_personal_lpai(job),
        "completion_state": "COMPLETED_TO_REQUESTED_GOAL" if job.get("runner_protocol") == TARGET_PROTOCOL else "FULL_COMPLETED",
    }
