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
BASELOCK_PATTERN = re.compile(r"^conan_shared/[A-Za-z0-9_.+-]+@livc/release-schu$")
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


def validate_source_snapshot(source, manifest_path):
    source = Path(source)
    manifest_path = Path(manifest_path)
    if source.resolve() != source or manifest_path != source / 'source-snapshot.json' or manifest_path.is_symlink():
        raise BuildError('invalid source snapshot location')
    if manifest_path.stat().st_size > 8 * 1024 * 1024:
        raise BuildError('source snapshot manifest too large')
    proof = json.loads(manifest_path.read_text())
    if (proof.get('schema_version') != 1 or not re.fullmatch('[a-f0-9]{40}', proof.get('source_commit', ''))
            or not re.fullmatch('[a-f0-9]{64}', proof.get('package_sha256', ''))
            or not proof.get('uroad_ref', '').startswith('uroad_m100/')):
        raise BuildError('source snapshot identity invalid')
    files = proof.get('files')
    if not isinstance(files, dict) or not 1 <= len(files) <= 60000:
        raise BuildError('source snapshot inventory invalid')
    for relative, expected in files.items():
        p = PurePosixPath(relative)
        if (p.is_absolute() or '..' in p.parts or not p.parts or str(p) != relative
                or not re.fullmatch('[a-f0-9]{64}', expected)):
            raise BuildError('source snapshot member invalid')
        path = source.joinpath(*p.parts)
        if path.resolve() != path or not path.is_file() or sha256_file(path) != expected:
            raise BuildError('source snapshot content differs')
    actual = {str(p.relative_to(source)) for p in source.rglob('*') if p.is_file()
              and p.relative_to(source).parts[0] not in ('build', 'install')
              and p != manifest_path}
    if actual != set(files) or 'conanfile.py' not in files:
        raise BuildError('source snapshot inventory differs')
    if sha256_file(source / 'build/conan.lock') != proof.get('source_lock_sha256'):
        raise BuildError('source snapshot lock differs')
    return proof


def verify_eval_macro(conanfile, expected):
    import ast
    def name(node):
        if isinstance(node,ast.Name):return node.id
        if isinstance(node,ast.Attribute):return name(node.value)+'.'+node.attr
        return ''
    values=[]
    for item in ast.walk(ast.parse(conanfile)):
        if not isinstance(item,ast.Assign) or len(item.targets)!=1:continue
        target=item.targets[0]
        index=target.slice if isinstance(target,ast.Subscript) else None
        if isinstance(index,ast.Index):index=index.value
        if (isinstance(target,ast.Subscript) and name(target.value)=='self._cmake.definitions'
                and isinstance(index,ast.Constant) and index.value=='WITH_EVAL_DUMP'):
            if not isinstance(item.value,ast.Constant) or type(item.value.value) is not bool:
                raise BuildError('eval dump assignment must be an explicit boolean')
            values.append(item.value.value)
    if values != [expected]:raise BuildError('conanfile.py eval dump flag differs from requested mode')


def verify_effective_mode(cache, expected):
    values=re.findall(r'^WITH_EVAL_DUMP:BOOL=([^\r\n]+)$',cache,re.M)
    if len(values)!=1 or values[0].upper() not in ('ON','OFF','TRUE','FALSE'):
        raise BuildError('effective CMake mode missing or ambiguous')
    if (values[0].upper() in ('ON','TRUE')) is not expected:
        raise BuildError('effective CMake mode differs from the official setting')


