"""Bounded, read-only workstation material inspection; standard library only.

The caller resolves private registry assets and supplies the owner's root. This
module never downloads, extracts, builds, executes package code, or contacts a
bench. PASS means these static checks passed, not runtime/ABI compatibility.
The entire source can be sent to ``python3 - <base64-json-request>`` over SSH.
"""
import base64
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import time
import zlib

try:
    from . import abi_symbols
except ImportError:
    # The remote caller injects this fixed administrator-owned source before
    # the standalone inspector. User requests cannot supply module code.
    abi_symbols = sys.modules.get('bench_gateway_abi_symbols')


DEFAULT_LIMITS = {
    "duration_seconds": 120,
    "max_file_bytes": 8 * 1024**3,
    "max_archive_expanded_bytes": 32 * 1024**3,
    "max_member_bytes": 8 * 1024**3,
    "max_members": 100000,
    "max_metadata_bytes": 256 * 1024,
}
FIELDS = ("package", "camera_so", "lidar_so", "m100_so")
TARGET_ELF = {"bits": 64, "byte_order": "little", "machine": 183, "type": "ET_DYN"}
NOT_CHECKED = [
    "完整动态符号、ABI 和所需运行库兼容性（仅另行检查限定的相机/雷达 provider 符号）",
    "相机/雷达回放端点及软件运行兼容性",
    "M100 编译、YAML 修改、台架连接和小场景预检",
]


class InspectionError(Exception):
    def __init__(self, code, message, next_action):
        self.code, self.message, self.next_action = code, message, next_action
        super().__init__(message)


def _fail(code, message, next_action):
    raise InspectionError(code, message, next_action)


def _path_parts(value, allow_root=False):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or "\\" in value or "\x00" in value or value.startswith("/")):
        _fail("UNSAFE_PATH", "路径不是安全的 APP 内相对路径。", "请提供正确的软件包或 APP 内相对目标路径。")
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if ".." in parts or (not parts and not allow_root):
        _fail("UNSAFE_PATH", "路径包含越界或空目标。", "请提供正确的软件包或 APP 内相对目标路径。")
    return "/".join(parts)


def _elf(header):
    if len(header) < 64 or header[:4] != b"\x7fELF" or header[4] not in (1, 2) or header[5] not in (1, 2) or header[6] != 1:
        _fail("INVALID_ELF", "SO 文件没有有效的 ELF 头。", "请提供为本次软件包编译的完整共享库文件。")
    order = "little" if header[5] == 1 else "big"
    kind = int.from_bytes(header[16:18], order)
    if kind != 3:
        _fail("NOT_SHARED_LIBRARY", "ELF 类型不是共享对象（ET_DYN）。", "请提供共享库成品 SO，不能使用源码或未链接的目标文件。")
    machine = int.from_bytes(header[18:20], order)
    if not machine:
        _fail("INVALID_ELF", "ELF 没有有效的机器架构。", "请提供完整共享库文件。")
    return {"bits": 32 if header[4] == 1 else 64, "byte_order": order,
            "machine": machine, "type": "ET_DYN"}


def _target_elf(header):
    evidence = _elf(header)
    if evidence != TARGET_ELF:
        _fail("ELF_TARGET_MISMATCH", "SO 不是本台架要求的 AArch64 ELF64 小端共享对象。",
              "请提供 AArch64 ELF64 小端的软件包和对应依赖；架构一致仍不能代替 ABI 和本次小场景预检。")
    return evidence


class _Budget:
    def __init__(self, limits):
        self.limits = dict(DEFAULT_LIMITS)
        if not isinstance(limits, dict) or set(limits) - set(self.limits):
            _fail("INVALID_LIMITS", "检查上限配置无效。", "请管理员检查材料检查器配置。")
        for key, value in limits.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= self.limits[key]:
                _fail("INVALID_LIMITS", "检查上限只能在默认范围内收紧。", "请管理员检查材料检查器配置。")
            self.limits[key] = value
        self.started = time.monotonic()

    def check(self):
        if time.monotonic() - self.started > self.limits["duration_seconds"]:
            _fail("TIME_LIMIT", "材料静态检查达到时限，已停止。", "请确认文件大小和工作站负载，或由管理员离线检查；不会反复自动重试。")


