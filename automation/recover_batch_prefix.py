#!/usr/bin/env python3
"""Collect completed scenes after a held batch restores and releases the bench.

This is a workstation-only continuation. It never submits a Runner transaction
or replays a scene. Each child keeps its own immutable prediction product.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from batch_lease import BatchLeaseError, read_object, sha256, validated_inputs
from prediction_transfer import collect_predictions_to_workstation, load_json


def recover(run_dir: Path, locks_dir: Path, adapter: Path, adapter_sha256: str,
            profile_path: Path) -> dict:
    run_dir = run_dir.expanduser().resolve()
    locks_dir = locks_dir.expanduser().resolve()
    if (locks_dir / "bench.lock").exists():
        raise BatchLeaseError("bench lock remains; recovery requires verified release")
    restore = read_object(run_dir / "restore-verification.json")
    if restore.get("run_id") != run_dir.name or restore.get("restore_ok") is not True:
        raise BatchLeaseError("parent formal App restore is not verified")
    manifest, children = validated_inputs(run_dir, locks_dir, adapter, adapter_sha256,
                                          require_lock=False)
    profile = load_json(profile_path.expanduser().resolve())
    recovered = []
    first_missing = False
    for entry, child_path, child in children:
        run_id = entry["run_id"]
        result_path = run_dir / "batch-scene-results" / f"{run_id}.json"
        if not result_path.is_file():
            first_missing = True
            continue
        receipt = read_object(result_path)
        if (receipt.get("run_id") != run_id or receipt.get("scene") != entry["scene"]
                or receipt.get("job_sha256") != sha256(child_path)):
            raise BatchLeaseError(f"scene receipt identity differs: {run_id}")
        if receipt.get("state") not in {"SUCCEEDED", "REPLAY_COMPLETED"}:
            first_missing = True
            continue
        if first_missing:
            raise BatchLeaseError("completed scenes must form a prefix of the reviewed batch")
        evidence = run_dir / "batch-recovery" / run_id
        product = collect_predictions_to_workstation(child, profile, evidence, execute=True)
        if product.get("state") not in {"WORKSTATION_PREDICTIONS_COLLECTED",
                                        "WORKSTATION_PREDICTIONS_REUSED"}:
            raise BatchLeaseError(f"scene collection did not produce a verified product: {run_id}")
        recovered.append({"run_id": run_id, "scene": entry["scene"],
                          "product": product["workstation_product"],
                          "inventory_sha256": product["inventory_sha256"]})
    if not recovered:
        raise BatchLeaseError("no completed scene is available for collection")
    return {"state": "COMPLETED_PREFIX_COLLECTED", "batch_id": manifest["batch_id"],
            "scene_count": len(recovered), "scenes": recovered}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--locks-dir", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--adapter-sha256", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = recover(args.run_dir, args.locks_dir, args.adapter,
                         args.adapter_sha256, args.profile)
    except (BatchLeaseError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
