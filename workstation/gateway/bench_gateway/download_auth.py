"""Local credentials for explicitly allowed HTTPS download hosts.

The caller sends the returned header over SSH stdin for one download. Credentials
are never part of an asset, task, URL, command line, or model input.
Personal credentials stay isolated. A separately entered and verified dedicated
team account may authorize downloads for enabled users without sharing assets.
"""
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .core import GatewayError, Store, owner_key


_ERROR = '下载认证配置不可用，请在本机配置页重新保存'
_HOST = re.compile(r'(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z')


def _scope(cfg, user, host):
    owner = owner_key(user)
    Store(cfg['state_root']).require_user(user)
    if (not isinstance(host, str) or not _HOST.fullmatch(host)
            or any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-') for part in host.split('.'))
            or host not in cfg.get('download_hosts', [])):
        raise GatewayError('下载认证仅支持已允许的精确 HTTPS 域名')
    root = Path(cfg['project_root'])
    if not root.is_absolute():
        raise GatewayError(_ERROR)
    _no_links(root)
    path = root / 'runtime' / 'download-auth' / owner / (hashlib.sha256(host.encode()).hexdigest() + '.json')
    _no_links(path)
    return owner, path


def _no_links(path):
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(part, 'is_junction', lambda: False)()
                or (os.name == 'nt' and getattr(info, 'st_file_attributes', 0) & 0x400)):
            raise GatewayError(_ERROR)


def _windows_api():
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    return wintypes, kernel, crypt, advapi


