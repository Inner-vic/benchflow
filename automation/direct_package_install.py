#!/usr/bin/env python3
"""Direct APP overlay installer. Run the controller on the workstation.

The original archive and any replacement files are copied to /map once. The
bench worker unpacks once, then overlays only changed files while the existing
Runner bench.lock is held. Failed attempts and backups remain under their
immutable run ID. This tool does not replay scenes or switch Runner current.
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ROOT = Path("/home/bench-user/bench-eval")
BENCH_STAGE_ROOT = Path("/map/bench-eval/install-stage")
FORMAL_ROOT = Path("/app/livc")
SMCTL = ["/framework/bin/liware.tool.smctl"]
EXPECTED_STATUS = {"fusion": "active", "replay": "inactive", "selinux": "Enforcing", "mount": "clear"}


class InstallError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def relative_name(value: str) -> str:
    path = PurePosixPath(value)
    if not value or value.startswith("-") or "\\" in value or path.is_absolute() or ".." in path.parts:
        raise InstallError(f"unsafe relative target: {value!r}")
    cleaned = "/".join(part for part in path.parts if part not in ("", "."))
    if not cleaned:
        raise InstallError("empty relative target")
    return cleaned


def archive_members(package: Path) -> list[tarfile.TarInfo]:
    try:
        with tarfile.open(package, "r:gz") as archive:
            members = archive.getmembers()
    except (tarfile.TarError, OSError) as exc:
        raise InstallError(f"invalid APP archive: {exc}") from exc
    if not members:
        raise InstallError("APP archive is empty")
    seen: set[str] = set()
    symlink_paths: set[str] = set()
    for member in members:
        name = relative_name(member.name.rstrip("/"))
        parts = PurePosixPath(name).parts
        if parts[0] != "livc":
            raise InstallError(f"APP must have one livc/ root: {member.name!r}")
        if name in seen:
            raise InstallError(f"duplicate archive member: {name}")
        seen.add(name)
        if not (member.isdir() or member.isfile() or member.issym()):
            raise InstallError(f"unsupported archive member: {member.name!r}")
        if member.issym():
            link = member.linkname
            if not link or link.startswith("/") or "\\" in link:
                raise InstallError(f"unsafe archive link: {name} -> {link}")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), link))
            if resolved != "livc" and not resolved.startswith("livc/"):
                raise InstallError(f"archive link escapes livc: {name} -> {link}")
            symlink_paths.add(name)
    for name in seen:
        if any("/".join(PurePosixPath(name).parts[:count]) in symlink_paths
               for count in range(1, len(PurePosixPath(name).parts))):
            raise InstallError(f"archive member is below a symlink: {name}")
    return members


def extract_once(package: Path, destination: Path) -> None:
    members = archive_members(package)
    destination.mkdir(parents=True, exist_ok=False)
    directory_modes = {relative_name(member.name.rstrip("/")): member.mode & 0o777
                       for member in members if member.isdir()}
    with tarfile.open(package, "r:gz") as archive:
        for member in members:
            target = destination / relative_name(member.name.rstrip("/"))
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise InstallError(f"missing archive payload: {member.name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                target.chmod(member.mode & 0o777)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
    # mkdir obeys the worker's restrictive umask; the service user must be able
    # to traverse APP directories after a clean install.
    for parent, dirs, _ in os.walk(destination, followlinks=False):
        for name in dirs:
            path = Path(parent) / name
            if not path.is_symlink():
                relative = path.relative_to(destination).as_posix()
                path.chmod(directory_modes.get(relative, 0o755))


def all_entries(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir() or root.is_symlink():
        raise InstallError(f"candidate root is not a directory: {root}")
    result: list[tuple[str, Path]] = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        base = Path(parent)
        for name in sorted(dirs + files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            result.append((relative, path))
    return result


def kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_file():
        return "file"
    if path.is_dir():
        return "dir"
    if path.exists():
        raise InstallError(f"unsupported target type: {path}")
    return "absent"


def changed_entries(candidate: Path, formal: Path, *, force_overwrite: bool = False) -> list[str]:
    changes: list[str] = []
    for name, source in all_entries(candidate):
        target = formal / name
        source_kind, target_kind = kind(source), kind(target)
        if source_kind == "dir":
            if target_kind not in ("dir", "absent"):
                raise InstallError(f"directory conflicts with formal file: {name}")
            continue
        if target_kind == "dir":
            raise InstallError(f"file conflicts with formal directory: {name}")
        if force_overwrite:
            changes.append(name)
            continue
        if source_kind != target_kind:
            changes.append(name)
        elif source_kind == "symlink" and os.readlink(source) != os.readlink(target):
            changes.append(name)
        elif source_kind == "file" and (
            stat.S_IMODE(source.stat().st_mode) != stat.S_IMODE(target.stat().st_mode)
            or not filecmp.cmp(source, target, shallow=False)
        ):
            changes.append(name)
    return changes


def signature(path: Path) -> dict:
    entry_kind = kind(path)
    if entry_kind == "file":
        return {"kind": "file", "sha256": sha256(path), "mode": stat.S_IMODE(path.stat().st_mode)}
    if entry_kind == "symlink":
        return {"kind": "symlink", "target": os.readlink(path)}
    return {"kind": entry_kind}


def copy_entry(source: Path, target: Path, temporary_suffix: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + temporary_suffix)
    if kind(temporary) != "absent":
        raise InstallError(f"temporary install file already exists: {temporary}")
    try:
        if source.is_symlink():
            temporary.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, temporary)
        else:
            raise InstallError(f"unsupported source entry: {source}")
        os.replace(temporary, target)
    except BaseException:
        if kind(temporary) in ("file", "symlink"):
            temporary.unlink()
        raise


def check_parent_dirs(root: Path, name: str) -> None:
    current = root
    for part in PurePosixPath(name).parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise InstallError(f"unsafe formal parent: {current}")


def run_command(argv: list[str], *, env: dict | None = None) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
    if result.returncode:
        raise InstallError(f"command failed ({result.returncode}): {argv[0]} {result.stderr[-500:]}")
    return result.stdout.strip()


class BenchService:
    def __init__(self) -> None:
        self.env = {**os.environ, "LD_LIBRARY_PATH": "/framework/lib:/usr/lib"}

    def status(self) -> dict[str, str]:
        fusion = run_command([*SMCTL, "is-active", "infer_expert_fusion"], env=self.env)
        replay = run_command([*SMCTL, "is-active", "infer_expert_pcm_replay"], env=self.env)
        selinux = run_command(["getenforce"])
        mount = "mounted" if subprocess.run(["findmnt", "-M", str(FORMAL_ROOT)],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 else "clear"
        return {"fusion": fusion, "replay": replay, "selinux": selinux, "mount": mount}

    def stop(self) -> None:
        run_command([*SMCTL, "unloadsdf", "infer_expert_fusion"], env=self.env)
        for _ in range(30):
            if self.status()["fusion"] == "inactive":
                return
            time.sleep(1)
        raise InstallError("fusion did not stop")

    def start(self) -> None:
        load_output = run_command([*SMCTL, "loadsdf", "/app/livc/sdf/infer_expert_fusion.sdf"], env=self.env)
        for _ in range(30):
            if self.status()["fusion"] == "active":
                return
            time.sleep(1)
        raise InstallError(f"fusion did not start after loadsdf: {load_output[-500:]}")

    def writable(self) -> None:
        run_command(["/usr/sbin/setenforce", "0"])

    def enforcing(self) -> None:
        run_command(["/usr/sbin/setenforce", "1"])

    def relabel(self, root: Path, changes: list[str]) -> None:
        paths: set[Path] = set()
        for name in changes:
            if name == ".":
                paths.add(root)
                continue
            target = root / name
            if kind(target) != "absent":
                paths.add(target)
            parent = target.parent
            while parent != root:
                paths.add(parent)
                parent = parent.parent
        if paths:
            run_command(["restorecon", "-F", *(str(path) for path in sorted(paths))])


def prepare_worker(stage: Path, spec: dict) -> dict:
    package = stage / "incoming/package.app.tar.gz"
    candidate = stage / "unpacked"
    extract_once(package, candidate)
    root = candidate / "livc"
    for item in spec["replacements"]:
        name = relative_name(item["target"])
        source = stage / "incoming" / item["staged_name"]
        check_parent_dirs(root, name)
        target = root / name
        if target.is_dir():
            raise InstallError(f"replacement target is a directory: {name}")
        mode = stat.S_IMODE(target.stat().st_mode) if target.is_file() else stat.S_IMODE(source.stat().st_mode)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        shutil.copyfile(source, target)
        target.chmod(mode)
    receipt = {"state": "READY_TO_INSTALL", "run_id": spec["run_id"],
               "package_sha256": spec["package_sha256"], "replacements": spec["replacements"],
               "prepared_at": utc_now()}
    write_json(stage / "prepare.json", receipt)
    return receipt


def restore_formal(formal: Path, backup: Path, old: dict[str, dict], changes: list[str], service: BenchService) -> None:
    if service.status()["fusion"] == "active":
        service.stop()
    service.writable()
    for name in changes:
        target = formal / name
        if old[name]["kind"] == "absent":
            if kind(target) not in ("absent", "file", "symlink"):
                raise InstallError(f"cannot remove unexpected rollback target: {name}")
            if kind(target) != "absent":
                target.unlink()
        else:
            copy_entry(backup / name, target, ".restoring")
    service.relabel(formal, changes)
    service.enforcing()
    service.start()
    if {name: signature(formal / name) for name in changes} != old or service.status() != EXPECTED_STATUS:
        raise InstallError("rollback did not restore old files and service state")


def tree_signatures(root: Path) -> dict[str, dict]:
    result = {name: signature(path) for name, path in all_entries(root)}
    result["."] = {"kind": "dir", "mode": stat.S_IMODE(root.stat().st_mode)}
    for name, path in all_entries(root):
        if path.is_dir() and not path.is_symlink():
            result[name]["mode"] = stat.S_IMODE(path.stat().st_mode)
    return result


def activate_clean_worker(stage: Path, spec: dict, *, formal: Path = FORMAL_ROOT,
                          service: BenchService | None = None) -> dict:
    """Replace the formal tree from the prepared /map candidate, with full rollback."""
    service = service or BenchService()
    candidate = stage / "unpacked/livc"
    backup = stage / "backup-full/livc"
    stopped = False
    mutation_started = False
    try:
        if not (stage / "prepare.json").is_file():
            raise InstallError("prepare receipt is absent")
        if not candidate.is_dir() or candidate.is_symlink() or not formal.is_dir() or formal.is_symlink():
            raise InstallError("candidate or formal APP root is unsafe")
        if not (candidate / "sdf/infer_expert_fusion.sdf").is_file():
            raise InstallError("candidate lacks the required fusion SDF")
        baseline = service.status()
        if {key: baseline[key] for key in ("fusion", "replay", "mount")} != {
                key: EXPECTED_STATUS[key] for key in ("fusion", "replay", "mount")
        } or baseline["selinux"] not in ("Enforcing", "Permissive"):
            raise InstallError("formal service baseline differs")
        service.stop()
        stopped = True
        service.writable()
        backup.parent.mkdir(exist_ok=False)
        shutil.copytree(formal, backup, symlinks=True, copy_function=shutil.copy2)
        old = tree_signatures(backup)
        preserved = []
        for name in spec.get("preserve_from_formal", []):
            source = backup / name
            target = candidate / name
            check_parent_dirs(backup, name)
            check_parent_dirs(candidate, name)
            if target.exists() or target.is_symlink():
                continue
            if kind(source) not in ("file", "symlink"):
                raise InstallError(f"required formal control file is absent: {name}")
            copy_entry(source, target, ".preserving-" + spec["run_id"])
            preserved.append(name)
        # The installed service may write below APP directories. Preserve the
        # known-working directory permissions while removing obsolete files.
        for parent, dirs, _ in os.walk(candidate, followlinks=False):
            for name in dirs:
                path = Path(parent) / name
                relative = path.relative_to(candidate)
                old_path = backup / relative
                if not path.is_symlink() and old_path.is_dir() and not old_path.is_symlink():
                    path.chmod(stat.S_IMODE(old_path.stat().st_mode))
        candidate.chmod(stat.S_IMODE(backup.stat().st_mode))
        expected = tree_signatures(candidate)
        write_json(stage / "install-plan.json", {
            "mode": "clean-formal", "old_entries": len(old), "new_entries": len(expected),
            "backup": str(backup), "preserved_controls": preserved,
        })
        mutation_started = True
        shutil.rmtree(formal)
        shutil.copytree(candidate, formal, symlinks=True, copy_function=shutil.copy2)
        service.relabel(formal, list(expected))
        if tree_signatures(formal) != expected:
            raise InstallError("clean install final tree verification failed")
        service.enforcing()
        service.start()
        if service.status() != EXPECTED_STATUS:
            raise InstallError("clean install service verification failed")
        return {"state": "FORMAL_ACTIVATED_VERIFIED", "safe_to_release": True,
                "run_id": spec["run_id"], "mode": "clean-formal",
                "old_entries": len(old), "installed_entries": len(expected),
                "preserved_controls": preserved, "backup": str(backup), "completed_at": utc_now()}
    except BaseException as exc:
        receipt = {"state": "INSTALL_FAILED_PRESERVED", "safe_to_release": False,
                   "run_id": spec["run_id"], "mode": "clean-formal",
                   "error": str(exc), "backup": str(backup), "failed_at": utc_now()}
        try:
            if mutation_started:
                if service.status()["fusion"] == "active":
                    service.stop()
                service.writable()
                if formal.is_symlink():
                    raise InstallError("unexpected formal symlink blocks rollback")
                if formal.exists():
                    shutil.rmtree(formal)
                shutil.copytree(backup, formal, symlinks=True, copy_function=shutil.copy2)
                service.relabel(formal, list(tree_signatures(backup)))
                if tree_signatures(formal) != tree_signatures(backup):
                    raise InstallError("clean install rollback inventory differs")
            if stopped:
                service.enforcing()
                service.start()
            elif service.status()["selinux"] != "Enforcing":
                service.enforcing()
            if service.status() != EXPECTED_STATUS:
                raise InstallError("service restoration differs")
            receipt.update(state="OLD_FORMAL_RESTORED" if mutation_started else "NO_FORMAL_CHANGE",
                           safe_to_release=True)
        except BaseException as rollback_exc:
            receipt["rollback_error"] = str(rollback_exc)
        return receipt


def activate_worker(stage: Path, spec: dict, *, formal: Path = FORMAL_ROOT,
                    service: BenchService | None = None) -> dict:
    if spec.get("clean_formal"):
        return activate_clean_worker(stage, spec, formal=formal, service=service)
    service = service or BenchService()
    candidate = stage / "unpacked/livc"
    backup = stage / "backup"
    changes: list[str] = []
    old: dict[str, dict] = {}
    stopped = False
    try:
        if not (stage / "prepare.json").is_file():
            raise InstallError("prepare receipt is absent")
        baseline = service.status()
        if {key: baseline[key] for key in ("fusion", "replay", "mount")} != {key: EXPECTED_STATUS[key] for key in ("fusion", "replay", "mount")} or baseline["selinux"] not in ("Enforcing", "Permissive"):
            raise InstallError("formal service baseline differs")
        changes = changed_entries(candidate, formal, force_overwrite=bool(spec.get("force_overwrite")))
        old = {name: signature(formal / name) for name in changes}
        expected = {name: signature(candidate / name) for name in changes}
        backup.mkdir(exist_ok=False)
        for name in changes:
            check_parent_dirs(formal, name)
            if old[name]["kind"] != "absent":
                target = backup / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if old[name]["kind"] == "symlink":
                    target.symlink_to(os.readlink(formal / name))
                else:
                    shutil.copy2(formal / name, target)
        write_json(stage / "install-plan.json", {"changes": changes, "old": old, "expected": expected})
        service.stop()
        stopped = True
        service.writable()
        for name in changes:
            copy_entry(candidate / name, formal / name, ".installing-" + spec["run_id"])
        service.relabel(formal, changes)
        service.enforcing()
        service.start()
        observed = {name: signature(formal / name) for name in changes}
        if observed != expected or service.status() != EXPECTED_STATUS:
            raise InstallError("final file or service verification failed")
        receipt = {"state": "FORMAL_ACTIVATED_VERIFIED", "safe_to_release": True,
                   "run_id": spec["run_id"], "completed_at": utc_now(), "changed_files": changes,
                   "old": old, "installed": observed, "backup": str(backup)}
    except BaseException as exc:
        receipt = {"state": "INSTALL_FAILED_PRESERVED", "safe_to_release": False,
                   "run_id": spec["run_id"], "failed_at": utc_now(), "error": str(exc),
                   "changed_files": changes, "backup": str(backup)}
        try:
            if stopped:
                restore_formal(formal, backup, old, changes, service)
                receipt.update(state="OLD_FORMAL_RESTORED", safe_to_release=True)
            else:
                if service.status()["selinux"] != "Enforcing":
                    service.enforcing()
                if service.status() == EXPECTED_STATUS:
                    receipt.update(state="NO_FORMAL_CHANGE", safe_to_release=True)
        except BaseException as rollback_exc:
            receipt["rollback_error"] = str(rollback_exc)
    return receipt


def checked_run(argv: list[str], *, timeout: int = 1800, env: dict | None = None) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    if result.returncode:
        raise InstallError(f"command failed ({result.returncode}): {argv[0]} {result.stderr[-600:]}")
    return result.stdout.strip()


def parse_replacements(values: list[str]) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for value in values:
        name, separator, source = value.partition("=")
        name = relative_name(name)
        if not separator or name in seen:
            raise InstallError(f"invalid or duplicate replacement: {value!r}")
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise InstallError(f"replacement file is missing: {path}")
        seen.add(name)
        result.append((name, path))
    return result


def resolve_m100(args: argparse.Namespace) -> Path:
    if args.m100_so:
        if args.build_source or args.build_commit or args.build_container:
            raise InstallError("choose supplied --m100-so or build inputs, not both")
        path = args.m100_so.expanduser().resolve()
        if not path.is_file():
            raise InstallError(f"M100 SO is missing: {path}")
        return path
    if not (args.build_source and args.build_commit and args.build_container):
        raise InstallError("M100 SO not supplied; provide --build-source, --build-commit and --build-container")
    build_script = Path(__file__).with_name("build_uroad_eval.py")
    if not build_script.is_file():
        build_script = args.root / "current/automation/build_uroad_eval.py"
    command = [sys.executable, str(build_script),
               "--source", str(args.build_source), "--expected-commit", args.build_commit,
               "--artifact-id", args.run_id + "-m100", "--container", args.build_container,
               "--artifacts-root", "/mnt/data/bench-eval/builds",
               "--offline-lock",
               "--execute"]
    output = checked_run(command, timeout=3600)
    built = json.loads(output)
    if built.get("state") != "BUILT":
        raise InstallError("M100 build did not return BUILT")
    return Path(built["output"])


def controller(args: argparse.Namespace) -> dict:
    if not args.execute:
        raise InstallError("direct formal installation requires --execute")
    runtime_automation = args.root / "current/automation"
    if runtime_automation.is_dir():
        sys.path.insert(0, str(runtime_automation))
    started = time.monotonic()
    if not ID_RE.fullmatch(args.run_id):
        raise InstallError("invalid immutable run ID")
    if not args.package:
        raise InstallError("--package must be a fresh APP file transferred from the local PC")
    package = args.package.expanduser().resolve()
    if not package.is_file() or not package.name.endswith(".app.tar.gz"):
        raise InstallError("--package must be an existing .app.tar.gz file")
    if args.expected_sha256 and not SHA_RE.fullmatch(args.expected_sha256):
        raise InstallError("invalid expected APP SHA-256")
    package_hash = sha256(package)
    if args.expected_sha256 and package_hash != args.expected_sha256:
        raise InstallError("APP SHA-256 differs from expected identity")
    input_seconds = round(time.monotonic() - started, 3)
    replacements = parse_replacements(args.replace)
    if any(name == "lib/liburoad_m100.so" for name, _ in replacements):
        raise InstallError("pass M100 SO with --m100-so, not --replace")
    replacements += [("lib/liburoad_m100.so", resolve_m100(args))]
    run_dir = args.root / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    incoming = run_dir / "incoming-links"
    incoming.mkdir()
    (incoming / "package.app.tar.gz").symlink_to(package)
    staged_replacements = []
    for index, (name, path) in enumerate(replacements, start=1):
        staged_name = f"replacement-{index:03d}"
        (incoming / staged_name).symlink_to(path)
        staged_replacements.append({"target": name, "staged_name": staged_name, "sha256": sha256(path)})
    spec = {"run_id": args.run_id, "package_sha256": package_hash,
            "package_source": str(package), "replacements": staged_replacements,
            "force_overwrite": args.force_overwrite, "clean_formal": args.clean_formal,
            "preserve_from_formal": [relative_name(name) for name in args.preserve_from_formal],
            "created_at": utc_now()}
    if spec["preserve_from_formal"] and not args.clean_formal:
        raise InstallError("--preserve-from-formal requires --clean-formal")
    if args.source_url:
        parsed = urlsplit(args.source_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise InstallError("--source-url must be a credential-free HTTPS URL without query or fragment")
        spec["source_url"] = args.source_url
    write_json(run_dir / "input.json", spec)
    stage = BENCH_STAGE_ROOT / args.run_id
    from ssh_transport import command as ssh_command, environment as ssh_environment
    ssh_base = ssh_command("ssh", ["-F", str(args.root / "config/ssh_config")], args.bench_auth_mode)
    ssh = [*ssh_base, "bench-avm"]
    transport_env = ssh_environment(args.bench_auth_mode)
    lock = args.root / "locks/bench.lock"
    claimed = False
    activation_started = False
    try:
        payload = {"run_id": args.run_id, "purpose": "direct-package-install", "claimed_at": utc_now(), "pid": os.getpid()}
        descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        claimed = True
        locked_at = time.monotonic()
        checked_run(ssh + ["setenforce 0"], env=transport_env)
        checked_run(ssh + [f"test ! -e {shlex.quote(str(stage))} && test ! -L {shlex.quote(str(stage))} && install -d -m 0700 {shlex.quote(str(stage / 'incoming'))}"], env=transport_env)
        files = [str(incoming / "package.app.tar.gz"),
                 *(str(incoming / item["staged_name"]) for item in staged_replacements),
                 str(Path(__file__).resolve()), str(run_dir / "input.json")]
        checked_run(["rsync", "--archive", "--copy-links", "--partial", "-e", shlex.join(ssh_base),
                     *files, f"bench-avm:{stage / 'incoming'}/"], timeout=3600, env=transport_env)
        transfer_seconds = round(time.monotonic() - started - input_seconds, 3)
        worker = stage / "incoming/direct_package_install.py"
        remote_spec = stage / "incoming/input.json"
        prepared = json.loads(checked_run(ssh + [f"python3 {shlex.quote(str(worker))} --worker-prepare {shlex.quote(str(stage))} {shlex.quote(str(remote_spec))}"], timeout=3600, env=transport_env))
        write_json(run_dir / "prepare.json", prepared)
        if prepared.get("state") != "READY_TO_INSTALL":
            raise InstallError("bench preparation did not complete")
        prepare_seconds = round(time.monotonic() - started - input_seconds - transfer_seconds, 3)
        activation_started = True
        activated = json.loads(checked_run(ssh + [f"python3 {shlex.quote(str(worker))} --worker-activate {shlex.quote(str(stage))} {shlex.quote(str(remote_spec))}"], timeout=3600, env=transport_env))
        activated["timing_seconds"] = {"input": input_seconds, "transfer": transfer_seconds,
                                       "prepare": prepare_seconds,
                                       "locked_install": round(time.monotonic() - started - input_seconds - transfer_seconds - prepare_seconds, 3),
                                       "lock_held": round(time.monotonic() - locked_at, 3)}
        if not activated.get("safe_to_release"):
            write_json(run_dir / "activation.json", activated)
            raise InstallError("bench install or rollback unverified; bench lock retained")
        if activated.get("state") == "FORMAL_ACTIVATED_VERIFIED":
            try:
                quoted_stage = shlex.quote(str(stage))
                checked_run(ssh + [
                    "set -e; setenforce 0; trap 'setenforce 1' EXIT; "
                    f"test -d {quoted_stage} && test ! -L {quoted_stage} && "
                    f"rm -rf -- {quoted_stage} && test ! -e {quoted_stage}; "
                    "setenforce 1; trap - EXIT; test \"$(getenforce)\" = Enforcing"
                ], env=transport_env)
                activated["bench_stage_cleanup"] = "REMOVED_AFTER_VERIFIED_INSTALL"
            except BaseException as cleanup_exc:
                activated["bench_stage_cleanup"] = f"LEFT_FOR_MAINTENANCE: {cleanup_exc}"
        write_json(run_dir / "activation.json", activated)
        if json.loads(lock.read_text(encoding="utf-8")).get("run_id") != args.run_id:
            raise InstallError("bench lock ownership changed")
        lock.unlink()
        claimed = False
        return activated
    except BaseException as exc:
        if claimed and not activation_started:
            try:
                checked_run(ssh + ["setenforce 1"], env=transport_env)
                if checked_run(ssh + ["getenforce"], env=transport_env) != "Enforcing":
                    raise InstallError("SELinux did not return to Enforcing")
                if json.loads(lock.read_text(encoding="utf-8")).get("run_id") != args.run_id:
                    raise InstallError("bench lock ownership changed")
                lock.unlink()
                claimed = False
            except BaseException as restore_exc:
                exc = InstallError(f"{exc}; preparation recovery unverified: {restore_exc}")
        failure = {"run_id": args.run_id, "state": "CONTROLLER_FAILED", "error": str(exc),
                   "bench_lock_retained": claimed, "failed_at": utc_now()}
        write_json(run_dir / "failure.json", failure)
        raise InstallError(json.dumps(failure, ensure_ascii=False)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--worker-prepare", type=Path)
    mode.add_argument("--worker-activate", type=Path)
    mode.add_argument("--inspect-archive", type=Path)
    parser.add_argument("worker_spec", nargs="?", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--source-url", help="Provenance only; never fetched on the workstation")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--replace", action="append", default=[], metavar="RELATIVE_PATH=WORKSTATION_FILE")
    parser.add_argument("--m100-so", type=Path)
    parser.add_argument("--build-source", type=Path)
    parser.add_argument("--build-commit")
    parser.add_argument("--build-container")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--force-overwrite", action="store_true", help="Rewrite all APP files even when identical to formal files")
    parser.add_argument("--clean-formal", action="store_true", help="Replace the full formal APP tree, preserving a /map rollback copy")
    parser.add_argument("--preserve-from-formal", action="append", default=[], metavar="RELATIVE_PATH",
                        help="Keep one required control file absent from the APP, from the verified old formal tree")
    parser.add_argument("--bench-auth-mode", choices=("publickey", "keyboard-interactive-empty"),
                        default="keyboard-interactive-empty")
    args = parser.parse_args()
    try:
        if args.inspect_archive:
            members = archive_members(args.inspect_archive)
            result = {"state": "ARCHIVE_LAYOUT_SUPPORTED", "members": len(members),
                      "symlinks": sum(member.issym() for member in members)}
        elif args.worker_prepare or args.worker_activate:
            if args.worker_spec is None:
                raise InstallError("worker spec is required")
            spec = json.loads(args.worker_spec.read_text(encoding="utf-8"))
            stage = args.worker_prepare or args.worker_activate
            if stage != BENCH_STAGE_ROOT / spec["run_id"]:
                raise InstallError("worker stage path differs from immutable run ID")
            result = prepare_worker(stage, spec) if args.worker_prepare else activate_worker(stage, spec)
        else:
            if not args.run_id:
                raise InstallError("--run-id is required")
            result = controller(args)
        print(json.dumps(result, ensure_ascii=False))
        if result["state"] in ("ARCHIVE_LAYOUT_SUPPORTED", "READY_TO_INSTALL", "FORMAL_ACTIVATED_VERIFIED"):
            return 0
        return 0 if args.worker_activate and result.get("safe_to_release") else 2
    except (InstallError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
