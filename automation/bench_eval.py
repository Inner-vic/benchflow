#!/usr/bin/env python3
"""Unified, auditable command-line entry point for offline bench evaluation.

The control commands in this file are read-only. Commands that may contact a
bench are delegated to the existing narrowly-scoped tools, which keep their
own explicit execution flags and path guards.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from network_doctor import collect_snapshot, controller_ip_from_environment, diagnose
from queue_worker import (
    SUPPORTED_EXECUTORS,
    process_next,
    run_worker_loop,
    start_background_worker,
    stop_background_worker,
    worker_status,
)
from runner_state import (
    RunnerError,
    approve_stage,
    cancel_run,
    claim_bench,
    queue_snapshot,
    requeue_run,
    release_bench,
    runner_status,
    submit_job,
    validate_run_id,
)
from workflow_planner import (
    LEGACY_PROTOCOL,
    SUPPORTED_PROTOCOLS,
    TARGET_PROTOCOL,
    build_target_steps,
    plan_metadata,
    required_approvals,
    validate_workflow,
)


VERSION = "1.8.2"
RUNNER_PROTOCOL_VERSION = TARGET_PROTOCOL
ACCEPTED_RUNNER_PROTOCOLS = SUPPORTED_PROTOCOLS
TOOL_COMMANDS = {
    "artifact": "artifact_pipeline.py",
    "build-eval-lib": "build_uroad_eval.py",
    "prepare-replay": "prepare_replay.py",
    "stage-package": "stage_package.py",
    "stage-existing": "stage_existing_package.py",
    "prepare-config": "prepare_bench_config.py",
    "compare-trees": "compare_trees.py",
    "replay": "replay_client.py",
    "validate-output": "validate_predictions.py",
    "transfer-predictions": "prediction_transfer.py",
    "parse-eval-report": "parse_eval_report.py",
    "run-evaluation": "evaluation_runner.py",
    "batch": "batch_plan.py",
}
WORKFLOW = (
    ("ACQUIRE", "Resolve an approved APP/Lockfile source and verify provenance", False),
    ("BUILD", "Build and archive the evaluation-dump uroad library when required", False),
    ("INTAKE", "Register APP, Lockfile and all three replacement libraries by hash", False),
    ("ASSEMBLE", "Create an isolated candidate package from approved libraries on the workstation", False),
    ("PREFLIGHT", "Validate the job, assets, paths and unresolved decisions", False),
    ("STAGE", "Verify and extract the selected release below /app/livc-test/<run-id>", False),
    ("CONFIGURE", "Generate and compare the staged offline-replay configuration candidate", False),
    ("ACTIVATE", "Stop/coexist with the active App and start the isolated candidate", True),
    ("REPLAY", "Connect RecorderHMI and send the selected scene", True),
    ("COLLECT", "Validate and transfer prediction output", False),
    ("EVALUATE", "Run the approved development-space evaluation entry point", False),
    ("RESTORE", "Restore and verify the original App whenever activation changed bench state", False),
    ("REPORT", "Record versions, hashes, evidence, metrics and restoration state", False),
)

# This table intentionally distinguishes a documented stage from an implemented
# adapter.  It is used by ``audit`` so a dry-run cannot be mistaken for a working
# end-to-end live workflow.
AUTOMATION_COVERAGE = {
    "ACQUIRE": {
        "status": "IMPLEMENTED",
        "entrypoint": "bench-eval artifact acquire or a verified workstation/local source path",
        "missing": [],
    },
    "BUILD": {
        "status": "IMPLEMENTED",
        "entrypoint": "bench-eval build-eval-lib",
        "missing": [],
    },
    "INTAKE": {
        "status": "IMPLEMENTED",
        "entrypoint": "bench-eval artifact intake",
        "missing": [],
    },
    "PREFLIGHT": {
        "status": "IMPLEMENTED",
        "entrypoint": "bench-eval preflight",
        "missing": [],
    },
    "STAGE": {
        "status": "IMPLEMENTED",
        "entrypoint": "bench-eval stage-package / stage-existing",
        "missing": [],
    },
    "ASSEMBLE": {
        "status": "IMPLEMENTED",
        "entrypoint": "bench-eval artifact assemble",
        "missing": [],
    },
    "CONFIGURE": {
        "status": "IMPLEMENTED",
        "entrypoint": "bench-eval prepare-config / compare-trees",
        "missing": [],
    },
    "ACTIVATE": {
        "status": "PARTIAL",
        "entrypoint": "bench-eval worker once --executor full-evaluation",
        "missing": ["Runner 1.7.0 full-batch live validation"],
    },
    "REPLAY": {
        "status": "IMPLEMENTED",
        "entrypoint": "bench-eval replay",
        "missing": [],
    },
    "COLLECT": {
        "status": "PARTIAL",
        "entrypoint": "bench-eval validate-output / transfer-predictions",
        "missing": ["Runner 1.7.0 full-batch live validation"],
    },
    "EVALUATE": {
        "status": "PARTIAL",
        "entrypoint": "bench-eval run-evaluation / parse-eval-report",
        "missing": ["Runner 1.7.0 full-batch live validation"],
    },
    "RESTORE": {
        "status": "PARTIAL",
        "entrypoint": "profile-transaction mandatory restore plus verification",
        "missing": ["Runner 1.7.0 full-batch live validation"],
    },
    "REPORT": {
        "status": "PARTIAL",
        "entrypoint": "run evidence directories and status",
        "missing": ["single final report assembled from every stage checkpoint"],
    },
}
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
SENSITIVE_KEYS = ("password", "passwd", "jwt", "access_token", "secret")


class CliError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CliError(f"job file does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"cannot read job file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CliError("job document must be a JSON object")
    return value


def walk(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else str(key)
            yield child_path, child
            yield from walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{prefix}[{index}]"
            yield child_path, child
            yield from walk(child, child_path)


def todo_fields(job: dict[str, Any]) -> list[str]:
    return sorted(
        path for path, value in walk(job)
        if isinstance(value, str) and value.strip().upper().startswith("TODO:")
    )


def embedded_secret_fields(job: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    for path, value in walk(job):
        key = path.rsplit(".", 1)[-1].lower()
        if any(marker in key for marker in SENSITIVE_KEYS) and value not in (None, "", False):
            findings.append(path)
        if isinstance(value, str) and "-----BEGIN " in value and "PRIVATE KEY-----" in value:
            findings.append(path)
    return sorted(set(findings))


def embedded_command_fields(job: dict[str, Any]) -> list[str]:
    """Jobs select a command set; executable content is profile-only."""
    forbidden = {"command", "commands", "argv", "shell", "script"}
    return sorted(
        path for path, _value in walk(job)
        if not re.search(r"\[\d+\]$", path)
        and path.rsplit(".", 1)[-1].lower() in forbidden
    )


def evaluate_job(job: dict[str, Any], source: Path | None = None) -> dict[str, Any]:
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    required_values = {
        "schema_version": job.get("schema_version"),
        "runner_protocol": job.get("runner_protocol"),
        "run_id": job.get("run_id"),
        "mode": job.get("mode"),
    }
    for path, value in required_values.items():
        if value in (None, ""):
            blockers.append({"code": "MISSING_REQUIRED", "path": path, "message": "value is required by job.schema.json"})
    if job.get("schema_version") not in (None, 1):
        blockers.append({"code": "JOB_SCHEMA_VERSION", "path": "schema_version", "message": "only schema version 1 is accepted"})
    if job.get("run_id") not in (None, ""):
        try:
            validate_run_id(job.get("run_id"))
        except RunnerError as exc:
            blockers.append({"code": "INVALID_RUN_ID", "path": "run_id", "message": str(exc)})

    for path in todo_fields(job):
        blockers.append({"code": "UNRESOLVED_TODO", "path": path, "message": "value is unresolved"})
    for path in embedded_secret_fields(job):
        blockers.append({"code": "EMBEDDED_SECRET", "path": path, "message": "use an SSH alias or local credential store"})
    for path in embedded_command_fields(job):
        blockers.append({"code": "JOB_EMBEDDED_COMMAND", "path": path, "message": "select execution.command_set_id; commands belong only in the workstation profile"})
    requested_protocol = job.get("runner_protocol")
    if requested_protocol not in (None, *ACCEPTED_RUNNER_PROTOCOLS):
        blockers.append({
            "code": "RUNNER_PROTOCOL_MISMATCH",
            "path": "runner_protocol",
            "message": (
                f"job requires {requested_protocol}; runner accepts "
                f"{', '.join(ACCEPTED_RUNNER_PROTOCOLS)}"
            ),
        })
    blockers.extend(validate_workflow(job))

    declared = job.get("blocked_before_start", [])
    if declared is not None and not isinstance(declared, list):
        blockers.append({"code": "INVALID_BLOCKER_LIST", "path": "blocked_before_start", "message": "must be a list"})
    elif isinstance(declared, list):
        for index, message in enumerate(declared):
            blockers.append({"code": "DECLARED_BLOCKER", "path": f"blocked_before_start[{index}]", "message": str(message)})

    release = job.get("release_package") or job.get("deployment") or {}
    if not isinstance(release, dict):
        blockers.append({"code": "INVALID_RELEASE", "path": "release_package", "message": "must be an object"})
        release = {}
    package_source = str(release.get("source") or release.get("package_local_path") or "")
    package_hash = str(release.get("sha256") or release.get("package_sha256") or "")
    stage_root = str(release.get("isolated_run") or release.get("bench_stage_root") or "")

    if package_source and not package_source.endswith(".tar.gz"):
        blockers.append({"code": "PACKAGE_FORMAT", "path": "release_package.source", "message": "release package must end with .tar.gz"})
    if package_hash and not SHA256_PATTERN.fullmatch(package_hash):
        blockers.append({"code": "PACKAGE_HASH", "path": "release_package.sha256", "message": "expected a 64-character SHA-256"})
    if stage_root and not (stage_root == "/app/livc-test" or stage_root.startswith("/app/livc-test/")):
        blockers.append({"code": "UNSAFE_STAGE_ROOT", "path": "release_package.isolated_run", "message": "must stay below /app/livc-test"})
    if stage_root in ("/app", "/app/livc"):
        blockers.append({"code": "ACTIVE_PATH_TARGET", "path": "release_package.isolated_run", "message": "active application paths are forbidden staging targets"})

    bench = job.get("bench_observation", {})
    if isinstance(bench, dict) and bench.get("clock_skew_policy") == "warn":
        warnings.append({"code": "CLOCK_SKEW_ACCEPTED", "path": "bench_observation.clock_skew_policy", "message": "record both controller and bench timestamps"})
    if isinstance(release, dict) and release.get("activated") is True:
        warnings.append({"code": "ALREADY_ACTIVATED", "path": "release_package.activated", "message": "verify the active version before another change"})

    structural_blocker_codes = {
        "UNRESOLVED_TODO", "EMBEDDED_SECRET", "JOB_EMBEDDED_COMMAND", "INVALID_BLOCKER_LIST", "INVALID_RELEASE",
        "MISSING_REQUIRED", "JOB_SCHEMA_VERSION", "INVALID_RUN_ID", "PACKAGE_FORMAT", "PACKAGE_HASH",
        "UNSAFE_STAGE_ROOT", "ACTIVE_PATH_TARGET", "RUNNER_PROTOCOL_MISMATCH",
        "INVALID_WORKFLOW", "WORKFLOW_PROTOCOL_REQUIRED", "MISSING_WORKFLOW_TARGETS",
        "INVALID_WORKFLOW_TARGET", "DUPLICATE_WORKFLOW_TARGET", "INVALID_SO_POLICY",
        "INVALID_REUSE_POLICY", "INVALID_WORKFLOW_SOURCES", "AMBIGUOUS_PREDICTION_SOURCE",
        "TARGET_ALREADY_SUPPLIED",
    }
    structural_blockers = [item for item in blockers if item["code"] in structural_blocker_codes]
    return {
        "schema_version": 1,
        "checked_at": utc_now(),
        "job_file": str(source) if source else None,
        "run_id": job.get("run_id"),
        "status": "READY" if not blockers else "BLOCKED",
        "stage_ready": not structural_blockers,
        "replay_ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "approval_required_for": required_approvals(job),
    }


def build_plan(job: dict[str, Any], source: Path) -> dict[str, Any]:
    preflight = evaluate_job(job, source)
    protocol = str(job.get("runner_protocol") or LEGACY_PROTOCOL)
    metadata = plan_metadata(job)
    formal_update = metadata.get("bench_strategy") == "formal_update"
    return {
        "schema_version": 1,
        "created_at": utc_now(),
        "job_file": str(source),
        "run_id": job.get("run_id"),
        "runner_protocol": protocol,
        "preflight": preflight,
        "workflow": metadata,
        "required_approvals": list(preflight["approval_required_for"]),
        "steps": build_target_steps(
            job,
            stage_ready=preflight["stage_ready"],
            replay_ready=preflight["replay_ready"],
        ),
        "safety": {
            "active_path_write_allowed": formal_update,
            "automatic_retry_of_mutations": False,
            "credentials_embedded": False,
            "activation_approval_includes_mandatory_restore": not formal_update,
            "success_requires_restore_verification_after_activation": not formal_update,
            "success_requires_formal_activation_verification": formal_update,
            "failure_requires_restore_verification_after_activation": True,
            "default_failure_action": "restore_once_and_preserve_evidence",
        },
    }


def audit_workflow(job: dict[str, Any] | None = None, source: Path | None = None) -> dict[str, Any]:
    """Return an honest, machine-readable implementation and readiness audit."""
    stages = []
    missing_adapters: list[dict[str, str]] = []
    for order, (state, description, approval) in enumerate(WORKFLOW, start=1):
        coverage = AUTOMATION_COVERAGE[state]
        stage = {
            "order": order,
            "state": state,
            "description": description,
            "implementation_status": coverage["status"],
            "entrypoint": coverage["entrypoint"],
            "explicit_approval_required": approval,
            "missing": list(coverage["missing"]),
        }
        stages.append(stage)
        for item in coverage["missing"]:
            missing_adapters.append({"stage": state, "item": item})

    preflight = evaluate_job(job, source) if job is not None else None
    fully_implemented = all(item["implementation_status"] == "IMPLEMENTED" for item in stages)
    live_ready = bool(fully_implemented and preflight and preflight["status"] == "READY")
    return {
        "schema_version": 1,
        "runner_protocol": RUNNER_PROTOCOL_VERSION,
        "checked_at": utc_now(),
        "job_file": str(source) if source else None,
        "scope": {
            "package_acquisition": "ORCHESTRATED_WITH_EXTERNAL_AUTH",
            "evaluation_library_compilation": "IMPLEMENTED_WORKSTATION_LIDOCKER",
            "workflow_starts_when": "a Jenkins build link or local APP path and approved library sources are available",
        },
        "offline_development_ready": True,
        "coordination": {
            "persistent_fifo_queue": True,
            "background_worker": True,
            "profile_transaction_executor": True,
            "full_evaluation_executor": True,
            "live_device_command_set_approved": False,
            "available_executors": list(SUPPORTED_EXECUTORS),
        },
        "live_end_to_end_automated": fully_implemented,
        "live_job_ready": live_ready,
        "stages": stages,
        "missing_adapters": missing_adapters,
        "job_preflight": preflight,
        "next_offline_work": [
            "fill and review each user's LPAI run.sh command set and authentication alias",
            "fill and review the workstation device command set without placing commands in jobs",
        ],
        "requires_live_environment": [
            "discover and approve exact App stop/start/health commands",
            "verify output paths and required prediction file prefixes on a fresh replay",
            "verify development-space destination and run.sh arguments with fresh predictions",
        ],
    }


def write_or_print(value: Any, output: Path | None) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output:
        output = output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


def read_status(runs_dir: Path, run_id: str, locks_dir: Path | None = None) -> dict[str, Any]:
    run_dir = runs_dir.expanduser().resolve() / run_id
    if not run_dir.is_dir():
        raise CliError(f"run directory does not exist: {run_dir}")
    artifacts = sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*") if path.is_file())
    events_path = run_dir / "events.jsonl"
    last_event: Any = None
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    last_event = json.loads(line)
                except json.JSONDecodeError:
                    last_event = {"state": "INVALID_EVENT_LOG", "raw": line}
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "last_event": last_event,
        "artifacts": artifacts,
    }
    if (run_dir / "state.json").is_file():
        effective_locks = locks_dir or runs_dir.expanduser().resolve().parent / "locks"
        result["runner"] = runner_status(runs_dir, effective_locks, run_id)
    return result


def delegate(tool: str, arguments: list[str]) -> int:
    script = Path(__file__).resolve().parent / TOOL_COMMANDS[tool]
    completed = subprocess.run([sys.executable, str(script), *arguments])
    return completed.returncode


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="bench-eval",
        description="Auditable offline bench-evaluation control plane",
        epilog="Tool commands: " + ", ".join(sorted(TOOL_COMMANDS)),
    )
    subparsers = result.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version", help="Print the CLI version")
    subparsers.add_parser("capabilities", help="Print the stable runner protocol and supported operations")
    doctor = subparsers.add_parser("doctor", help="Run read-only PC/workstation/bench connectivity diagnostics")
    doctor.add_argument("--profile", type=Path, default=Path.home() / "bench-eval" / "config" / "device.json")
    doctor.add_argument("--controller-ip", help="Personal-computer IP; defaults to the SSH_CONNECTION client IP")
    doctor.add_argument("--snapshot", type=Path, help="Use a saved fact snapshot instead of live network probes")
    doctor.add_argument("--timeout", type=float, default=2.0, help="Read-only TCP and command timeout in seconds")
    doctor.add_argument("--output", type=Path)
    for name in ("plan", "preflight"):
        child = subparsers.add_parser(name, help=f"Create a {name} result without contacting remote hosts")
        child.add_argument("--job", required=True, type=Path)
        child.add_argument("--output", type=Path)
    audit = subparsers.add_parser("audit", help="Audit implementation coverage without contacting remote hosts")
    audit.add_argument("--job", type=Path, help="Optional job whose readiness should also be checked")
    audit.add_argument("--output", type=Path)
    subparsers.add_parser("self-test", help="Run the portable runtime's offline unit tests")
    status = subparsers.add_parser("status", help="Read local evidence for one run")
    status.add_argument("--run-id", required=True)
    status.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    status.add_argument("--locks-dir", type=Path, default=Path.home() / "bench-eval" / "locks")
    queue = subparsers.add_parser("queue", help="List the active run, ready queue and jobs waiting for input")
    queue.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    queue.add_argument("--locks-dir", type=Path, default=Path.home() / "bench-eval" / "locks")
    submit = subparsers.add_parser("submit", help="Submit a durable job to the shared workstation runner")
    submit.add_argument("--job", required=True, type=Path)
    submit.add_argument("--submitted-by", required=True)
    submit.add_argument("--client", default="personal-ai")
    submit.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    approve = subparsers.add_parser("approve", help="Record an explicit stage approval for a submitted run")
    approve.add_argument("--run-id", required=True)
    approve.add_argument("--stage", required=True, choices=("ACTIVATE", "REPLAY"))
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    claim = subparsers.add_parser("claim-bench", help="Atomically claim the one shared bench for a ready run")
    claim.add_argument("--run-id", required=True)
    claim.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    claim.add_argument("--locks-dir", type=Path, default=Path.home() / "bench-eval" / "locks")
    release = subparsers.add_parser("release-bench", help="Release the shared bench after no activation, verified restore, or verified formal activation")
    release.add_argument("--run-id", required=True)
    release.add_argument("--reason", required=True, choices=("NO_ACTIVATION", "RESTORE_VERIFIED"))
    release.add_argument("--evidence", type=Path)
    release.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    release.add_argument("--locks-dir", type=Path, default=Path.home() / "bench-eval" / "locks")
    cancel = subparsers.add_parser("cancel", help="Cancel a run before it claims the bench")
    cancel.add_argument("--run-id", required=True)
    cancel.add_argument("--cancelled-by", required=True)
    cancel.add_argument("--reason", required=True)
    cancel.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    cancel.add_argument("--locks-dir", type=Path, default=Path.home() / "bench-eval" / "locks")
    requeue = subparsers.add_parser("requeue", help="Requeue a coordination failure or validation-blocked run")
    requeue.add_argument("--run-id", required=True)
    requeue.add_argument("--requeued-by", required=True)
    requeue.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    requeue.add_argument("--locks-dir", type=Path, default=Path.home() / "bench-eval" / "locks")
    worker = subparsers.add_parser("worker", help="Manage the persistent workstation queue worker")
    worker_actions = worker.add_subparsers(dest="worker_action", required=True)
    for action in ("once", "start"):
        child = worker_actions.add_parser(action)
        child.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
        child.add_argument("--locks-dir", type=Path, default=Path.home() / "bench-eval" / "locks")
        child.add_argument("--executor", choices=SUPPORTED_EXECUTORS, default="coordination-smoke")
        child.add_argument("--profile", type=Path, help="Workstation-owned profile for profile-transaction or full-evaluation")
        if action == "start":
            child.add_argument("--poll-seconds", type=float, default=2.0)
            child.add_argument("--log", type=Path)
    for action in ("status", "stop"):
        child = worker_actions.add_parser(action)
        child.add_argument("--locks-dir", type=Path, default=Path.home() / "bench-eval" / "locks")
    worker_loop = subparsers.add_parser("_worker-loop", help=argparse.SUPPRESS)
    worker_loop.add_argument("--runs-dir", required=True, type=Path)
    worker_loop.add_argument("--locks-dir", required=True, type=Path)
    worker_loop.add_argument("--executor", required=True, choices=SUPPORTED_EXECUTORS)
    worker_loop.add_argument("--poll-seconds", required=True, type=float)
    worker_loop.add_argument("--token", required=True)
    worker_loop.add_argument("--profile", type=Path)
    simulate = subparsers.add_parser("simulate", help="Run the legacy end-to-end simulation")
    simulate.add_argument("--config", required=True, type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in TOOL_COMMANDS:
        return delegate(arguments[0], arguments[1:])

    args = parser().parse_args(arguments)
    if args.command == "version":
        print(VERSION)
        return 0
    if args.command == "capabilities":
        write_or_print(
            {
                "runner": "bench-eval",
                "runner_version": VERSION,
                "runner_protocol": RUNNER_PROTOCOL_VERSION,
                "accepted_runner_protocols": list(ACCEPTED_RUNNER_PROTOCOLS),
                "role": "shared_deterministic_executor_without_ai",
                "accepted_job_schema_versions": [1],
                "logical_multi_user": True,
                "exclusive_bench_lock": True,
                "persistent_fifo_queue": True,
                "background_worker": True,
                "worker_executors": list(SUPPORTED_EXECUTORS),
                "commands": [
                    "doctor", "preflight", "plan", "audit", "submit", "approve", "queue", "cancel",
                    "requeue", "claim-bench", "release-bench", "status", "worker", *sorted(TOOL_COMMANDS),
                ],
                "artifact_sources": ["workstation_path", "bench_existing", "windows_upload", "jfrog_https"],
                "artifact_pipeline": {
                    "workstation_direct_download": True,
                    "credentials": "external_netrc_only",
                    "download_requires_expected_sha256": True,
                    "required_libraries": ["libxcamera.so", "libxlidar.so", "liburoad_m100.so"],
                    "candidate_assembly_isolated_on_workstation": True,
                },
                "prediction_route": {
                    "authoritative": "bench_to_shared_workstation_product",
                    "optional_personal_copy": "shared_workstation_product_to_submitter_lpai",
                },
                "full_dataset_batch": {
                    "supported": True,
                    "execution_unit": "one_scene_one_transaction",
                    "resumable": True,
                    "unsafe_restore_failure_stops_queue": True,
                    "commands": ["batch plan", "batch submit", "batch approve", "batch status"],
                },
            },
            None,
        )
        return 0
    if args.command == "doctor":
        profile_path = args.profile.expanduser().resolve()
        profile = load_json(profile_path)
        if args.snapshot:
            snapshot = load_json(args.snapshot.expanduser().resolve())
        else:
            snapshot = collect_snapshot(profile, args.controller_ip or controller_ip_from_environment(), args.timeout)
        result = diagnose(snapshot, profile)
        write_or_print(result, args.output)
        return 2 if result["status"] == "BLOCKED" else 0
    if args.command == "simulate":
        script = Path(__file__).resolve().parent / "run.py"
        return subprocess.run([sys.executable, str(script), "--config", str(args.config), "--simulate"]).returncode
    if args.command == "status":
        write_or_print(read_status(args.runs_dir, args.run_id, args.locks_dir), None)
        return 0
    if args.command == "queue":
        write_or_print(queue_snapshot(args.runs_dir, args.locks_dir), None)
        return 0
    if args.command == "approve":
        write_or_print(approve_stage(args.runs_dir, args.run_id, args.stage, args.approved_by), None)
        return 0
    if args.command == "claim-bench":
        write_or_print(claim_bench(args.runs_dir, args.locks_dir, args.run_id), None)
        return 0
    if args.command == "release-bench":
        write_or_print(release_bench(args.runs_dir, args.locks_dir, args.run_id, args.reason, args.evidence), None)
        return 0
    if args.command == "cancel":
        write_or_print(cancel_run(args.runs_dir, args.locks_dir, args.run_id, args.cancelled_by, args.reason), None)
        return 0
    if args.command == "requeue":
        write_or_print(requeue_run(args.runs_dir, args.locks_dir, args.run_id, args.requeued_by), None)
        return 0
    if args.command == "worker":
        if args.worker_action == "once":
            write_or_print(process_next(args.runs_dir, args.locks_dir, args.executor, args.profile), None)
        elif args.worker_action == "start":
            write_or_print(
                start_background_worker(
                    script=Path(__file__),
                    runs_dir=args.runs_dir,
                    locks_dir=args.locks_dir,
                    executor=args.executor,
                    profile_path=args.profile,
                    poll_seconds=args.poll_seconds,
                    log_path=args.log,
                ),
                None,
            )
        elif args.worker_action == "status":
            write_or_print(worker_status(args.locks_dir), None)
        else:
            write_or_print(stop_background_worker(args.locks_dir), None)
        return 0
    if args.command == "_worker-loop":
        return run_worker_loop(
            runs_dir=args.runs_dir,
            locks_dir=args.locks_dir,
            executor=args.executor,
            profile_path=args.profile,
            poll_seconds=args.poll_seconds,
            token=args.token,
        )
    if args.command == "self-test":
        runtime_dir = Path(__file__).resolve().parent
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(runtime_dir), "-p", "test_*.py"],
            cwd=runtime_dir,
        )
        return completed.returncode
    if args.command == "audit":
        if args.job:
            job_path = args.job.expanduser().resolve()
            result = audit_workflow(load_json(job_path), job_path)
        else:
            result = audit_workflow()
        write_or_print(result, args.output)
        return 0

    if args.command == "submit":
        job_path = args.job.expanduser().resolve()
        job = load_json(job_path)
        secret_fields = embedded_secret_fields(job)
        if secret_fields:
            raise CliError(f"refusing to persist a job containing credential fields: {secret_fields}")
        requested_protocol = job.get("runner_protocol")
        if requested_protocol not in (None, *ACCEPTED_RUNNER_PROTOCOLS):
            raise CliError(
                f"runner protocol mismatch: job requires {requested_protocol}; "
                f"runner accepts {', '.join(ACCEPTED_RUNNER_PROTOCOLS)}"
            )
        write_or_print(
            submit_job(
                job=job,
                source=job_path,
                plan=build_plan(job, job_path),
                runs_dir=args.runs_dir,
                submitted_by=args.submitted_by,
                client=args.client,
            ),
            None,
        )
        return 0

    job_path = args.job.expanduser().resolve()
    job = load_json(job_path)
    result = evaluate_job(job, job_path) if args.command == "preflight" else build_plan(job, job_path)
    write_or_print(result, args.output)
    if args.command == "preflight" and result["status"] != "READY":
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CliError, RunnerError) as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