class _ForwardReader:
    """Forward-only decompression with byte/time bounds, without tar stream caches."""
    def __init__(self, source, budget):
        self.source, self.budget, self.position = source, budget, 0

    def tell(self):
        return self.position

    def read(self, size=-1):
        self.budget.check()
        if size < 0 or size > 1024 * 1024:
            _fail("ARCHIVE_READ_LIMIT", "包内元数据单次读取超过安全上限。", "请检查压缩包格式，或提供可检查的 APP 包。")
        remaining = self.budget.limits["max_archive_expanded_bytes"] - self.position
        data = self.source.read(min(size, int(max(remaining, 0)) + 1))
        self.position += len(data)
        if self.position > self.budget.limits["max_archive_expanded_bytes"]:
            _fail("ARCHIVE_SIZE_LIMIT", "软件包展开数据超过检查上限。", "请检查 APP 包内容或由管理员核实合理的大小上限。")
        self.budget.check()
        return data

    def seek(self, offset, whence=0):
        target = offset if whence == 0 else self.position + offset if whence == 1 else -1
        if target < self.position:
            _fail("ARCHIVE_SEEK", "软件包要求向后读取，不能完成有界检查。", "请提供标准 tar.gz APP 包。")
        while self.position < target:
            if not self.read(min(1024 * 1024, target - self.position)):
                _fail("ARCHIVE_TRUNCATED", "软件包内容不完整。", "请重新提供完整 APP 软件包并核对 SHA-256。")
        return self.position


def _resolved_member(path, entries, budget):
    """Resolve links as an extractor would, including links in parent directories."""
    pending, result, followed = path.split("/"), [], 0
    while pending:
        budget.check()
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not result:
                _fail("ARCHIVE_LINK_ESCAPE", "APP 包含逃逸到包外的链接。", "请修复软件包链接后重新提供。")
            result.pop()
            continue
        result.append(part)
        entry = entries.get("/".join(result))
        if entry and entry["kind"] in ("symlink", "hardlink"):
            followed += 1
            if followed > 40:
                _fail("ARCHIVE_LINK_LOOP", "APP 包含循环或过深链接。", "请修复软件包链接后重新提供。")
            result = result[:-1] if entry["kind"] == "symlink" else []
            pending = entry["link"].split("/") + pending
    return "/".join(result)


