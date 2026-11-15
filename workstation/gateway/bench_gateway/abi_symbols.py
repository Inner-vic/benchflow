"""Bounded, read-only camera/lidar provider symbol checks, not complete ABI proof.

Reuses the original artifact_pipeline.py rule: only M100 strong undefined
symbols containing 7XCamera or 6XLidar are matched to the corresponding SO.
Unlike that rule, symbol version suffixes are preserved. No SO is executed.
This module is also injected under ``bench_gateway_abi_symbols`` over SSH stdin.
"""
import hashlib
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time


READELF = '/usr/bin/readelf'
LIBRARIES = ('libxcamera.so', 'libxlidar.so', 'liburoad_m100.so')
PROVIDERS = {'libxcamera.so': ('7XCamera',), 'libxlidar.so': ('6XLidar',)}
MAX_OUTPUT_BYTES = 8 * 1024**2
MAX_SYMBOLS = 200000
LIMITATIONS = [
    '仅检查 M100 强未定义符号中名称包含 7XCamera / 6XLidar 的限定 provider 规则',
    '保留 @ 版本；无版本需求仅接受无版本导出或 @@ 默认版本导出',
    '未验证完整 ABI、结构体布局、所有依赖库、动态加载顺序或实际运行兼容性',
]


class SymbolCheckError(Exception):
    def __init__(self, code, message, next_action):
        self.code, self.message, self.next_action = code, message, next_action
        super().__init__(message)


def _fail(code, message, action):
    raise SymbolCheckError(code, message, action)


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        _fail('SYMBOL_CHECK_TIMEOUT', '限定 provider 符号检查达到时限，未完成核验。',
              '请检查 SO 大小和工作站负载，或由管理员离线核验；不会反复自动重试。')


def _identity(stream):
    info = os.fstat(stream.fileno())
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sha256(stream, deadline):
    stream.seek(0)
    digest = hashlib.sha256()
    while True:
        _check_deadline(deadline)
        block = stream.read(1024 * 1024)
        if not block:
            return digest.hexdigest()
        digest.update(block)


def _tool_supported():
    return os.name == 'posix' and Path('/proc/self/fd').is_dir() and Path(READELF).is_file()


