#!/usr/bin/env python3
"""Profile-only LPAI evaluation command adapter and report collector."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from parse_eval_report import ReportError, compare_metrics, parse_report, validate_result
from ssh_transport import command as ssh_transport_command
from ssh_transport import environment as ssh_environment
from ssh_transport import normalize_auth_mode
from workflow_receipts import (
    allocate_attempt,
    content_identity,
    sha256_file,
    write_receipt,
)


SAFE_ALIAS = re.compile(r"^[A-Za-z0-9._-]+$")
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
ALLOWED_PLACEHOLDERS = {"run_id", "run_dir", "scene", "prediction_path", "config_path", "local_report_path"}
SENSITIVE_KEYS = ("password", "passwd", "token", "secret", "private_key")
REQUIRED_COMMANDS = ("evaluate", "collect_report")
OPTIONAL_COMMANDS = ("prepare_config", "precheck")


class EvaluationError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"JSON must contain an object: {path}")
    return value


def _walk(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, child
            yield from _walk(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{prefix}[{index}]")


def validate_command(name: str, command: Any) -> None:
    if not isinstance(command, dict) or command.get("transport") not in ("local", "ssh"):
        raise EvaluationError(f"evaluation command {name} requires local or ssh transport")
    argv = command.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
        raise EvaluationError(f"evaluation command {name}.argv must be a non-empty string array")
    if command["transport"] == "ssh" and not SAFE_ALIAS.fullmatch(str(command.get("host_alias") or "")):
        raise EvaluationError(f"evaluation command {name} requires a safe SSH host_alias")
    timeout = command.get("timeout_seconds", 3600)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 86400:
        raise EvaluationError(f"evaluation command {name} timeout must be between 1 and 86400 seconds")
    for token in argv:
        placeholders = set(PLACEHOLDER.findall(token))
        remainder = PLACEHOLDER.sub("", token)
        if placeholders - ALLOWED_PLACEHOLDERS or "{" in remainder or "}" in remainder:
            raise EvaluationError(f"evaluation command {name} has unsupported placeholder in {token!r}")
    capture = command.get("capture_stdout_to")
    if capture is not None and (name != "collect_report" or capture != "{local_report_path}"):
        raise EvaluationError("only collect_report may capture stdout, and only to {local_report_path}")


def validate_remote_below(value: str, root: str, label: str) -> str:
    path = PurePosixPath(value)
    allowed = PurePosixPath(root)
    if not path.is_absolute() or ".." in path.parts or not allowed.is_absolute():
        raise EvaluationError(f"{label} must be an absolute normalized LPAI path")
    try:
        relative = path.relative_to(allowed)
    except ValueError as exc:
        raise EvaluationError(f"{label} must stay below {allowed}") from exc
    if not relative.parts:
        raise EvaluationError(f"{label} must not equal its allowed root")
    return str(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(pending, path)


def attempt_scoped_config(path: str, run_id: str, attempt: str) -> str:
    value = PurePosixPath(path)
    return str(value.parent / f"{run_id}-{attempt}" / value.name)


def evaluation_identity(job: dict[str, Any], resolved: dict[str, Any]) -> str:
    request = job["evaluation"]
    context = resolved["context"]
    return content_identity({
        "schema_version": 1,
        "evaluation_set_id": request["evaluation_set_id"],
        "scene": context["scene"],
        "prediction_path": context["prediction_path"],
        "config_path": resolved["job_config_path"],
        "selected_profile": resolved["selected"],
        "commands": resolved["commands"],
        "acceptance_policy": resolved["acceptance_policy"],
        "validity_policy": resolved["validity_policy"],
    })


def validate_attempt(
    attempt_dir: Path,
    identity: str,
    validity_policy: dict[str, Any] | None,
) -> dict[str, Any] | None:
    manifest_path = attempt_dir / "attempt.json"
    report_path = attempt_dir / "evaluation-report.txt"
    summary_path = attempt_dir / "evaluation-summary.json"
    if not manifest_path.is_file() or not report_path.is_file() or not summary_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        parsed = parse_report(report_path)
    except (OSError, json.JSONDecodeError, ReportError):
        return None
    if not isinstance(manifest, dict) or not isinstance(summary, dict):
        return None
    if manifest.get("state") != "SUCCEEDED" or manifest.get("evaluation_identity") != identity:
        return None
    if summary.get("evaluation_identity") != identity:
        return None
    validity = validate_result(parsed, validity_policy)
    if validity.get("decision") != "VALID":
        return None
    if parsed.get("evaluated_tasks") != summary.get("evaluated_tasks") or parsed.get("metrics") != summary.get("metrics"):
        return None
    return {
        **summary,
        "state": "EVALUATION_REUSED",
        "mutations_performed": False,
        "attempt": str(manifest.get("attempt") or ""),
        "attempt_dir": str(attempt_dir),
        "local_report_path": str(report_path),
        "summary_path": str(summary_path),
    }


def reusable_evaluation(
    run_dir: Path,
    identity: str,
    validity_policy: dict[str, Any] | None,
) -> dict[str, Any] | None:
    root = run_dir.expanduser().resolve() / "evaluation-attempts"
    candidates: list[tuple[str, Path]] = []
    if root.is_dir():
        for manifest_path in root.glob("*/a[0-9][0-9][0-9]/attempt.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            if manifest.get("state") != "SUCCEEDED" or manifest.get("evaluation_identity") != identity:
                continue
            candidates.append((str(manifest.get("attempt") or ""), manifest_path.parent))
    for _attempt, attempt_dir in sorted(candidates, reverse=True):
        reused = validate_attempt(attempt_dir, identity, validity_policy)
        if reused is not None:
            return reused
    return None


def discover_evaluation_attempts(
    run_dir: Path,
    *,
    identity: str | None = None,
    validity_policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    root = run_dir.expanduser().resolve() / "evaluation-attempts"
    results: list[dict[str, Any]] = []
    if not root.is_dir():
        return results
    for manifest_path in sorted(root.glob("*/a[0-9][0-9][0-9]/attempt.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            results.append({
                "state": "INVALID_CACHE",
                "attempt_dir": str(manifest_path.parent),
                "reason": str(exc),
            })
            continue
        candidate_identity = str(manifest.get("evaluation_identity") or "")
        reusable = None
        if identity is None or candidate_identity == identity:
            reusable = validate_attempt(manifest_path.parent, candidate_identity, validity_policy)
        results.append({
            **manifest,
            "attempt_dir": str(manifest_path.parent),
            "matches_identity": identity is None or candidate_identity == identity,
            "reusable": reusable is not None,
        })
    return results


def finalize_attempt(
    run_dir: Path,
    attempt_dir: Path,
    attempt_manifest: dict[str, Any],
    state: str,
    *,
    targets: tuple[str, ...],
    details: dict[str, Any],
    output_identity: str | None = None,
    verification: dict[str, Any] | None = None,
) -> None:
    attempt_manifest.update(state=state, finished_at=utc_now())
    write_json(attempt_dir / "attempt.json", attempt_manifest)
    receipt_state = "SUCCEEDED" if state == "SUCCEEDED" else "FAILED"
    write_receipt(
        run_dir,
        "EVALUATE",
        receipt_state,
        targets=targets,
        input_identity=attempt_manifest["evaluation_identity"],
        output_identity=output_identity,
        source=str(attempt_dir),
        verification=verification,
        attempt=attempt_manifest["attempt"],
        details=details,
    )


def resolve_evaluation(job: dict[str, Any], profile: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    request = job.get("evaluation")
    if not isinstance(request, dict):
        raise EvaluationError("job is missing evaluation")
    evaluation = profile.get("evaluation")
    sets = evaluation.get("sets") if isinstance(evaluation, dict) else None
    selected = sets.get(request.get("evaluation_set_id")) if isinstance(sets, dict) else None
    if not isinstance(selected, dict):
        raise EvaluationError(f"unknown evaluation.evaluation_set_id: {request.get('evaluation_set_id')}")
    for field, value in _walk(selected):
        key = field.rsplit(".", 1)[-1].lower()
        if any(marker in key for marker in SENSITIVE_KEYS) and value not in (None, "", False):
            raise EvaluationError(f"evaluation Profile must not contain credentials: {field}")
    commands = selected.get("commands")
    if not isinstance(commands, dict) or any(name not in commands for name in REQUIRED_COMMANDS):
        raise EvaluationError(f"evaluation set requires commands: {list(REQUIRED_COMMANDS)}")
    if set(commands) - set((*OPTIONAL_COMMANDS, *REQUIRED_COMMANDS)):
        raise EvaluationError("evaluation set contains unsupported commands")
    for name in commands:
        validate_command(name, commands[name])
    prediction_path = validate_remote_below(
        str(request.get("prediction_path") or ""), str(selected.get("allowed_prediction_root") or ""), "prediction_path"
    )
    config_path = validate_remote_below(
        str(request.get("config_path") or ""), str(selected.get("allowed_config_root") or ""), "config_path"
    )
    resolved_run = run_dir.expanduser().resolve()
    replay_input = job.get("replay_input")
    scene = replay_input.get("scene") if isinstance(replay_input, dict) else None
    if not isinstance(scene, str) or not scene:
        raise EvaluationError("evaluation requires replay_input.scene")
    return {
        "evaluation": evaluation,
        "selected": selected,
        "commands": commands,
        "job_config_path": config_path,
        "context": {
            "run_id": str(job.get("run_id")),
            "run_dir": str(resolved_run),
            "scene": scene,
            "prediction_path": prediction_path,
            "config_path": config_path,
            "local_report_path": str(resolved_run / "evaluation-report.txt"),
        },
        "acceptance_policy": request.get("acceptance_policy"),
        "validity_policy": request.get("validity_policy"),
    }


class EvaluationCommandRunner:
    def __init__(self, evaluation: dict[str, Any]):
        self.ssh_binary = str(evaluation.get("ssh_binary") or "ssh")
        args = evaluation.get("ssh_args", ["-o", "BatchMode=yes"])
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise EvaluationError("evaluation.ssh_args must be a string array")
        self.ssh_args = args
        try:
            self.ssh_auth_mode = normalize_auth_mode(evaluation.get("ssh_auth_mode"))
        except ValueError as exc:
            raise EvaluationError(str(exc)) from exc

    def run(self, name: str, command: dict[str, Any], context: dict[str, str]) -> dict[str, Any]:
        def render(token: str) -> str:
            return PLACEHOLDER.sub(lambda match: context[match.group(1)], token)

        argv = [render(item) for item in command["argv"]]
        if command["transport"] == "ssh":
            argv = [
                *ssh_transport_command(self.ssh_binary, self.ssh_args, self.ssh_auth_mode),
                command["host_alias"], "--", shlex.join(argv),
            ]
        started = time.monotonic()
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, text=True, capture_output=True, shell=False,
            timeout=float(command.get("timeout_seconds", 3600)),
            env=ssh_environment(self.ssh_auth_mode) if command["transport"] == "ssh" else None,
        )
        capture = command.get("capture_stdout_to")
        if completed.returncode == 0 and capture:
            destination = Path(render(str(capture))).expanduser().resolve()
            expected = Path(context["local_report_path"]).expanduser().resolve()
            if destination != expected:
                raise EvaluationError("report capture path must equal local_report_path")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(completed.stdout, encoding="utf-8")
        return {
            "step": name,
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": completed.stdout[-100_000:],
            "stderr": completed.stderr[-100_000:],
        }


def run_evaluation(
    job: dict[str, Any], profile: dict[str, Any], run_dir: Path, *, execute: bool,
    runner: Any | None = None, allow_reuse: bool = True,
) -> dict[str, Any]:
    resolved = resolve_evaluation(job, profile, run_dir)
    context = dict(resolved["context"])
    identity = evaluation_identity(job, resolved)
    plan = {
        "state": "EVALUATION_PLANNED",
        "run_id": context["run_id"],
        "evaluation_set_id": job["evaluation"]["evaluation_set_id"],
        "evaluation_identity": identity,
        "prediction_path": context["prediction_path"],
        "config_path": resolved["job_config_path"],
        "local_report_path": context["local_report_path"],
        "commands_from_profile_only": True,
        "mutations_performed": False,
    }
    if not execute:
        return plan
    destination = Path(context["run_dir"])
    destination.mkdir(parents=True, exist_ok=True)
    reused = reusable_evaluation(destination, identity, resolved["validity_policy"]) if allow_reuse else None
    if reused is not None:
        return {**plan, **reused}

    scene = context["scene"]
    attempt, attempt_dir = allocate_attempt(destination, "evaluation", scope=scene)
    context["config_path"] = attempt_scoped_config(
        resolved["job_config_path"], context["run_id"], attempt,
    )
    context["run_id"] = f"{context['run_id']}-{attempt}"
    context["local_report_path"] = str(attempt_dir / "evaluation-report.txt")
    attempt_manifest = {
        "schema_version": 1,
        "node": "EVALUATE",
        "scene": scene,
        "attempt": attempt,
        "state": "RUNNING",
        "evaluation_identity": identity,
        "started_at": utc_now(),
        "finished_at": None,
        "remote_run_id": context["run_id"],
        "remote_config_path": context["config_path"],
    }
    write_json(attempt_dir / "attempt.json", attempt_manifest)
    plan.update(
        attempt=attempt,
        attempt_dir=str(attempt_dir),
        remote_run_id=context["run_id"],
        config_path=context["config_path"],
        local_report_path=context["local_report_path"],
    )
    targets = tuple(
        str(item)
        for item in job.get("workflow", {}).get("targets", ["evaluation_report"])
    )
    engine = runner or EvaluationCommandRunner(resolved["evaluation"])
    execution_order = (
        *(("prepare_config",) if "prepare_config" in resolved["commands"] else ()),
        *(("precheck",) if "precheck" in resolved["commands"] else ()),
        *REQUIRED_COMMANDS,
    )
    for name in execution_order:
        try:
            result = engine.run(name, resolved["commands"][name], context)
        except Exception as exc:
            result = {"step": name, "ok": False, "exit_code": None, "error": str(exc)}
        write_json(attempt_dir / f"evaluation-{name}.json", result)
        if not result.get("ok"):
            details = {
                "failed_step": name,
                "local_report_path": context["local_report_path"],
                "validity_policy": resolved["validity_policy"],
            }
            finalize_attempt(
                destination, attempt_dir, attempt_manifest, "FAILED",
                targets=targets, details=details,
            )
            return {
                **plan,
                "state": "EVALUATION_FAILED",
                "failed_step": name,
                "mutations_performed": True,
            }
    try:
        parsed = parse_report(Path(context["local_report_path"]))
    except (ReportError, OSError) as exc:
        finalize_attempt(
            destination, attempt_dir, attempt_manifest, "FAILED",
            targets=targets,
            details={
                "failed_step": "parse_report",
                "message": str(exc),
                "local_report_path": context["local_report_path"],
                "validity_policy": resolved["validity_policy"],
            },
        )
        return {
            **plan,
            "state": "EVALUATION_FAILED",
            "failed_step": "parse_report",
            "message": str(exc),
            "mutations_performed": True,
        }
    validity = validate_result(parsed, resolved["validity_policy"])
    state = "EVALUATION_COMPLETED" if validity["decision"] == "VALID" else "EVALUATION_INVALID"
    summary = {
        **plan,
        "state": state,
        "mutations_performed": True,
        "evaluated_tasks": parsed["evaluated_tasks"],
        "metrics": parsed["metrics"],
        "validity": validity,
        "acceptance": (
            compare_metrics(parsed["metrics"], resolved["acceptance_policy"])
            if state == "EVALUATION_COMPLETED"
            else {"decision": "NOT_EVALUATED", "reason": "evaluation result failed validity gates", "checks": []}
        ),
    }
    summary_path = attempt_dir / "evaluation-summary.json"
    write_json(summary_path, summary)
    compatibility_summary = destination / "evaluation-summary.json"
    compatibility_report = destination / "evaluation-report.txt"
    write_json(compatibility_summary, summary)
    compatibility_report.write_bytes(Path(context["local_report_path"]).read_bytes())
    details = {
        "local_report_path": context["local_report_path"],
        "summary_path": str(summary_path),
        "validity_policy": resolved["validity_policy"],
        "evaluated_tasks": parsed["evaluated_tasks"],
        "metrics": parsed["metrics"],
        "validity": validity,
        "acceptance": summary["acceptance"],
    }
    if state == "EVALUATION_COMPLETED":
        output_identity = content_identity({
            "evaluated_tasks": parsed["evaluated_tasks"],
            "metrics": parsed["metrics"],
            "validity": validity,
            "acceptance": summary["acceptance"],
        })
        finalize_attempt(
            destination, attempt_dir, attempt_manifest, "SUCCEEDED",
            targets=targets,
            details=details,
            output_identity=output_identity,
            verification={
                "method": "lpai-run-sh-report-v1",
                "report_sha256": sha256_file(Path(context["local_report_path"])),
            },
        )
    else:
        finalize_attempt(
            destination, attempt_dir, attempt_manifest, "FAILED",
            targets=targets,
            details={**details, "reason": "validity gate rejected evaluation result"},
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an approved LPAI evaluation command set")
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    result = run_evaluation(load_json(args.job), load_json(args.profile), args.run_dir, execute=args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["state"] not in ("EVALUATION_FAILED", "EVALUATION_INVALID") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvaluationError, ReportError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "EVALUATION_BLOCKED", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
