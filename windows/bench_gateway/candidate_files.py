"""Immutable file inputs for an owner-specific replay binding.

Python 3.8, standard library only. Neither public function writes any file,
registers an asset, contacts a bench, or executes software from an input package.
The dataset declaration is frozen here; dataset_input must verify its authority
and actual files separately. The frozen record must stay in trusted job storage.
"""
import contextlib
import hashlib
import gzip
import json
import os
from pathlib import Path
import re
import signal
import stat
import tarfile
import threading
import time
import zlib

LIMITS = {"duration_seconds": 120, "max_package_bytes": 8 * 1024**3,
          "max_archive_bytes": 32 * 1024**3, "max_members": 100000,
          "max_metadata_bytes": 256 * 1024, "max_yaml_bytes": 1024 * 1024,
          "max_total_yaml_bytes": 8 * 1024 * 1024,
          "max_receipt_bytes": 1024 * 1024, "max_diff_bytes": 4 * 1024 * 1024}

class InputValidationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

def fail(code, message):
    raise InputValidationError(code, message)

class Budget:
    def __init__(self):
        self.started = time.monotonic()
        self.limits = dict(LIMITS)
    def check(self):
        if time.monotonic() - self.started > self.limits["duration_seconds"]:
            fail("TIME_LIMIT", "冻结输入核验达到时限，请稍后重试。")

@contextlib.contextmanager
def bounded():
    budget = Budget()
    previous_handler = previous_timer = None
    try:
        if hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread():
            def expired(_signum, _frame):
                fail("TIME_LIMIT", "冻结输入核验达到时限，请稍后重试。")
            previous_handler = signal.signal(signal.SIGALRM, expired)
            previous_timer = signal.getitimer(signal.ITIMER_REAL)
            delay = budget.limits["duration_seconds"]
            if previous_timer[0]:
                delay = min(delay, previous_timer[0])
            signal.setitimer(signal.ITIMER_REAL, delay)
        yield budget
    except InputValidationError:
        raise
    except (OSError, EOFError, RuntimeError, ValueError, TypeError, KeyError, RecursionError, tarfile.TarError, zlib.error):
        fail("INPUT_UNAVAILABLE", "冻结输入无法安全读取，请核对文件、收据和工作站权限。")
    finally:
        if previous_timer is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0]:
                signal.setitimer(signal.ITIMER_REAL, max(0.000001, previous_timer[0] - (time.monotonic() - budget.started)), previous_timer[1])

def relative(raw, allow_root=False):
    if (not isinstance(raw, str) or not raw or len(raw) > 4096 or raw.startswith("/")
            or "\\" in raw or "\x00" in raw or ":" in raw):
        fail("UNSAFE_TARGET", "APP 相对路径无效。")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in parts or (not parts and not allow_root):
        fail("UNSAFE_TARGET", "APP 相对路径不能越界。")
    return "/".join(parts)

def no_links(path):
    for part in list(reversed(path.parents)) + [path]:
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            fail("PATH_LINK", "本人目录、文件和任务目录不能经过符号链接。")


def checked_root(raw):
    if not isinstance(raw, str) or not Path(raw).is_absolute() or ".." in Path(raw).parts:
        fail("OWNER_PATH", "本人工作站根目录无效。")
    root = Path(raw)
    no_links(root)
    root = root.resolve(strict=True)
    if not root.is_dir():
        fail("OWNER_PATH", "本人工作站根目录不是目录。")
    return root


def fingerprint(stream):
    st = os.fstat(stream.fileno())
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


