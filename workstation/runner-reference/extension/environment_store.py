"""Workstation-only candidate slots and immutable, content-verified releases."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import uuid


class EnvironmentError(ValueError):
    pass


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", value):
        raise EnvironmentError("invalid identity")
    return value


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    with temp.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def relative(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value or str(p) == ".":
        raise EnvironmentError("unsafe relative path: " + value)
    return p.as_posix()


def copy_file(source, target):
    source, target = Path(source), Path(target)
    if source.is_symlink() or not source.is_file():
        raise EnvironmentError("source must be a regular file: " + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)  # Never hardlink mutable drafts to releases.
    if sha(source) != sha(target):
        raise EnvironmentError("source changed during snapshot: " + str(source))


def inventory(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise EnvironmentError("symlink in frozen snapshot")
        if path.is_file() and path != Path(root) / "manifest.json":
            result[path.relative_to(root).as_posix()] = {
                "sha256": sha(path), "mode": path.stat().st_mode & 0o777}
    return result


def seal(root, metadata):
    manifest = {**metadata, "files": inventory(root)}
    write(Path(root) / "manifest.json", manifest)
    return sha(Path(root) / "manifest.json")


def verify(root, expected):
    root = Path(root)
    if root.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        raise EnvironmentError("invalid frozen snapshot binding")
    if sha(root / "manifest.json") != expected:
        raise EnvironmentError("frozen manifest hash changed")
    manifest = read(root / "manifest.json")
    if inventory(root) != manifest["files"]:
        raise EnvironmentError("frozen snapshot files changed")
    return manifest


@contextlib.contextmanager
def locked(root):
    import fcntl
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".registry.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def limit(root, actor):
    path = Path(root) / "limits.json"
    cfg = read(path) if path.exists() else {"default": 1, "users": {}}
    return cfg.get("users", {}).get(actor, cfg.get("default", 1))


def set_limit(root, actor, maximum):
    identity(actor)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise EnvironmentError("limit must be a positive integer")
    with locked(root):
        slots = list((Path(root) / "users" / actor / "slots").glob("*/slot.json"))
        if len(slots) > maximum:
            raise EnvironmentError("remove unused slots explicitly before lowering limit: " +
                                   ", ".join(p.parent.name for p in slots))
        path = Path(root) / "limits.json"
        cfg = read(path) if path.exists() else {"schema_version": 1, "default": 1, "users": {}}
        cfg["users"][actor] = maximum
        write(path, cfg)
    return {"actor": actor, "limit": maximum}


def create_slot(root, actor, slot):
    identity(actor); identity(slot)
    with locked(root):
        base = Path(root) / "users" / actor / "slots"
        path = base / slot
        if path.exists():
            raise EnvironmentError("slot already exists")
        if len(list(base.glob("*/slot.json"))) >= limit(root, actor):
            raise EnvironmentError("slot limit reached; choose an existing slot or raise the limit")
        write(path / "slot.json", {"actor": actor, "slot": slot, "verified": None})
    return read(path / "slot.json")


def slot_path(root, actor, slot):
    path = Path(root) / "users" / identity(actor) / "slots" / identity(slot)
    if not (path / "slot.json").is_file() or path.is_symlink():
        raise EnvironmentError("slot does not exist")
    return path


def prepare(root, actor, slot, package, replacements=None, replace_draft=False):
    """Keep an original archive plus incremental overlays; do not touch the bench."""
    with locked(root):
        base = slot_path(root, actor, slot)
        draft = base / "draft"
        if draft.exists() and not replace_draft:
            raise EnvironmentError("draft exists; use update or explicitly replace the draft")
        temp = base / (".draft-" + uuid.uuid4().hex)
        temp.mkdir()
        copy_file(package, temp / "original.app.tar.gz")
        overlays = {}
        for target, source in (replacements or {}).items():
            target = relative(target)
            copy_file(source, temp / "overlays" / target)
            overlays[target] = sha(temp / "overlays" / target)
        write(temp / "draft.json", {"actor": actor, "slot": slot,
                                   "package_sha256": sha(temp / "original.app.tar.gz"),
                                   "overlays": overlays})
        # Preserve the previous draft for explicit cleanup; never silently delete inputs.
        if draft.exists():
            os.replace(draft, base / ("retired-draft-" + uuid.uuid4().hex))
        os.replace(temp, draft)
    return read(draft / "draft.json")


def update(root, actor, slot, target, source):
    target = relative(target)
    with locked(root):
        draft = slot_path(root, actor, slot) / "draft"
        meta = read(draft / "draft.json")
        temp = draft / (".overlay-" + uuid.uuid4().hex)
        copy_file(source, temp)
        dest = draft / "overlays" / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp, dest)
        meta["overlays"][target] = sha(dest)
        write(draft / "draft.json", meta)
    return meta


def assemble(archive_path, overlays, output):
    """Safe extraction and overlay in a temporary workstation directory."""
    with tempfile.TemporaryDirectory(dir=str(Path(output).parent)) as directory:
        tree = Path(directory)
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names = set()
            for m in members:
                name = relative(m.name.rstrip("/"))
                if not name.startswith("livc/") and name != "livc":
                    raise EnvironmentError("APP must have a single livc root")
                if name in names:
                    raise EnvironmentError("duplicate archive member")
                names.add(name)
                if not (m.isdir() or m.isfile() or m.issym()):
                    raise EnvironmentError("unsupported archive member")
                if m.issym():
                    link = PurePosixPath(m.linkname)
                    if link.is_absolute() or "\\" in m.linkname:
                        raise EnvironmentError("unsafe archive symlink")
                    resolved = (tree / name).parent.joinpath(m.linkname).resolve()
                    app_root = (tree / 'livc').resolve()
                    if resolved != app_root and app_root not in resolved.parents:
                        raise EnvironmentError("archive symlink escapes root")
            for m in members:
                target = tree / relative(m.name.rstrip("/"))
                for parent in target.parents:
                    if parent == tree: break
                    if parent.is_symlink():
                        raise EnvironmentError("archive member beneath symlink")
                target.parent.mkdir(parents=True, exist_ok=True)
                if m.isdir():
                    target.mkdir(exist_ok=True)
                elif m.isfile():
                    with archive.extractfile(m) as src, target.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                    target.chmod(m.mode & 0o777)
                else:
                    target.symlink_to(m.linkname)
            for m in reversed(members):
                if m.isdir(): (tree / m.name).chmod(m.mode & 0o777)
        for target_name, source in overlays.items():
            target = tree / "livc" / relative(target_name)
            if any(p.is_symlink() for p in target.parents if p != tree):
                raise EnvironmentError("overlay parent is a symlink")
            if target.is_symlink(): target.unlink()
            mode = target.stat().st_mode & 0o777 if target.is_file() else 0o644
            copy_file(source, target)
            target.chmod(mode)
        with tarfile.open(output, "w:gz", compresslevel=1) as archive:
            archive.add(tree / "livc", arcname="livc")


def freeze(root, actor, slot, version, evidence):
    identity(version)
    with locked(root):
        base = slot_path(root, actor, slot)
        draft = base / "draft"
        meta = read(draft / "draft.json")
        if sha(draft / "original.app.tar.gz") != meta["package_sha256"]:
            raise EnvironmentError("draft original changed")
        overlays = {k: draft / "overlays" / relative(k) for k in meta["overlays"]}
        if any(sha(p) != meta["overlays"][k] for k, p in overlays.items()):
            raise EnvironmentError("draft overlay changed")
        path = base / "versions" / version
        if path.exists(): raise EnvironmentError("version already exists")
        temp = path.parent / (".partial-" + uuid.uuid4().hex)
        temp.mkdir(parents=True)
        assemble(draft / "original.app.tar.gz", overlays, temp / "candidate.app.tar.gz")
        with tarfile.open(temp / "candidate.app.tar.gz", 'r:gz') as archive:
            for name in ('liburoad_m100.so', 'libxcamera.so', 'libxlidar.so'):
                try: member = archive.getmember('livc/lib/' + name)
                except KeyError: continue
                if not member.isfile(): raise EnvironmentError('key SO must be a regular file')
                target = temp / 'libraries' / name
                target.parent.mkdir(exist_ok=True)
                with archive.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
        copy_file(evidence, temp / "compatibility.json")
        review = read(temp / "compatibility.json")
        if (review.get("decision") != "REVIEWED_FOR_VALIDATION"
                or review.get("original_sha256") != meta["package_sha256"]
                or review.get("overlays") != meta["overlays"]):
            raise EnvironmentError("explicit compatibility review required; static symbols alone are insufficient")
        fingerprint = seal(temp, {"schema_version": 1, "actor": actor, "slot": slot,
                                 "version": version, "state": "FROZEN_UNVERIFIED",
                                 "original_sha256": meta["package_sha256"], "overlays": meta["overlays"]})
        os.replace(temp, path)
        # No verified pointer is updated here.
    return {"actor": actor, "slot": slot, "version": version,
            "path": str(path), "manifest_sha256": fingerprint, "state": "FROZEN_UNVERIFIED"}


def status(root, actor):
    identity(actor)
    base = Path(root) / "users" / actor / "slots"
    slots = []
    for path in sorted(base.glob('*/slot.json')):
        record = read(path)
        draft = path.parent / 'draft/draft.json'
        record['draft'] = read(draft) if draft.exists() else None
        record['versions'] = [{'version': p.parent.name, 'manifest_sha256': sha(p),
                               'current_verified': (record.get('verified') or {}).get('version') == p.parent.name}
                              for p in sorted((path.parent/'versions').glob('*/manifest.json'))]
        slots.append(record)
    return {"actor": actor, "limit": limit(root, actor), "slot_count": len(slots), "slots": slots}
