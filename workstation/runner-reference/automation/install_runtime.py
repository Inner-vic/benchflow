#!/usr/bin/env python3
"""Install a versioned bench-eval runtime on an Ubuntu orchestration host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


RUNTIME_FILES = (
    "install_runtime.py",
    "README.md",
    "config.example.json",
    "job.schema.json",
    "bench_eval.py",
    "test_bench_eval.py",
    "batch_plan.py",
    "test_batch_plan.py",
    "artifact_pipeline.py",
    "test_artifact_pipeline.py",
    "build_uroad_eval.py",
    "ssh_transport.py",
    "test_ssh_transport.py",
    "network_doctor.py",
    "test_network_doctor.py",
    "runner_state.py",
    "test_runner_state.py",
    "workflow_planner.py",
    "test_workflow_planner.py",
    "workflow_receipts.py",
    "target_workflow.py",
    "test_target_workflow.py",
    "queue_worker.py",
    "test_queue_worker.py",
    "bench_transaction.py",
    "test_bench_transaction.py",
    "full_transaction.py",
    "test_full_transaction.py",
    "prediction_transfer.py",
    "test_prediction_transfer.py",
    "parse_eval_report.py",
    "test_parse_eval_report.py",
    "evaluation_runner.py",
    "test_evaluation_runner.py",
    "lpai_eval_adapter.py",
    "test_lpai_eval_adapter.py",
    "sw0055_scene_adapter.py",
    "test_sw0055_scene_adapter.py",
    "fixtures/eval_report.sample.txt",
    "fixtures/raw-pack-11.inventory.json",
    "run.py",
    "stage_package.py",
    "test_stage_package.py",
    "prepare_replay.py",
    "test_prepare_replay.py",
    "replay_client.py",
    "test_replay_client.py",
    "validate_predictions.py",
    "test_validate_predictions.py",
    "stage_existing_package.py",
    "test_stage_existing_package.py",
    "prepare_bench_config.py",
    "test_prepare_bench_config.py",
    "compare_trees.py",
    "test_compare_trees.py",
    "workflow-contract.json",
    "control-plane-contract.json",
    "profiles/device.example.json",
    "profiles/transaction-smoke.example.json",
    "profiles/sw0055-2723-full.workstation.json",
    "profiles/user.example.json",
    "jobs/raw-pack-11-full.template.json",
    "runs/sw0055-2723-replay-20260917-001/bench_session.sh",
    "runs/sw0055-2723-replay-20260917-001/run-candidate.sh",
    "runs/sw0055-2723-replay-20260917-001/infer_expert_pcm_replay.sdf",
    "runs/sw0055-2723-replay-20260917-001/workstation_jpeg_camera_replay.py",
    "runs/sw0055-2723-replay-20260917-001/evaluation_param.complete.yaml",
    "runs/v151-formal-20260918-001/prepare_v151_truth_layout.py",
    "runs/v151-formal-20260918-001/stream_v151_truth_to_lpai.ps1",
    "runs/v151-formal-20260918-001/run_v151_evaluations.ps1",
    "runs/v151-formal-20260918-001/test_v151_powershell.py",
    "catalogs/assets.example.json",
    "assets/uroad-build-evaldump.sh",
    "assets/uroad-lidocker-precheck.sh",
    "assets/uroad-lidocker-container.sh",
    "assets/uroad-3.1.0-enable-eval-dump.patch",
    "assets/uroad-3.1.0-align-sw0055-deps.patch",
)
DATA_DIRS = ("cache/packages", "config", "jobs", "locks", "runs")
LAUNCHER = """#!/bin/sh
exec python3 \"$(dirname \"$0\")/../current/automation/bench_eval.py\" \"$@\"
"""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_target(path: Path) -> None:
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise ValueError("target-root cannot be a filesystem root or the user's home directory")
    if len(path.parts) < 3:
        raise ValueError("target-root is too broad")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the portable bench-eval runtime")
    parser.add_argument("--target-root", type=Path, default=Path.home() / "bench-eval")
    parser.add_argument("--activate", action="store_true", help="Atomically point current to this release")
    args = parser.parse_args()

    if sys.version_info < (3, 8):
        parser.error("Python 3.8 or newer is required")

    source_dir = Path(__file__).resolve().parent
    target_root = args.target_root.expanduser().resolve()
    try:
        validate_target(target_root)
    except ValueError as exc:
        parser.error(str(exc))

    missing = [name for name in RUNTIME_FILES if not (source_dir / name).is_file()]
    if missing:
        parser.error(f"runtime source is incomplete: {missing}")

    hashes = {name: file_sha256(source_dir / name) for name in RUNTIME_FILES}
    aggregate = hashlib.sha256()
    for name in sorted(hashes):
        aggregate.update(name.encode("utf-8"))
        aggregate.update(hashes[name].encode("ascii"))
    release_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + aggregate.hexdigest()[:10]

    release_dir = target_root / "releases" / release_id
    runtime_dir = release_dir / "automation"
    if release_dir.exists():
        parser.error(f"release already exists: {release_dir}")

    runtime_dir.mkdir(parents=True, exist_ok=False)
    for relative in DATA_DIRS:
        (target_root / relative).mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_FILES:
        destination = runtime_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_dir / name, destination)

    python_files = [str(runtime_dir / name) for name in RUNTIME_FILES if name.endswith(".py")]
    compile_result = subprocess.run(
        [sys.executable, "-m", "py_compile", *python_files],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if compile_result.returncode != 0:
        raise RuntimeError(f"installed runtime failed syntax validation: {compile_result.stderr.strip()}")

    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_dir": str(source_dir),
        "target_root": str(target_root),
        "python": sys.version.split()[0],
        "files": hashes,
        "activated": bool(args.activate),
    }
    if args.activate:
        current = target_root / "current"
        if current.exists() and not current.is_symlink():
            parser.error(f"refusing to replace non-symlink path: {current}")
        pending = target_root / f".current-{os.getpid()}"
        if pending.exists() or pending.is_symlink():
            pending.unlink()
        pending.symlink_to(Path("releases") / release_id, target_is_directory=True)
        os.replace(str(pending), str(current))

        bin_dir = target_root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        launcher = bin_dir / "bench-eval"
        launcher.write_text(LAUNCHER, encoding="utf-8")
        launcher.chmod(0o755)

    manifest["launcher"] = str(target_root / "bin" / "bench-eval") if args.activate else None
    (release_dir / "install-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
