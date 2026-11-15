"""Inspect or explicitly approve an exact HTTPS download host, without network I/O.

Examples:
  python tools/manage_download_hosts.py check --root <gateway> --url <file-url>
  python tools/manage_download_hosts.py add-approved-host --root <gateway> --host <approved-host>

Approval records a host only. It does not configure credentials or prove that a
link returns a package. Authenticated files may instead be downloaded by an
authorized tool directly into the user's workstation directory.
"""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


class DownloadHostError(ValueError):
    pass


def _host(value):
    if not isinstance(value, str) or len(value) > 253:
        raise DownloadHostError('需要明确的下载主机名，不接受 URL、通配符或凭据')
    result = value.lower()
    if not result or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                             for label in result.split('.')):
        raise DownloadHostError('需要明确的下载主机名，不接受 URL、通配符或凭据')
    return result


def manage(action, root, *, host=None, url=None):
    """Return nonsecret policy fields; mutate only an explicitly approved host."""
    if action not in ('check', 'add-approved-host'):
        raise DownloadHostError('未知管理操作')
    if action == 'add-approved-host' and (not host or url is not None):
        raise DownloadHostError('新增仅接受 --host 指定的已批准主机名')
    if action == 'check' and host is not None:
        raise DownloadHostError('检查请使用 --url，或省略链接查看当前白名单')
    path = Path(root) / 'config' / 'gateway.json'
    try:
        original = path.read_bytes()
        data = json.loads(original.decode('utf-8-sig'))
    except (OSError, UnicodeError, ValueError):
        raise DownloadHostError('无法读取有效的 gateway.json 配置') from None
    if not isinstance(data, dict):
        raise DownloadHostError('gateway.json 必须为对象')
    configured = data.get('download_hosts', [])
    if not isinstance(configured, list):
        raise DownloadHostError('download_hosts 必须为明确主机名列表')
    hosts = [_host(value) for value in configured]
    maximum = data.get('max_download_bytes', 8 * 1024**3)
    seconds = data.get('material_download_timeout_seconds', 300)
    if (isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0
            or isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds < float('inf')):
        raise DownloadHostError('下载大小或时间预算配置无效，未变更白名单')
    changed = False
    if action == 'add-approved-host':
        approved = _host(host)
        if approved not in hosts:
            # Keep every unrelated field and every existing host in its original
            # representation; no source URL or credential is persisted here.
            data['download_hosts'] = configured + [approved]
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.download-hosts-', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write((json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.read_bytes() != original:
                    raise DownloadHostError('配置在操作期间变化，未覆盖；请重新检查')
                os.replace(temporary, path)
                changed = True
            except OSError:
                raise DownloadHostError('白名单保存失败，未确认变更；请检查本机配置') from None
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
            hosts.append(approved)
    result = {'state': 'PASS', 'download_hosts': hosts, 'changed': changed,
              'maximum_bytes': min(maximum, 8 * 1024**3),
              'timeout_seconds': min(seconds, 300),
              'network_actions': 'NOT_RUN', 'credentials_configured': False}
    if url is not None:
        try:
            parsed = urlsplit(url)
            hostname = _host(parsed.hostname)
            valid = (parsed.scheme == 'https' and not parsed.username and not parsed.password
                     and parsed.port in (None, 443))
        except (TypeError, ValueError):
            raise DownloadHostError('检查链接必须为完整 HTTPS 文件 URL，不能包含用户名或密码') from None
        if not valid:
            raise DownloadHostError('检查链接必须为完整 HTTPS 文件 URL，不能包含用户名或密码')
        result.update(host=hostname, allowed=hostname in hosts)
        if not result['allowed']:
            result['state'] = 'NEEDS_CONFIGURATION'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'add-approved-host'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--host')
    parser.add_argument('--url')
    args = parser.parse_args(argv)
    try:
        result = manage(args.action, args.root, host=args.host, url=args.url)
    except DownloadHostError as exc:
        print(json.dumps({'state': 'FAILED', 'message': str(exc), 'network_actions': 'NOT_RUN'}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['state'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
