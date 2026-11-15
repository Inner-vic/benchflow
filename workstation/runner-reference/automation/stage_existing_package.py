#!/usr/bin/env python3
"""Validate and isolate an application archive that already exists on the bench."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from ssh_transport import command as ssh_transport_command
from ssh_transport import environment as ssh_environment
from ssh_transport import normalize_auth_mode


ALLOWED_SOURCE_PARENT = Path("/app")
ALLOWED_STAGE_ROOT = Path("/app/livc-test")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:@-]+$")
SPACE_MARGIN_BYTES = 512 * 1024 * 1024


class ExistingStageError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_archive(path: Path) -> dict[str, Any]:
    members = 0
    files = 0
    unpacked_bytes = 0
    top_levels: set[str] = set()
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                members += 1
                if "\\" in member.name:
                    raise ExistingStageError(f"archive member uses a backslash: {member.name!r}")
                member_path = PurePosixPath(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ExistingStageError(f"unsafe archive member path: {member.name!r}")
                parts = tuple(part for part in member_path.parts if part not in ("", "."))
                if not parts:
                    raise ExistingStageError("archive contains an empty member path")
                top_levels.add(parts[0])
                if member.isdev() or member.isfifo():
                    raise ExistingStageError(f"archive contains a device or FIFO: {member.name!r}")
                if member.issym() or member.islnk():
                    link = PurePosixPath(member.linkname)
                    if link.is_absolute() or ".." in link.parts:
                        raise ExistingStageError(
                            f"archive contains an unsafe link: {member.name!r} -> {member.linkname!r}"
                        )
                elif member.isfile():
                    files += 1
                    unpacked_bytes += member.size
                elif not member.isdir():
                    raise ExistingStageError(f"unsupported archive member: {member.name!r}")
    except (tarfile.TarError, OSError) as exc:
        raise ExistingStageError(f"cannot inspect archive: {exc}") from exc
    if files == 0:
        raise ExistingStageError("archive contains no regular files")
    if len(top_levels) != 1:
        raise ExistingStageError(f"archive must contain one top-level directory: {sorted(top_levels)}")
    return {
        "member_count": members,
        "file_count": files,
        "unpacked_bytes": unpacked_bytes,
        "top_level": next(iter(top_levels)),
    }


def remote_worker(args: argparse.Namespace) -> int:
    os.umask(0o077)
    source = Path(args.source)
    if not source.name.endswith(".tar.gz"):
        raise ExistingStageError("source package must end with .tar.gz")
    if not source.is_file() or source.is_symlink():
        raise ExistingStageError(f"source is not a regular file: {source}")
    resolved_source = source.resolve()
    if resolved_source.parent != ALLOWED_SOURCE_PARENT:
        raise ExistingStageError(f"source must be a direct file below {ALLOWED_SOURCE_PARENT}")
    if not RUN_ID_PATTERN.fullmatch(args.run_id):
        raise ExistingStageError("invalid run-id")

    expected_sha256 = args.expected_sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ExistingStageError("expected SHA-256 is invalid")
    actual_sha256 = sha256_file(resolved_source)
    if actual_sha256 != expected_sha256:
        raise ExistingStageError(
            f"SHA-256 mismatch: expected={expected_sha256}, actual={actual_sha256}"
        )
    archive_info = validate_archive(resolved_source)

    run_dir = ALLOWED_STAGE_ROOT / args.run_id
    if run_dir.exists():
        raise ExistingStageError(f"run directory already exists: {run_dir}")
    free_bytes = shutil.disk_usage(ALLOWED_STAGE_ROOT.parent).free
    required_bytes = archive_info["unpacked_bytes"] + SPACE_MARGIN_BYTES
    if free_bytes < required_bytes:
        raise ExistingStageError(
            f"insufficient /app space: need {required_bytes} bytes, available {free_bytes}"
        )

    run_dir.mkdir(parents=True, exist_ok=False)
    state = "VALIDATED_ON_BENCH"
    if args.extract:
        unpacked_dir = run_dir / "unpacked"
        unpacked_dir.mkdir(parents=False, exist_ok=False)
        with tarfile.open(resolved_source, "r:gz") as archive:
            archive.extractall(unpacked_dir)
        state = "EXTRACTED_ISOLATED"

    bench_epoch = int(time.time())
    clock_skew = bench_epoch - args.controller_epoch
    manifest = {
        "schema_version": 1,
        "run_id": args.run_id,
        "created_at": utc_now(),
        "state": state,
        "package": {
            "bench_source_path": str(resolved_source),
            "size_bytes": resolved_source.stat().st_size,
            "sha256": actual_sha256,
            **archive_info,
        },
        "bench": {
            "run_dir": str(run_dir),
            "unpacked_dir": str(run_dir / "unpacked") if args.extract else None,
            "available_bytes_before": free_bytes,
            "clock": {
                "controller_epoch": args.controller_epoch,
                "bench_epoch": bench_epoch,
                "skew_seconds": clock_skew,
                "within_300_seconds": abs(clock_skew) <= 300,
                "policy": "warn",
            },
        },
        "safety": {
            "active_app_untouched": True,
            "source_package_modified": False,
            "activation_performed": False,
            "cleanup_performed": False,
        },
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


def ssh_base(ssh_config: Path | None, auth_mode: str = "publickey") -> list[str]:
    configured: list[str] = []
    if ssh_config:
        configured.extend(["-F", str(ssh_config)])
    configured.extend(["-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10"])
    return ssh_transport_command("ssh", configured, auth_mode)


def run(
    command: Sequence[str], *, input_text: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(shlex.quote(part) for part in command))
    result = subprocess.run(command, input=input_text, text=True, capture_output=True, env=env)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    return result


def controller_main() -> int:
    parser = argparse.ArgumentParser(description="Stage an existing /app/*.tar.gz package in isolation")
    parser.add_argument("--bench", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ssh-config", type=Path)
    parser.add_argument(
        "--ssh-auth-mode",
        choices=("publickey", "keyboard-interactive-empty"),
        default="publickey",
    )
    parser.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    parser.add_argument("--extract", action="store_true")
    args = parser.parse_args()

    if not HOST_PATTERN.fullmatch(args.bench) or args.bench.startswith("-"):
        parser.error("invalid bench SSH target")
    if not RUN_ID_PATTERN.fullmatch(args.run_id):
        parser.error("invalid run-id")
    ssh_config = args.ssh_config.expanduser().resolve() if args.ssh_config else None
    if ssh_config and not ssh_config.is_file():
        parser.error(f"SSH config does not exist: {ssh_config}")
    auth_mode = normalize_auth_mode(args.ssh_auth_mode)
    local_run = args.runs_dir.expanduser().resolve() / args.run_id
    if local_run.exists():
        parser.error(f"local run directory already exists: {local_run}")

    worker_source = Path(__file__).read_text(encoding="utf-8")
    controller_epoch = int(time.time())
    command = [
        *ssh_base(ssh_config, auth_mode),
        args.bench,
        "python3",
        "-",
        "--remote-worker",
        "--source",
        args.source,
        "--expected-sha256",
        args.expected_sha256,
        "--run-id",
        args.run_id,
        "--controller-epoch",
        str(controller_epoch),
    ]
    if args.extract:
        command.append("--extract")
    result = run(command, input_text=worker_source, env=ssh_environment(auth_mode))
    if result.returncode != 0:
        raise ExistingStageError(f"remote staging failed with exit code {result.returncode}")
    output_lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not output_lines:
        raise ExistingStageError("remote worker returned no manifest")
    try:
        manifest = json.loads(output_lines[-1])
    except json.JSONDecodeError as exc:
        raise ExistingStageError("remote worker returned an invalid manifest") from exc
    local_run.mkdir(parents=True, exist_ok=False)
    (local_run / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Completed safely: {manifest['state']}; /app/livc was not modified.")
    return 0


def parse_remote_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote-worker", action="store_true")
    parser.add_argument("--source", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--controller-epoch", required=True, type=int)
    parser.add_argument("--extract", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        if "--remote-worker" in sys.argv[1:]:
            raise SystemExit(remote_worker(parse_remote_args(sys.argv[1:])))
        raise SystemExit(controller_main())
    except ExistingStageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