def _archive(stream, budget):
    limits, entries, headers_seen, declared_bytes = budget.limits, {}, [0], 0

    class BoundedTarInfo(tarfile.TarInfo):
        def _proc_member(self, archive):
            budget.check()
            headers_seen[0] += 1
            if headers_seen[0] > limits["max_members"]:
                _fail("ARCHIVE_MEMBER_LIMIT", "软件包条目数超过检查上限。", "请检查软件包内容，或由管理员离线核验。")
            if self.size < 0 or self.size > limits["max_member_bytes"]:
                _fail("ARCHIVE_MEMBER_SIZE_LIMIT", "软件包单个条目大小超过检查上限。", "请检查 APP 包内容。")
            if self.type in (b"x", b"g", b"L", b"K", b"X") and self.size > limits["max_metadata_bytes"]:
                _fail("ARCHIVE_METADATA_LIMIT", "软件包扩展元数据超过检查上限。", "请提供标准 tar.gz APP 包。")
            if self.type == tarfile.GNUTYPE_SPARSE:
                _fail("ARCHIVE_SPARSE", "暂不支持 APP 包中的稀疏文件。", "请提供不包含稀疏文件的 APP 包。")
            return super()._proc_member(archive)

    stream.seek(0)
    with gzip.GzipFile(fileobj=stream, mode="rb") as compressed:
        reader = _ForwardReader(compressed, budget)
        with tarfile.open(fileobj=reader, mode="r:", tarinfo=BoundedTarInfo) as archive:
            for member in archive:
                budget.check()
                name = _path_parts(member.name, allow_root=member.isdir())
                if not name and member.isdir():
                    continue  # Conventional './' root directory is legal.
                if member.sparse is not None or any(k.startswith("GNU.sparse") for k in member.pax_headers):
                    _fail("ARCHIVE_SPARSE", "暂不支持 APP 包中的稀疏文件。", "请提供不包含稀疏文件的 APP 包。")
                if member.size < 0 or member.size > limits["max_member_bytes"]:
                    _fail("ARCHIVE_MEMBER_SIZE_LIMIT", "软件包单个条目大小超过检查上限。", "请检查 APP 包内容。")
                kind = "file" if member.isfile() else "directory" if member.isdir() else "symlink" if member.issym() else "hardlink" if member.islnk() else None
                if kind is None:
                    _fail("ARCHIVE_SPECIAL_FILE", "APP 包包含设备、FIFO 或不支持的特殊条目。", "请移除特殊条目并重新提供 APP 包。")
                if name in entries and not (kind == entries[name]["kind"] == "directory"):
                    _fail("ARCHIVE_DUPLICATE_PATH", "APP 包中有重复目标路径，不能安全确定最终文件。", "请提供不含重复文件路径的软件包。")
                entry = {"kind": kind, "size": member.size}
                if kind in ("symlink", "hardlink"):
                    link = member.linkname
                    if not link or len(link) > 4096 or link.startswith("/") or "\\" in link or "\x00" in link:
                        _fail("ARCHIVE_LINK_ESCAPE", "APP 包含绝对或无效链接。", "请提供只指向包内的相对链接。")
                    entry["link"] = link
                if kind == "file":
                    declared_bytes += member.size
                    if declared_bytes > limits["max_archive_expanded_bytes"]:
                        _fail("ARCHIVE_SIZE_LIMIT", "软件包展开文件总量超过检查上限。", "请检查 APP 包内容或由管理员离线核验。")
                    # All headers stay small; link targets may use a different filename.
                    entry["header"] = reader.read(min(64, member.size))
                entries[name] = entry
        # Validate gzip CRC/truncation and reject hidden archives after tar's EOF.
        while True:
            trailing = reader.read(1024 * 1024)
            if not trailing:
                break
            if any(trailing):
                _fail("ARCHIVE_TRAILING_DATA", "APP 包结束标记后存在非零内容。", "请提供单一、完整的 tar.gz APP 软件包。")
        expanded_bytes = reader.position
    if not entries:
        _fail("ARCHIVE_EMPTY", "APP 软件包没有可检查的内容。", "请提供完整 APP 软件包。")
    # A link declared as a/b lives at b if a -> '.'. Resolve its actual
    # location before evaluating b's own target; lexical normalization alone
    # would incorrectly accept a/b -> '../outside' in that layout.
    link_index = dict(entries)
    for _ in range(40):
        changed = False
        for name, entry in entries.items():
            if entry["kind"] not in ("symlink", "hardlink"):
                continue
            parent, _, leaf = name.rpartition("/")
            resolved_parent = _resolved_member(parent, link_index, budget) if parent else ""
            actual_name = (resolved_parent + "/" if resolved_parent else "") + leaf
            if actual_name in link_index:
                if link_index[actual_name] is not entry:
                    _fail("ARCHIVE_DUPLICATE_PATH", "APP 包内链接导致多个条目落到同一路径。", "请消除包内路径冲突后重新提供。")
            else:
                link_index[actual_name] = entry
                changed = True
        if not changed:
            break
    else:
        _fail("ARCHIVE_LINK_LOOP", "APP 包含循环或过深链接。", "请修复软件包链接后重新提供。")
    actual_files = {}
    for name, entry in entries.items():
        resolved = _resolved_member(name, link_index, budget)
        entry["resolved"] = resolved
        if entry["kind"] == "file":
            if resolved in actual_files:
                _fail("ARCHIVE_DUPLICATE_PATH", "APP 包内链接导致多个文件落到同一路径。", "请消除包内路径冲突后重新提供。")
            actual_files[resolved] = entry
    for entry in entries.values():
        if entry["kind"] == "hardlink" and entry["resolved"] not in actual_files:
            _fail("ARCHIVE_DANGLING_HARDLINK", "APP 包内硬链接没有对应普通文件。", "请修复硬链接后重新提供软件包。")
    return entries, actual_files, {"members": len(entries), "headers": headers_seen[0],
                                   "declared_file_bytes": declared_bytes,
                                   "expanded_bytes_read": expanded_bytes}


