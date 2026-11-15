"""Prepare bounded, private YAML candidates without modifying a package or bench.

Standalone Python 3.8 source: ``python3 - <base64-json-request>``. The deployment
must supply PyYAML on its fixed Python path; a request cannot select import paths.
PREPARED certifies candidate files and a receipt, never a live configuration.
"""
import base64
import contextlib
import difflib
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import tarfile
import time
import threading
import uuid
import zlib


LIMITS = {
    "duration_seconds": 120, "max_package_bytes": 8 * 1024**3,
    "max_archive_bytes": 32 * 1024**3, "max_members": 100000,
    "max_metadata_bytes": 256 * 1024, "max_yaml_bytes": 1024 * 1024,
    "max_total_yaml_bytes": 8 * 1024 * 1024, "max_nodes": 50000,
    "max_depth": 50, "max_operations": 32,
}


class PreparationError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def fail(code, message):
    raise PreparationError(code, message)


class Budget:
    def __init__(self, supplied):
        self.limits = dict(LIMITS)
        if not isinstance(supplied, dict) or set(supplied) - set(LIMITS):
            fail("INVALID_LIMITS", "YAML 准备上限配置无效。")
        for key, value in supplied.items():
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= LIMITS[key]:
                fail("INVALID_LIMITS", "YAML 准备上限只能在默认范围内收紧。")
            if key != "duration_seconds" and type(value) is not int:
                fail("INVALID_LIMITS", "文件和结构上限必须为整数。")
            self.limits[key] = value
        self.started = time.monotonic()

    def check(self):
        if time.monotonic() - self.started > self.limits["duration_seconds"]:
            fail("TIME_LIMIT", "YAML 准备达到时限，已停止；请核对文件后再提交。")


def relative(raw, allow_root=False):
    if (not isinstance(raw, str) or not raw or len(raw) > 4096 or
            raw.startswith("/") or "\\" in raw or "\x00" in raw or ":" in raw):
        fail("UNSAFE_PATH", "请提供安全的 APP 内相对 YAML 路径。")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in parts or (not parts and not allow_root):
        fail("UNSAFE_PATH", "YAML 路径不能越界或为空。")
    return "/".join(parts)


def no_links(path):
    for part in list(reversed(path.parents)) + [path]:
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            fail("PATH_LINK", "本人目录、文件和任务目录不能经过符号链接。")


def owner_root(raw):
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
def open_owned(root, raw, size_limit):
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
    if not 0 < st.st_size <= size_limit:
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


def read_archive(stream, targets, budget):
    entries, content, headers = {}, {}, [0]
    total_yaml = 0

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
                if kind == "file" and any(name == t or name.endswith("/" + t) for t in targets):
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
        matches = [name for name in entries if name == target or name.endswith("/" + target)]
        if len(matches) != 1:
            fail("TARGET_MISSING" if not matches else "TARGET_AMBIGUOUS", "包内找不到 YAML 目标。" if not matches else "包内存在同名 YAML；请提供完整且唯一的相对路径。")
        name = matches[0]
        parents = ["/".join(name.split("/")[:i]) for i in range(1, len(name.split("/")))]
        if entries[name]["kind"] != "file" or any(entries.get(p, {}).get("kind") in ("link", "file") for p in parents):
            fail("TARGET_LINK", "目标 YAML 必须是包内普通文件，路径不能经过链接。")
        selected[target] = (name, content[name])
    return selected


def validate_tree(value, budget):
    queue, count = [(value, 0)], 0
    while queue:
        current, depth = queue.pop()
        count += 1
        budget.check()
        if depth > budget.limits["max_depth"] or count > budget.limits["max_nodes"]:
            fail("YAML_COMPLEXITY", "YAML 或修改值的层级、节点数超过上限。")
        kind = type(current)
        if kind is dict:
            if any(type(k) is not str for k in current):
                fail("YAML_KEY_TYPE", "YAML 映射字段名必须是字符串。")
            queue.extend((child, depth + 1) for child in current.values())
        elif kind is list:
            queue.extend((child, depth + 1) for child in current)
        elif kind not in (str, bool, int, float, type(None)) or (kind is float and not math.isfinite(current)):
            fail("YAML_VALUE_TYPE", "YAML 只支持普通字符串、有限数字、布尔、空值、列表和映射。")


