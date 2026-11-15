#!/usr/bin/env python3
"""Register release inputs and assemble an isolated candidate package.

This tool runs on the shared Ubuntu workstation.  It never contacts or writes
to the bench.  Intake copies verified inputs into an immutable artifact
directory; assembly replaces only the three approved replay/evaluation shared
libraries in an extracted copy of the App package and emits a new tar.gz plus
an evidence manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit


APPROVED_LIBRARIES = {"libxcamera.so", "libxlidar.so", "liburoad_m100.so"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
REPLAY_SYMBOL_PROVIDERS = {
    "libxcamera.so": ("7XCamera",),
    "libxlidar.so": ("6XLidar",),
}


class ArtifactError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_id(value: str, label: str) -> None:
    if not ID_PATTERN.fullmatch(value):
        raise ArtifactError(f"{label} may contain only letters, numbers, dot, underscore and hyphen")


def checked_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ArtifactError(f"{label} does not exist: {resolved}")
    if resolved.stat().st_size == 0:
        raise ArtifactError(f"{label} is empty: {resolved}")
    return resolved


def validate_expected(actual: str, expected: str | None, label: str) -> None:
    if expected is None:
        return
    if not SHA256_PATTERN.fullmatch(expected):
        raise ArtifactError(f"invalid expected SHA-256 for {label}")
    if actual.lower() != expected.lower():
        raise ArtifactError(f"SHA-256 mismatch for {label}: expected={expected.lower()} actual={actual}")


def normalize_artifactory_url(value: str) -> str:
    """Return a credential-free HTTPS artifact URL.

    JFrog's browser UI exposes ``/ui/native/<repo>/...`` links while direct
    clients use ``/artifactory/<repo>/...``.  Normalising that form lets a
    user paste the visible browser link without teaching the runner about a
    particular repository.  Query strings are rejected because they often
    contain temporary credentials.
    """
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ArtifactError("artifact URL must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise ArtifactError("artifact URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ArtifactError("artifact URL must not contain a query string or fragment")
    path = parsed.path
    if path.startswith("/ui/native/"):
        path = "/artifactory/" + path[len("/ui/native/") :]
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def acquire(args: argparse.Namespace) -> dict[str, Any]:
    """Download one APP or Lockfile to the workstation with resumable curl."""
    validate_id(args.artifact_id, "artifact-id")
    if not SHA256_PATTERN.fullmatch(args.expected_sha256 or ""):
        raise ArtifactError("--expected-sha256 is required and must contain 64 hexadecimal characters")
    url = normalize_artifactory_url(args.url)
    parsed = urlsplit(url)
    filename = args.filename or unquote(PurePosixPath(parsed.path).name)
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        raise ArtifactError("artifact filename must be a plain file name")
    if not (filename.endswith(".app.tar.gz") or filename.endswith(".lock")):
        raise ArtifactError("workstation acquisition only accepts .app.tar.gz or .lock artifacts")

    cache_root = args.cache_root.expanduser().resolve()
    artifact_dir = cache_root / args.artifact_id
    destination = artifact_dir / filename
    partial = artifact_dir / f".{filename}.partial"
    evidence = artifact_dir / f"{filename}.acquire.json"
    netrc = args.netrc_file.expanduser().resolve() if args.netrc_file else None
    if netrc is not None:
        if not netrc.is_file():
            raise ArtifactError(f"external netrc file does not exist: {netrc}")
        if os.name == "posix" and netrc.stat().st_mode & 0o077:
            raise ArtifactError("external netrc file must not be accessible by group or others (expected mode 600)")

    result = {
        "schema_version": 1,
        "operation": "artifact-acquire",
        "artifact_id": args.artifact_id,
        "created_at": utc_now(),
        "state": "PLANNED",
        "source_url": url,
        "destination": str(destination),
        "partial_path": str(partial),
        "expected_sha256": args.expected_sha256.lower(),
        "authentication": "external_netrc" if netrc else "none",
        "credentials_persisted_in_result": False,
        "resume_supported": True,
        "mutations_performed": False,
    }
    if not args.execute:
        return result

    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        actual = sha256_file(destination)
        validate_expected(actual, args.expected_sha256, "existing downloaded artifact")
        result.update(
            state="ALREADY_PRESENT",
            size_bytes=destination.stat().st_size,
            sha256=actual,
        )
        write_json(evidence, result)
        return result

    command = [
        args.curl_binary,
        "--fail",
        "--location",
        "--show-error",
        "--silent",
        "--retry",
        "3",
        "--retry-delay",
        "2",
        "--connect-timeout",
        "15",
        "--continue-at",
        "-",
        "--output",
        str(partial),
    ]
    if netrc is not None:
        command.extend(["--netrc", "--netrc-file", str(netrc)])
    command.append(url)
    try:
        completed = subprocess.run(command, check=False)
    except OSError as exc:
        raise ArtifactError(f"failed to launch curl: {exc}") from exc
    if completed.returncode != 0:
        raise ArtifactError(
            f"artifact download failed with curl exit code {completed.returncode}; partial file was preserved"
        )
    if not partial.is_file() or partial.stat().st_size == 0:
        raise ArtifactError("artifact download produced no data")
    actual = sha256_file(partial)
    validate_expected(actual, args.expected_sha256, "downloaded artifact")
    os.replace(partial, destination)
    result.update(
        state="DOWNLOADED",
        size_bytes=destination.stat().st_size,
        sha256=actual,
        mutations_performed=True,
    )
    write_json(evidence, result)
    return result


def safe_archive_members(app: Path) -> tuple[list[tarfile.TarInfo], dict[str, str]]:
    targets: dict[str, str] = {}
    try:
        with tarfile.open(app, "r:gz") as archive:
            members = archive.getmembers()
    except (tarfile.TarError, OSError) as exc:
        raise ArtifactError(f"invalid .tar.gz App package: {exc}") from exc
    if not members:
        raise ArtifactError("App package contains no members")
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or not member.name or ".." in name.parts:
            raise ArtifactError(f"unsafe archive member path: {member.name!r}")
        if member.ischr() or member.isblk() or member.isfifo():
            raise ArtifactError(f"unsupported special archive member: {member.name!r}")
        if member.issym() or member.islnk():
            link = PurePosixPath(member.linkname)
            if link.is_absolute() or ".." in (name.parent / link).parts:
                raise ArtifactError(f"unsafe archive link: {member.name!r} -> {member.linkname!r}")
        if member.isfile() and name.name in APPROVED_LIBRARIES:
            if name.name in targets:
                raise ArtifactError(
                    f"App package contains multiple targets named {name.name}; explicit layout support is required"
                )
            targets[name.name] = member.name
    return members, targets


def elf_aarch64(path: Path) -> dict[str, Any]:
    header = path.read_bytes()[:64]
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise ArtifactError(f"replacement is not an ELF shared library: {path}")
    byte_order = "little" if header[5] == 1 else "big" if header[5] == 2 else ""
    if not byte_order:
        raise ArtifactError(f"replacement has an invalid ELF byte order: {path}")
    machine = int.from_bytes(header[18:20], byte_order)
    if header[4] != 2 or machine != 183:
        raise ArtifactError(f"replacement must be ELF64 AArch64 (machine=183): {path}")
    return {"elf_class": 64, "machine": "AArch64", "byte_order": byte_order}


def readelf_output(readelf_binary: str, option: str, path: Path) -> str:
    try:
        completed = subprocess.run(
            [readelf_binary, option, "--wide", str(path)],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        raise ArtifactError(f"failed to launch {readelf_binary} for ABI validation: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise ArtifactError(f"readelf failed for {path.name}: {detail}")
    return completed.stdout


def parse_dynamic_symbols(output: str) -> tuple[set[str], set[str]]:
    """Return exported and required global/weak symbols from readelf output."""
    exported: set[str] = set()
    required: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 8 or not fields[0].rstrip(":").isdigit():
            continue
        binding = fields[4]
        section = fields[6]
        symbol = fields[7].split("@", 1)[0]
        if not symbol or binding not in {"GLOBAL", "WEAK"}:
            continue
        if section == "UND":
            if binding == "GLOBAL":
                required.add(symbol)
        else:
            exported.add(symbol)
    return exported, required


def verify_replay_abi(libraries: dict[str, Path], readelf_binary: str = "readelf") -> dict[str, Any]:
    """Statically prove the uroad replay ABI against its camera/lidar providers.

    The check deliberately uses dynamic symbols instead of filenames or ELF
    headers alone.  It catches the observed failure mode where a valid AArch64
    libxlidar.so lacked a C++ callback required by liburoad_m100.so.
    """
    symbols: dict[str, dict[str, set[str]]] = {}
    for name in sorted(APPROVED_LIBRARIES):
        exported, required = parse_dynamic_symbols(
            readelf_output(readelf_binary, "--dyn-syms", libraries[name])
        )
        symbols[name] = {"exported": exported, "required": required}

    uroad_required = symbols["liburoad_m100.so"]["required"]
    providers: dict[str, Any] = {}
    missing_all: list[dict[str, str]] = []
    for provider, markers in REPLAY_SYMBOL_PROVIDERS.items():
        required = sorted(symbol for symbol in uroad_required if any(marker in symbol for marker in markers))
        exported = symbols[provider]["exported"]
        missing = sorted(symbol for symbol in required if symbol not in exported)
        providers[provider] = {
            "required_symbol_count": len(required),
            "matched_symbol_count": len(required) - len(missing),
            "missing_symbols": missing,
        }
        missing_all.extend({"provider": provider, "symbol": symbol} for symbol in missing)

    result = {
        "status": "COMPATIBLE" if not missing_all else "INCOMPATIBLE",
        "method": "readelf-dynamic-symbol-provider-match-v1",
        "consumer": "liburoad_m100.so",
        "providers": providers,
        "libraries": {
            name: {
                "exported_symbol_count": len(value["exported"]),
                "required_global_symbol_count": len(value["required"]),
            }
            for name, value in sorted(symbols.items())
        },
        "missing_symbols": missing_all,
    }
    if missing_all:
        summary = ", ".join(f"{item['provider']}:{item['symbol']}" for item in missing_all[:8])
        suffix = " ..." if len(missing_all) > 8 else ""
        raise ArtifactError(f"replay library ABI is incompatible; missing dynamic symbols: {summary}{suffix}")
    return result


def parse_libraries(values: list[str]) -> dict[str, Path]:
    libraries: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or name not in APPROVED_LIBRARIES:
            raise ArtifactError("--library must be one of libxcamera.so=PATH, libxlidar.so=PATH, liburoad_m100.so=PATH")
        if name in libraries:
            raise ArtifactError(f"duplicate replacement: {name}")
        libraries[name] = checked_file(Path(raw_path), name)
    missing = sorted(APPROVED_LIBRARIES - set(libraries))
    if missing:
        raise ArtifactError(f"all three approved libraries are required; missing={missing}")
    return libraries


def intake(args: argparse.Namespace) -> dict[str, Any]:
    validate_id(args.artifact_id, "artifact-id")
    app = checked_file(args.app, "App package")
    lock = checked_file(args.lock, "Lockfile")
    if not app.name.endswith(".app.tar.gz"):
        raise ArtifactError("App package name must end with .app.tar.gz")
    members, targets = safe_archive_members(app)
    if set(targets) != APPROVED_LIBRARIES:
        raise ArtifactError(f"App package must contain one target for each approved library; found={sorted(targets)}")
    libraries = parse_libraries(args.library)
    app_sha = sha256_file(app)
    lock_sha = sha256_file(lock)
    validate_expected(app_sha, args.expected_app_sha256, "App package")
    validate_expected(lock_sha, args.expected_lock_sha256, "Lockfile")
    library_records = {}
    for name, path in sorted(libraries.items()):
        library_records[name] = {
            "source_path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            **elf_aarch64(path),
        }
    abi_compatibility = verify_replay_abi(
        libraries,
        readelf_binary=getattr(args, "readelf_binary", "readelf"),
    )

    cache_root = args.cache_root.expanduser().resolve()
    artifact_dir = cache_root / args.artifact_id
    result = {
        "schema_version": 1,
        "operation": "artifact-intake",
        "artifact_id": args.artifact_id,
        "created_at": utc_now(),
        "state": "PLANNED",
        "app": {
            "source_path": str(app),
            "size_bytes": app.stat().st_size,
            "sha256": app_sha,
            "member_count": len(members),
            "library_targets": targets,
        },
        "lock": {"source_path": str(lock), "size_bytes": lock.stat().st_size, "sha256": lock_sha},
        "libraries": library_records,
        "abi_compatibility": abi_compatibility,
        "artifact_dir": str(artifact_dir),
        "mutations_performed": False,
    }
    if not args.execute:
        return result
    cache_root.mkdir(parents=True, exist_ok=True)
    partial = cache_root / f".{args.artifact_id}.partial-{os.getpid()}"
    if artifact_dir.exists() or partial.exists():
        raise ArtifactError(f"refusing to overwrite existing artifact directory: {artifact_dir}")
    partial.mkdir(mode=0o700)
    try:
        shutil.copy2(app, partial / app.name)
        shutil.copy2(lock, partial / lock.name)
        lib_dir = partial / "libraries"
        lib_dir.mkdir()
        for name, source in libraries.items():
            destination = lib_dir / name
            shutil.copy2(source, destination)
            result["libraries"][name]["cached_path"] = str(artifact_dir / "libraries" / name)
        result["app"]["cached_path"] = str(artifact_dir / app.name)
        result["lock"]["cached_path"] = str(artifact_dir / lock.name)
        result["state"] = "REGISTERED"
        result["mutations_performed"] = True
        write_json(partial / "artifact-manifest.json", result)
        os.replace(partial, artifact_dir)
    except Exception:
        if partial.exists():
            shutil.rmtree(partial)
        raise
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    resolved = checked_file(path, "artifact manifest")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"invalid artifact manifest: {exc}") from exc
    if not isinstance(value, dict) or value.get("state") != "REGISTERED":
        raise ArtifactError("artifact manifest is not in REGISTERED state")
    return value


def verify_manifest_file(record: dict[str, Any], label: str) -> Path:
    path = checked_file(Path(record.get("cached_path", "")), label)
    actual = sha256_file(path)
    if actual != record.get("sha256"):
        raise ArtifactError(f"cached {label} hash differs from artifact manifest")
    return path


def assemble(args: argparse.Namespace) -> dict[str, Any]:
    validate_id(args.run_id, "run-id")
    artifact = load_manifest(args.manifest)
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
    runs_root = args.runs_dir.expanduser().resolve()
    run_dir = runs_root / args.run_id
    result = {
        "schema_version": 1,
        "operation": "assemble-candidate",
        "run_id": args.run_id,
        "artifact_id": artifact["artifact_id"],
        "created_at": utc_now(),
        "state": "PLANNED",
        "candidate_dir": str(run_dir / "candidate"),
        "candidate_package": str(run_dir / "candidate.app.tar.gz"),
        "targets": targets,
        "mutations_performed": False,
        "bench_contacted": False,
    }
    if not args.execute:
        return result
    runs_root.mkdir(parents=True, exist_ok=True)
    partial = runs_root / f".{args.run_id}.partial-{os.getpid()}"
    if run_dir.exists() or partial.exists():
        raise ArtifactError(f"refusing to overwrite existing assembly run: {run_dir}")
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
            if not target.is_file():
                raise ArtifactError(f"extracted library target is missing: {relative}")
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
        with tarfile.open(candidate_package, "w:gz") as archive:
            for child in sorted(candidate.iterdir(), key=lambda item: item.name):
                archive.add(child, arcname=child.name, recursive=True)
        result["changes"] = changes
        result["candidate_sha256"] = sha256_file(candidate_package)
        result["state"] = "CANDIDATE_ASSEMBLED"
        result["mutations_performed"] = True
        write_json(partial / "candidate-manifest.json", result)
        os.replace(partial, run_dir)
    except Exception:
        if partial.exists():
            shutil.rmtree(partial)
        raise
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Verify release inputs and assemble a three-library candidate")
    actions = result.add_subparsers(dest="action", required=True)
    download = actions.add_parser("acquire", help="Download one APP or Lockfile directly to the workstation")
    download.add_argument("--artifact-id", required=True)
    download.add_argument("--url", required=True)
    download.add_argument("--filename")
    download.add_argument("--expected-sha256", required=True)
    download.add_argument("--netrc-file", type=Path)
    download.add_argument("--curl-binary", default="curl")
    download.add_argument("--cache-root", type=Path, default=Path.home() / "bench-eval" / "cache" / "downloads")
    download.add_argument("--execute", action="store_true")
    register = actions.add_parser("intake", help="Register App, Lockfile and the three approved libraries")
    register.add_argument("--artifact-id", required=True)
    register.add_argument("--app", required=True, type=Path)
    register.add_argument("--lock", required=True, type=Path)
    register.add_argument("--library", action="append", default=[])
    register.add_argument("--expected-app-sha256")
    register.add_argument("--expected-lock-sha256")
    register.add_argument("--cache-root", type=Path, default=Path.home() / "bench-eval" / "cache" / "assets")
    register.add_argument("--readelf-binary", default="readelf", help=argparse.SUPPRESS)
    register.add_argument("--execute", action="store_true")
    build = actions.add_parser("assemble", help="Create an isolated candidate package from a registered artifact")
    build.add_argument("--manifest", required=True, type=Path)
    build.add_argument("--run-id", required=True)
    build.add_argument("--runs-dir", type=Path, default=Path.home() / "bench-eval" / "runs")
    build.add_argument("--execute", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.action == "acquire":
            value = acquire(args)
        elif args.action == "intake":
            value = intake(args)
        else:
            value = assemble(args)
    except ArtifactError as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc), "mutations_performed": False}, ensure_ascii=False))
        return 2
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
