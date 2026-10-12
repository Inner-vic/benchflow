#!/usr/bin/env python3
"""Run a reviewed scene batch inside one existing Runner bench transaction.

Use this as the precheck and replay commands of a dedicated isolated_candidate
Profile command set. The unchanged Runner owns bench.lock, activates the bench
once, and always runs its restore/verify_restore steps after replay returns.
The scene adapter is a Profile-owned executable, never a command from a Job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


class BatchLeaseError(RuntimeError):
    pass


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
OUTPUT_TYPES = {"pred_traj", "traj_xy", "pred_img", "pred_road"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchLeaseError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BatchLeaseError(f"expected JSON object: {path}")
    return value


def write_once(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise BatchLeaseError(f"refusing to overwrite evidence: {path}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def common_identity(job: dict[str, Any]) -> str:
    """Fingerprint only inputs shared by every scene, excluding run/output IDs."""
    workflow = job.get("workflow") or {}
    identity = {
        "app_sha256": (job.get("app_artifact") or {}).get("sha256"),
        "lock_sha256": (job.get("lockfile_artifact") or {}).get("sha256"),
        "evaluation_library_sha256": ((job.get("artifacts") or {}).get("evaluation_library") or {}).get("sha256"),
        "configuration_sha256": (job.get("configuration_input") or {}).get("sha256"),
        "dataset_root": (job.get("replay_input") or {}).get("dataset_root"),
        "prepared_manifest": (job.get("replay_input") or {}).get("prepared_manifest"),
        "bench_strategy": workflow.get("bench_strategy"),
        "targets": sorted(workflow.get("targets") or []),
        "so_policy": workflow.get("so_policy"),
        "reviewed_offline_keys": sorted((job.get("configuration_policy") or {}).get("reviewed_offline_keys") or []),
    }
    if not all(identity[key] for key in (
        "app_sha256", "lock_sha256", "evaluation_library_sha256", "configuration_sha256",
        "dataset_root", "prepared_manifest", "bench_strategy", "targets",
    )):
        raise BatchLeaseError("child Job lacks common input identity")
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def require_owner_lock(run_dir: Path, locks_dir: Path) -> None:
    lock = read_object(locks_dir / "bench.lock")
    if lock.get("run_id") != run_dir.name:
        raise BatchLeaseError("the parent Runner run does not own bench.lock")
    state = read_object(run_dir / "state.json")
    if state.get("state") not in {"BENCH_CLAIMED", "RUNNING"}:
        raise BatchLeaseError("parent Runner run is not in a claimed or running state")


def validate_gate(gate_run: Path, identity: str,
                  recovery_evidence_dir: Path | None = None) -> None:
    gate_run = gate_run.expanduser().resolve()
    if not gate_run.is_dir() or not (gate_run / "job.json").is_file():
        raise BatchLeaseError("Gate run is absent")
    if common_identity(read_object(gate_run / "job.json")) != identity:
        raise BatchLeaseError("Gate inputs differ from this batch")
    gate_state = read_object(gate_run / "state.json")
    restore_receipt = gate_run / "restore-verification.json"
    if (not restore_receipt.is_file()
            or read_object(restore_receipt).get("restore_ok") is not True):
        raise BatchLeaseError("Gate restore verification is missing or failed")
    if gate_state.get("state") == "COMPLETED_TO_REQUESTED_GOAL":
        gate_report = gate_run / "receipts" / "report.json"
        gate_collection = gate_run / "receipts" / "collect-workstation.json"
        if not gate_report.is_file() or read_object(gate_report).get("state") != "SUCCEEDED":
            raise BatchLeaseError("Gate product report is missing or failed")
        if not gate_collection.is_file() or read_object(gate_collection).get("state") != "SUCCEEDED":
            raise BatchLeaseError("Gate workstation product collection is missing or failed")
        return
    if gate_state.get("state") != "WORKSTATION_COLLECTION_FAILED" or recovery_evidence_dir is None:
        raise BatchLeaseError("Gate did not complete or have a verified collection-only recovery")
    replay_log = read_object(gate_run / "logs" / "replay.json")
    if replay_log.get("ok") is not True:
        raise BatchLeaseError("Gate replay itself did not succeed")
    events = [json.loads(line) for line in (gate_run / "events.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip()]
    if (not events or events[-1].get("event") != "WORKSTATION_COLLECTION_FAILED"
            or events[-1].get("message") != "hop-1 SHA-256 inventory mismatch; partial cache preserved"
            or not any(item.get("event") == "BENCH_RELEASED"
                       and item.get("reason") == "RESTORE_VERIFIED" for item in events)):
        raise BatchLeaseError("Gate recovery is not the reviewed collection-only failure")
    evidence_dir = recovery_evidence_dir.expanduser().resolve()
    if evidence_dir == gate_run or not evidence_dir.is_relative_to(gate_run):
        raise BatchLeaseError("Gate recovery evidence must be inside the Gate run")
    recovery = read_object(evidence_dir / "workstation-predictions.json")
    if (recovery.get("state") != "WORKSTATION_PREDICTIONS_REUSED"
            or recovery.get("run_id") != gate_run.name
            or recovery.get("producer_run_id") != gate_run.name):
        raise BatchLeaseError("Gate collection recovery identity is invalid")
    from prediction_transfer import TransferError, local_inventory, verify_workstation_product
    product = Path(str(recovery.get("workstation_product") or "")).expanduser().resolve()
    try:
        files = local_inventory(product)
        if not files:
            raise BatchLeaseError("Gate recovered product is empty")
        verified = verify_workstation_product(product, files)
    except (OSError, ValueError, TransferError) as exc:
        raise BatchLeaseError(f"Gate recovered product verification failed: {exc}") from exc
    gate_source = (read_object(gate_run / "job.json").get("predictions") or {}).get("source_path")
    if (verified.get("producer_run_id") != gate_run.name
            or verified.get("source") != gate_source
            or recovery.get("source") != gate_source
            or verified.get("inventory_sha256") != recovery.get("inventory_sha256")):
        raise BatchLeaseError("Gate recovered product differs from recovery evidence")


def validated_inputs(run_dir: Path, locks_dir: Path, adapter: Path, adapter_sha256: str,
                     *, require_lock: bool = True) -> tuple[dict[str, Any], list[tuple[dict[str, Any], Path, dict[str, Any]]]]:
    if require_lock:
        require_owner_lock(run_dir, locks_dir)
    job = read_object(run_dir / "job.json")
    if job.get("run_id") != run_dir.name or (job.get("workflow") or {}).get("bench_strategy") != "isolated_candidate":
        raise BatchLeaseError("parent Job must select the isolated_candidate transaction")
    batch = job.get("batch_lease")
    if not isinstance(batch, dict):
        raise BatchLeaseError("parent Job lacks batch_lease metadata")
    manifest_path = Path(str(batch.get("manifest_path") or "")).expanduser().resolve()
    if not manifest_path.is_file() or sha256(manifest_path) != batch.get("manifest_sha256"):
        raise BatchLeaseError("batch manifest is absent or changed")
    if not adapter.is_file() or sha256(adapter) != adapter_sha256:
        raise BatchLeaseError("Profile-owned scene adapter is absent or changed")
    manifest = read_object(manifest_path)
    if manifest.get("batch_id") != batch.get("batch_id"):
        raise BatchLeaseError("batch identity differs between Job and manifest")
    entries = manifest.get("scenes")
    if not isinstance(entries, list) or not entries:
        raise BatchLeaseError("batch has no scenes")
    expected = batch.get("child_job_sha256")
    if not isinstance(expected, dict) or len(expected) != len(entries):
        raise BatchLeaseError("parent Job must pin every child Job hash")
    children: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
    run_ids: set[str] = set()
    parent_output = PurePosixPath(str((job.get("predictions") or {}).get("source_path") or ""))
    if not parent_output.is_absolute():
        raise BatchLeaseError("parent prediction source must be an absolute batch root")
    outputs: list[PurePosixPath] = []
    identity: str | None = None
    root = manifest_path.parent
    for position, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or entry.get("scene_number") != position:
            raise BatchLeaseError("batch scene order is invalid")
        run_id = str(entry.get("run_id") or "")
        scene = str(entry.get("scene") or "")
        if (not SAFE_ID.fullmatch(run_id) or not SAFE_ID.fullmatch(scene)
                or run_id in run_ids):
            raise BatchLeaseError("batch scene or run ID is empty or duplicated")
        run_ids.add(run_id)
        relative = Path(str(entry.get("job_file") or ""))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise BatchLeaseError("child Job path escapes the batch directory")
        child_path = (root / relative).resolve()
        if not child_path.is_relative_to(root) or not child_path.is_file():
            raise BatchLeaseError("child Job is absent or outside the batch directory")
        if sha256(child_path) != expected.get(run_id):
            raise BatchLeaseError(f"child Job changed: {run_id}")
        child = read_object(child_path)
        if child.get("run_id") != run_id or (child.get("replay_input") or {}).get("scene") != scene:
            raise BatchLeaseError(f"child Job scene identity mismatch: {run_id}")
        output = PurePosixPath(str((child.get("predictions") or {}).get("source_path") or ""))
        if (not output.is_absolute() or output != parent_output / scene
                or any(output.is_relative_to(other) or other.is_relative_to(output) for other in outputs)):
            raise BatchLeaseError("scene prediction root must equal batch root / scene")
        outputs.append(output)
        child_identity = common_identity(child)
        if identity is None:
            identity = child_identity
        elif identity != child_identity:
            raise BatchLeaseError("child Jobs do not share the same package, configuration and dataset identity")
        children.append((entry, child_path, child))
    if identity != batch.get("common_identity_sha256"):
        raise BatchLeaseError("parent Job does not pin the batch common identity")
    max_seconds = batch.get("max_replay_seconds")
    if not isinstance(max_seconds, int) or not 1 <= max_seconds <= 3540:
        raise BatchLeaseError("batch max_replay_seconds must fit within the Runner command limit")
    required = batch.get("required_output_types")
    if (not isinstance(required, list) or not required
            or any(not isinstance(name, str) or name not in OUTPUT_TYPES for name in required)
            or len(set(required)) != len(required)):
        raise BatchLeaseError("batch must pin distinct prediction types from the reviewed YAML and goal")
    gate = batch.get("gate")
    if not isinstance(gate, dict):
        raise BatchLeaseError("a completed single-scene Gate receipt is required")
    gate_run = Path(str(gate.get("run_dir") or ""))
    recovery_dir = gate.get("recovery_evidence_dir")
    validate_gate(gate_run, identity, Path(recovery_dir) if recovery_dir else None)
    return manifest, children


def execute_scene(adapter: Path, run_dir: Path, child_path: Path, scene: str, timeout_seconds: int) -> dict[str, Any]:
    command = [sys.executable, str(adapter), "scene", "--parent-run-dir", str(run_dir),
               "--job", str(child_path), "--scene", scene]
    result = run_adapter(command, timeout_seconds)
    if result.returncode:
        raise BatchLeaseError(f"scene adapter failed rc={result.returncode}: {result.stderr[-800:]}")
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BatchLeaseError("scene adapter did not return a JSON receipt") from exc
    if not isinstance(receipt, dict) or receipt.get("state") != "SUCCEEDED" or receipt.get("scene") != scene:
        raise BatchLeaseError("scene adapter returned an invalid success receipt")
    if (not isinstance(receipt.get("inventory_sha256"), str)
            or len(receipt["inventory_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in receipt["inventory_sha256"].lower())):
        raise BatchLeaseError("scene adapter did not verify its prediction inventory")
    return receipt


def run_adapter(command: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    if timeout_seconds < 1:
        raise BatchLeaseError("adapter deadline reached")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, stdin=subprocess.DEVNULL, start_new_session=os.name == "posix")
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            # SIGINT lets Python replay adapters execute their finally/cleanup.
            os.killpg(process.pid, signal.SIGINT)
        else:
            process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate()
        raise BatchLeaseError(f"scene adapter exceeded {timeout_seconds}s and was stopped") from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def execute_sweep(adapter: Path, run_dir: Path, manifest_path: Path, timeout_seconds: int) -> tuple[dict[str, Any], int]:
    command = [sys.executable, str(adapter), "sweep", "--parent-run-dir", str(run_dir),
               "--batch-manifest", str(manifest_path)]
    result = run_adapter(command, timeout_seconds)
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BatchLeaseError(f"sweep adapter did not return JSON; rc={result.returncode}: {result.stderr[-800:]}") from exc
    if not isinstance(receipt, dict) or not isinstance(receipt.get("completed"), list):
        raise BatchLeaseError("sweep adapter returned an invalid batch receipt")
    return receipt, result.returncode


def replay_batch(run_dir: Path, locks_dir: Path, adapter: Path, adapter_sha256: str, timeout_seconds: int) -> dict[str, Any]:
    manifest, children = validated_inputs(run_dir, locks_dir, adapter, adapter_sha256)
    pinned = read_object(run_dir / "job.json")["batch_lease"]
    deadline = time.monotonic() + pinned["max_replay_seconds"]
    results = run_dir / "batch-scene-results"
    if results.exists():
        raise BatchLeaseError("batch replay evidence already exists; retry requires a new parent run ID")
    results.mkdir(mode=0o700 if os.name != "nt" else 0o777)
    completed: list[dict[str, Any]] = []
    if pinned.get("adapter_mode", "scene") == "sweep":
        if sha256(adapter) != adapter_sha256:
            raise BatchLeaseError("sweep adapter changed before replay")
        manifest_path = Path(pinned["manifest_path"])
        try:
            sweep_receipt, return_code = execute_sweep(
                adapter, run_dir, manifest_path,
                min(timeout_seconds, max(1, int(deadline - time.monotonic()))),
            )
        except BatchLeaseError as exc:
            write_once(run_dir / "batch-sweep-failure.json", {
                "state": "FAILED", "batch_id": manifest["batch_id"], "error": str(exc),
                "finished_at": utc_now(),
            })
            raise
        returned = sweep_receipt["completed"]
        if len(returned) > len(children):
            raise BatchLeaseError("sweep returned more scenes than the immutable batch")
        required = pinned.get("required_output_types")
        if not isinstance(required, list) or not required or any(
            name not in ("pred_traj", "traj_xy", "pred_img", "pred_road") for name in required
        ):
            raise BatchLeaseError("sweep requires explicit prediction types from the reviewed YAML and goal")
        for position, item in enumerate(returned):
            entry, child_path, _child = children[position]
            if not isinstance(item, dict) or item.get("scene") != entry["scene"]:
                raise BatchLeaseError("sweep scene order or identity differs from the batch")
            counts = item.get("counts")
            if not isinstance(counts, dict) or any(int(counts.get(name) or 0) < 1 for name in required):
                raise BatchLeaseError(f"sweep output types are missing for {entry['scene']}")
            record = {
                "schema_version": 1, "run_id": entry["run_id"], "scene": entry["scene"],
                "state": "REPLAY_COMPLETED", "job_sha256": sha256(child_path),
                "counts": counts, "adapter_receipt": item, "recorded_at": utc_now(),
            }
            write_once(results / f"{entry['run_id']}.json", record)
            completed.append({"run_id": entry["run_id"], "scene": entry["scene"], "counts": counts})
        if return_code or sweep_receipt.get("state") != "SUCCEEDED" or len(completed) != len(children):
            failed = children[len(completed)][0]["run_id"] if len(completed) < len(children) else None
            write_once(run_dir / "batch-sweep-failure.json", {
                "state": "FAILED", "batch_id": manifest["batch_id"],
                "completed_count": len(completed), "failed_run_id": failed,
                "adapter_return_code": return_code, "adapter_state": sweep_receipt.get("state"),
                "finished_at": utc_now(),
            })
            raise BatchLeaseError(f"sweep stopped after {len(completed)} scenes; failed run={failed}")
        summary = {"schema_version": 1, "batch_id": manifest["batch_id"],
                   "state": "SCENES_REPLAYED", "scene_count": len(completed),
                   "completed": completed, "finished_at": utc_now()}
        write_once(run_dir / "batch-scene-summary.json", summary)
        return summary
    if pinned.get("adapter_mode", "scene") != "scene":
        raise BatchLeaseError("unsupported batch adapter mode")
    for entry, child_path, _child in children:
        require_owner_lock(run_dir, locks_dir)
        scene = str(entry["scene"])
        run_id = str(entry["run_id"])
        started = utc_now()
        try:
            if sha256(adapter) != adapter_sha256 or sha256(child_path) != pinned["child_job_sha256"][run_id]:
                raise BatchLeaseError("scene adapter or child Job changed during the batch")
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                raise BatchLeaseError("batch replay deadline reached before this scene")
            adapter_receipt = execute_scene(adapter, run_dir, child_path, scene,
                                            min(timeout_seconds, remaining))
        except (BatchLeaseError, subprocess.TimeoutExpired) as exc:
            write_once(results / f"{run_id}.json", {
                "schema_version": 1, "run_id": run_id, "scene": scene,
                "state": "FAILED", "started_at": started, "finished_at": utc_now(),
                "job_sha256": sha256(child_path), "error": str(exc),
            })
            raise BatchLeaseError(f"batch stopped at {run_id}: {exc}") from exc
        record = {
            "schema_version": 1, "run_id": run_id, "scene": scene,
            "state": "SUCCEEDED", "started_at": started, "finished_at": utc_now(),
            "job_sha256": sha256(child_path), "inventory_sha256": adapter_receipt["inventory_sha256"],
            "adapter_receipt": adapter_receipt,
        }
        write_once(results / f"{run_id}.json", record)
        completed.append({"run_id": run_id, "scene": scene, "inventory_sha256": record["inventory_sha256"]})
    summary = {"schema_version": 1, "batch_id": manifest["batch_id"], "state": "SCENES_COMPLETED",
               "scene_count": len(completed), "completed": completed, "finished_at": utc_now()}
    write_once(run_dir / "batch-scene-summary.json", summary)
    return summary


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=("precheck", "replay"))
    result.add_argument("--run-dir", type=Path, required=True)
    result.add_argument("--locks-dir", type=Path, required=True)
    result.add_argument("--adapter", type=Path, required=True)
    result.add_argument("--adapter-sha256", required=True)
    result.add_argument("--adapter-timeout-seconds", "--scene-timeout-seconds",
                        dest="adapter_timeout_seconds", type=int, default=3540)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.action == "precheck":
            manifest, children = validated_inputs(args.run_dir.resolve(), args.locks_dir.resolve(),
                                                  args.adapter.resolve(), args.adapter_sha256)
            value = {"state": "READY", "batch_id": manifest["batch_id"], "scene_count": len(children)}
        else:
            value = replay_batch(args.run_dir.resolve(), args.locks_dir.resolve(),
                                 args.adapter.resolve(), args.adapter_sha256, args.adapter_timeout_seconds)
    except (BatchLeaseError, OSError) as exc:
        print(json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