def load_yaml(raw, yaml, budget):
    if len(raw) > budget.limits["max_yaml_bytes"]:
        fail("YAML_SIZE_LIMIT", "YAML 超过大小上限。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        fail("YAML_ENCODING", "YAML 必须使用 UTF-8 编码。")
    if "\x00" in text:
        fail("YAML_INVALID", "YAML 包含非法字符。")

    class StrictLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if type(key) is not str:
                    fail("YAML_KEY_TYPE", "YAML 映射字段名必须是字符串。")
                if key in result:
                    fail("YAML_DUPLICATE_KEY", "YAML 有重复字段，不能可靠确定修改结果。")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        documents, nodes, depth = 0, 0, 0
        for event in yaml.parse(text, Loader=StrictLoader):
            budget.check()
            nodes += 1
            if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None) is not None:
                fail("YAML_ALIAS", "YAML 锚点、别名和合并引用暂不支持，请提供展开后的配置。")
            if getattr(event, "tag", None) is not None:
                fail("YAML_TAG", "YAML 显式类型标签暂不支持。")
            if isinstance(event, yaml.events.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    fail("YAML_MULTIDOC", "只能处理单文档 YAML。")
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
            if nodes > budget.limits["max_nodes"] or depth > budget.limits["max_depth"]:
                fail("YAML_COMPLEXITY", "YAML 层级或节点数超过上限。")
        value = yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError:
        fail("YAML_INVALID", "YAML 语法无效；请核对文件，未输出原始内容。")
    validate_tree(value, budget)
    if type(value) not in (dict, list):
        fail("YAML_ROOT_TYPE", "配置根节点必须是映射或列表。")
    return value


def same_structure(before, after, budget):
    pending = [(before, after)]
    while pending:
        budget.check()
        old, new = pending.pop()
        if type(old) is not type(new):
            fail("VALUE_TYPE_MISMATCH", "修改值的类型与原字段不一致。")
        if type(old) is dict:
            if set(old) != set(new):
                fail("FIELD_STRUCTURE", "字段更新不能新增或删除映射字段；请提供完整替换文件。")
            pending.extend((old[k], new[k]) for k in old)
        elif type(old) is list:
            if len(old) != len(new):
                fail("FIELD_STRUCTURE", "字段更新不能改变列表长度；请提供完整替换文件。")
            pending.extend(zip(old, new))


def json_value(raw, budget):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > budget.limits["max_yaml_bytes"]:
        fail("VALUE_INVALID", "修改值必须是大小受限的 JSON 值。")
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                fail("VALUE_INVALID", "修改值有重复字段。")
            value[key] = child
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: fail("VALUE_INVALID", "修改值不能是非有限数字。"))
    except (ValueError, RecursionError):
        fail("VALUE_INVALID", "修改值不是有效的 JSON。")
    validate_tree(value, budget)
    return value


def set_value(document, path, value, budget):
    current = document
    for index, key in enumerate(path):
        budget.check()
        if type(current) is dict and key in current:
            slot = key
        elif type(current) is list and re.fullmatch(r"0|[1-9][0-9]{0,8}", key) and int(key) < len(current):
            slot = int(key)
        else:
            fail("FIELD_MISSING", "原 YAML 中找不到指定字段或列表索引，请提供准确路径。")
        if index == len(path) - 1:
            same_structure(current[slot], value, budget)
            current[slot] = value
        else:
            current = current[slot]