def _readelf(stream, deadline, maximum):
    """Drain both pipes with a hard combined cap; never run the input library."""
    if not _tool_supported():
        _fail('SYMBOL_TOOL_UNAVAILABLE', '工作站缺少固定 readelf 或只读文件描述符能力，未检查符号。',
              '请管理员准备 /usr/bin/readelf 和 Linux /proc；不能把未检查当作符号匹配。')
    _check_deadline(deadline)
    command = [READELF, '--dyn-syms', '--wide', '--', '/proc/self/fd/' + str(stream.fileno())]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, pass_fds=(stream.fileno(),),
                                   env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})
    except OSError:
        _fail('SYMBOL_TOOL_UNAVAILABLE', '固定 readelf 无法启动，未检查符号。',
              '请管理员检查工具安装和权限；不执行 SO 或使用 ldd 代替。')
    selector = selectors.DefaultSelector()
    output, total = [], 0
    try:
        for pipe in (process.stdout, process.stderr):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ)
        while selector.get_map():
            _check_deadline(deadline)
            for key, _ in selector.select(min(.2, max(0, deadline - time.monotonic()))):
                try:
                    block = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                total += len(block)
                if total > maximum:
                    _fail('SYMBOL_OUTPUT_LIMIT', 'readelf 输出超过检查上限，未完成符号核验。',
                          '请管理员离线检查异常或过大的符号表；不放宽为未经检查即通过。')
                if key.fileobj is process.stdout:
                    output.append(block)
        _check_deadline(deadline)
        try:
            code = process.wait(timeout=max(.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _fail('SYMBOL_CHECK_TIMEOUT', '限定 provider 符号检查达到时限，未完成核验。',
                  '请检查 SO 大小和工作站负载后重新核验。')
        if code:
            _fail('SYMBOL_READELF_FAILED', 'readelf 不能读取该 SO 的动态符号表。',
                  '请提供完整成品 SO，或由管理员核验工具支持；未执行该 SO。')
        try:
            return b''.join(output).decode('ascii')
        except UnicodeError:
            _fail('SYMBOL_TABLE_UNSUPPORTED', '动态符号输出格式不受当前限定检查支持。',
                  '请管理员离线核验，不能按已匹配继续执行。')
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        process.stdout.close()
        process.stderr.close()


def _symbol(value):
    if (len(value) > 1024 or not re.fullmatch(r'[A-Za-z0-9_.$+@-]+', value)
            or value.count('@') > 2 or (value.count('@') == 2 and '@@' not in value)
            or value.endswith('@')):
        _fail('SYMBOL_TABLE_UNSUPPORTED', '动态符号名称或版本格式不受当前检查支持。',
              '请管理员离线核验该 SO；不会忽略无法解析的符号后继续。')
    return value


def parse_dynamic_symbols(output):
    """Return (exported lookup names, strong requirements), retaining versions."""
    if len(output.encode('utf-8')) > MAX_OUTPUT_BYTES:
        _fail('SYMBOL_OUTPUT_LIMIT', '动态符号输出超过检查上限。', '请管理员离线核验该 SO。')
    tables = re.findall(r"^Symbol table '\.dynsym' contains ([0-9]+) entries:\s*$", output, re.M)
    if len(tables) != 1 or not 0 < int(tables[0]) <= MAX_SYMBOLS:
        _fail('SYMBOL_TABLE_UNSUPPORTED', '缺少可识别的非空 .dynsym 动态符号表，未检查符号。',
              '请提供完整成品 SO，或由管理员核验符号表格式。')
    exported, required, rows = set(), set(), 0
    for line in output.splitlines():
        fields = line.split()
        if not fields or not fields[0].rstrip(':').isdigit():
            continue
        rows += 1
        if len(fields) < 7 or rows > MAX_SYMBOLS:
            _fail('SYMBOL_TABLE_UNSUPPORTED', '动态符号表条目格式不完整。', '请管理员离线核验该 SO。')
        if len(fields) == 7:
            null_entry = (fields[0] == '0:' and re.fullmatch(r'0+', fields[1])
                          and fields[2:] == ['0', 'NOTYPE', 'LOCAL', 'DEFAULT', 'UND'])
            local_section = (fields[0].rstrip(':') != '0' and fields[3:5] == ['SECTION', 'LOCAL']
                             and fields[5] in ('DEFAULT', 'INTERNAL', 'HIDDEN', 'PROTECTED')
                             and fields[6].isdigit() and int(fields[6]) > 0)
            if null_entry or local_section:
                # Unnamed LOCAL SECTION records are legal metadata, not
                # provider names. They still count towards the declared table
                # size so missing or truncated output cannot pass unnoticed.
                continue
        if len(fields) < 8:
            _fail('SYMBOL_TABLE_UNSUPPORTED', '动态符号表条目缺少名称。', '请管理员离线核验该 SO。')
        binding, visibility, section = fields[4], fields[5], fields[6]
        name = _symbol(fields[7])
        if binding not in ('GLOBAL', 'WEAK'):
            continue
        if section == 'UND':
            if binding == 'GLOBAL':
                if '@@' in name:
                    _fail('SYMBOL_TABLE_UNSUPPORTED', '未定义符号包含不支持的默认版本标记。', '请管理员离线核验该 SO。')
                required.add(name)
        elif visibility in ('DEFAULT', 'PROTECTED'):
            exported.add(name.replace('@@', '@'))
            if '@@' in name:
                exported.add(name.split('@@', 1)[0])
    if rows != int(tables[0]):
        _fail('SYMBOL_TABLE_UNSUPPORTED', '动态符号表条目数量与声明不一致，不能使用截断输出。',
              '请核验工具和完整符号表；不会按部分输出通过。')
    return exported, required


def check_libraries(libraries, *, duration_seconds=60, maximum_output_bytes=MAX_OUTPUT_BYTES):
    """Check only the fixed three caller-owner-validated SOs, with frozen hashes."""
    if (not isinstance(libraries, dict) or set(libraries) != set(LIBRARIES)
            or isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float))
            or not 0 < duration_seconds <= 60 or isinstance(maximum_output_bytes, bool)
            or not isinstance(maximum_output_bytes, int) or not 0 < maximum_output_bytes <= MAX_OUTPUT_BYTES):
        _fail('SYMBOL_REQUEST_INVALID', '限定符号检查输入或上限无效。', '请管理员检查固定三 SO 和检查预算。')
    deadline, symbols, verified = time.monotonic() + duration_seconds, {}, {}
    for name in LIBRARIES:
        asset = libraries[name]
        if (not isinstance(asset, dict) or not isinstance(asset.get('remote_path'), str)
                or not Path(asset['remote_path']).is_absolute()
                or not isinstance(asset.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', asset['sha256'])):
            _fail('SYMBOL_REQUEST_INVALID', '限定符号检查材料缺少有效路径或 SHA-256。', '请重新核验本人材料。')
        path = Path(asset['remote_path'])
        try:
            descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(descriptor, 'rb') as stream:
                before = _identity(stream)
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    _fail('SYMBOL_FILE_CHANGED', name + ' 不是普通文件。', '请重新登记完整成品 SO。')
                if _sha256(stream, deadline) != asset['sha256'] or _identity(stream) != before:
                    _fail('SYMBOL_FILE_CHANGED', name + ' 在符号检查前与冻结材料不一致。', '请停止修改文件并重新检查材料。')
                output = _readelf(stream, deadline, maximum_output_bytes)
                exported, required = parse_dynamic_symbols(output)
                current = path.lstat()
                if (_sha256(stream, deadline) != asset['sha256'] or _identity(stream) != before
                        or stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != before[:2]):
                    _fail('SYMBOL_FILE_CHANGED', name + ' 在符号检查期间发生变化。', '请停止修改文件并重新检查材料。')
                symbols[name] = {'exported': exported, 'required': required}
                verified[name] = {'sha256': asset['sha256'], 'exported_lookup_name_count': len(exported),
                                  'strong_required_symbol_count': len(required)}
        except OSError:
            _fail('SYMBOL_FILE_UNAVAILABLE', name + ' 在限定符号检查期间无法读取。', '请检查本人文件与工作站权限。')
    providers, missing_all, count = {}, [], 0
    for provider, markers in PROVIDERS.items():
        required = sorted(symbol for symbol in symbols['liburoad_m100.so']['required'] if any(marker in symbol for marker in markers))
        missing = [symbol for symbol in required if symbol not in symbols[provider]['exported']]
        providers[provider] = {'required_symbol_count': len(required), 'matched_symbol_count': len(required) - len(missing),
                               'missing_symbols': missing}
        count += len(required)
        missing_all.extend({'provider': provider, 'symbol': symbol} for symbol in missing)
    return {'status': 'LIMITED_PROVIDER_SYMBOLS_MISSING' if missing_all else
            'LIMITED_PROVIDER_SYMBOLS_MATCH' if count else 'LIMITED_PROVIDER_SYMBOLS_NO_REQUIREMENTS',
            'method': 'readelf-dynamic-symbol-provider-match-versioned-v1', 'consumer': 'liburoad_m100.so',
            'providers': providers, 'libraries': verified, 'required_symbol_count': count,
            'missing_symbols': missing_all, 'limitations': list(LIMITATIONS),
            'so_executed': False, 'full_abi_compatibility': 'NOT_CHECKED'}
