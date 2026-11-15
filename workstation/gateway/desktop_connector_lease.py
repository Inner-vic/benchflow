"""Workstation lease for exactly one desktop Feishu connector; no task access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid


def write(path, value):
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(value),encoding='utf-8');tmp.chmod(0o600);os.replace(tmp,path)


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def native_bridge_pids(root):
    result=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            parts=(p/'cmdline').read_bytes().decode(errors='replace').split('\0')
            if (parts and Path(parts[0]).name=='cc-connect-linux-amd64' and
                    str(root/'config/cc-connect.toml') in parts):result.append(int(p.name))
        except OSError:pass
    return result


def release(path, request, generation):
    value=read(path)
    if (request.get('generation')!=generation or request.get('lease')!=value.get('lease') or
            request.get('client')!=value.get('client') or request.get('child_stopped') is not True):
        raise ValueError('Connector release ownership differs')
    value.update(state='STOPPED',connected=False,updated=time.time());write(path,value)
    return {'kind':'released'}


def serve(root):
    cfg=json.loads((root/'config/gateway.json').read_text(encoding='utf-8-sig'))
    generation=cfg.get('desktop_bridge_generation')
    if not generation:raise ValueError('Desktop connector handoff not configured')
    folder=root/'runtime/desktop-bridge';folder.mkdir(exist_ok=True)
    path=folder/'lease.json'
    name='\0bench-desktop-bridge-'+hashlib.sha256(str(root).encode()).hexdigest()[:24]
    guard=socket.socket(socket.AF_UNIX);guard.bind(name)
    request=json.loads(sys.stdin.readline(8193))
    if request.get('op')=='release':
        print(json.dumps(release(path,request,generation)),flush=True);return
    if request.get('op')!='open' or request.get('generation')!=generation:
        raise ValueError('Unexpected connector request')
    previous=read(path)
    if previous.get('state') not in (None,'STOPPED'):
        raise ValueError('Previous desktop connector requires ownership review')
    client=request.get('client')
    if not isinstance(client,str) or len(client)!=32 or any(c not in '0123456789abcdef' for c in client):
        raise ValueError('Invalid connector client identity')
    value={'lease':uuid.uuid4().hex,'client':client,'generation':generation,'state':'STANDBY',
           'connected':False,'updated':time.time(),'desktop_pid':None}
    write(path,value)
    secret=json.loads((root/'runtime/credentials.json').read_text())
    if secret['app_id']!=cfg['feishu_app_id']:raise ValueError('Bot identity differs')
    # This packet goes only to the native desktop host's SSH pipe. Never log it.
    print(json.dumps({'kind':'bootstrap','lease':value['lease'],'generation':generation,
        'cc_project':cfg['cc_project'],'app_id':secret['app_id'],'app_secret':secret['app_secret']}),flush=True)
    stopped=False
    try:
        for line in sys.stdin:
            if len(line)>8192:raise ValueError('Connector heartbeat too large')
            packet=json.loads(line)
            if packet.get('lease')!=value['lease'] or packet.get('client')!=client:
                raise ValueError('Connector ownership differs')
            if packet.get('op')=='release':
                print(json.dumps(release(path,packet,generation)),flush=True);stopped=True;return
            if packet.get('op')!='heartbeat':raise ValueError('Unknown connector operation')
            now_cfg=json.loads((root/'config/gateway.json').read_text(encoding='utf-8-sig'))
            allowed=(now_cfg.get('desktop_bridge_enabled') is True and
                     now_cfg.get('desktop_bridge_generation')==generation and not native_bridge_pids(root))
            connected=allowed and packet.get('connected') is True
            value.update(updated=time.time(),connected=connected,
                state='CONNECTED' if connected else 'STARTING' if allowed else 'STANDBY',
                desktop_pid=packet.get('pid') if type(packet.get('pid')) is int else None)
            write(path,value)
            print(json.dumps({'kind':'ack','allow_connect':allowed,'lease':value['lease']}),flush=True)
    finally:
        latest=read(path)
        if not stopped and latest.get('lease')==value['lease'] and latest.get('state')!='STOPPED':
            latest.update(state='NEEDS_REVIEW',connected=False,updated=time.time());write(path,latest)
        guard.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    root=parser.parse_args().root.resolve(strict=True)
    if root!=Path(__file__).resolve().parent:raise ValueError('Receiver root differs')
    try:serve(root)
    except Exception:
        print(json.dumps({'kind':'error','code':'CONNECTOR_LEASE_UNAVAILABLE'}),flush=True);raise SystemExit(2)


if __name__=='__main__':main()