def mkdir_owned(root, parts):
    current = root
    for part in parts:
        current = current / part
        with directory_handle(current.parent) as directory_fd:
            try:
                if directory_fd is None:
                    current.mkdir(mode=0o700)
                else:
                    os.mkdir(current.name, mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
        no_links(current)
        if not current.is_dir() or root not in current.resolve(strict=True).parents:
            fail("OUTPUT_PATH", "任务输出目录无效。")
    return current


def write_new(root, path, data):
    no_links(path.parent)
    if root not in path.parent.resolve(strict=True).parents:
        fail("OUTPUT_PATH", "任务输出不能越出本人目录。")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with directory_handle(path.parent) as directory_fd:
        fd = os.open(str(path), flags, 0o600) if directory_fd is None else os.open(path.name, flags, 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    no_links(path)
    if data:
        with open_owned(root, str(path), len(data)) as verification:
            if verification.read(len(data) + 1) != data:
                fail("OUTPUT_VERIFY", "任务文件写入后校验失败。")
    elif path.stat().st_size != 0:
        fail("OUTPUT_VERIFY", "任务文件写入后校验失败。")


def semantic_digest(raw,yaml,budget,overrides):
    document=load_yaml(raw,yaml,budget)
    for field,value in overrides.items():
        node=document;parts=field.split('.')
        for part in parts[:-1]:
            if not isinstance(node,dict) or part not in node:node=None;break
            node=node[part]
        if isinstance(node,dict) and parts[-1] in node:node[parts[-1]]=value
    return hashlib.sha256(json.dumps(document,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def run(request):
    result = {"schema_version": 1, "status": "NEEDS_INPUT", "checked_at": time.time(),
              "scope": "task-copy-only", "package_modified": False, "bench_modified": False,
              "changes": [], "outputs": [], "issues": []}
    budget, previous_handler, previous_timer = None, None, None
    try:
        if not isinstance(request, dict):
            fail("INVALID_REQUEST", "YAML 准备请求无效。")
        budget = Budget(request.get("limits", {}))
        # Remote execution is a standalone Linux process. An alarm also bounds
        # parser/diff CPU and blocking reads between cooperative budget checks.
        if hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread():
            def expired(_signum, _frame):
                fail("TIME_LIMIT", "YAML 准备达到时限，已停止；请核对文件后再提交。")
            previous_handler = signal.signal(signal.SIGALRM, expired)
            previous_timer = signal.setitimer(signal.ITIMER_REAL, budget.limits["duration_seconds"])
        try:
            import yaml
        except ImportError:
            fail("YAML_DEPENDENCY_MISSING", "工作站未部署受控 PyYAML 依赖，请管理员完成部署。")
        root = owner_root(request.get("owner_root"))
        preparation_id = request.get("preparation_id")
        if not isinstance(preparation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", preparation_id):
            fail("PREPARATION_ID_INVALID", "任务准备编号无效。")
        package = request.get("package")
        if not isinstance(package, dict) or not isinstance(package.get("sha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", package["sha256"]):
            fail("PACKAGE_INVALID", "软件包需要本人目录路径和登记的 SHA-256。")
        plan = request.get("plan")
        operations = plan.get("operations") if isinstance(plan, dict) else None
        if not isinstance(operations, list) or not 0 < len(operations) <= budget.limits["max_operations"]:
            fail("PLAN_INVALID", "请提供数量受限的明确 YAML 修改操作。")
        normalized = []
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("operation") not in ("set", "replace"):
                fail("PLAN_INVALID", "YAML 只支持已有字段更新或本人文件替换。")
            target = relative(operation.get("target"))
            if target.startswith("livc/"):
                target = target[len("livc/"):]
            if not target.lower().endswith((".yaml", ".yml")):
                fail("TARGET_EXTENSION", "目标必须是 YAML 文件。")
            path = operation.get("path", [])
            if operation["operation"] == "set" and (not isinstance(path, list) or not 0 < len(path) <= 32 or any(not isinstance(k, str) or not k or len(k) > 256 for k in path)):
                fail("FIELD_PATH", "请提供已有字段的逐级路径。")
            if operation["operation"] == "replace" and path:
                fail("PLAN_INVALID", "整份替换不能同时指定字段路径。")
            normalized.append(dict(operation, target=target, path=path))

        with open_owned(root, package.get("remote_path"), budget.limits["max_package_bytes"]) as stream:
            expected_size, hashed = os.fstat(stream.fileno()).st_size, 0
            digest = hashlib.sha256()
            while True:
                budget.check()
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                hashed += len(chunk)
                if hashed > expected_size:
                    fail("FILE_CHANGED", "软件包读取期间增长，已停止。")
                digest.update(chunk)
            if digest.hexdigest() != package["sha256"].lower():
                fail("HASH_MISMATCH", "软件包 SHA-256 与登记值不一致。")
            selected = read_archive(stream, {op["target"] for op in normalized}, budget)

        originals, documents, final_bytes, changes = {}, {}, {}, []
        for operation in normalized:
            budget.check()
            member_name, original = selected[operation["target"]]
            target = member_name[len("livc/"):]
            if target not in originals:
                originals[target] = original
                documents[target] = load_yaml(original, yaml, budget)
                final_bytes[target] = original
            if operation["operation"] == "replace":
                with open_owned(root, operation.get("source"), budget.limits["max_yaml_bytes"]) as source:
                    replacement = source.read(budget.limits["max_yaml_bytes"] + 1)
                documents[target] = load_yaml(replacement, yaml, budget)
                final_bytes[target] = replacement
            else:
                value = json_value(operation.get("value_json"), budget)
                set_value(documents[target], operation["path"], value, budget)
                final_bytes[target] = yaml.safe_dump(documents[target], allow_unicode=True, sort_keys=False, default_flow_style=False).encode("utf-8")
                # Reparse rendered output under the same limits, so serializing
                # cannot silently produce unsupported data or a larger file.
                load_yaml(final_bytes[target], yaml, budget)
            changes.append({"target": target, "operation": operation["operation"], "path": operation["path"]})
        budget.check()
        batch = uuid.uuid4().hex
        directory = mkdir_owned(root, ["preparations", preparation_id, "yaml", batch])
        outputs = []
        for number, target in enumerate(originals):
            budget.check()
            # Filenames are generated, not user controlled; original APP path
            # remains metadata for later explicit adapter binding.
            candidate = directory / ("candidate-%02d.yaml" % number)
            diff_path = directory / ("candidate-%02d.diff" % number)
            after, before = final_bytes[target], originals[target]
            diff = "".join(difflib.unified_diff(before.decode("utf-8-sig").splitlines(keepends=True), after.decode("utf-8-sig").splitlines(keepends=True), fromfile="original/" + target, tofile="candidate/" + target)).encode("utf-8")
            write_new(root, candidate, after)
            write_new(root, diff_path, diff)
            overrides=(request.get('semantic_overrides') or {}).get(target,{})
            semantic=lambda raw:semantic_digest(raw,yaml,budget,overrides)
            outputs.append({"target": target, "path": str(candidate), "sha256": hashlib.sha256(after).hexdigest(), "before_sha256": hashlib.sha256(before).hexdigest(),
                            "semantic_sha256":semantic(after),"before_semantic_sha256":semantic(before),
                            "diff_path": str(diff_path), "diff_sha256": hashlib.sha256(diff).hexdigest(), "size_bytes": len(after)})
        receipt = {"schema_version": 1, "preparation_id": preparation_id, "package_sha256": package["sha256"].lower(), "scope": "task-copy-only", "changes": changes, "outputs": outputs, "created_at": time.time()}
        receipt_path = directory / "receipt.json"
        budget.check()
        write_new(root, receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2).encode("utf-8"))
        result.update(status="PREPARED", changes=changes, outputs=outputs, receipt_path=str(receipt_path), package_sha256=package["sha256"].lower(), preparation_id=preparation_id)
    except PreparationError as exc:
        result["issues"].append({"field": "yaml_request", "code": exc.code, "message": exc.message, "next_action": "请修正本次 YAML 请求或提供正确的本人配置文件；不会自动反复重试。"})
    except (OSError, ValueError, RuntimeError, EOFError, tarfile.TarError, zlib.error, RecursionError, UnicodeError):
        result["issues"].append({"field": "yaml_request", "code": "YAML_PREPARATION_FAILED", "message": "无法安全读取材料或生成任务副本；未修改原包或台架。", "next_action": "请核对文件格式、本人路径和工作站权限后再提交。"})
    finally:
        if previous_timer is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0]:
                remaining = max(0.000001, previous_timer[0] - (time.monotonic() - budget.started))
                signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])
    if budget:
        result["duration_seconds"] = round(time.monotonic() - budget.started, 3)
    return result


if __name__ == "__main__":
    try:
        supplied = json.loads(base64.b64decode(sys.argv[1], validate=True))
    except (IndexError, ValueError, UnicodeError):
        supplied = None
    print(json.dumps(run(supplied), ensure_ascii=False))
