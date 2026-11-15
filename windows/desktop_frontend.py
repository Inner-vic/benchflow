"""PC-owned CC Connect with workstation-only business state and ephemeral secret."""
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import threading
import time
import uuid


def options():return {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}


def command(cfg,script):
    from desktop_gateway_agent import argv
    result=argv(cfg)
    result[-1]=' '.join(shlex.quote(x) for x in [cfg['remote_python'],'-X','utf8','-B',
        cfg['remote_project_root']+'/'+script,'--root',cfg['remote_project_root']])
    return result


def packets(stream):
    messages=queue.Queue()
    def read():
        try:
            while True:
                line=stream.readline(65537)
                if not line or len(line)>65536:break
                messages.put(line)
        finally:messages.put(None)
    threading.Thread(target=read,daemon=True).start()
    def receive(timeout=12):
        line=messages.get(timeout=timeout)
        if line is None:raise ConnectionError('Lease pipe closed')
        value=json.loads(line)
        if value.get('kind')=='error':raise ConnectionError('Connector lease unavailable')
        return value
    return receive


def terminate(child):
    if child is None:return
    if child.poll() is None:
        child.terminate()
        try:child.wait(timeout=8)
        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
    if child.poll() is None:raise RuntimeError('Desktop connector termination unconfirmed')


def config_text(root,bootstrap):
    from bench_gateway.setup import cc_config
    cfg={'project_root':str(root),'cc_project':bootstrap['cc_project'],'feishu_app_id':bootstrap['app_id']}
    path=cc_config(cfg)
    q=lambda v:json.dumps(str(v),ensure_ascii=False)
    python=Path(sys.executable)
    # GUI Python preserves redirected stdio while avoiding a console window
    # when CC Connect starts its exec adapter on Windows.
    if os.name=='nt' and python.with_name('pythonw.exe').is_file():python=python.with_name('pythonw.exe')
    lines=path.read_text(encoding='utf-8').splitlines()
    result=[]
    for line in lines:
        if line.startswith('cmd = '):
            argv=[python,root/'desktop_gateway_agent.py']
            settings=json.loads((root/'config/gateway.json').read_text(encoding='utf-8-sig'))
            adapter=root/'tools/desktop_gateway_adapter.exe'
            if settings.get('desktop_menu_native_adapter') and adapter.is_file():argv.insert(0,adapter)
            line='cmd = ['+', '.join(q(x) for x in argv)+']'
        if line.startswith('data_dir = '):line='data_dir = '+q(root/'runtime/desktop-frontend/cc-cache')
        result.append(line)
        if line.startswith('allow_chat = '):result.append('gateway_file_references = true')
    path.write_text('\n'.join(result)+'\n',encoding='utf-8')
    return path


