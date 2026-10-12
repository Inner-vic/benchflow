#!/usr/bin/env python3
"""Pin a reviewed multi-scene batch into one parent Runner Job.

The parent Job uses a dedicated isolated_candidate Profile command set whose
precheck/replay commands call batch_lease.py. No bench mutation occurs here.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path, PurePosixPath

from batch_lease import BatchLeaseError, common_identity, read_object, sha256, validate_gate, write_once
from bench_eval import evaluate_job
from bench_transaction import selected_command_set


def prepare(
    *, batch_dir: Path, gate_run: Path, parent_template: Path, profile_path: Path,
    output: Path, gate_replay_seconds: float, adapter_mode: str = "scene",
    required_output_types: tuple[str, ...] = (),
    gate_recovery_evidence_dir: Path | None = None,
) -> dict:
    if not math.isfinite(gate_replay_seconds) or gate_replay_seconds <= 0:
        raise BatchLeaseError("measured Gate replay seconds must be positive")
    if adapter_mode not in {"sweep", "scene"}:
        raise BatchLeaseError("adapter mode must be sweep or scene")
    allowed_types = {"pred_traj", "traj_xy", "pred_img", "pred_road"}
    if (not required_output_types or len(set(required_output_types)) != len(required_output_types)
            or not set(required_output_types) <= allowed_types):
        raise BatchLeaseError("required output types must come from the reviewed YAML and task goal")
    batch_dir = batch_dir.expanduser().resolve()
    manifest_path = batch_dir / "batch.json"
    manifest = read_object(manifest_path)
    entries = manifest.get("scenes")
    if not isinstance(entries, list) or not entries:
        raise BatchLeaseError("batch has no scenes")
    template = read_object(parent_template.expanduser().resolve())
    parent_id = template.get("run_id")
    if not isinstance(parent_id, str) or not parent_id or parent_id in {e.get("run_id") for e in entries}:
        raise BatchLeaseError("parent run ID is empty or reused by a child")
    if (template.get("workflow") or {}).get("bench_strategy") != "isolated_candidate":
        raise BatchLeaseError("parent Job must use isolated_candidate for verified final restore")
    identity: str | None = None
    child_hashes: dict[str, str] = {}
    parent_output = PurePosixPath(str((template.get("predictions") or {}).get("source_path") or ""))
    if not parent_output.is_absolute():
        raise BatchLeaseError("parent prediction source must be an absolute batch root")
    prediction_roots: list[PurePosixPath] = []
    for position, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or entry.get("scene_number") != position:
            raise BatchLeaseError("batch scene order is invalid")
        run_id = entry.get("run_id")
        relative = Path(str(entry.get("job_file") or ""))
        if not isinstance(run_id, str) or not run_id or relative.is_absolute() or ".." in relative.parts:
            raise BatchLeaseError("invalid child run ID or Job path")
        child_path = (batch_dir / relative).resolve()
        if not child_path.is_relative_to(batch_dir) or not child_path.is_file():
            raise BatchLeaseError("child Job is absent or outside batch directory")
        child = read_object(child_path)
        if child.get("run_id") != run_id or (child.get("replay_input") or {}).get("scene") != entry.get("scene"):
            raise BatchLeaseError("child Job scene identity differs from manifest")
        source = PurePosixPath(str((child.get("predictions") or {}).get("source_path") or ""))
        if (not source.is_absolute() or source != parent_output / str(entry.get("scene") or "")
                or any(source.is_relative_to(other) or other.is_relative_to(source) for other in prediction_roots)):
            raise BatchLeaseError("child prediction root must equal batch root / scene")
        prediction_roots.append(source)
        child_identity = common_identity(child)
        if identity is None:
            identity = child_identity
        elif identity != child_identity:
            raise BatchLeaseError("batch child inputs differ")
        child_hashes[run_id] = sha256(child_path)
    assert identity is not None
    if common_identity(template) != identity:
        raise BatchLeaseError("parent template inputs differ from the child Jobs")
    gate_run = gate_run.expanduser().resolve()
    if gate_recovery_evidence_dir is not None:
        gate_recovery_evidence_dir = gate_recovery_evidence_dir.expanduser().resolve()
    validate_gate(gate_run, identity, gate_recovery_evidence_dir)
    if gate_run.name in child_hashes:
        raise BatchLeaseError("Gate scene is already complete; exclude it from held batch replay")
    _execution, command_set = selected_command_set(template, profile_path)
    replay = command_set["commands"]["replay"]
    runner_limit = float(replay.get("timeout_seconds", 60))
    # One command carries all scenes; leave time for the outer Runner restore.
    estimated = 60 + len(entries) * gate_replay_seconds * 1.35
    if estimated > min(runner_limit - 60, 3540):
        raise BatchLeaseError(
            f"estimated replay {estimated:.0f}s exceeds safe single-command limit; split the batch into smaller held chunks"
        )
    template["batch_lease"] = {
        "batch_id": manifest["batch_id"],
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256(manifest_path),
        "child_job_sha256": child_hashes,
        "common_identity_sha256": identity,
        "gate": {"run_dir": str(gate_run), **({"recovery_evidence_dir": str(gate_recovery_evidence_dir)}
                                           if gate_recovery_evidence_dir else {})},
        "max_replay_seconds": min(int(runner_limit - 45), 3540),
        "adapter_mode": adapter_mode,
        "required_output_types": list(required_output_types),
    }
    preflight = evaluate_job(template, parent_template)
    if preflight["status"] != "READY":
        raise BatchLeaseError(f"parent Job preflight is blocked: {preflight['blockers']}")
    write_once(output.expanduser().resolve(), template)
    return {
        "state": "PARENT_JOB_READY", "run_id": parent_id,
        "batch_id": manifest["batch_id"], "scene_count": len(entries),
        "estimated_replay_seconds": round(estimated), "runner_replay_timeout_seconds": runner_limit,
        "parent_job": str(output.expanduser().resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", required=True, type=Path)
    parser.add_argument("--gate-run", required=True, type=Path)
    parser.add_argument("--gate-recovery-evidence-dir", type=Path)
    parser.add_argument("--parent-template", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--gate-replay-seconds", required=True, type=float)
    parser.add_argument("--adapter-mode", choices=("sweep", "scene"), default="scene")
    parser.add_argument("--required-output-type", action="append", default=[])
    args = parser.parse_args()
    try:
        result = prepare(batch_dir=args.batch_dir, gate_run=args.gate_run,
                         parent_template=args.parent_template, profile_path=args.profile,
                         output=args.output, gate_replay_seconds=args.gate_replay_seconds,
                         adapter_mode=args.adapter_mode,
                         required_output_types=tuple(args.required_output_type),
                         gate_recovery_evidence_dir=args.gate_recovery_evidence_dir)
    except (BatchLeaseError, OSError) as exc:
        print(json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
