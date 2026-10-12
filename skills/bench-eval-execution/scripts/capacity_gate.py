#!/usr/bin/env python3
"""Read-only workstation and bench capacity gate before candidate staging.

The per-run plan supplies resource floors. This tool never stages, relocates,
deletes, or starts a bench transaction.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import tarfile
from pathlib import Path
from typing import Any


REMOTE_STATS = """import json, os, sys
def disk(path):
    value = os.statvfs(path)
    return {"available_bytes": value.f_bavail * value.f_frsize,
            "available_inodes": value.f_favail, "device_id": os.stat(path).st_dev}
with open('/proc/meminfo', encoding='utf-8') as stream:
    memory = {part[0].rstrip(':'): int(part[1]) * 1024
              for line in stream if (part := line.split()) and len(part) >= 2}
print(json.dumps({"stage": disk(sys.argv[1]), "run": disk(sys.argv[2]), "memory_available_bytes":
                  memory["MemAvailable"]}))
"""


class CapacityError(RuntimeError):
    pass


def nonnegative(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapacityError(f"{label} must be a nonnegative integer")
    return value


def positive(value: Any, label: str) -> int:
    result = nonnegative(value, label)
    if result == 0:
        raise CapacityError(f"{label} must be greater than zero")
    return result


def validate_plan(plan: Any) -> None:
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise CapacityError("unsupported capacity plan schema")
    expected = {
        "workstation": {"system_path", "data_path", "system_free_floor_bytes",
                        "data_free_floor_bytes", "memory_floor_bytes"},
        "bench": {"ssh_alias", "ssh_config", "stage_filesystem_path", "run_filesystem_path",
                  "runner_free_floor_bytes", "output_budget_bytes", "stage_safety_bytes",
                  "inode_floor", "memory_floor_bytes"},
    }
    if set(plan) != {"schema_version", "package", "workstation", "bench"}:
        raise CapacityError("capacity plan root fields do not match schema")
    for group, fields in expected.items():
        if not isinstance(plan[group], dict) or set(plan[group]) != fields:
            raise CapacityError(f"capacity plan {group} fields do not match schema")
    for label, value in (
        ("package", plan["package"]),
        ("workstation.system_path", plan["workstation"]["system_path"]),
        ("workstation.data_path", plan["workstation"]["data_path"]),
        ("bench.ssh_config", plan["bench"]["ssh_config"]),
        ("bench.stage_filesystem_path", plan["bench"]["stage_filesystem_path"]),
        ("bench.run_filesystem_path", plan["bench"]["run_filesystem_path"]),
    ):
        if not isinstance(value, str) or not value.startswith("/") or ".." in Path(value).parts:
            raise CapacityError(f"{label} must be a simple absolute Linux path")
    for group, fields in (
        ("workstation", ("system_free_floor_bytes", "data_free_floor_bytes", "memory_floor_bytes")),
        ("bench", ("runner_free_floor_bytes", "output_budget_bytes", "stage_safety_bytes",
                   "inode_floor", "memory_floor_bytes")),
    ):
        for field in fields:
            positive(plan[group][field], f"{group}.{field}")
    if plan["bench"]["stage_safety_bytes"] < 512 * 1024 * 1024:
        raise CapacityError("stage_safety_bytes is below the stage-package 512 MiB floor")


def archive_usage(path: Path) -> dict[str, int]:
    if not path.is_file() or not path.name.endswith(".tar.gz"):
        raise CapacityError("package must be an existing .tar.gz file")
    unpacked = 0
    inodes = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if member.isfile():
                    unpacked += member.size
                inodes += 1
    except (OSError, tarfile.TarError) as exc:
        raise CapacityError(f"cannot inspect candidate archive: {exc}") from exc
    if not unpacked or not inodes:
        raise CapacityError("candidate archive is empty")
    return {"package_bytes": path.stat().st_size,
            "unpacked_bytes": unpacked, "archive_members": inodes}


def disk_usage(path: Path) -> dict[str, int]:
    value = os.statvfs(path)
    return {"available_bytes": value.f_bavail * value.f_frsize,
            "available_inodes": value.f_favail}


def memory_available() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise CapacityError("MemAvailable missing from /proc/meminfo")


def bench_stats(alias: str, ssh_config: str, stage_path: str,
                run_path: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", alias):
        raise CapacityError("invalid bench SSH alias")
    for path in (stage_path, run_path):
        if not path.startswith("/") or ".." in Path(path).parts:
            raise CapacityError("invalid bench filesystem path")
    # The bench accepts the configured empty keyboard-interactive response;
    # BatchMode=yes suppresses it and makes this read-only gate unusable.
    environment = {**os.environ, "SSH_ASKPASS": "/bin/true",
                   "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": "bench-eval"}
    command = ["ssh", "-F", ssh_config, "-o", "BatchMode=no",
               "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no",
               "-o", "KbdInteractiveAuthentication=yes",
               "-o", "PreferredAuthentications=keyboard-interactive",
               "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=5", alias,
               shlex.join(["python3", "-c", REMOTE_STATS, stage_path, run_path])]
    result = subprocess.run(command, env=environment, capture_output=True,
                            text=True, timeout=20)
    if result.returncode:
        raise CapacityError(f"bench capacity query failed: {result.stderr.strip()[-400:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CapacityError("bench capacity response is not JSON") from exc


def evaluate(plan: dict[str, Any], archive: dict[str, int],
             workstation: dict[str, Any], bench: dict[str, Any]) -> dict[str, Any]:
    policy = plan["bench"]
    local = plan["workstation"]
    runner_floor = positive(policy["runner_free_floor_bytes"], "runner_free_floor_bytes")
    output_budget = positive(policy["output_budget_bytes"], "output_budget_bytes")
    stage_safety = positive(policy["stage_safety_bytes"], "stage_safety_bytes")
    if stage_safety < 512 * 1024 * 1024:
        raise CapacityError("stage_safety_bytes is below the stage-package 512 MiB floor")
    inode_floor = positive(policy["inode_floor"], "inode_floor")
    # The stage tool needs archive + extracted files + its own margin. The
    # Runner additionally needs its floor and output budget *after* staging.
    same_filesystem = bench["stage"]["device_id"] == bench["run"]["device_id"]
    if same_filesystem:
        needed = archive["package_bytes"] + archive["unpacked_bytes"] + max(
            stage_safety, runner_floor + output_budget)
        storage_checks = {
            "bench_stage_and_run_bytes": {
                "available": bench["stage"]["available_bytes"], "required": needed},
            "bench_inodes": {
                "available": bench["stage"]["available_inodes"],
                "required": archive["archive_members"] + inode_floor},
        }
    else:
        storage_checks = {
            "bench_stage_bytes": {
                "available": bench["stage"]["available_bytes"],
                "required": archive["package_bytes"] + archive["unpacked_bytes"] + stage_safety},
            "bench_run_bytes": {
                "available": bench["run"]["available_bytes"],
                "required": runner_floor + output_budget},
            "bench_stage_inodes": {
                "available": bench["stage"]["available_inodes"],
                "required": archive["archive_members"] + inode_floor},
            "bench_run_inodes": {
                "available": bench["run"]["available_inodes"],
                "required": inode_floor},
        }
    checks = {
        **storage_checks,
        "bench_memory_bytes": {
            "available": bench["memory_available_bytes"],
            "required": positive(policy["memory_floor_bytes"], "bench.memory_floor_bytes")},
        "workstation_system_bytes": {
            "available": workstation["system"]["available_bytes"],
            "required": positive(local["system_free_floor_bytes"], "system_free_floor_bytes")},
        "workstation_data_bytes": {
            "available": workstation["data"]["available_bytes"],
            "required": positive(local["data_free_floor_bytes"], "data_free_floor_bytes")},
        "workstation_memory_bytes": {
            "available": workstation["memory_available_bytes"],
            "required": positive(local["memory_floor_bytes"], "workstation.memory_floor_bytes")},
    }
    for check in checks.values():
        check["ok"] = check["available"] >= check["required"]
        check["shortfall"] = max(0, check["required"] - check["available"])
    return {
        "schema_version": 1,
        "state": "READY" if all(x["ok"] for x in checks.values()) else "BLOCKED_CAPACITY",
        "package": archive,
        "checks": checks,
        "mutations_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        validate_plan(plan)
        package = Path(plan["package"]).expanduser().resolve()
        archive = archive_usage(package)
        local = plan["workstation"]
        workstation = {
            "system": disk_usage(Path(local["system_path"])),
            "data": disk_usage(Path(local["data_path"])),
            "memory_available_bytes": memory_available(),
        }
        remote = plan["bench"]
        bench = bench_stats(remote["ssh_alias"], remote["ssh_config"],
                            remote["stage_filesystem_path"], remote["run_filesystem_path"])
        report = evaluate(plan, archive, workstation, bench)
    except (CapacityError, KeyError, OSError, ValueError) as exc:
        report = {"schema_version": 1, "state": "BLOCKED_CAPACITY_QUERY",
                  "error": str(exc), "mutations_performed": False}
    body = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        if args.output.exists():
            raise SystemExit("capacity output already exists; use a new immutable path")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
    print(body, end="")
    return 0 if report["state"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
