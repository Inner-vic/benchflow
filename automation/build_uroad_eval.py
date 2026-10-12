#!/usr/bin/env python3
"""Build and archive the uroad evaluation-dump shared library in LiDocker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")
BASELOCK_PATTERN = re.compile(r"^[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+-]+@[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+-]+$")
CONTAINER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class BuildError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_capture(argv: list[str], cwd: Path) -> str:
    completed = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    if completed.returncode != 0:
        raise BuildError(f"command failed ({completed.returncode}): {' '.join(argv)}\n{completed.stderr.strip()}")
    return completed.stdout.strip()


def inspect_elf(path: Path) -> dict[str, object]:
    header = path.read_bytes()[:64]
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise BuildError("build output is not ELF")
    order = "little" if header[5] == 1 else "big" if header[5] == 2 else ""
    machine = int.from_bytes(header[18:20], order) if order else -1
    if header[4] != 2 or machine != 183:
        raise BuildError(f"build output must be ELF64 AArch64; class={header[4]} machine={machine}")
    return {"elf_class": 64, "machine": "AArch64", "byte_order": order}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build liburoad_m100.so with WITH_EVAL_DUMP in the current LiDocker environment")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--artifacts-root", type=Path, default=Path.home() / "bench-eval" / "cache" / "builds")
    parser.add_argument("--baselock", default="example_toolchain/1.0@example/stable")
    parser.add_argument("--container", help="Running approved LiDocker container ID or name; required with --execute")
    parser.add_argument("--container-workdir", default="/root/workspace/apps")
    parser.add_argument("--offline-lock", action="store_true", help="Use source/build/conan.lock and cached Conan packages without running build.sh")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    try:
        if not ID_PATTERN.fullmatch(args.artifact_id):
            raise BuildError("invalid artifact-id")
        if not COMMIT_PATTERN.fullmatch(args.expected_commit):
            raise BuildError("expected-commit must be a 7-40 character hexadecimal Git commit")
        if not BASELOCK_PATTERN.fullmatch(args.baselock):
            raise BuildError("invalid baselock reference")
        container_workdir = PurePosixPath(args.container_workdir)
        if not container_workdir.is_absolute() or ".." in container_workdir.parts:
            raise BuildError("container-workdir must be an absolute path without parent traversal")
        if args.container and not CONTAINER_PATTERN.fullmatch(args.container):
            raise BuildError("invalid LiDocker container ID or name")
        source = args.source.expanduser().resolve()
        if not (source / "build.sh").is_file() or not (source / "conanfile.py").is_file():
            raise BuildError(f"source is not a uroad build tree: {source}")
        commit = run_capture(["git", "rev-parse", "HEAD"], source)
        if not commit.lower().startswith(args.expected_commit.lower()):
            raise BuildError(f"source commit mismatch: expected={args.expected_commit} actual={commit}")
        conanfile = (source / "conanfile.py").read_text(encoding="utf-8")
        if not re.search(r"WITH_EVAL_DUMP[^=\r\n]{0,8}=\s*True\b", conanfile):
            raise BuildError("conanfile.py does not enable WITH_EVAL_DUMP=True")
        diff = run_capture(["git", "diff", "--binary"], source)
        diff_sha = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        if args.offline_lock and not (source / "build/conan.lock").is_file():
            raise BuildError("offline build requires source/build/conan.lock")
        output_root = args.artifacts_root.expanduser().resolve() / args.artifact_id
        result = {
            "schema_version": 1,
            "operation": "build-uroad-eval-library",
            "state": "PLANNED",
            "source": str(source),
            "commit": commit,
            "source_diff_sha256": diff_sha,
            "baselock": args.baselock,
            "container": args.container,
            "container_workdir": str(container_workdir),
            "build_mode": "offline-lock" if args.offline_lock else "build.sh",
            "artifact_dir": str(output_root),
            "requires": "run inside the approved LiDocker toolchain environment",
            "mutations_performed": False,
        }
        if not args.execute:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if not args.container:
            raise BuildError("--container is required with --execute; start LiDocker and pass its running container ID")
        inspected = run_capture(["docker", "inspect", args.container], source)
        try:
            container_info = json.loads(inspected)[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise BuildError(f"cannot parse docker inspect output for {args.container}") from exc
        mounts = container_info.get("Mounts", [])
        source_mounted = any(
            Path(item.get("Source", "")).resolve() == source
            and PurePosixPath(item.get("Destination", "")) == container_workdir
            for item in mounts
        )
        if not source_mounted:
            raise BuildError(
                f"source {source} is not mounted at {container_workdir} in LiDocker container {args.container}"
            )
        partial = output_root.parent / f".{args.artifact_id}.partial-{os.getpid()}"
        if output_root.exists() or partial.exists():
            raise BuildError(f"refusing to overwrite build artifact: {output_root}")
        output_root.parent.mkdir(parents=True, exist_ok=True)
        partial.mkdir(mode=0o700)
        log = partial / "build.log"
        prefix = ["docker", "exec", "--workdir", str(container_workdir), args.container]
        commands = (
            [prefix + ["conan", "install", ".", "--install-folder=build", "-l", "build/conan.lock"],
             prefix + ["conan", "build", ".", "--build-folder=build", "--package-folder=install"],
             prefix + ["conan", "package", ".", "--build-folder=build", "--package-folder=install"]]
            if args.offline_lock else
            [prefix + ["./build.sh", "-t", "schu", "-b", "release", "-o", "platform=schu", "-v", args.baselock]]
        )
        try:
            with log.open("w", encoding="utf-8") as stream:
                for command in commands:
                    stream.write("COMMAND " + " ".join(command) + "\n")
                    stream.flush()
                    completed = subprocess.run(command, cwd=source, text=True, stdout=stream, stderr=subprocess.STDOUT)
                    if completed.returncode != 0:
                        raise BuildError(f"uroad build failed with exit code {completed.returncode}; see {log}")
            built = source / "install" / "lib" / "liburoad_m100.so"
            if not built.is_file() or built.stat().st_size == 0:
                raise BuildError("build completed without install/lib/liburoad_m100.so")
            destination = partial / "liburoad_m100.so"
            shutil.copy2(built, destination)
            result.update(
                {
                    "state": "BUILT",
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "output": str(output_root / "liburoad_m100.so"),
                    "size_bytes": destination.stat().st_size,
                    "sha256": sha256_file(destination),
                    "mutations_performed": True,
                    **inspect_elf(destination),
                }
            )
            (partial / "build-manifest.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(partial, output_root)
        except Exception as exc:
            if partial.exists():
                result.update(
                    {
                        "state": "BUILD_FAILED",
                        "failed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "error": str(exc),
                        "mutations_performed": True,
                    }
                )
                (partial / "build-manifest.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                os.replace(partial, output_root)
            raise
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except BuildError as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc), "mutations_performed": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
