"""Bounded, read-only network evidence from the workstation; never establishes SSH trust."""
import inspect
import ipaddress
import itertools
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

SYS_NET = Path('/sys/class/net')
_IFACE = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')


def _command(argv, limit=32768):
    """Read at most limit bytes and terminate a command after two seconds."""
    proc = None
    selector = None
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=dict(os.environ, LC_ALL='C'))
        os.set_blocking(proc.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        output = bytearray()
        deadline = time.monotonic() + 2.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            events = selector.select(min(remaining, 0.1))
            if not events:
                continue
            chunk = os.read(proc.stdout.fileno(), min(4096, limit + 1 - len(output)))
            if not chunk:
                code = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
                return code, bytes(output).decode('utf-8', errors='replace')
            output.extend(chunk)
            if len(output) > limit:
                return None
    except Exception:
        return None
    finally:
        if selector is not None:
            selector.close()
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=0.2)
                except Exception:
                    pass
            if proc.stdout is not None:
                proc.stdout.close()


def _json_command(argv):
    response = _command(argv)
    if response is None:
        return None, 'UNAVAILABLE'
    code, text = response
    if code:
        if any(term in text.lower() for term in ('network is unreachable', 'no route to host')):
            return None, 'NO_ROUTE'
        return None, 'UNAVAILABLE'
    try:
        value = json.loads(text)
        if not isinstance(value, list) or len(value) > 64:
            raise ValueError('invalid list')
        return value, None
    except (ValueError, TypeError):
        return None, 'INVALID_JSON'


def _small_text(path):
    try:
        with path.open('rb') as handle:
            value = handle.read(257)
        if len(value) > 256:
            return None
        return value.decode('ascii').strip()
    except (OSError, UnicodeError):
        return None


def _interface(name, role):
    root = SYS_NET / name
    carrier = {'0': 'DOWN', '1': 'UP'}.get(_small_text(root / 'carrier'), 'UNKNOWN')
    operstate = _small_text(root / 'operstate')
    if operstate not in ('unknown', 'notpresent', 'down', 'lowerlayerdown', 'testing', 'dormant', 'up'):
        operstate = 'unknown'
    speed = _small_text(root / 'speed')
    speed = int(speed) if speed and speed.isdigit() and len(speed) <= 9 else None
    if not speed or speed > 100000000:
        speed = None
    duplex = _small_text(root / 'duplex')
    if duplex not in ('full', 'half'):
        duplex = 'unknown'
    lowers = []
    try:
        for path in itertools.islice(root.iterdir(), 64):
            if path.name.startswith('lower_') and _IFACE.fullmatch(path.name[6:]):
                lowers.append(path.name[6:])
                if len(lowers) == 8:
                    break
    except OSError:
        pass
    return {'name': name, 'role': role, 'carrier': carrier, 'operstate': operstate,
            'speed_mbps': speed, 'duplex': duplex, 'lower_interfaces': sorted(set(lowers))}


def _neighbor(address, interface):
    value = {'state': 'UNKNOWN', 'identity_evidence': False}
    rows, error = _json_command(['ip', '-j', 'neigh', 'show', 'to', address, 'dev', interface])
    if error:
        return value
    allowed = {'INCOMPLETE', 'REACHABLE', 'STALE', 'DELAY', 'PROBE', 'FAILED', 'NOARP', 'PERMANENT'}
    for row in rows:
        if not isinstance(row, dict) or row.get('dst') != address or row.get('dev', interface) != interface:
            continue
        states = row.get('state')
        if isinstance(states, str):
            states = [states]
        if isinstance(states, list):
            states = [item for item in states[:8] if isinstance(item, str) and item in allowed]
            if states:
                value['state'] = ','.join(states)
        mac = row.get('lladdr')
        if isinstance(mac, str) and re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac):
            value['lladdr'] = mac.lower()
        break
    return value