def _protect(data, entropy, decrypt=False):
    if os.name != 'nt':
        return data
    types, kernel, crypt, _ = _windows_api()
    class Blob(ctypes.Structure):
        _fields_ = [('length', types.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    source_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(entropy)
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    extra = Blob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    if decrypt:
        call = crypt.CryptUnprotectData
        call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, types.DWORD, ctypes.POINTER(Blob)]
        ok = call(ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(output))
    else:
        call = crypt.CryptProtectData
        call.argtypes = [ctypes.POINTER(Blob), types.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, types.DWORD, ctypes.POINTER(Blob)]
        ok = call(ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(output))
    if not ok:
        raise GatewayError(_ERROR)
    try:
        return ctypes.string_at(output.data, output.length)
    finally:
        if output.data:
            ctypes.memset(output.data, 0, output.length)
            kernel.LocalFree(ctypes.cast(output.data, types.HLOCAL))

def _posix_protect(data,entropy,path,decrypt=False):
    import subprocess
    helper=Path(__file__).with_name('posix_secret.py')
    packet={'key':str(path.parents[1]/'.fernet-key'),'data':base64.b64encode(data).decode(),
        'scope':base64.b64encode(entropy).decode(),'decrypt':decrypt}
    p=subprocess.run(['/usr/bin/python3','-B',str(helper)],input=json.dumps(packet),text=True,
        capture_output=True,timeout=10)
    if p.returncode:raise GatewayError(_ERROR)
    return base64.b64decode(p.stdout.strip(),validate=True)


def _restrict_access(path, directory=False):
    """Replace the DACL with current-user access, or enforce POSIX 0700/0600."""
    _no_links(path)
    if os.name != 'nt':
        os.chmod(path, 0o700 if directory else 0o600)
        if stat.S_IMODE(path.stat().st_mode) != (0o700 if directory else 0o600):
            raise GatewayError(_ERROR)
        return
    types, kernel, _, advapi = _windows_api()
    kernel.GetCurrentProcess.restype = types.HANDLE
    kernel.CloseHandle.argtypes = [types.HANDLE]
    advapi.OpenProcessToken.argtypes = [types.HANDLE, types.DWORD, ctypes.POINTER(types.HANDLE)]
    advapi.GetTokenInformation.argtypes = [types.HANDLE, ctypes.c_int, ctypes.c_void_p, types.DWORD, ctypes.POINTER(types.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(types.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [types.LPCWSTR, types.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(types.DWORD)]
    advapi.SetFileSecurityW.argtypes = [types.LPCWSTR, types.DWORD, ctypes.c_void_p]
    token = types.HANDLE()
    sid_text = types.LPWSTR()
    descriptor = ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise GatewayError(_ERROR)
        length = types.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not length.value:
            raise GatewayError(_ERROR)
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise GatewayError(_ERROR)
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise GatewayError(_ERROR)
        flags = 'OICI' if directory else ''
        sddl = 'D:P(A;' + flags + ';FA;;;' + sid_text.value + ')'
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise GatewayError(_ERROR)
        if not advapi.SetFileSecurityW(str(path), 0x00000004 | 0x80000000, descriptor):
            raise GatewayError(_ERROR)
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, types.HLOCAL))
        if token:
            kernel.CloseHandle(token)


def _ensure_directory(path):
    _no_links(path)
    path.mkdir(exist_ok=True)
    _no_links(path)
    if not path.is_dir():
        raise GatewayError(_ERROR)
    _restrict_access(path, directory=True)


def _basic(username, password):
    if (not isinstance(username, str) or not username or len(username) > 256
            or username.strip() != username or ':' in username
            or not isinstance(password, str) or not password or len(password) > 4096
            or any(c in username + password for c in '\r\n\x00')):
        raise GatewayError('下载账号或密码格式无效')
    return 'Basic ' + base64.b64encode((username + ':' + password).encode('utf-8')).decode('ascii')


def save_basic(cfg, user, host, username, password):
    owner, path = _scope(cfg, user, host)
    _save_basic(owner, path, host, username, password)


def _save_basic(owner, path, host, username, password):
    header = _basic(username, password)
    entropy = ('bench-download-auth-v1\0' + owner + '\0' + host).encode('utf-8')
    protected = _protect(header.encode('ascii'), entropy) if os.name=='nt' else _posix_protect(header.encode('ascii'),entropy,path)
    envelope = {'schema_version': 1, 'owner_key': owner, 'host': host, 'scheme': 'basic',
                'protection': 'windows-dpapi-current-user' if os.name == 'nt' else 'posix-fernet-v1',
                'blob': base64.b64encode(protected).decode('ascii')}
    raw = (json.dumps(envelope, ensure_ascii=True, sort_keys=True) + '\n').encode('ascii')
    temp = None
    try:
        runtime = path.parents[2]
        _no_links(runtime)
        if not runtime.exists():
            _ensure_directory(runtime)
        for directory in (path.parents[1], path.parent):
            _ensure_directory(directory)
        temp = path.with_name('.saving-' + uuid.uuid4().hex)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(str(temp), flags, 0o600)
        try:
            _restrict_access(temp)
            with os.fdopen(fd, 'wb') as target:
                fd = None
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
        finally:
            if fd is not None:
                os.close(fd)
        _no_links(path)
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            raise GatewayError(_ERROR)
        os.replace(temp, path)
        temp = None
    except Exception:
        raise GatewayError(_ERROR) from None
    finally:
        if temp is not None:
            try:
                temp.unlink()
            except OSError:
                pass


def load_authorization(cfg, user, host):
    owner, path = _scope(cfg, user, host)
    # Team credentials are a separately configured local download capability,
    # never another user's credentials or a cross-owner asset fallback.
    if cfg.get('_download_auth_team_candidate_host') == host:
        owner, path = _team_scope(cfg, user, host, candidate=True)
        value = _load_authorization(owner, path, host)
        if value is None:
            raise GatewayError('团队下载候选凭据尚未保存')
        return value
    team_owner, team_path = _team_scope(cfg, user, host)
    if team_path.exists():
        owner, path = team_owner, team_path
    return _load_authorization(owner, path, host)


def _load_authorization(owner, path, host):
    try:
        if not path.exists():
            return None
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size < 65536:
            raise GatewayError(_ERROR)
        if os.name != 'nt' and stat.S_IMODE(info.st_mode) != 0o600:
            raise GatewayError(_ERROR)
        # Reapply the current-user DACL before reading an existing Windows blob.
        if os.name == 'nt':
            _restrict_access(path)
        data = json.loads(path.read_text(encoding='ascii'))
        protection = 'windows-dpapi-current-user' if os.name == 'nt' else 'posix-fernet-v1'
        if (set(data) != {'schema_version', 'owner_key', 'host', 'scheme', 'protection', 'blob'}
                or data['schema_version'] != 1 or data['owner_key'] != owner or data['host'] != host
                or data['scheme'] != 'basic' or data['protection'] != protection):
            raise GatewayError(_ERROR)
        entropy = ('bench-download-auth-v1\0' + owner + '\0' + host).encode('utf-8')
        encrypted=base64.b64decode(data['blob'], validate=True)
        header = (_protect(encrypted,entropy,decrypt=True) if os.name=='nt' else _posix_protect(encrypted,entropy,path,decrypt=True)).decode('ascii')
        if not header.startswith('Basic '):
            raise GatewayError(_ERROR)
        decoded = base64.b64decode(header[6:], validate=True).decode('utf-8')
        username, separator, password = decoded.partition(':')
        if not separator or _basic(username, password) != header:
            raise GatewayError(_ERROR)
        return header
    except Exception:
        raise GatewayError(_ERROR) from None


def _team_scope(cfg, user, host, candidate=False):
    _, personal = _scope(cfg, user, host)
    path = personal.parents[1] / 'team-download-v1' / personal.name
    if candidate:
        path = path.with_suffix('.candidate.json')
    _no_links(path)
    return 'team-download-v1', path


def save_team_candidate(cfg, user, host, username, password):
    """Local administrator entry only; saving does not enable shared use."""
    owner, path = _team_scope(cfg, user, host, candidate=True)
    _save_basic(owner, path, host, username, password)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def activate_team_candidate(cfg, user, host, expected_digest, verification):
    """Promote only the exact candidate whose workstation probe succeeded."""
    if (verification.get('state') != 'PASS' or verification.get('host') != host
            or verification.get('credentials_configured') is not True
            or verification.get('asset_writes') != 0 or verification.get('gzip_magic') is not True):
        raise GatewayError('团队下载认证尚未通过工作站验证，原认证保持不变')
    owner, candidate = _team_scope(cfg, user, host, candidate=True)
    if (_load_authorization(owner, candidate, host) is None
            or hashlib.sha256(candidate.read_bytes()).hexdigest() != expected_digest):
        raise GatewayError('团队下载候选凭据已变化，请重新验证')
    _, active = _team_scope(cfg, user, host)
    if active.exists() and (not active.is_file() or active.stat().st_nlink != 1):
        raise GatewayError(_ERROR)
    os.replace(candidate, active)


def team_credential_status(cfg, user, host):
    owner, path = _team_scope(cfg, user, host)
    try:
        configured = _load_authorization(owner, path, host) is not None
    except GatewayError:
        return {'configured': False, 'needs_update': True, 'host': host, 'scope': 'team-download'}
    return {'configured': configured, 'needs_update': False, 'host': host, 'scope': 'team-download'}


def credential_status(cfg, user, host):
    """Only public metadata; never return account names, secrets, or headers."""
    _scope(cfg, user, host)
    try:
        configured = load_authorization(cfg, user, host) is not None
    except GatewayError:
        return {'configured': False, 'needs_update': True, 'host': host, 'scheme': 'basic'}
    return {'configured': configured, 'needs_update': False, 'host': host, 'scheme': 'basic'}