def _fingerprint(stream):
    info = os.fstat(stream.fileno())
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _archive_cached(stream,budget,root,package_hash,version):
    """Cache decompression only; live file hashes/ELF/provider checks remain mandatory."""
    if not isinstance(version,str) or not re.fullmatch('[a-f0-9]{64}',version):
        return _archive(stream,budget), 'DISABLED'
    version=hashlib.sha256((version+json.dumps(budget.limits,sort_keys=True)).encode()).hexdigest()
    directory=root/'.material-cache';path=directory/(package_hash+'-'+version+'.json')
    def encode(value):
        if isinstance(value,bytes):return {'_bytes':base64.b64encode(value).decode()}
        raise TypeError()
    def decode(value):
        return base64.b64decode(value['_bytes'],validate=True) if set(value)=={'_bytes'} else value
    def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),default=encode)
    try:
        if not directory.is_symlink() and path.is_file() and not path.is_symlink() and path.stat().st_size<=16*1024**2:
            saved=json.loads(path.read_text(encoding='utf-8'),object_hook=decode)
            if (saved['version']==version and saved['package_sha256']==package_hash
                    and hashlib.sha256(canonical(saved['value']).encode()).hexdigest()==saved['value_sha256']):
                return tuple(saved['value']), 'HIT'
    except (OSError,ValueError,KeyError,TypeError):pass
    value=_archive(stream,budget)
    try:
        if directory.is_symlink():return value,'MISS'
        directory.mkdir(exist_ok=True)
        if directory.resolve()!=directory:return value,'MISS'
        saved={'version':version,'package_sha256':package_hash,'value':value,
               'value_sha256':hashlib.sha256(canonical(value).encode()).hexdigest()}
        payload=canonical(saved)
        if len(payload.encode())<=16*1024**2:
            import uuid
            temp=directory/('.'+uuid.uuid4().hex)
            with temp.open('x',encoding='utf-8') as f:f.write(payload)
            os.chmod(temp,0o600);os.replace(temp,path)
    except OSError:pass  # A cache write failure cannot invalidate the real inspection.
    return value,'MISS'


