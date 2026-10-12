#!/usr/bin/env python3
"""Generic protocol 1.1 control-plane entry around the reviewed Runner.

The Job and Profile identify the package, libraries, data, scenes, bench
strategy and requested products. This script does not contain candidate or
dataset constants and never issues bench commands directly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

RUN_ROOT = Path("/home/bench-user/bench-eval")
CLI = RUN_ROOT / "bin/bench-eval"
MIN_RUNNER_VERSION = (1, 8, 2)
WORK_ROOT = RUN_ROOT / "smoke/bench-eval-execution"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TARGETS = {"workstation_predictions", "lpai_predictions", "evaluation_report"}


class WorkflowError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_once(path: Path, value: dict) -> None:
    if path.exists():
        raise WorkflowError(f"immutable evidence already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def runner(*arguments: str, timeout: int = 120) -> dict:
    completed = subprocess.run([str(CLI), *arguments], stdin=subprocess.DEVNULL,
                               text=True, capture_output=True, check=False, timeout=timeout)
    if completed.returncode:
        raise WorkflowError(f"Runner {' '.join(arguments[:2])} failed: "
                            f"{(completed.stderr or completed.stdout)[-1600:]}")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise WorkflowError("Runner response is not a JSON object")
    return value


def validate_access(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    if (report.get("contract") != "bench-workstation-access/v1"
            or report.get("status") != "READY"
            or report.get("safe_to_submit") is not True
            or report.get("mutations_performed") is not False):
        raise WorkflowError("fixed workstation access is not READY")
    raw = str(report.get("checked_at", "")).replace("Z", "+00:00")
    raw = re.sub(r"(\.\d{6})\d+(?=[+-]\d{2}:\d{2}$)", r"\1", raw)
    checked = datetime.fromisoformat(raw)
    if checked.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - checked).total_seconds() <= 1800:
        raise WorkflowError("fixed workstation access report is stale or lacks a time zone")
    return report


def job_identity(job_path: Path, profile_path: Path) -> tuple[str, dict]:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    if not isinstance(job, dict):
        raise WorkflowError("Job must be an object")
    run_id = job.get("run_id")
    if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
        raise WorkflowError("Job has no valid immutable run_id")
    if job.get("runner_protocol") != "1.1" or job.get("mode") != "target_workflow":
        raise WorkflowError("generic workflow requires protocol 1.1 target_workflow")
    workflow = job.get("workflow") or {}
    targets = workflow.get("targets")
    if not isinstance(targets, list) or not targets or len(targets) != len(set(targets)) or not set(targets) <= TARGETS:
        raise WorkflowError("Job has invalid requested targets")
    if not profile_path.is_file():
        raise WorkflowError("reviewed workstation Profile is missing")
    if any(key in job for key in ("command", "argv", "shell", "script")):
        raise WorkflowError("Job must select reviewed Profile commands, not embed executable commands")
    return run_id, job


def prepare(job_path: Path, profile_path: Path, access_path: Path) -> dict:
    access = validate_access(access_path)
    run_id, job = job_identity(job_path, profile_path)
    if (RUN_ROOT / "runs" / run_id).exists() or (WORK_ROOT / run_id).exists():
        raise WorkflowError("run ID already has evidence; choose a new immutable ID")
    capabilities = runner("capabilities")
    version = capabilities.get("runner_version", "")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(version))
    parsed = tuple(map(int, match.groups())) if match else ()
    if (not parsed or parsed < MIN_RUNNER_VERSION or parsed[0] != MIN_RUNNER_VERSION[0]
            or capabilities.get("runner_protocol") != "1.1"):
        raise WorkflowError("Runner version or protocol differs from this Skill contract")
    executors = capabilities.get("coordination", {}).get("available_executors")
    if executors is not None and "target-workflow" not in executors:
        raise WorkflowError("Runner lacks the target-workflow executor")
    preflight = runner("preflight", "--job", str(job_path))
    if preflight.get("status") != "READY" or not preflight.get("replay_ready"):
        raise WorkflowError(f"Runner preflight blocked: {preflight.get('blockers')}")
    plan = runner("plan", "--job", str(job_path))
    if plan.get("preflight", {}).get("status") != "READY":
        raise WorkflowError("Runner plan does not match READY preflight")
    sys.path.insert(0, str(RUN_ROOT / "current/automation"))
    from target_workflow import validate_target_job
    resolved = validate_target_job(job, profile_path, RUN_ROOT / "runs" / run_id)
    requires_bench = bool(resolved["requires_bench"])
    if (plan.get("workflow") or {}).get("requires_bench") is not requires_bench:
        raise WorkflowError("Runner plan and reviewed Profile disagree on bench dependency")
    approvals = preflight.get("approval_required_for")
    if approvals != (["ACTIVATE", "REPLAY"] if requires_bench else []):
        raise WorkflowError("Runner approvals disagree with target dependency plan")
    receipt = {
        "state": "READY_TO_SUBMIT", "run_id": run_id,
        "job_path": str(job_path.resolve()), "job_sha256": sha256(job_path),
        "profile_path": str(profile_path.resolve()), "profile_sha256": sha256(profile_path),
        "access_report": str(access_path.resolve()), "access_sha256": sha256(access_path),
        "targets": sorted(resolved["targets"]), "sources": resolved["sources"],
        "requires_bench": requires_bench, "requires_personal_lpai": bool(set(resolved["targets"]) & {"lpai_predictions", "evaluation_report"}),
        "approval_required_for": approvals, "runner_version": version,
        "plan_sha256": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
        "access_status": access["status"],
    }
    write_once(WORK_ROOT / run_id / "prepare.json", receipt)
    return receipt


def prepared(run_id: str) -> dict:
    if not SAFE_ID.fullmatch(run_id):
        raise WorkflowError("invalid run ID")
    receipt = json.loads((WORK_ROOT / run_id / "prepare.json").read_text(encoding="utf-8"))
    if receipt.get("state") != "READY_TO_SUBMIT" or receipt.get("run_id") != run_id:
        raise WorkflowError("prepared receipt is invalid")
    if receipt.get("runner_version") != EXPECTED_RUNNER_VERSION:
        raise WorkflowError("Runner version changed after prepare")
    for key in ("job", "profile", "access"):
        field = "access_report" if key == "access" else key + "_path"
        if sha256(Path(receipt[field])) != receipt[key + "_sha256"]:
            raise WorkflowError(f"{key} changed after prepare")
    validate_access(Path(receipt["access_report"]))
    job_identity(Path(receipt["job_path"]), Path(receipt["profile_path"]))
    return receipt


def execute(run_id: str, submitted_by: str, client: str) -> dict:
    receipt = prepared(run_id)
    if not SAFE_ID.fullmatch(submitted_by):
        raise WorkflowError("invalid submitted_by")
    if receipt["requires_personal_lpai"] and not os.environ.get("SSH_AUTH_SOCK"):
        raise WorkflowError("personal LPAI target requires a foreground forwarded SSH agent")
    queue = runner("queue")
    if queue.get("active") or queue.get("queued") or queue.get("waiting"):
        raise WorkflowError("shared Runner queue is not empty; no Job was submitted")
    marker = WORK_ROOT / run_id / "execution-started.json"
    write_once(marker, {"run_id": run_id, "submitted_by": submitted_by, "client": client})
    runner("submit", "--job", receipt["job_path"], "--submitted-by", submitted_by, "--client", client)
    for stage in receipt["approval_required_for"]:
        runner("approve", "--run-id", run_id, "--stage", stage, "--approved-by", submitted_by)
    result = runner("worker", "once", "--executor", "target-workflow", "--profile",
                    receipt["profile_path"], timeout=None)
    if result.get("run_id") != run_id:
        raise WorkflowError(f"worker processed a different run: {result.get('run_id')}")
    status = runner("status", "--run-id", run_id)
    state = ((status.get("runner") or {}).get("run") or {}).get("state")
    if result.get("status") != "PROCESSED" or state != "COMPLETED_TO_REQUESTED_GOAL":
        raise WorkflowError(f"requested goal incomplete; inspect Runner status for {run_id}: {state}")
    return {"worker": result, "status": status}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "execute", "status"))
    parser.add_argument("--job", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--access-report", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--submitted-by", default="")
    parser.add_argument("--client", default="codex-windows")
    args = parser.parse_args()
    try:
        if args.phase == "prepare":
            if not all((args.job, args.profile, args.access_report)):
                raise WorkflowError("prepare requires --job, --profile and --access-report")
            result = prepare(args.job, args.profile, args.access_report)
        elif args.phase == "execute":
            result = execute(args.run_id or "", args.submitted_by, args.client)
        else:
            result = runner("status", "--run-id", args.run_id or "")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"state": "BLOCKED", "phase": args.phase, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
