#!/usr/bin/env python3
"""Safely stage a .tar.gz package on the bench without activating it.

This script is intended to run on the Ubuntu workstation. It only writes below
``/app/livc-test/<run_id>`` on the bench, verifies the transfer hash, validates
the tar archive before extraction, and never touches the active ``/app/livc``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from ssh_transport import command as ssh_transport_command
from ssh_transport import environment as ssh_environment
from ssh_transport import normalize_auth_mode


ALLOWED_STAGE_ROOT = PurePosixPath("/app/livc-test")
ALLOWED_EXTERNAL_STAGE_ROOT = PurePosixPath("/map/bench-eval/isolated-stage")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:@-]+$")
SPACE_MARGIN_BYTES = 512 * 1024 * 1024


class StageError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(
    command: Sequence[str], *, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    rendered = " ".join(shlex.quote(part) for part in command)
    print(f"+ {rendered}")
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    if check and result.returncode != 0:
        raise StageError(f"command failed with exit code {result.returncode}: {rendered}")
    return result


def ssh_base(ssh_config: Path | None, auth_mode: str = "publickey") -> list[str]:
    configured: list[str] = []
    if ssh_config is not None:
        configured.extend(["-F", str(ssh_config)])
    configured.extend(
        [
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=yes",
        ]
    )
    return ssh_transport_command("ssh", configured, auth_mode)


def ssh(
    host: str,
    command: str,
    *,
    ssh_config: Path | None,
    auth_mode: str = "publickey",
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run(
        [*ssh_base(ssh_config, auth_mode), host, command],
        check=check,
        env=ssh_environment(auth_mode),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_archive(path: Path) -> dict[str, int]:
    member_count = 0
    file_count = 0
    unpacked_bytes = 0
    top_levels: set[str] = set()

    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                member_count += 1
                raw_name = member.name.replace("\\", "/")
                member_path = PurePosixPath(raw_name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise StageError(f"unsafe archive member path: {member.name!r}")
                clean_parts = [part for part in member_path.parts if part not in ("", ".")]
                if not clean_parts:
                    raise StageError("archive contains an empty member path")
                top_levels.add(clean_parts[0])

                if member.isdev() or member.isfifo():
                    raise StageError(f"archive contains a device or FIFO: {member.name!r}")
                if member.issym() or member.islnk():
                    link_path = PurePosixPath(member.linkname.replace("\\", "/"))
                    if link_path.is_absolute() or ".." in link_path.parts:
                        raise StageError(
                            f"archive contains an unsafe link: {member.name!r} -> {member.linkname!r}"
                        )
                if member.isfile():
                    file_count += 1
                    unpacked_bytes += member.size
    except (tarfile.TarError, OSError) as exc:
        raise StageError(f"cannot read tar.gz archive: {exc}") from exc

    if member_count == 0 or file_count == 0:
        raise StageError("archive is empty")
    if len(top_levels) != 1:
        raise StageError(f"archive must have one top-level directory, got: {sorted(top_levels)}")

    return {
        "member_count": member_count,
        "file_count": file_count,
        "unpacked_bytes": unpacked_bytes,
    }


def parse_available_bytes(df_output: str) -> int:
    lines = [line for line in df_output.splitlines() if line.strip()]
    if not lines:
        raise StageError("remote df returned no output")
    columns = lines[-1].split()
    if len(columns) < 4 or not columns[3].isdigit():
        raise StageError(f"cannot parse remote free space: {lines[-1]!r}")
    return int(columns[3]) * 1024


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage and optionally extract a tar.gz package under /app/livc-test on the bench."
    )
    parser.add_argument("--package", required=True, type=Path, help="Package already present on the workstation")
    parser.add_argument("--bench", required=True, help="SSH target such as root@203.0.113.20")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    parser.add_argument("--ssh-config", type=Path, help="Optional project-owned SSH config")
    parser.add_argument(
        "--ssh-auth-mode",
        choices=("publickey", "keyboard-interactive-empty"),
        default="publickey",
    )
    parser.add_argument(
        "--clock-skew-policy",
        choices=("warn", "block"),
        default="warn",
        help="How to handle a bench clock difference greater than 300 seconds",
    )
    parser.add_argument("--extract", action="store_true", help="Extract after validation and hash verification")
    parser.add_argument(
        "--storage-root", choices=(str(ALLOWED_STAGE_ROOT), str(ALLOWED_EXTERNAL_STAGE_ROOT)),
        default=str(ALLOWED_STAGE_ROOT),
        help="Store isolated bytes on /map while retaining /app/livc-test/<run-id> as the profile path",
    )
    args = parser.parse_args()

    package = args.package.expanduser().resolve()
    if not package.is_file():
        parser.error(f"package does not exist: {package}")
    if not package.name.endswith(".tar.gz"):
        parser.error("package name must end with .tar.gz")
    if not RUN_ID_PATTERN.fullmatch(args.run_id):
        parser.error("run-id may only contain letters, numbers, dot, underscore and hyphen")
    if not HOST_PATTERN.fullmatch(args.bench) or args.bench.startswith("-"):
        parser.error("invalid bench SSH target")
    ssh_config = args.ssh_config.expanduser().resolve() if args.ssh_config else None
    if ssh_config is not None and not ssh_config.is_file():
        parser.error(f"SSH config does not exist: {ssh_config}")
    auth_mode = normalize_auth_mode(args.ssh_auth_mode)

    profile_run = ALLOWED_STAGE_ROOT / args.run_id
    storage_root = PurePosixPath(args.storage_root)
    remote_run = storage_root / args.run_id
    incoming_dir = remote_run / "incoming"
    unpacked_dir = remote_run / "unpacked"
    remote_package = incoming_dir / package.name
    local_run = args.runs_dir.expanduser().resolve() / args.run_id
    if local_run.exists():
        parser.error(f"local run directory already exists: {local_run}")
    local_run.mkdir(parents=True, exist_ok=False)

    archive = validate_archive(package)
    package_size = package.stat().st_size
    package_sha256 = sha256_file(package)
    required_bytes = package_size + archive["unpacked_bytes"] + SPACE_MARGIN_BYTES

    quoted_remote_run = shlex.quote(str(remote_run))
    quoted_profile_run = shlex.quote(str(profile_run))
    existence = ssh(
        args.bench,
        f"if [ -e {quoted_remote_run} ] || [ -L {quoted_remote_run} ] || "
        f"[ -e {quoted_profile_run} ] || [ -L {quoted_profile_run} ]; "
        "then echo EXISTS; exit 42; else echo ABSENT; fi",
        ssh_config=ssh_config,
        auth_mode=auth_mode,
        check=False,
    )
    if existence.returncode == 42:
        raise StageError(f"remote run directory already exists: {remote_run}")
    if existence.returncode != 0:
        raise StageError("could not verify remote run directory")

    workstation_epoch = int(time.time())
    bench_epoch_output = ssh(
        args.bench, "date +%s", ssh_config=ssh_config, auth_mode=auth_mode
    ).stdout.strip()
    if not bench_epoch_output.isdigit():
        raise StageError(f"cannot read bench clock: {bench_epoch_output!r}")
    bench_epoch = int(bench_epoch_output)
    clock_skew_seconds = bench_epoch - workstation_epoch
    clock_ok = abs(clock_skew_seconds) <= 300
    if not clock_ok:
        print(
            f"WARNING: bench clock differs from workstation by {clock_skew_seconds} seconds; "
            f"policy={args.clock_skew_policy}."
        )
        if args.clock_skew_policy == "block":
            raise StageError("bench clock skew exceeds 300 seconds")

    storage_mount = "/app" if storage_root == ALLOWED_STAGE_ROOT else "/map"
    free_space = ssh(
        args.bench, f"df -Pk {storage_mount} | tail -n 1", ssh_config=ssh_config,
        auth_mode=auth_mode,
    ).stdout
    available_bytes = parse_available_bytes(free_space)
    if available_bytes < required_bytes:
        raise StageError(
            f"insufficient space on {storage_mount}: need {required_bytes} bytes including margin, "
            f"available {available_bytes} bytes"
        )

    ssh(
        args.bench,
        f"umask 077; mkdir -p {shlex.quote(str(incoming_dir))}",
        ssh_config=ssh_config,
        auth_mode=auth_mode,
    )
    remote_shell = " ".join(
        shlex.quote(part) for part in ssh_base(ssh_config, auth_mode)
    )
    run(
        [
            "rsync",
            "-e",
            remote_shell,
            "--archive",
            "--partial",
            "--",
            str(package),
            f"{args.bench}:{incoming_dir}/",
        ],
        env=ssh_environment(auth_mode),
    )

    remote_hash_output = ssh(
        args.bench,
        f"sha256sum {shlex.quote(str(remote_package))}",
        ssh_config=ssh_config,
        auth_mode=auth_mode,
    ).stdout.strip()
    remote_sha256 = remote_hash_output.split()[0] if remote_hash_output else ""
    if remote_sha256 != package_sha256:
        raise StageError(
            f"SHA-256 mismatch: workstation={package_sha256}, bench={remote_sha256 or 'missing'}"
        )

    state = "STAGED"
    extracted: dict[str, int] | None = None
    if args.extract:
        extract_log = remote_run / "extract.stderr.log"
        ssh(
            args.bench,
            "mkdir -p {unpacked} && tar -xzf {package} -C {unpacked} 2> {log}".format(
                unpacked=shlex.quote(str(unpacked_dir)),
                package=shlex.quote(str(remote_package)),
                log=shlex.quote(str(extract_log)),
            ),
            ssh_config=ssh_config,
            auth_mode=auth_mode,
        )
        inspection = ssh(
            args.bench,
            "printf 'files='; find {path} -type f | wc -l; "
            "printf 'disk_kib='; du -sk {path} | awk '{{print $1}}'; "
            "printf 'extract_warning_lines='; wc -l < {log}".format(
                path=shlex.quote(str(unpacked_dir)),
                log=shlex.quote(str(extract_log)),
            ),
            ssh_config=ssh_config,
            auth_mode=auth_mode,
        ).stdout
        extracted = {}
        for line in inspection.splitlines():
            key, separator, value = line.partition("=")
            if separator and value.strip().isdigit():
                extracted[key] = int(value.strip())
        state = "EXTRACTED_ISOLATED"

    manifest = {
        "schema_version": 1,
        "run_id": args.run_id,
        "created_at": utc_now(),
        "state": state,
        "safety": {
            "active_app_untouched": True,
            "allowed_stage_root": str(ALLOWED_STAGE_ROOT),
            "storage_root": str(storage_root),
            "activation_performed": False,
            "cleanup_performed": False,
        },
        "package": {
            "workstation_path": str(package),
            "bench_path": str(remote_package),
            "size_bytes": package_size,
            "sha256": package_sha256,
            **archive,
        },
        "bench": {
            "ssh_target": args.bench,
            "ssh_config": str(ssh_config) if ssh_config else None,
            "ssh_auth_mode": auth_mode,
            "run_dir": str(profile_run),
            "storage_dir": str(remote_run),
            "available_bytes_before": available_bytes,
            "clock": {
                "workstation_epoch": workstation_epoch,
                "bench_epoch": bench_epoch,
                "skew_seconds": clock_skew_seconds,
                "within_300_seconds": clock_ok,
                "policy": args.clock_skew_policy,
            },
            "extracted": extracted,
        },
    }
    manifest_path = local_run / "manifest.json"
    write_json(manifest_path, manifest)
    run(
        [
            "rsync",
            "-e",
            remote_shell,
            "--archive",
            "--",
            str(manifest_path),
            f"{args.bench}:{remote_run}/manifest.json",
        ],
        env=ssh_environment(auth_mode),
    )

    if storage_root != ALLOWED_STAGE_ROOT:
        expected_dir = "unpacked" if args.extract else "incoming"
        ssh(
            args.bench,
            f"set -e; test ! -e {quoted_profile_run}; test ! -L {quoted_profile_run}; "
            f"ln -s {quoted_remote_run} {quoted_profile_run}; "
            f"test -d {quoted_profile_run}/{expected_dir}",
            ssh_config=ssh_config,
            auth_mode=auth_mode,
        )

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"Completed safely: {state}; active directory /app/livc was not modified.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