def inspect(request):
    result = {"schema_version": 1, "status": "NEEDS_INPUT", "checked_at": time.time(),
              "evidence": {"scope": "static-only", "mutations_performed": False, "assets": {},
                           "not_checked": list(NOT_CHECKED), "target_elf": dict(TARGET_ELF)}, "checks": [], "issues": []}

    def issue(field, error):
        result["issues"].append({"field": field, "code": error.code,
                                 "message": error.message, "next_action": error.next_action})
        result["checks"].append({"field": field, "check": error.code, "status": "FAIL", "detail": error.message})

    def passed(field, check, detail):
        result["checks"].append({"field": field, "check": check, "status": "PASS", "detail": detail})

    try:
        if not isinstance(request, dict):
            _fail("INVALID_REQUEST", "材料检查请求格式无效。", "请重新提供本人的已登记材料。")
        budget = _Budget(request.get("limits", {}))
        raw_root = request.get("owner_root")
        if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
            _fail("OWNER_ROOT_INVALID", "本人资产根目录无效。", "请管理员检查本人工作站资产目录配置。")
        root = Path(raw_root)
        if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
            _fail("OWNER_ROOT_INVALID", "本人资产根目录不能是符号链接。", "请管理员修复本人资产目录。")
        root = root.resolve(strict=True)
        if not root.is_dir():
            _fail("OWNER_ROOT_INVALID", "本人资产根目录不存在或不是目录。", "请先登记本人工作站资产。")
    except InspectionError as exc:
        issue("request", exc)
        return result
    except (OSError, ValueError, RuntimeError):
        issue("request", InspectionError("OWNER_ROOT_INVALID", "无法访问本人资产根目录。", "请检查工作站目录权限后重试。"))
        return result

    handles, fingerprints, targets = {}, {}, {}
    with contextlib.ExitStack() as stack:
        for field in FIELDS:
            asset = request.get(field)
            if not asset:
                code = "M100_BUILD_REQUIRED" if field == "m100_so" else "PACKAGE_REQUIRED" if field == "package" else "DEPENDENCY_REQUIRED"
                message = "未提供 M100 成品 SO；编译配方尚需准备或核验。" if field == "m100_so" else "未提供这项已登记材料。"
                action = "请提供成品 liburoad_m100.so，或由管理员接入锁定源码和环境的构建配方；本检查不会编译。" if field == "m100_so" else "请提供本人已登记软件包或依赖文件。"
                issue(field, InspectionError(code, message, action))
                continue
            try:
                budget.check()
                if not isinstance(asset, dict) or not isinstance(asset.get("remote_path"), str) or not isinstance(asset.get("sha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", asset["sha256"]):
                    _fail("ASSET_INVALID", "材料缺少有效的工作站路径或 SHA-256。", "请重新登记本人材料。")
                path = Path(asset["remote_path"])
                if not path.is_absolute() or ".." in path.parts:
                    _fail("OWNER_PATH", "材料路径不在本人资产目录。", "请使用本人工作站目录内已登记的材料。")
                path = path.resolve(strict=True)
                if root not in path.parents:
                    _fail("OWNER_PATH", "材料路径不在本人资产目录，不能读取。", "请使用本人工作站目录内已登记的材料。")
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    _fail("NOT_REGULAR_FILE", "材料不是普通文件。", "请提供完整普通文件，不能使用目录或特殊设备。")
                if not 0 < info.st_size <= budget.limits["max_file_bytes"]:
                    _fail("FILE_SIZE_LIMIT", "材料为空或超过单文件检查上限。", "请检查材料大小并提供完整文件。")
                stream = stack.enter_context(path.open("rb"))
                before = _fingerprint(stream)
                if before[:3] != (info.st_dev, info.st_ino, info.st_size):
                    _fail("FILE_CHANGED", "打开材料时内容发生变化。", "请停止修改该文件并重新登记。")
                passed(field, "owner_path", "已核验文件实际路径属于本人目录。")
                hasher = hashlib.sha256()
                hashed_bytes = 0
                while True:
                    budget.check()
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    hashed_bytes += len(chunk)
                    if hashed_bytes > before[2]:
                        _fail("FILE_CHANGED", "检查期间材料增长，已停止读取。", "请停止写入该文件，完成传输后再重新登记。")
                    hasher.update(chunk)
                if _fingerprint(stream) != before:
                    _fail("FILE_CHANGED", "检查期间材料发生变化。", "请停止修改该文件并重新登记。")
                actual = hasher.hexdigest()
                if actual != asset["sha256"].lower():
                    _fail("HASH_MISMATCH", "材料内容与登记的 SHA-256 不一致。", "请核对正确版本并重新登记，不使用变化后的文件。")
                passed(field, "sha256", "文件 SHA-256 与登记值一致。")
                result["evidence"]["assets"][field] = {"remote_path": str(path), "sha256": actual, "size_bytes": before[2]}
                handles[field], fingerprints[field] = stream, before
                if field != "package":
                    stream.seek(0)
                    result["evidence"]["assets"][field]["elf"] = _target_elf(stream.read(64))
                    passed(field, "elf_header", "已识别 AArch64 ELF64 小端共享对象头；尚未验证 ABI。")
                    targets[field] = _path_parts(asset.get("target"))
            except InspectionError as exc:
                issue(field, exc)
            except (OSError, ValueError, RuntimeError):
                issue(field, InspectionError("FILE_UNAVAILABLE", "无法读取已登记材料文件。", "请检查文件是否存在及工作站访问权限。"))

        entries, actual_files = None, None
        if "package" in handles:
            try:
                (entries, actual_files, archive_evidence),cache_state = _archive_cached(handles['package'],budget,root,
                    result['evidence']['assets']['package']['sha256'],request.get('cache_version'))
                result['evidence']['archive_cache']=cache_state
                result['evidence']['cache_scope']='owner metadata only; software and bench unmodified'
                result["evidence"]["archive"] = archive_evidence
                passed("package", "archive_safety", "APP 压缩包路径、链接、文件类型及资源上限检查通过；未解包。")
            except InspectionError as exc:
                issue("package", exc)
            except (OSError, EOFError, ValueError, tarfile.TarError, RecursionError, zlib.error):
                issue("package", InspectionError("ARCHIVE_INVALID", "APP tar.gz 损坏、不完整或格式无法检查。", "请提供标准、完整的 APP tar.gz 并核对来源。"))

        if entries is not None:
            for field, target in targets.items():
                try:
                    budget.check()
                    matches = [name for name in entries if name == target or name.endswith("/" + target)]
                    if len(matches) != 1:
                        _fail("TARGET_MISSING" if not matches else "TARGET_AMBIGUOUS", "APP 内找不到对应替换目标。" if not matches else "APP 内存在多个匹配目标，不能自动选择。", "请提供软件包中准确且唯一的目标路径，或确认软件包与依赖版本。")
                    entry = entries[matches[0]]
                    original = actual_files.get(entry["resolved"])
                    if original is None:
                        _fail("TARGET_NOT_FILE", "替换目标未指向包内普通文件。", "请修复包内目标链接，或提供正确目标路径。")
                    original_elf = _target_elf(original["header"])
                    replacement = result["evidence"]["assets"][field]
                    replacement.update(target=target, resolved_target=matches[0], original_file=entry["resolved"], original_elf=original_elf)
                    if replacement["elf"] != original_elf:
                        _fail("ELF_ARCH_MISMATCH", "替换 SO 的 ELF 位数、字节序或机器架构与包内原 SO 不一致。", "请提供与该软件包相同架构的依赖版本；不要继续回灌。")
                    passed(field, "target_architecture", "唯一包内目标已定位，替换 SO 与原 SO 的 ELF 架构一致；尚未验证 ABI 和运行兼容性。")
                except InspectionError as exc:
                    issue(field, exc)
        if all(result["evidence"]["assets"].get(field, {}).get("elf") == TARGET_ELF
               for field in ("camera_so", "lidar_so", "m100_so")):
            if abi_symbols is None:
                result["evidence"]["limited_provider_symbols"] = {"status": "SYMBOL_COMPONENT_UNAVAILABLE",
                                                                  "full_abi_compatibility": "NOT_CHECKED"}
                issue("dependencies", InspectionError("SYMBOL_COMPONENT_UNAVAILABLE", "限定 provider 符号检查组件未加载。",
                      "请管理员部署固定符号检查组件；不能把未检查当作匹配。"))
            else:
                try:
                    budget.check()
                    libraries = {name: result["evidence"]["assets"][field] for field, name in (
                        ("camera_so", "libxcamera.so"), ("lidar_so", "libxlidar.so"), ("m100_so", "liburoad_m100.so"))}
                    remaining = budget.limits["duration_seconds"] - (time.monotonic() - budget.started)
                    symbols = abi_symbols.check_libraries(libraries, duration_seconds=min(60, remaining))
                    result["evidence"]["limited_provider_symbols"] = symbols
                    if symbols["status"] == "LIMITED_PROVIDER_SYMBOLS_MISSING":
                        for provider, evidence in symbols["providers"].items():
                            missing = evidence["missing_symbols"]
                            if missing:
                                field = "camera_so" if provider == "libxcamera.so" else "lidar_so"
                                detail = ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "")
                                issue(field, InspectionError("PROVIDER_SYMBOLS_MISSING", provider + " 缺少 M100 所需的限定强符号：" + detail,
                                      "请提供与本次 M100 对齐的相机/雷达 SO，或核对 M100 版本；不会自动反复修改或进入台架。"))
                    elif symbols["status"] in ("LIMITED_PROVIDER_SYMBOLS_MATCH", "LIMITED_PROVIDER_SYMBOLS_NO_REQUIREMENTS"):
                        detail = ("限定 provider 强符号匹配通过，共 " + str(symbols["required_symbol_count"]) + " 项；未验证完整 ABI 和运行兼容性。"
                                  if symbols["required_symbol_count"] else
                                  "已读取三 SO 动态符号，本规则未命中相机/雷达强符号需求；不能推断 ABI 兼容。")
                        passed("dependencies", symbols["status"], detail)
                    else:
                        _fail("SYMBOL_CHECK_INCOMPLETE", "限定 provider 符号检查未返回可确认结果。", "请管理员检查符号组件；不能按匹配通过。")
                except abi_symbols.SymbolCheckError as exc:
                    result["evidence"]["limited_provider_symbols"] = {"status": exc.code, "full_abi_compatibility": "NOT_CHECKED"}
                    issue("dependencies", exc)
                except InspectionError as exc:
                    issue("dependencies", exc)
        for field, stream in handles.items():
            try:
                if _fingerprint(stream) != fingerprints[field]:
                    _fail("FILE_CHANGED", "检查期间材料发生变化，本次证据不能使用。", "请停止修改文件并重新登记、检查。")
            except InspectionError as exc:
                issue(field, exc)
            except OSError:
                issue(field, InspectionError("FILE_CHANGED", "检查结束时无法重新确认材料文件。", "请确认文件未被修改或移除，然后重新检查。"))
    result["duration_seconds"] = round(time.monotonic() - budget.started, 3)
    result["evidence"]["limits"] = budget.limits
    if not result["issues"]:
        result["status"] = "PASS"
    return result


if __name__ == "__main__":
    try:
        supplied = json.loads(base64.b64decode(sys.argv[1], validate=True))
    except (IndexError, ValueError, UnicodeError):
        supplied = None
    print(json.dumps(inspect(supplied), ensure_ascii=False))