def serve(root):
    cfg=json.loads((root/'config/gateway.json').read_text(encoding='utf-8-sig'))
    cache=root/'runtime/desktop-frontend';cache.mkdir(parents=True,exist_ok=True)
    status_path=cache/'status.json'
    def status(state,**extra):
        value={'state':state,'pid':os.getpid(),'updated':time.time(),'build':'desktop-return-binary-v3',**extra}
        tmp=status_path.with_suffix('.tmp');tmp.write_text(json.dumps(value));os.replace(tmp,status_path)
    control=None;child=None;lease=None
    stage='CONTROL_START';failure=None;ssh_errors=[]
    client=uuid.uuid4().hex
    connected=threading.Event()
    try:
        status('CONNECTING')
        control=subprocess.Popen(command(cfg,'desktop_connector_lease.py'),stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0,**options())
        def error_categories():
            for raw in iter(lambda:control.stderr.readline(8193),b''):
                line=raw.decode('utf-8',errors='replace')
                for phrase,code in [('Permission denied','AUTH_DENIED'),('Host key verification failed','HOST_KEY'),
                                    ('Could not resolve hostname','HOST_ALIAS'),('Connection timed out','CONNECT_TIMEOUT')]:
                    if phrase in line:ssh_errors[:]=[code]
        threading.Thread(target=error_categories,daemon=True).start()
        receive=packets(control.stdout)
        def send(value):control.stdin.write((json.dumps(value)+'\n').encode('utf-8'));control.stdin.flush()
        send({'op':'open','client':client,'generation':cfg['desktop_bridge_generation']})
        stage='CONTROL_BOOTSTRAP'
        bootstrap=receive(timeout=60)
        if bootstrap.get('kind')!='bootstrap' or bootstrap.get('generation')!=cfg['desktop_bridge_generation']:
            raise ValueError('Unexpected connector bootstrap')
        lease=bootstrap['lease'];stage='CONFIGURATION';config=config_text(root,bootstrap)
        status('STANDBY')
        stage='HEARTBEAT'
        while True:
            send({'op':'heartbeat','lease':lease,'client':client,'connected':connected.is_set(),
                  'pid':child.pid if child else None})
            ack=receive()
            if ack.get('kind')!='ack' or ack.get('lease')!=lease:raise ValueError('Unexpected lease response')
            if ack.get('allow_connect') is True and child is None:
                env=os.environ.copy();env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
                env['BENCH_FEISHU_APP_SECRET']=bootstrap.pop('app_secret')
                child=subprocess.Popen([cfg['desktop_cc_binary'],'--config',str(config)],cwd=root,env=env,
                    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                    text=True,encoding='utf-8',errors='replace',**options())
                env.pop('BENCH_FEISHU_APP_SECRET',None)
                def logs():
                    for line in child.stdout:
                        if '[connected to wss://' in line:connected.set()
                        if 'disconnected' in line.lower():connected.clear()
                threading.Thread(target=logs,daemon=True).start()
                status('STARTING',connector_pid=child.pid)
            if child is not None:
                if child.poll() is not None:raise RuntimeError('Desktop CC Connect exited')
                if ack.get('allow_connect') is not True:raise RuntimeError('Desktop connector authorization withdrawn')
                status('CONNECTED' if connected.is_set() else 'STARTING',connector_pid=child.pid)
            time.sleep(2)
    except Exception as exc:
        failure={'stage':stage,'error_type':type(exc).__name__,'ssh_error_category':ssh_errors[-1] if ssh_errors else 'UNCLASSIFIED'}
        status('STOPPING_OR_UNCONFIRMED',failure=failure)
    finally:
        stopped=False
        try:terminate(child);stopped=True
        except Exception:pass
        released=False
        if stopped and control is not None and lease:
            value={'op':'release','lease':lease,'client':client,'generation':cfg['desktop_bridge_generation'],'child_stopped':True}
            try:
                control.stdin.write((json.dumps(value)+'\n').encode('utf-8'));control.stdin.flush()
                released=receive().get('kind')=='released'
            except Exception:
                # Only this host, after positive child termination, may acknowledge
                # an interrupted lease. Never clear another instance's record.
                try:
                    result=subprocess.run(command(cfg,'desktop_connector_lease.py'),input=json.dumps(value)+'\n',
                        capture_output=True,text=True,encoding='utf-8',timeout=20,**options())
                    released=result.returncode==0 and json.loads(result.stdout).get('kind')=='released'
                except Exception:pass
        if control is not None:
            try:control.stdin.close();control.wait(timeout=5)
            except Exception:control.terminate()
        status('STOPPED' if stopped and released else 'NEEDS_REVIEW',**({'failure':failure} if failure else {}))


if __name__=='__main__':
    if os.name=='nt' and Path(sys.executable).name.casefold()=='pythonw.exe':
        # Keep the native GUI action resident, but give the pipe controller
        # normal console-Python stdio handles without showing a window.
        env=os.environ.copy();env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
        if env.get('USERPROFILE'):env['HOME']=env['USERPROFILE']
        raise SystemExit(subprocess.call([str(Path(sys.executable).with_name('python.exe')),'-B',str(Path(__file__).resolve())],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,**options()))
    serve(Path(__file__).resolve().parent)
