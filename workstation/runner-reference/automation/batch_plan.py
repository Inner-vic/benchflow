#!/usr/bin/env python3
"""Plan and coordinate a resumable multi-scene bench-evaluation batch.

The batch layer deliberately reuses the existing one-scene transaction as its
unit of isolation.  It never embeds executable commands in a job: each child
job selects a workstation-owned command set and keeps its own activation,
restore, prediction, transfer and evaluation evidence.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench_eval import RUNNER_PROTOCOL_VERSION, build_plan, evaluate_job
from runner_state import RunnerError, approve_stage, read_json, submit_job, validate_run_id


TEMPLATE_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
ALLOWED_FIELDS = {"batch_id", "run_id", "scene", "scene_index", "scene_number", "scene_count"}
TERMINAL_SUCCESS_STATES = {
    "COMPLETED", "RELEASED", "FULL_COMPLETED", "COMPLETED_TO_REQUESTED_GOAL",
}
TERMINAL_FAILURE_STATES = {
    "FAILED_EXECUTION", "FAILED_COORDINATION", "TRANSFER_FAILED",
    "WORKSTATION_COLLECTION_FAILED", "LPAI_PUBLICATION_FAILED",
    "EVALUATION_FAILED", "EVALUATION_INVALID", "CANCELLED",
}
UNSAFE_STATES = {"RESTORE_FAILED_UNSAFE"}


class BatchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BatchError(f"{label} must contain a JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def render_value(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: render_value(child, context) for key, child in value.items()}
    if isinstance(value, list):
        return [render_value(child, context) for child in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in ALLOWED_FIELDS:
            raise BatchError(f"unsupported batch template field: {name}")
        return context[name]

    return TEMPLATE_FIELD.sub(replace, value)


def inventory_scenes(inventory: dict[str, Any], selected: list[str] | None = None) -> list[tuple[str, dict[str, Any]]]:
    scenes = inventory.get("scenes")
    if not isinstance(scenes, dict) or not scenes:
        raise BatchError("inventory.scenes must be a non-empty object")
    available = sorted(scenes)
    requested = available if not selected else list(dict.fromkeys(selected))
    missing = sorted(set(requested) - set(available))
    if missing:
        raise BatchError(f"selected scenes are absent from inventory: {missing}")
    result: list[tuple[str, dict[str, Any]]] = []
    for scene in requested:
        stats = scenes[scene]
        if not isinstance(stats, dict):
            raise BatchError(f"inventory stats must be an object for scene {scene}")
        result.append((scene, stats))
    return result


def nested_string(job: dict[str, Any], *path: str) -> str | None:
    value: Any = job
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, str) and value else None


def ensure_isolated_paths(jobs: list[dict[str, Any]]) -> None:
    """Reject shared mutable paths that would mix evidence between scenes."""
    fields = (
        ("release_package", "isolated_run"),
        ("predictions", "source_path"),
        ("predictions", "destination_path"),
        ("evaluation", "prediction_path"),
        ("evaluation", "config_path"),
    )
    for path in fields:
        values = [nested_string(job, *path) for job in jobs]
        present = [value for value in values if value is not None]
        if present and len(present) != len(jobs):
            raise BatchError(f"{'.'.join(path)} must be present for every scene or omitted for every scene")
        duplicates = sorted(value for value, count in Counter(present).items() if count > 1)
        if duplicates:
            raise BatchError(
                f"{'.'.join(path)} must be unique per scene; use {{scene}} or {{run_id}}: {duplicates}"
            )


def build_batch(
    base_job: dict[str, Any],
    inventory: dict[str, Any],
    batch_id: str,
    selected: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validate_run_id(batch_id)
    if len(batch_id) > 122:
        raise BatchError("batch_id must be at most 122 characters so child run ids remain valid")
    scenes = inventory_scenes(inventory, selected)
    jobs: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    total = len(scenes)
    for index, (scene, stats) in enumerate(scenes):
        run_id = f"{batch_id}-s{index + 1:03d}"
        context = {
            "batch_id": batch_id,
            "run_id": run_id,
            "scene": scene,
            "scene_index": str(index),
            "scene_number": str(index + 1),
            "scene_count": str(total),
        }
        job = render_value(copy.deepcopy(base_job), context)
        if not isinstance(job, dict):
            raise BatchError("rendered base job is not an object")
        job["schema_version"] = 1
        job["runner_protocol"] = str(job.get("runner_protocol") or RUNNER_PROTOCOL_VERSION)
        job["run_id"] = run_id
        replay_input = job.setdefault("replay_input", {})
        if not isinstance(replay_input, dict):
            raise BatchError("base_job.replay_input must be an object")
        replay_input["scene"] = scene
        replay_input["archive_stats"] = stats
        release = job.setdefault("release_package", {})
        if isinstance(release, dict) and not release.get("isolated_run"):
            release["isolated_run"] = f"/app/livc-test/{run_id}"
        job["batch"] = {
            "batch_id": batch_id,
            "scene_index": index,
            "scene_number": index + 1,
            "scene_count": total,
            "scene": scene,
        }
        jobs.append(job)
        entries.append(
            {
                "scene_index": index,
                "scene_number": index + 1,
                "scene": scene,
                "run_id": run_id,
                "job_file": f"jobs/{run_id}.json",
                "archive_stats": stats,
            }
        )

    ensure_isolated_paths(jobs)
    total_bytes = sum(int(stats.get("bytes") or 0) for _, stats in scenes)
    manifest = {
        "schema_version": 1,
        "batch_id": batch_id,
        "created_at": utc_now(),
        "runner_protocol": RUNNER_PROTOCOL_VERSION,
        "policy": {
            "execution_unit": "one_scene_one_transaction",
            "continue_after_scene_failure": True,
            "stop_after_unsafe_restore_failure": True,
            "retry_scope": "failed_scene_only_with_new_run_id",
            "large_data_route": "bench_to_shared_workstation_product_then_optional_submitter_lpai_copy",
        },
        "inventory": {
            "archive": inventory.get("archive"),
            "scene_count": total,
            "unpacked_bytes": total_bytes,
        },
        "scenes": entries,
    }
    return manifest, jobs


def create_batch(
    base_job_path: Path,
    inventory_path: Path,
    batch_id: str,
    output_dir: Path,
    selected: list[str] | None,
) -> dict[str, Any]:
    output = output_dir.expanduser().resolve()
    if output.exists():
        raise BatchError(f"batch output already exists; refusing to overwrite: {output}")
    base_job = load_object(base_job_path.expanduser().resolve(), "base job")
    inventory = load_object(inventory_path.expanduser().resolve(), "inventory")
    manifest, jobs = build_batch(base_job, inventory, batch_id, selected)
    try:
        (output / "jobs").mkdir(parents=True, exist_ok=False)
        for entry, job in zip(manifest["scenes"], jobs):
            write_json(output / entry["job_file"], job)
        write_json(output / "batch.json", manifest)
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return {**manifest, "batch_dir": str(output)}


def load_batch(batch_dir: Path) -> tuple[Path, dict[str, Any]]:
    root = batch_dir.expanduser().resolve()
    manifest = load_object(root / "batch.json", "batch manifest")
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise BatchError("batch manifest contains no scenes")
    return root, manifest


def batch_status(batch_dir: Path, runs_dir: Path) -> dict[str, Any]:
    root, manifest = load_batch(batch_dir)
    runs_root = runs_dir.expanduser().resolve()
    items: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for entry in manifest["scenes"]:
        run_id = str(entry["run_id"])
        state_path = runs_root / run_id / "state.json"
        if not state_path.is_file():
            state = "NOT_SUBMITTED"
        else:
            try:
                state = str(read_json(state_path).get("state") or "UNKNOWN")
            except RunnerError:
                state = "INVALID_STATE"
        if state in TERMINAL_SUCCESS_STATES:
            category = "succeeded"
        elif state in UNSAFE_STATES:
            category = "unsafe"
        elif state in TERMINAL_FAILURE_STATES or state == "INVALID_STATE":
            category = "failed"
        elif state == "NOT_SUBMITTED":
            category = "not_submitted"
        else:
            category = "in_progress"
        counts[category] += 1
        items.append({"scene": entry["scene"], "run_id": run_id, "state": state, "category": category})
    total = len(items)
    terminal = counts["succeeded"] + counts["failed"] + counts["unsafe"]
    if counts["unsafe"]:
        status = "UNSAFE"
    elif counts["succeeded"] == total:
        status = "COMPLETED"
    elif terminal == total:
        status = "COMPLETED_WITH_FAILURES"
    elif counts["not_submitted"] == total:
        status = "PLANNED"
    else:
        status = "IN_PROGRESS"
    return {
        "schema_version": 1,
        "batch_id": manifest.get("batch_id"),
        "checked_at": utc_now(),
        "status": status,
        "counts": {key: counts[key] for key in ("succeeded", "failed", "unsafe", "in_progress", "not_submitted")},
        "scenes": items,
        "batch_dir": str(root),
        "runs_dir": str(runs_root),
    }


def precheck_submission(batch_dir: Path, runs_dir: Path) -> tuple[Path, dict[str, Any], list[tuple[Path, dict[str, Any], dict[str, Any]]]]:
    root, manifest = load_batch(batch_dir)
    runs_root = runs_dir.expanduser().resolve()
    prepared: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    for entry in manifest["scenes"]:
        job_path = root / str(entry["job_file"])
        job = load_object(job_path, "child job")
        preflight = evaluate_job(job, job_path)
        if preflight["status"] != "READY":
            raise BatchError(f"child job is not ready: {entry['run_id']}: {preflight['blockers']}")
        existing = runs_root / str(entry["run_id"])
        if existing.exists():
            existing_job = load_object(existing / "job.json", "existing child job")
            if existing_job != job:
                raise BatchError(f"run id collision with different job: {entry['run_id']}")
        prepared.append((job_path, job, preflight))
    return root, manifest, prepared


def submit_batch(
    batch_dir: Path,
    runs_dir: Path,
    submitted_by: str,
    client: str,
    execute: bool,
) -> dict[str, Any]:
    root, manifest, prepared = precheck_submission(batch_dir, runs_dir)
    planned = [str(job["run_id"]) for _, job, _ in prepared]
    if not execute:
        return {"status": "DRY_RUN", "batch_id": manifest["batch_id"], "would_submit": planned}
    results: list[dict[str, Any]] = []
    for job_path, job, _ in prepared:
        run_dir = runs_dir.expanduser().resolve() / str(job["run_id"])
        if run_dir.is_dir():
            results.append({"run_id": job["run_id"], "status": "ALREADY_SUBMITTED"})
            continue
        result = submit_job(
            job=job,
            source=job_path,
            plan=build_plan(job, job_path),
            runs_dir=runs_dir,
            submitted_by=submitted_by,
            client=client,
        )
        results.append({"run_id": job["run_id"], "status": result["state"]})
    return {"status": "SUBMITTED", "batch_id": manifest["batch_id"], "batch_dir": str(root), "runs": results}


def approve_batch(batch_dir: Path, runs_dir: Path, stage: str, approved_by: str, execute: bool) -> dict[str, Any]:
    _root, manifest = load_batch(batch_dir)
    run_ids = [str(entry["run_id"]) for entry in manifest["scenes"]]
    missing = [run_id for run_id in run_ids if not (runs_dir.expanduser().resolve() / run_id).is_dir()]
    if missing:
        raise BatchError(f"submit every child before approval; missing runs: {missing}")
    if not execute:
        return {"status": "DRY_RUN", "batch_id": manifest["batch_id"], "stage": stage, "would_approve": run_ids}
    results = [approve_stage(runs_dir, run_id, stage, approved_by) for run_id in run_ids]
    return {"status": "APPROVED", "batch_id": manifest["batch_id"], "stage": stage, "runs": results}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Plan, submit and summarize full-dataset scene batches")
    commands = result.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="Expand one reviewed base job into isolated per-scene jobs")
    plan.add_argument("--base-job", required=True, type=Path)
    plan.add_argument("--inventory", required=True, type=Path)
    plan.add_argument("--batch-id", required=True)
    plan.add_argument("--output-dir", required=True, type=Path)
    plan.add_argument("--scene", action="append", help="Optional selected scene; default is every inventory scene")

    status = commands.add_parser("status", help="Aggregate durable child-run states")
    status.add_argument("--batch-dir", required=True, type=Path)
    status.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")

    submit = commands.add_parser("submit", help="Submit all ready child jobs; dry-run unless --execute")
    submit.add_argument("--batch-dir", required=True, type=Path)
    submit.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    submit.add_argument("--submitted-by", required=True)
    submit.add_argument("--client", default="personal-ai")
    submit.add_argument("--execute", action="store_true")

    approve = commands.add_parser("approve", help="Approve one stage for every submitted child; dry-run unless --execute")
    approve.add_argument("--batch-dir", required=True, type=Path)
    approve.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    approve.add_argument("--stage", required=True, choices=("ACTIVATE", "REPLAY"))
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--execute", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "plan":
        result = create_batch(args.base_job, args.inventory, args.batch_id, args.output_dir, args.scene)
    elif args.command == "status":
        result = batch_status(args.batch_dir, args.runs_dir)
    elif args.command == "submit":
        result = submit_batch(args.batch_dir, args.runs_dir, args.submitted_by, args.client, args.execute)
    else:
        result = approve_batch(args.batch_dir, args.runs_dir, args.stage, args.approved_by, args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BatchError, RunnerError) as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