@contextlib.contextmanager
def directory_handle(path):
    """Pin each Linux path component; O_NOFOLLOW on the leaf alone is weaker."""
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
        no_links(path)
        yield None
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def open_owned(root, raw, size_limit, allow_empty=False):
    if not isinstance(raw, str):
        fail("OWNER_PATH", "材料必须提供本人工作站目录中的绝对路径。")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        fail("OWNER_PATH", "材料必须位于本人工作站目录。")
    no_links(path)
    path = path.resolve(strict=True)
    if root not in path.parents:
        fail("OWNER_PATH", "不能读取其他用户或本人目录之外的文件。")
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        fail("NOT_REGULAR_FILE", "材料必须是普通文件，不能是链接或特殊文件。")
    if not (0 <= st.st_size <= size_limit and (allow_empty or st.st_size > 0)):
        fail("FILE_SIZE_LIMIT", "材料为空或超过本次处理大小上限。")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with directory_handle(path.parent) as directory_fd:
        fd = os.open(str(path), flags) if directory_fd is None else os.open(path.name, flags, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        before = fingerprint(stream)
        if before[:3] != (st.st_dev, st.st_ino, st.st_size):
            fail("FILE_CHANGED", "读取时材料发生变化，请完成传输后再提交。")
        yield stream
        no_links(path)
        after = path.stat()
        if fingerprint(stream) != before or (after.st_dev, after.st_ino) != before[:2]:
            fail("FILE_CHANGED", "处理期间材料发生变化，本次候选不能使用。")


class ForwardReader:
    def __init__(self, stream, budget):
        self.stream, self.budget, self.position = stream, budget, 0

    def tell(self):
        return self.position

    def read(self, size=-1):
        self.budget.check()
        if size < 0 or size > 1024 * 1024:
            fail("ARCHIVE_READ_LIMIT", "软件包元数据读取超过上限。")
        remaining = max(0, self.budget.limits["max_archive_bytes"] - self.position)
        data = self.stream.read(min(size, remaining + 1))
        self.position += len(data)
        if self.position > self.budget.limits["max_archive_bytes"]:
            fail("ARCHIVE_SIZE_LIMIT", "软件包展开数据超过上限。")
        self.budget.check()
        return data

    def seek(self, offset, whence=0):
        target = offset if whence == 0 else self.position + offset if whence == 1 else -1
        if target < self.position:
            fail("ARCHIVE_INVALID", "软件包要求向后读取，无法完成有界检查。")
        while self.position < target:
            if not self.read(min(1024 * 1024, target - self.position)):
                fail("ARCHIVE_INVALID", "软件包不完整。")
        return self.position


def read_archive(stream, targets, yaml_targets, budget):
    entries, content, headers = {}, {}, [0]
    total_yaml = 0
    yaml_members = {"livc/" + target for target in yaml_targets}

    class BoundedInfo(tarfile.TarInfo):
        def _proc_member(self, archive):
            budget.check()
            headers[0] += 1
            if headers[0] > budget.limits["max_members"]:
                fail("ARCHIVE_MEMBER_LIMIT", "软件包条目数超过上限。")
            if self.size < 0 or self.size > budget.limits["max_archive_bytes"]:
                fail("ARCHIVE_SIZE_LIMIT", "软件包条目大小超过上限。")
            if self.type in (b"x", b"g", b"L", b"K", b"X") and self.size > budget.limits["max_metadata_bytes"]:
                fail("ARCHIVE_METADATA_LIMIT", "软件包扩展元数据超过上限。")
            if self.type == tarfile.GNUTYPE_SPARSE:
                fail("ARCHIVE_INVALID", "暂不支持包含稀疏文件的软件包。")
            return super()._proc_member(archive)

    stream.seek(0)
    with gzip.GzipFile(fileobj=stream, mode="rb") as compressed:
        reader = ForwardReader(compressed, budget)
        with tarfile.open(fileobj=reader, mode="r:", tarinfo=BoundedInfo) as archive:
            for member in archive:
                budget.check()
                name = relative(member.name, allow_root=member.isdir())
                if not name and member.isdir():
                    continue
                if (name == "livc" and not member.isdir()) or (name != "livc" and not name.startswith("livc/")):
                    fail("ARCHIVE_ROOT", "APP 软件包必须仅包含 livc 根目录；请核对软件包类型。")
                if member.sparse is not None or any(k.startswith("GNU.sparse") for k in member.pax_headers):
                    fail("ARCHIVE_INVALID", "暂不支持包含稀疏文件的软件包。")
                kind = "file" if member.isfile() else "dir" if member.isdir() else "link" if member.issym() or member.islnk() else None
                if not kind:
                    fail("ARCHIVE_SPECIAL_FILE", "软件包包含设备或其他特殊文件。")
                if name in entries and not kind == entries[name]["kind"] == "dir":
                    fail("ARCHIVE_DUPLICATE_PATH", "软件包存在重复路径，不能确定最终配置。")
                entry = {"kind": kind}
                if kind == "link":
                    # Unrelated safe links may occur in SO bundles, but they can
                    # never be a YAML target or an ancestor of a selected file.
                    link = member.linkname
                    if not link or link.startswith("/") or "\\" in link or "\x00" in link or ":" in link or len(link) > 4096:
                        fail("ARCHIVE_LINK", "软件包包含不安全链接。")
                    entry.update(link=link, symbolic=member.issym())
                entries[name] = entry
                if kind == "file" and name in yaml_members:
                    if member.size > budget.limits["max_yaml_bytes"]:
                        fail("YAML_SIZE_LIMIT", "目标 YAML 超过大小上限。")
                    total_yaml += member.size
                    if total_yaml > budget.limits["max_total_yaml_bytes"]:
                        fail("YAML_SIZE_LIMIT", "目标 YAML 总量超过大小上限。")
                    content[name] = reader.read(member.size)
                    if len(content[name]) != member.size:
                        fail("ARCHIVE_INVALID", "软件包 YAML 内容不完整。")
        while True:
            trailing = reader.read(1024 * 1024)
            if not trailing:
                break
            if any(trailing):
                fail("ARCHIVE_TRAILING_DATA", "软件包结束标记后存在非零内容。")

    if not any(entry["kind"] == "file" for entry in entries.values()):
        fail("ARCHIVE_EMPTY", "APP 软件包没有普通文件。")

    # Resolve every link without touching a filesystem. Fail closed on escapes,
    # loops, or parents that are not directories. Selected YAML never follows it.
    def resolve(name):
        pending, resolved, followed = name.split("/"), [], 0
        while pending:
            budget.check()
            part = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved:
                    fail("ARCHIVE_LINK", "软件包链接指向包外。")
                resolved.pop()
                continue
            resolved.append(part)
            entry = entries.get("/".join(resolved))
            if entry and entry["kind"] == "link":
                followed += 1
                if followed > 40:
                    fail("ARCHIVE_LINK", "软件包存在循环或过深链接。")
                resolved = resolved[:-1] if entry["symbolic"] else []
                pending = entry["link"].split("/") + pending
            elif entry and entry["kind"] == "file" and pending:
                fail("ARCHIVE_PATH_CONFLICT", "软件包把普通文件用作目录。")
        return "/".join(resolved)

    for name, entry in entries.items():
        if entry["kind"] == "link":
            # Links beneath other links are deliberately unsupported: their
            # effective locations require extractor-specific interpretation.
            if any(entries.get("/".join(name.split("/")[:i]), {}).get("kind") == "link"
                   for i in range(1, len(name.split("/")))):
                fail("ARCHIVE_LINK", "软件包存在嵌套链接，不能安全处理配置。")
            destination = resolve(name)
            if destination != "livc" and not destination.startswith("livc/"):
                fail("ARCHIVE_LINK", "软件包链接指向 livc 目录之外。")
    selected = {}
    for target in targets:
        matches = ["livc/" + target] if "livc/" + target in entries else []
        if len(matches) != 1:
            fail("TARGET_MISSING" if not matches else "TARGET_AMBIGUOUS", "包内找不到准确的替换目标。" if not matches else "包内存在同名 YAML；请提供完整且唯一的相对路径。")
        name = matches[0]
        parents = ["/".join(name.split("/")[:i]) for i in range(1, len(name.split("/")))]
        if entries[name]["kind"] != "file" or any(entries.get(p, {}).get("kind") in ("link", "file") for p in parents):
            fail("TARGET_LINK", "替换目标必须是包内普通文件，路径不能经过链接。")
        selected[target] = hashlib.sha256(content[name]).hexdigest() if target in yaml_targets else None
    return selected


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha_string(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        fail("HASH_INVALID", "材料或收据缺少有效的 SHA-256。")
    return value


def target_path(value, yaml_only=False):
    normalized = relative(value)
    if normalized != value or normalized == "livc" or normalized.startswith("livc/"):
        fail("UNSAFE_TARGET", "替换目标必须是规范的 APP 相对路径。")
    if yaml_only and not normalized.lower().endswith((".yaml", ".yml")):
        fail("YAML_TARGET", "YAML 候选只能替换 YAML 文件，不能覆盖 SO 或其他文件。")
    return normalized


def identity(asset, kind, owner=None):
    if not isinstance(asset, dict) or asset.get("kind") != kind:
        fail("ASSET_IDENTITY", "登记材料类型不匹配。")
    for key in ("id", "owner", "name"):
        if not isinstance(asset.get(key), str) or not 0 < len(asset[key]) <= 256 or "\x00" in asset[key]:
            fail("ASSET_IDENTITY", "登记材料身份字段无效。")
    if type(asset.get("version")) is not int or asset["version"] <= 0:
        fail("ASSET_IDENTITY", "登记材料版本无效。")
    if owner is not None and asset["owner"] != owner:
        fail("ASSET_OWNER", "不能绑定其他用户的软件包或依赖。")
    return {key: asset[key] for key in ("id", "owner", "kind", "name", "version")}


def read_file(root, path, expected, limit, budget, keep=False, allow_empty=False):
    if expected is not None:
        sha_string(expected)
    with open_owned(root, path, limit, allow_empty=allow_empty) as stream:
        size, count, chunks = os.fstat(stream.fileno()).st_size, 0, []
        digest = hashlib.sha256()
        while True:
            budget.check()
            block = stream.read(min(1024 * 1024, size - count + 1))
            if not block:
                break
            count += len(block)
            if count > size:
                fail("FILE_CHANGED", "材料读取期间增长，请完成传输后再绑定。")
            digest.update(block)
            if keep:
                chunks.append(block)
        actual = digest.hexdigest()
        if expected is not None and actual != expected:
            fail("HASH_MISMATCH", "材料实际字节与冻结的 SHA-256 不一致。")
    return {"path": str(Path(path).resolve(strict=True)), "sha256": actual}, b"".join(chunks)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("RECEIPT_INVALID", "收据存在重复字段。")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: fail("RECEIPT_INVALID", "收据包含非有限数字。"))
    except (UnicodeError, ValueError, RecursionError):
        fail("RECEIPT_INVALID", "收据不是有效的单一 JSON 文档。")


def validate_preparation_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        fail("PREPARATION_ID", "材料准备编号无效。")


def receipt_directory(root, preparation_id, raw):
    if not isinstance(raw, str):
        fail("RECEIPT_PATH", "YAML 收据路径无效。")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        fail("RECEIPT_PATH", "YAML 收据必须位于本人任务准备目录。")
    no_links(path)
    path = path.resolve(strict=True)
    expected = root / "preparations" / preparation_id / "yaml"
    if (path.name != "receipt.json" or path.parent.parent != expected
            or not re.fullmatch(r"[a-f0-9]{32}", path.parent.name)):
        fail("RECEIPT_PATH", "YAML 收据不属于指定任务的独立候选目录。")
    return path


def yaml_receipt(root, preparation_id, package_sha256, submitted, budget, expected_hash=None):
    path = receipt_directory(root, preparation_id, submitted.get("receipt_path"))
    descriptor, raw = read_file(root, str(path), expected_hash, budget.limits["max_receipt_bytes"], budget, keep=True)
    saved = strict_json(raw)
    if (not isinstance(saved, dict) or type(saved.get("schema_version")) is not int or saved["schema_version"] != 1
            or saved.get("scope") != "task-copy-only" or saved.get("preparation_id") != preparation_id
            or saved.get("package_sha256") != package_sha256):
        fail("RECEIPT_IDENTITY", "YAML 收据的软件包或任务身份不匹配。")
    # During freeze, compare the actual file with the complete preparation
    # result held in trusted local storage. During validate its pinned SHA is
    # the authority, and the output inventory is checked against frozen inputs.
    if expected_hash is None:
        if (submitted.get("status") != "PREPARED" or submitted.get("package_modified") is not False
                or submitted.get("bench_modified") is not False):
            fail("YAML_NOT_PREPARED", "YAML 准备记录未确认候选生成完成。")
        for key in ("schema_version", "scope", "preparation_id", "package_sha256", "changes", "outputs"):
            if canonical(submitted.get(key)) != canonical(saved.get(key)):
                fail("RECEIPT_MISMATCH", "实际 YAML 收据与材料准备记录不一致。")
    outputs, changes = saved.get("outputs"), saved.get("changes")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 32 or not isinstance(changes, list) or not 1 <= len(changes) <= 32:
        fail("RECEIPT_INVENTORY", "YAML 收据的修改项或产物清单无效。")
    replacements, before, locations = [], {}, {str(path)}
    for output in outputs:
        if not isinstance(output, dict):
            fail("RECEIPT_INVENTORY", "YAML 候选产物字段无效。")
        target = target_path(output.get("target"), yaml_only=True)
        if target in before:
            fail("TARGET_CONFLICT", "YAML 收据存在重复替换目标。")
        before[target] = sha_string(output.get("before_sha256"))
        actual_paths = {}
        for key, hash_key, limit in (("path", "sha256", "max_yaml_bytes"), ("diff_path", "diff_sha256", "max_diff_bytes")):
            candidate = output.get(key)
            if not isinstance(candidate, str) or not Path(candidate).is_absolute() or ".." in Path(candidate).parts:
                fail("OUTPUT_PATH", "YAML 候选或差异路径无效。")
            no_links(Path(candidate))
            actual_path = Path(candidate).resolve(strict=True)
            if actual_path.parent != path.parent or str(actual_path) in locations:
                fail("OUTPUT_PATH", "YAML 候选或差异必须是收据目录内独立文件。")
            locations.add(str(actual_path))
            descriptor_file, _ = read_file(root, str(actual_path), sha_string(output.get(hash_key)), budget.limits[limit], budget, allow_empty=(key == "diff_path"))
            actual_paths[key] = descriptor_file
        if type(output.get("size_bytes")) is not int or output["size_bytes"] != Path(actual_paths["path"]["path"]).stat().st_size:
            fail("OUTPUT_SIZE", "YAML 候选大小与收据不一致。")
        replacements.append({"target": target, **actual_paths["path"], "kind": "yaml"})
    covered, operations = set(), set()
    for change in changes:
        if not isinstance(change, dict) or change.get("target") not in before or change.get("operation") not in ("set", "replace"):
            fail("RECEIPT_CHANGES", "YAML 修改清单与候选目标不一致。")
        field = change.get("path")
        if not isinstance(field, list) or len(field) > 32 or any(not isinstance(p, str) or not p or len(p) > 256 for p in field):
            fail("RECEIPT_CHANGES", "YAML 修改字段路径无效。")
        if (change["operation"] == "set" and not field) or (change["operation"] == "replace" and field):
            fail("RECEIPT_CHANGES", "YAML 修改操作与字段路径不一致。")
        operation = (change["target"], tuple(field))
        if operation in operations:
            fail("RECEIPT_CHANGES", "YAML 修改清单存在重复或冲突操作。")
        operations.add(operation)
        covered.add(change["target"])
    if covered != set(before):
        fail("RECEIPT_CHANGES", "YAML 候选与修改目标清单不一致。")
    return descriptor, replacements, before


def dataset_identity(asset, owner):
    if not isinstance(asset, dict) or asset.get("kind") != "dataset" or not isinstance(asset.get("id"), str) or not asset["id"]:
        fail("DATASET_IDENTITY", "数据集登记身份无效。")
    if asset.get("owner") != owner and not (asset.get("owner") == "shared" and asset.get("origin") == "dataset_catalog"):
        fail("ASSET_OWNER", "不能绑定其他用户的私有数据集。")
    if not isinstance(asset.get("data"), dict) or not asset["data"] or asset.get("origin") not in (None, "dataset_catalog"):
        fail("DATASET_IDENTITY", "数据集声明格式或来源无效。")
    value = {key: asset.get(key) for key in ("id", "owner", "origin", "data")}
    # Bound metadata; no contents or remote dataset filesystem is read here.
    raw = canonical(value)
    if len(raw) > LIMITS["max_receipt_bytes"]:
        fail("DATASET_IDENTITY", "数据集声明超过允许大小。")
    return json.loads(raw)


def validate_inventory(replacements):
    if not isinstance(replacements, list) or len(replacements) > 64:
        fail("REPLACEMENT_INVENTORY", "替换清单无效或超过上限。")
    targets = set()
    for item in replacements:
        if not isinstance(item, dict) or item.get("kind") not in ("dependency", "yaml"):
            fail("REPLACEMENT_INVENTORY", "替换条目类型无效。")
        if set(item) != {"target", "path", "sha256", "kind"}:
            fail("REPLACEMENT_INVENTORY", "替换条目字段不完整或包含未知字段。")
        sha_string(item.get("sha256"))
        target = target_path(item.get("target"), yaml_only=item["kind"] == "yaml")
        if target in targets or any(target.startswith(previous + "/") or previous.startswith(target + "/") for previous in targets):
            fail("TARGET_CONFLICT", "替换项存在相同目标或父子路径冲突。")
        if item["kind"] == "dependency" and target.lower().endswith((".yaml", ".yml")):
            fail("DEPENDENCY_TARGET", "配置文件必须经过 YAML 收据流程，不能作为依赖库绕过。")
        targets.add(target)
    return targets


def check_package(root, package, replacements, before, budget):
    sha_string(package.get("sha256"))
    with open_owned(root, package.get("path"), budget.limits["max_package_bytes"]) as stream:
        size, count, digest = os.fstat(stream.fileno()).st_size, 0, hashlib.sha256()
        while True:
            budget.check()
            block = stream.read(min(1024 * 1024, size - count + 1))
            if not block:
                break
            count += len(block)
            if count > size:
                fail("FILE_CHANGED", "软件包读取期间增长。")
            digest.update(block)
        if digest.hexdigest() != package["sha256"]:
            fail("HASH_MISMATCH", "软件包内容与登记或冻结值不一致。")
        original = read_archive(stream, {item["target"] for item in replacements}, set(before), budget)
        if any(original.get(target) != expected for target, expected in before.items()):
            fail("YAML_BASE_MISMATCH", "YAML 候选的原文件 SHA-256 与当前软件包不一致。")


def freeze_inputs(owner_root, preparation_id, assets, yaml_preparation=None):
    """Validate byte identities and return a canonical, immutable input record."""
    with bounded() as budget:
        root = checked_root(owner_root)
        validate_preparation_id(preparation_id)
        if not isinstance(assets, dict):
            fail("ASSETS_INVALID", "材料清单无效。")
        package_asset = assets.get("package")
        package_identity = identity(package_asset, "package")
        owner = package_identity["owner"]
        package_data = package_asset.get("data")
        if not isinstance(package_data, dict):
            fail("PACKAGE_INVALID", "软件包材料字段无效。")
        package = {"path": package_data.get("remote_path"), "sha256": sha_string(package_data.get("sha256"))}
        dependencies = assets.get("dependencies")
        if not isinstance(dependencies, list) or len(dependencies) > 32:
            fail("DEPENDENCIES_INVALID", "依赖清单无效或超过上限。")
        replacements, dependency_identities = [], []
        seen_ids = {package_identity["id"]}
        for asset in dependencies:
            item_identity = identity(asset, "dependency", owner)
            if item_identity["id"] in seen_ids:
                fail("ASSET_IDENTITY", "材料清单包含重复资产编号。")
            seen_ids.add(item_identity["id"])
            data = asset.get("data")
            if not isinstance(data, dict):
                fail("DEPENDENCY_INVALID", "依赖字段无效。")
            target = target_path(data.get("target"))
            descriptor, _ = read_file(root, data.get("remote_path"), sha_string(data.get("sha256")), budget.limits["max_package_bytes"], budget)
            replacements.append({"target": target, **descriptor, "kind": "dependency"})
            dependency_identities.append({**item_identity, "target": target})
        identity(assets.get("dataset"), "dataset")
        dataset = dataset_identity(assets.get("dataset"), owner)
        receipt, before = None, {}
        if yaml_preparation:
            submitted = yaml_preparation
            if isinstance(submitted, dict) and "yaml_preparation" in submitted:
                if submitted.get("status") != "STATIC_PASSED":
                    fail("YAML_NOT_PREPARED", "整体材料准备尚未静态通过。")
                submitted = submitted["yaml_preparation"]
            if not isinstance(submitted, dict):
                fail("YAML_NOT_PREPARED", "YAML 准备记录格式无效。")
            if submitted:
                receipt, yaml_replacements, before = yaml_receipt(root, preparation_id, package["sha256"], submitted, budget)
                replacements.extend(yaml_replacements)
        validate_inventory(replacements)
        check_package(root, package, replacements, before, budget)
        package["path"] = str(Path(package["path"]).resolve(strict=True))
        frozen = {"schema_version": 1, "owner": owner, "preparation_id": preparation_id,
                  "package": package, "replacements": sorted(replacements, key=lambda item: item["target"]),
                  "yaml_receipt": receipt, "dataset_identity": dataset,
                  "asset_identities": {"package": package_identity, "dependencies": sorted(dependency_identities, key=lambda item: item["target"])}}
        frozen["input_digest"] = hashlib.sha256(canonical(frozen)).hexdigest()
        budget.check()
        return frozen


def validate_inputs(owner_root, frozen):
    """Recheck a trusted frozen record and every source byte before execution."""
    with bounded() as budget:
        root = checked_root(owner_root)
        if not isinstance(frozen, dict):
            fail("FROZEN_INVALID", "冻结输入记录无效。")
        if set(frozen) != {"schema_version", "owner", "preparation_id", "package", "replacements", "yaml_receipt", "dataset_identity", "asset_identities", "input_digest"}:
            fail("FROZEN_INVALID", "冻结输入记录字段不完整或包含未知字段。")
        expected = sha_string(frozen.get("input_digest"))
        current = {key: value for key, value in frozen.items() if key != "input_digest"}
        if hashlib.sha256(canonical(current)).hexdigest() != expected:
            fail("INPUT_DIGEST_MISMATCH", "冻结输入记录已变化。")
        if type(frozen.get("schema_version")) is not int or frozen["schema_version"] != 1:
            fail("FROZEN_INVALID", "冻结输入版本无效。")
        validate_preparation_id(frozen.get("preparation_id"))
        metadata = frozen.get("asset_identities")
        if not isinstance(metadata, dict):
            fail("FROZEN_INVALID", "冻结输入缺少资产身份。")
        package_identity = identity(metadata.get("package"), "package", frozen.get("owner"))
        if package_identity["owner"] != frozen.get("owner"):
            fail("ASSET_OWNER", "冻结输入所属用户不一致。")
        dataset = frozen.get("dataset_identity")
        if not isinstance(dataset, dict) or set(dataset) != {"id", "owner", "origin", "data"}:
            fail("DATASET_IDENTITY", "冻结数据源声明无效。")
        dataset_identity({"kind": "dataset", **dataset}, frozen["owner"])
        replacements = frozen.get("replacements")
        validate_inventory(replacements)
        dependencies = [item for item in replacements if item["kind"] == "dependency"]
        identities = metadata.get("dependencies")
        if not isinstance(identities, list) or len(identities) > 32 or len(identities) != len(dependencies):
            fail("ASSET_IDENTITY", "冻结依赖身份与文件清单不一致。")
        targets, ids = set(), {package_identity["id"]}
        for item in identities:
            data = identity(item, "dependency", frozen["owner"])
            target = target_path(item.get("target"))
            if target in targets or data["id"] in ids:
                fail("ASSET_IDENTITY", "冻结材料身份重复。")
            targets.add(target)
            ids.add(data["id"])
        if targets != {item["target"] for item in dependencies}:
            fail("ASSET_IDENTITY", "冻结依赖身份目标与文件清单不一致。")
        for item in dependencies:
            descriptor, _ = read_file(root, item.get("path"), item.get("sha256"), budget.limits["max_package_bytes"], budget)
            if descriptor["path"] != item["path"]:
                fail("FROZEN_PATH", "冻结文件路径不是规范路径。")
        receipt, before = frozen.get("yaml_receipt"), {}
        yaml_items = [item for item in replacements if item["kind"] == "yaml"]
        if receipt is None:
            if yaml_items:
                fail("RECEIPT_MISSING", "YAML 替换缺少对应收据。")
        else:
            if not isinstance(receipt, dict) or set(receipt) != {"path", "sha256"}:
                fail("RECEIPT_INVALID", "冻结 YAML 收据字段无效。")
            _, actual_yaml, before = yaml_receipt(root, frozen["preparation_id"], frozen["package"]["sha256"], {"receipt_path": receipt["path"]}, budget, sha_string(receipt["sha256"]))
            if sorted(actual_yaml, key=lambda item: item["target"]) != sorted(yaml_items, key=lambda item: item["target"]):
                fail("RECEIPT_MISMATCH", "冻结 YAML 替换清单与持久收据不一致。")
        if not isinstance(frozen.get("package"), dict) or set(frozen["package"]) != {"path", "sha256"}:
            fail("PACKAGE_INVALID", "冻结软件包字段无效。")
        check_package(root, frozen["package"], replacements, before, budget)
        if str(Path(frozen["package"]["path"]).resolve(strict=True)) != frozen["package"]["path"]:
            fail("FROZEN_PATH", "冻结软件包路径不是规范路径。")
        budget.check()