def main() -> int:
    parser = argparse.ArgumentParser(description="Build liburoad_m100.so with WITH_EVAL_DUMP in the current LiDocker environment")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--artifacts-root", type=Path, default=Path.home() / "bench-eval" / "cache" / "builds")
    parser.add_argument("--baselock")
    parser.add_argument("--container", help="Running approved LiDocker container ID or name; required with --execute")
    parser.add_argument("--container-workdir", default="/root/workspace/apps")
    parser.add_argument("--eval-dump",choices=("on","off"),default="on")
    parser.add_argument("--offline-lock", action="store_true", help="Use source/build/conan.lock and cached Conan packages without running build.sh")
    parser.add_argument("--source-snapshot", type=Path)
    parser.add_argument("--credential-launcher", action="store_true", help="Use the fixed private credential launcher inside the isolated container")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    try:
        if not ID_PATTERN.fullmatch(args.artifact_id):
            raise BuildError("invalid artifact-id")
        if not COMMIT_PATTERN.fullmatch(args.expected_commit):
            raise BuildError("expected-commit must be a 7-40 character hexadecimal Git commit")
        if (not args.offline_lock and not args.baselock) or (args.baselock and not BASELOCK_PATTERN.fullmatch(args.baselock)):
            raise BuildError("invalid baselock reference")
        container_workdir = PurePosixPath(args.container_workdir)
        if not container_workdir.is_absolute() or ".." in container_workdir.parts:
            raise BuildError("container-workdir must be an absolute path without parent traversal")
        if args.container and not CONTAINER_PATTERN.fullmatch(args.container):
            raise BuildError("invalid LiDocker container ID or name")
        source = args.source.expanduser().resolve()
        if not (source / "conanfile.py").is_file() or (not args.offline_lock and not (source / "build.sh").is_file()):
            raise BuildError(f"source is not a uroad build tree: {source}")
        snapshot = validate_source_snapshot(source,args.source_snapshot) if args.source_snapshot else None
        commit = snapshot["source_commit"] if snapshot else run_capture(["git", "rev-parse", "HEAD"], source)
        if not commit.lower().startswith(args.expected_commit.lower()):
            raise BuildError(f"source commit mismatch: expected={args.expected_commit} actual={commit}")
        conanfile = (source / "conanfile.py").read_text(encoding="utf-8")
        verify_eval_macro(conanfile,args.eval_dump=="on")
        diff_sha = snapshot["recipe_patch_sha256"] if snapshot else hashlib.sha256(run_capture(["git", "diff", "--binary"], source).encode("utf-8")).hexdigest()
        if args.offline_lock and not (source / "build/conan.lock").is_file():
            raise BuildError("offline build requires source/build/conan.lock")
        output_root = args.artifacts_root.expanduser().resolve() / args.artifact_id
        result = {
            "schema_version": 1,
            "operation": "build-uroad-eval-library",
            "state": "PLANNED",
            "source": str(source),
            "commit": commit,
            "source_identity": "conan-revision-and-gitlab-commit" if snapshot else "git-checkout",
            "source_diff_sha256": diff_sha,
            "baselock": args.baselock if not args.offline_lock else None,
            "container": args.container,
            "container_workdir": str(container_workdir),
            "build_mode": "offline-lock" if args.offline_lock else "build.sh",
            "eval_dump": args.eval_dump=="on",
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
            [prefix + (["python3", "/run/m100-auth/launch.py"] if args.credential_launcher else []) +
             ["./build.sh", "-t", "schu", "-o", "platform=schu", "-s", "-v", args.baselock]]
        )
        try:
            with log.open("w", encoding="utf-8") as stream:
                for command in commands:
                    stream.write("COMMAND " + " ".join(command) + "\n")
                    stream.flush()
                    completed = subprocess.run(command, cwd=source, text=True, stdout=stream, stderr=subprocess.STDOUT)
                    if completed.returncode != 0:
                        raise BuildError(f"uroad build failed with exit code {completed.returncode}; see {log}")
            if snapshot and snapshot.get('build_entry') == 'upstream-buildsh-v1':
                # build.sh generates its own lock. Check what it actually used,
                # rather than claiming the old derived lock is equivalent.
                graph = json.loads((source/'build/conan_shared.lock').read_text())
                pins = {**snapshot['ci']['build_requirement_pins'],**snapshot['ci'].get('sensor_development_pins',{})}
                for name, expected in pins.items():
                    actual = {n['ref'].split('#')[0] for n in graph['graph_lock']['nodes'].values()
                              if n.get('context','host')=='host' and n.get('ref','').startswith(name+'/')}
                    if actual != {expected}:
                        raise BuildError('generated build.sh lock differs from Jenkins: ' + name)
                shutil.copy2(source/'build/conan_shared.lock',partial/'conan_shared.lock')
                cache = (source/'build/CMakeCache.txt').read_text()
                verify_effective_mode(cache,args.eval_dump=='on')
                if sha256_file(source/'build.sh') != snapshot['build_script_sha256']:
                    raise BuildError('upstream build.sh changed during execution')
                result.update(build_script_sha256=snapshot['build_script_sha256'],ci=snapshot['ci'],
                              generated_lock_sha256=sha256_file(source/'build/conan_shared.lock'),
                              effective_eval_dump=args.eval_dump=='on')
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
