#!/usr/bin/env python3
"""Profile-driven, fail-closed bench execution transaction.

Jobs select a reviewed command-set id.  They never carry shell text.  The
command set lives on the shared workstation and is the only authority for
commands that can change the bench.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from runner_state import RunnerError, append_event, atomic_write_json, release_bench, set_state, utc_now
from ssh_transport import command as ssh_transport_command
from ssh_transport import environment as ssh_environment
from ssh_transport import normalize_auth_mode


STRATEGY_STEPS = {
    "use_current_app": ("precheck", "health_current", "replay"),
    "isolated_candidate": (
        "precheck",
        "capture_baseline",
        "stop_active",
        "start_candidate",
        "health_candidate",
        "replay",
        "restore",
        "verify_restore",
    ),
}
ALLOWED_TRANSPORTS = ("local", "ssh")
ALLOWED_PLACEHOLDERS = ("run_id", "run_dir", "scene")
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
SENSITIVE_KEYS = ("password", "passwd", "token", "secret", "private_key")
MAX_CAPTURE_CHARS = 100_000


def _walk(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, child
            yield from _walk(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]"
            yield path, child
            yield from _walk(child, path)


def load_command_set(profile_path: Path, command_set_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = profile_path.expanduser().resolve()
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"cannot read execution profile {path}: {exc}") from exc
    if not isinstance(profile, dict):
        raise RunnerError("execution profile must contain a JSON object")
    for field, value in _walk(profile):
        key = field.rsplit(".", 1)[-1].lower()
        if any(marker in key for marker in SENSITIVE_KEYS) and value not in (None, "", False):
            raise RunnerError(f"execution profile must not contain credentials: {field}")

    execution = profile.get("execution")
    if not isinstance(execution, dict):
        raise RunnerError("execution profile is missing execution")
    command_sets = execution.get("command_sets")
    if not isinstance(command_sets, dict):
        raise RunnerError("execution.command_sets must be an object")
    command_set = command_sets.get(command_set_id)
    if not isinstance(command_set, dict):
        raise RunnerError(f"unknown execution.command_set_id: {command_set_id}")
    strategy = command_set.get("strategy")
    if strategy not in STRATEGY_STEPS:
        raise RunnerError(f"unsupported command-set strategy: {strategy}")
    commands = command_set.get("commands")
    if not isinstance(commands, dict):
        raise RunnerError("selected command set is missing commands")
    missing = [step for step in STRATEGY_STEPS[strategy] if step not in commands]
    if missing:
        raise RunnerError(f"selected command set is missing required commands: {missing}")
    extra = sorted(set(commands) - set(STRATEGY_STEPS[strategy]))
    if extra:
        raise RunnerError(f"selected command set has unsupported command names: {extra}")
    for step, spec in commands.items():
        validate_command_spec(step, spec)
    return execution, command_set


def validate_command_spec(step: str, spec: Any) -> None:
    if not isinstance(spec, dict):
        raise RunnerError(f"command {step} must be an object")
    transport = spec.get("transport")
    if transport not in ALLOWED_TRANSPORTS:
        raise RunnerError(f"command {step} transport must be local or ssh")
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
        raise RunnerError(f"command {step}.argv must be a non-empty string array")
    if transport == "ssh" and not isinstance(spec.get("host_alias"), str):
        raise RunnerError(f"command {step} requires host_alias")
    timeout = spec.get("timeout_seconds", 60)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
        raise RunnerError(f"command {step}.timeout_seconds must be between 1 and 3600")
    for token in argv:
        placeholders = PLACEHOLDER.findall(token)
        unknown = sorted(set(placeholders) - set(ALLOWED_PLACEHOLDERS))
        remainder = PLACEHOLDER.sub("", token)
        if unknown or "{" in remainder or "}" in remainder:
            raise RunnerError(f"command {step} uses unsupported placeholders: {unknown or [token]}")


def selected_command_set(job: dict[str, Any], profile_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    execution = job.get("execution")
    if not isinstance(execution, dict) or not isinstance(execution.get("command_set_id"), str):
        raise RunnerError("profile-transaction requires job.execution.command_set_id")
    return load_command_set(profile_path, execution["command_set_id"])


def _render(token: str, context: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in ALLOWED_PLACEHOLDERS:
            raise RunnerError(f"unsupported command placeholder: {name}")
        return context[name]

    return PLACEHOLDER.sub(replace, token)


class CommandRunner:
    def run(
        self,
        step: str,
        spec: dict[str, Any],
        execution: dict[str, Any],
        context: dict[str, str],
    ) -> dict[str, Any]:
        rendered = [_render(item, context) for item in spec["argv"]]
        if spec["transport"] == "local":
            command = rendered
        else:
            ssh_binary = str(execution.get("ssh_binary") or "ssh")
            ssh_args = execution.get("ssh_args", ["-o", "BatchMode=yes"])
            if not isinstance(ssh_args, list) or any(not isinstance(item, str) for item in ssh_args):
                raise RunnerError("execution.ssh_args must be a string array")
            try:
                auth_mode = normalize_auth_mode(execution.get("ssh_auth_mode"))
            except ValueError as exc:
                raise RunnerError(str(exc)) from exc
            command = [
                *ssh_transport_command(ssh_binary, ssh_args, auth_mode),
                spec["host_alias"], "--", shlex.join(rendered),
            ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=float(spec.get("timeout_seconds", 60)),
                shell=False,
                env=ssh_environment(auth_mode) if spec["transport"] == "ssh" else None,
            )
            return {
                "step": step,
                "ok": completed.returncode == 0,
                "exit_code": completed.returncode,
                "timed_out": False,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": completed.stdout[-MAX_CAPTURE_CHARS:],
                "stderr": completed.stderr[-MAX_CAPTURE_CHARS:],
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "step": step,
                "ok": False,
                "exit_code": None,
                "timed_out": True,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": str(exc.stdout or "")[-MAX_CAPTURE_CHARS:],
                "stderr": str(exc.stderr or "")[-MAX_CAPTURE_CHARS:],
            }


def _run_step(
    run_dir: Path,
    step: str,
    commands: dict[str, Any],
    execution: dict[str, Any],
    context: dict[str, str],
    command_runner: Any,
) -> dict[str, Any]:
    append_event(run_dir, {"event": "TRANSACTION_STEP_STARTED", "state": "RUNNING", "step": step})
    try:
        result = command_runner.run(step, commands[step], execution, context)
    except Exception as exc:
        result = {"step": step, "ok": False, "exit_code": None, "timed_out": False, "error": str(exc)}
    result = {"recorded_at": utc_now(), **result}
    atomic_write_json(run_dir / "logs" / f"{step}.json", result)
    append_event(
        run_dir,
        {
            "event": "TRANSACTION_STEP_FINISHED",
            "state": "RUNNING",
            "step": step,
            "ok": bool(result.get("ok")),
            "exit_code": result.get("exit_code"),
        },
    )
    return result


def _finish_failure(run_dir: Path, executor: str, failed_step: str, restored: bool) -> None:
    set_state(run_dir, "FAILED_EXECUTION", executor=executor, failed_step=failed_step, restore_verified=restored)
    append_event(
        run_dir,
        {"event": "TRANSACTION_FAILED", "state": "FAILED_EXECUTION", "failed_step": failed_step, "restore_verified": restored},
    )


def execute_profile_transaction(
    *,
    runs_dir: Path,
    locks_dir: Path,
    run_id: str,
    profile_path: Path,
    command_runner: Any | None = None,
) -> dict[str, Any]:
    run_dir = runs_dir.expanduser().resolve() / run_id
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
    execution, command_set = selected_command_set(job, profile_path)
    strategy = command_set["strategy"]
    commands = command_set["commands"]
    runner = command_runner or CommandRunner()
    replay_input = job.get("replay_input")
    scene = replay_input.get("scene") if isinstance(replay_input, dict) else None
    context = {"run_id": run_id, "run_dir": str(run_dir), "scene": str(scene or "")}
    executor = "profile-transaction"

    set_state(run_dir, "RUNNING", executor=executor, strategy=strategy)
    append_event(run_dir, {"event": "TRANSACTION_STARTED", "state": "RUNNING", "strategy": strategy})

    if strategy == "use_current_app":
        for step in STRATEGY_STEPS[strategy]:
            result = _run_step(run_dir, step, commands, execution, context, runner)
            if not result.get("ok"):
                release_bench(runs_dir, locks_dir, run_id, "NO_ACTIVATION", None)
                _finish_failure(run_dir, executor, step, False)
                return {"status": "FAILED", "run_id": run_id, "failed_step": step, "restore_required": False}
        release_bench(runs_dir, locks_dir, run_id, "NO_ACTIVATION", None)
        set_state(run_dir, "COMPLETED", executor=executor, strategy=strategy, restore_verified=False)
        append_event(run_dir, {"event": "TRANSACTION_COMPLETED", "state": "COMPLETED", "strategy": strategy})
        return {"status": "PROCESSED", "run_id": run_id, "state": "COMPLETED"}

    for step in ("precheck", "capture_baseline"):
        result = _run_step(run_dir, step, commands, execution, context, runner)
        if not result.get("ok"):
            release_bench(runs_dir, locks_dir, run_id, "NO_ACTIVATION", None)
            _finish_failure(run_dir, executor, step, False)
            return {"status": "FAILED", "run_id": run_id, "failed_step": step, "restore_required": False}

    # Mark before stop_active: a partially successful stop may already have
    # changed the bench even when the command reports failure.
    set_state(run_dir, "RUNNING", executor=executor, strategy=strategy, activation_started=True)
    append_event(run_dir, {"event": "ACTIVATION_STARTED", "state": "RUNNING"})
    failed_step: str | None = None
    for step in ("stop_active", "start_candidate", "health_candidate", "replay"):
        result = _run_step(run_dir, step, commands, execution, context, runner)
        if not result.get("ok"):
            failed_step = step
            break

    restore = _run_step(run_dir, "restore", commands, execution, context, runner)
    verify = _run_step(run_dir, "verify_restore", commands, execution, context, runner)
    restore_ok = bool(restore.get("ok") and verify.get("ok"))
    if not restore_ok:
        failure = "restore" if not restore.get("ok") else "verify_restore"
        set_state(
            run_dir,
            "RESTORE_FAILED_UNSAFE",
            executor=executor,
            failed_step=failed_step or failure,
            restore_failed_step=failure,
            bench_lock_retained=True,
        )
        append_event(
            run_dir,
            {"event": "RESTORE_FAILED_UNSAFE", "state": "RESTORE_FAILED_UNSAFE", "failed_step": failure, "bench_lock_retained": True},
        )
        return {"status": "UNSAFE", "run_id": run_id, "failed_step": failed_step or failure, "bench_lock_retained": True}

    evidence = run_dir / "restore-verification.json"
    atomic_write_json(
        evidence,
        {
            "schema_version": 1,
            "run_id": run_id,
            "verified_at": utc_now(),
            "restore_log": "logs/restore.json",
            "verify_log": "logs/verify_restore.json",
            "restore_ok": True,
        },
    )
    release_bench(runs_dir, locks_dir, run_id, "RESTORE_VERIFIED", evidence)
    if failed_step:
        _finish_failure(run_dir, executor, failed_step, True)
        return {"status": "FAILED", "run_id": run_id, "failed_step": failed_step, "restore_verified": True}

    set_state(run_dir, "COMPLETED", executor=executor, strategy=strategy, restore_verified=True)
    append_event(run_dir, {"event": "TRANSACTION_COMPLETED", "state": "COMPLETED", "strategy": strategy, "restore_verified": True})
    return {"status": "PROCESSED", "run_id": run_id, "state": "COMPLETED", "restore_verified": True}
