"""Desktop CC Connect exec facade; no business database or attachment staging."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid

from bridge_protocol import MAX_PACKET, packet, validate_response


def argv(cfg):
    host = cfg.get('workstation', 'ubuntu-workstation')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', host):
        raise ValueError('Workstation must be a fixed SSH alias')
    root = cfg['remote_project_root']
    python = cfg['remote_python']
    if not root.startswith('/') or not python.startswith('/') or any(c in root+python for c in '\r\n\0'):
        raise ValueError('Invalid fixed workstation paths')
    command = ' '.join(shlex.quote(x) for x in [python, '-X', 'utf8', '-B', root+'/desktop_gateway_receiver.py', '--root', root])
    binary = cfg.get('ssh_binary') or str(Path(os.environ.get('WINDIR', 'C:/Windows'))/'System32/OpenSSH/ssh.exe')
    config_path=cfg.get('desktop_ssh_config') or cfg.get('ssh_config')
    config = ['-F', str(Path(config_path).resolve(strict=True))] if config_path else []
    if cfg.get('desktop_ssh_identity_file'):config+=['-i',str(Path(cfg['desktop_ssh_identity_file']).resolve(strict=True))]
    if cfg.get('desktop_ssh_known_hosts'):config+=['-o','UserKnownHostsFile='+str(Path(cfg['desktop_ssh_known_hosts']).resolve(strict=True))]
    return [binary, *config, '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UpdateHostKeys=no', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3', host, command]


def forward(cfg, project, session, text, runner=subprocess.run):
    request = packet(project, session, text)
    if cfg.get('desktop_menu_warm_transport') and text.startswith('@@BENCH_MENU@@'):
        from desktop_menu_transport import forward as warm_forward
        response = warm_forward(Path(__file__).resolve().parent, project, session, text)
        if response is not None:return response
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    # One attempt only. A timeout after stdin was sent has an unknown outcome.
    result = runner(argv(cfg), input=request.encode('utf-8'), capture_output=True,
                    timeout=max(120,min(900,float(cfg.get('desktop_intake_timeout_seconds',600)))), **options)
    if result.returncode:
        raise RuntimeError('Workstation intake result is unconfirmed')
    return validate_response(result.stdout.decode('utf-8') if isinstance(result.stdout,bytes) else result.stdout)


def error_response():
    return '\n'.join(json.dumps(v, ensure_ascii=False) for v in [
        {'type': 'thread.started', 'thread_id': 'bridge-unknown-'+uuid.uuid4().hex},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text':
            '工作站消息处理结果未确认，未自动重发。请先核对原任务卡；不要重复点击确认或据此重跑台架。'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0}},
    ])+'\n'


def main():
    if '--version' in sys.argv:
        print('codex-cli bench-desktop-transport 1.0');return
    if 'app-server' in sys.argv:
        print('Runtime configuration unavailable for transport adapter', file=sys.stderr);return
    try:
        root = Path(__file__).resolve().parent
        cfg = json.loads((root/'config/gateway.json').read_text(encoding='utf-8-sig'))
        project = os.environ.get('CC_PROJECT', '')
        if not project or project != cfg['cc_project']:
            raise ValueError('Unexpected CC Connect project')
        text = sys.stdin.read(MAX_PACKET+1).strip()
        preamble='Before answering, follow these project-level instructions for this cc-connect session.'
        marker='\n\n---\n\nUser message:\n'
        if text.startswith(preamble) and marker in text:text=text.split(marker,1)[1]
        response = forward(cfg, project, os.environ.get('CC_SESSION_KEY', ''), text)
    except Exception:
        # Never print exceptions: SSH errors may contain sensitive messages.
        response = error_response()
    sys.stdout.write(response);sys.stdout.flush()


if __name__ == '__main__':main()
