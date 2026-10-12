#!/usr/bin/env python3
"""Resume only a verified workstation collection after a known inventory-order error.

The installed Runner's failed receipt and .partial remain unchanged. The
specified compatible collection module checks every source and partial file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


KNOWN_FAILURE = "hop-1 SHA-256 inventory mismatch; partial cache preserved"


class RecoveryError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RecoveryError(f"missing evidence: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RecoveryError(f"invalid evidence object: {path}")
    return value


def verify_conditions(run_dir: Path, runner_root: Path) -> None:
    restore = read_json(run_dir / "restore-verification.json")
    if restore.get("restore_ok") is not True:
        raise RecoveryError("formal bench restoration is not verified")
    if (runner_root / "locks/bench.lock").exists():
        raise RecoveryError("bench lock still exists")
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        raise RecoveryError("Runner events are missing")
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    if not events or events[-1].get("event") != "WORKSTATION_COLLECTION_FAILED" or \
       events[-1].get("message") != KNOWN_FAILURE:
        raise RecoveryError("last Runner event is not the reviewed inventory-order failure")
    if not any(item.get("event") == "BENCH_RELEASED" and
               item.get("reason") == "RESTORE_VERIFIED" for item in events):
        raise RecoveryError("Runner has no verified bench release event")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--runner-root", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--module-dir", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    runner_root = args.runner_root.expanduser().resolve()
    evidence = args.evidence_dir.expanduser().resolve()
    try:
        evidence.relative_to(run_dir)
        evidence_inside_run = evidence != run_dir
    except ValueError:
        evidence_inside_run = False
    if not run_dir.is_dir() or not evidence_inside_run or evidence.exists():
        parser.error("evidence must be a new directory inside the existing run")
    if not args.module_dir.is_dir() or not (args.module_dir / "prediction_transfer.py").is_file():
        parser.error("compatible collection module is missing")
    verify_conditions(run_dir, runner_root)
    job = read_json(run_dir / "job.json")
    profile = read_json(args.profile)
    if job.get("run_id") != run_dir.name:
        raise RecoveryError("Job run ID differs from evidence directory")
    sys.path.insert(0, str(args.module_dir.resolve()))
    from prediction_transfer import (  # noqa: E402
        collect_predictions_to_workstation,
        resolve_collection,
    )

    collection_plan = resolve_collection(job, profile, evidence)
    if collection_plan["cache"].exists():
        raise RecoveryError("workstation product already exists; verify its producer receipt instead")
    try:
        result = collect_predictions_to_workstation(job, profile, evidence, execute=True)
    except Exception as exc:
        raise RecoveryError(f"collection module blocked recovery: {exc}") from exc
    if result.get("state") != "WORKSTATION_PREDICTIONS_REUSED":
        raise RecoveryError(f"unexpected collection state: {result.get('state')}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RecoveryError as exc:
        print(json.dumps({"state": "BLOCKED_COLLECTION_RECOVERY", "error": str(exc)},
                         ensure_ascii=False))
        raise SystemExit(2)
