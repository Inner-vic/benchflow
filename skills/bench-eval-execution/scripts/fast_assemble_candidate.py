#!/usr/bin/env python3
"""Assemble a verified candidate with fast gzip compression on the workstation.

Uses the installed Runner's artifact identity and archive-layout checks. It
does not contact the bench or activate an App. Failed partial runs are retained.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

runner_automation = Path(os.environ.get("BENCH_EVAL_AUTOMATION", Path.home() / "bench-eval/current/automation"))
sys.path.insert(0, str(runner_automation))
from artifact_pipeline import (  # noqa: E402
    APPROVED_LIBRARIES,
    ArtifactError,
    load_manifest,
    safe_archive_members,
    sha256_file,
    utc_now,
    validate_id,
    verify_manifest_file,
    write_json,
)


def package_candidate(candidate: Path, output: Path, pigz: str | None) -> str:
    children = sorted(candidate.iterdir(), key=lambda item: item.name)
    if pigz:
        with output.open("wb") as stream:
            process = subprocess.Popen(
                [pigz, "-1", "-p", "4", "-n"], stdin=subprocess.PIPE,
                stdout=stream, stderr=subprocess.PIPE,
            )
            try:
                assert process.stdin is not None
                with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                    for child in children:
                        archive.add(child, arcname=child.name, recursive=True)
                process.stdin.close()
                assert process.stderr is not None
                error = process.stderr.read().decode("utf-8", "replace")
                if process.wait() != 0:
                    raise ArtifactError(f"pigz failed: {error[-1000:]}")
            except Exception:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                raise
        return "pigz-level-1-4-threads"
    with tarfile.open(output, "w:gz", compresslevel=1) as archive:
        for child in children:
            archive.add(child, arcname=child.name, recursive=True)
    return "python-gzip-level-1"


def assemble(manifest: Path, run_id: str, runs_dir: Path, execute: bool) -> dict:
    validate_id(run_id, "run-id")
    artifact = load_manifest(manifest)
    app = verify_manifest_file(artifact["app"], "App package")
    replacements = {
        name: verify_manifest_file(record, name)
        for name, record in artifact.get("libraries", {}).items()
    }
    if set(replacements) != APPROVED_LIBRARIES:
        raise ArtifactError("artifact manifest does not contain the complete approved library set")
    members, targets = safe_archive_members(app)
    if set(targets) != APPROVED_LIBRARIES:
        raise ArtifactError("App library layout changed since intake")
    run_dir = runs_dir.expanduser().resolve() / run_id
    partial = run_dir.with_name(f".{run_id}.partial-{os.getpid()}")
    result = {
        "schema_version": 1,
        "operation": "assemble-candidate",
        "run_id": run_id,
        "artifact_id": artifact["artifact_id"],
        "created_at": utc_now(),
        "state": "PLANNED",
        "candidate_dir": str(run_dir / "candidate"),
        "candidate_package": str(run_dir / "candidate.app.tar.gz"),
        "targets": targets,
        "mutations_performed": False,
        "bench_contacted": False,
    }
    if not execute:
        return result
    if run_dir.exists() or partial.exists():
        raise ArtifactError(f"candidate run ID already exists: {run_id}")
    candidate = partial / "candidate"
    backup = partial / "original-libs"
    candidate.mkdir(parents=True)
    backup.mkdir()
    try:
        with tarfile.open(app, "r:gz") as archive:
            archive.extractall(candidate, members=members)
        changes = {}
        for name, member_name in sorted(targets.items()):
            relative = Path(*PurePosixPath(member_name).parts)
            target = candidate / relative
            if not target.is_file() or target.is_symlink():
                raise ArtifactError(f"extracted library target is not a regular file: {relative}")
            original_sha = sha256_file(target)
            backup_path = backup / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_path)
            shutil.copy2(replacements[name], target)
            changes[name] = {
                "relative_path": relative.as_posix(),
                "original_sha256": original_sha,
                "replacement_sha256": sha256_file(target),
            }
        candidate_package = partial / "candidate.app.tar.gz"
        result["compression"] = package_candidate(candidate, candidate_package, shutil.which("pigz"))
        result["changes"] = changes
        result["candidate_sha256"] = sha256_file(candidate_package)
        result["state"] = "CANDIDATE_ASSEMBLED"
        result["mutations_performed"] = True
        write_json(partial / "candidate-manifest.json", result)
        os.replace(partial, run_dir)
        return result
    except Exception:
        # Preserve the partial directory for diagnosis; a retry needs a new ID.
        write_json(partial / "failure.json", {
            "run_id": run_id, "state": "FAILED_PRESERVED", "created_at": utc_now(),
        })
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval/runs")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(assemble(args.manifest, args.run_id, args.runs_dir, args.execute), ensure_ascii=False, indent=2))
        return 0
    except (ArtifactError, OSError, tarfile.TarError) as exc:
        print(json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