def inspect_network(base, ssh_responded=False):
    """Observe the configured direct IP route and its sysfs carrier snapshot."""
    value = {'schema_version': 1, 'probe_kind': 'workstation-readonly-network-v1',
             'state': 'SSH_RESPONDED' if ssh_responded is True else 'UNKNOWN', 'ssh_responded': ssh_responded is True,
             'identity_verified': False, 'physical_state': 'UNKNOWN',
             'route': {'state': 'UNKNOWN'}, 'interfaces': [],
             'neighbor': {'state': 'UNKNOWN', 'identity_evidence': False}, 'issues': []}
    try:
        response = _command(['ssh', '-G', '-F', str(Path(base) / 'config/ssh_config'), 'bench-avm'])
        if response is None or response[0] != 0:
            value['issues'].append('SSH_CONFIG_UNAVAILABLE')
            return value
        config = {}
        for line in response[1].splitlines()[:512]:
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].lower() in ('hostname', 'port', 'proxyjump', 'proxycommand'):
                key = parts[0].lower()
                if key in config:
                    value['issues'].append('SSH_CONFIG_AMBIGUOUS')
                    return value
                config[key] = parts[1].strip()
        if any(config.get(key, 'none').lower() != 'none' for key in ('proxyjump', 'proxycommand')):
            value.update(state='CONFIG_UNSUPPORTED')
            value['issues'].append('PROXY_CONFIGURED')
            return value
        address = config.get('hostname', '')
        if '%' in address:
            raise ValueError('scoped IP unsupported')
        address = str(ipaddress.ip_address(address))
        port_text = config.get('port', '')
        if not port_text.isdigit() or len(port_text) > 5 or not 1 <= int(port_text) <= 65535:
            raise ValueError('invalid port')
        value['target'] = {'address': address, 'port': int(port_text)}
        rows, error = _json_command(['ip', '-j', 'route', 'get', address])
        if error or not rows:
            missing = error == 'NO_ROUTE' or rows == []
            value['route']['state'] = 'MISSING' if missing else 'UNKNOWN'
            value['state'] = 'NO_ROUTE' if missing else 'UNKNOWN'
            value['issues'].append('NO_ROUTE' if missing else 'ROUTE_UNAVAILABLE')
            if value['ssh_responded']:
                value['state'] = 'SSH_RESPONDED'
                value['issues'].append('SSH_ROUTE_SNAPSHOT_DISAGREEMENT')
            return value
        route = rows[0]
        if not isinstance(route, dict):
            raise ValueError('invalid route')
        if route.get('type') in ('unreachable', 'blackhole', 'prohibit', 'throw'):
            value['route']['state'] = 'MISSING'
            value['state'] = 'SSH_RESPONDED' if value['ssh_responded'] else 'NO_ROUTE'
            value['issues'].append('NO_ROUTE')
            return value
        interface = route.get('dev')
        if not isinstance(interface, str) or not _IFACE.fullmatch(interface):
            raise ValueError('route interface absent')
        value['route'] = {'state': 'PRESENT', 'interface': interface}
        for key, output in (('prefsrc', 'source'), ('src', 'source'), ('gateway', 'gateway')):
            candidate = route.get(key)
            if isinstance(candidate, str):
                try:
                    value['route'][output] = str(ipaddress.ip_address(candidate))
                except ValueError:
                    pass
        pending = [(interface, 'route')]
        visited = set()
        while pending and len(visited) < 8:
            name, role = pending.pop(0)
            if name in visited:
                continue
            visited.add(name)
            details = _interface(name, role)
            value['interfaces'].append(details)
            pending.extend((lower, 'lower') for lower in details['lower_interfaces'] if lower not in visited)
        leaves = [item for item in value['interfaces'] if not item['lower_interfaces']]
        carriers = {item['carrier'] for item in leaves}
        if carriers == {'UP'}:
            value['physical_state'] = 'UP'
        elif carriers == {'DOWN'}:
            value['physical_state'] = 'DOWN'
        elif 'UP' in carriers and 'DOWN' in carriers:
            value['physical_state'] = 'INCONSISTENT'
        value['neighbor'] = _neighbor(address, interface)
        if value['ssh_responded']:
            value['state'] = 'SSH_RESPONDED'
            if value['physical_state'] == 'DOWN':
                value['physical_state'] = 'INCONSISTENT'
                value['issues'].append('SSH_CARRIER_SNAPSHOT_DISAGREEMENT')
        elif value['physical_state'] == 'DOWN':
            value['state'] = 'PHYSICAL_LINK_DOWN'
        elif value['physical_state'] == 'UP':
            value['state'] = 'LINK_UP'
        return value
    except Exception:
        value['issues'].append('NETWORK_CHECK_UNAVAILABLE')
        if value['ssh_responded']:
            value['state'] = 'SSH_RESPONDED'
        return value


def format_network(value):
    """Chinese display text; carrier and neighbor evidence do not authenticate a bench."""
    if not isinstance(value, dict):
        return '工作站网口：未取得网络诊断。'
    lines = []
    if value.get('state') == 'CONFIG_UNSUPPORTED':
        lines.append('工作站网口：SSH 使用代理，未按直连台架推断物理链路。')
    elif isinstance(value.get('route'), dict) and value['route'].get('state') == 'MISSING':
        lines.append('工作站路由：本次未找到台架目标路由。')
    details = []
    interfaces = value.get('interfaces')
    for item in (interfaces[:8] if isinstance(interfaces, list) else []):
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        if not isinstance(name, str) or not _IFACE.fullmatch(name):
            continue
        role = '下层网口' if item.get('role') == 'lower' else '路由网口'
        carrier = {'UP': '有载波', 'DOWN': '无载波', 'UNKNOWN': '载波未知'}.get(item.get('carrier'), '载波未知')
        parts = [name + '（' + role + '）' + carrier]
        speed = item.get('speed_mbps')
        if isinstance(speed, int) and not isinstance(speed, bool) and 0 < speed <= 100000000:
            parts.append(str(speed) + ' Mbps')
        duplex = {'full': '全双工', 'half': '半双工'}.get(item.get('duplex'))
        if duplex:
            parts.append(duplex)
        details.append('，'.join(parts))
    if details:
        lines.append('工作站网口：' + '；'.join(details))
    elif not lines:
        lines.append('工作站网口：本次未能确认载波与路由。')
    if value.get('ssh_responded') is True:
        lines.append('台架目标 SSH：已响应，当前网络链路可达；身份与登录结果见 SSH 检查。')
        if value.get('physical_state') == 'INCONSISTENT':
            lines.append('网口快照与 SSH 回应不一致，不能据此判定物理断线。')
    else:
        lines.append('台架目标 SSH：本次未确认服务回应。')
        if value.get('physical_state') == 'DOWN':
            lines.append('网口未检测到载波，请现场检查连线、台架供电及网口。')
    lines.append('载波与 ARP 仅用于网络定位，不能证明台架设备身份。')
    return '\n'.join(lines)


# Only repository-managed Python source is included; no downloaded or user text is executed.
REMOTE_CODE = '\n'.join([
    'import ipaddress,itertools,json,os,re,selectors,subprocess,time',
    'from pathlib import Path',
    "SYS_NET=Path('/sys/class/net')",
    "_IFACE=re.compile(r'[A-Za-z0-9_.:-]{1,64}\\Z')",
] + [inspect.getsource(function) for function in
     (_command, _json_command, _small_text, _interface, _neighbor, inspect_network)])
