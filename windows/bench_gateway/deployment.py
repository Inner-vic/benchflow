"""Local lifecycle management for the bridge and serial worker; no system service changes."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from .config import load,save
from .core import GatewayError,Store,worker_lock
from .setup import launch_command


def directory(cfg):
    return Path(cfg['project_root'])/'runtime/service'


def read(path):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}


def active(state,now=None):
    return state.get('phase') in ('STARTING','RUNNING','STOPPING') and 0 <= (time.time() if now is None else now)-state.get('heartbeat',0) < 10


def stop_matches(request,state):
    return bool(state.get('instance') and request.get('instance')==state['instance'])


class _HeartbeatWriter:
    """A status-file failure must not orphan live bridge/worker processes."""
    def __init__(self,path):
        self.path=path
        self.failed=False
        self.last_notice=0

    @staticmethod
    def notice(event,**fields):
        try:print(json.dumps({'component':'supervisor','event':event,**fields}),flush=True)
        except OSError:pass  # Full/unavailable log storage must not orphan children either.

    def write(self,state):
        try:
            save(self.path,state)
        except OSError as error:
            now=time.monotonic()
            if not self.failed or now-self.last_notice>=30:
                # Do not log paths, state payloads or exception text.
                self.notice('heartbeat_write_failed',error_type=type(error).__name__)
                self.last_notice=now
            self.failed=True
            return False
        if self.failed:
            self.notice('heartbeat_write_recovered')
        self.failed=False
        return True


def status(cfg):
    if cfg.get('deployment_role')=='ai-client':
        from .ai_client_admin import status as remote_status
        return remote_status(cfg)
    state=read(directory(cfg)/'status.json')
    alive=active(state)
    if not alive and state.get('phase') in ('STARTING','RUNNING','STOPPING'):state['phase']='STALE'
    result={**state,'active':alive,'mode':cfg['mode'],'live_enabled':cfg.get('live_enabled',False),
            'notifications_enabled':cfg.get('notifications_enabled',False),'log_directory':str(directory(cfg)/'logs')}
    with Store(cfg['state_root']).connect() as c:
        result['jobs']={r['state']:r['count'] for r in c.execute('SELECT state,count(*) AS count FROM jobs GROUP BY state')}
        result['job_lanes']={lane:{r['state']:r['count'] for r in c.execute('SELECT state,count(*) AS count FROM jobs WHERE lane=? GROUP BY state',(lane,))} for lane in ('hardware','postprocess')}
        result['pending_notifications']=c.execute('SELECT count(*) FROM outbox WHERE sent=0').fetchone()[0]+c.execute("SELECT count(*) FROM queue_outbox q JOIN jobs j ON j.id=q.job WHERE q.sent=0 AND j.state IN ('QUEUED','RUNNING')").fetchone()[0]
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparations'").fetchone():
            result['preparations']={r['state']:r['count'] for r in c.execute('SELECT state,count(*) AS count FROM preparations GROUP BY state')}
            result['pending_notifications']+=c.execute("SELECT count(*) FROM preparations WHERE state IN ('STATIC_PASSED','NEEDS_INPUT') AND sent=0").fetchone()[0]
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_jobs'").fetchone():
            result['archives']={r['state']:r['count'] for r in c.execute('SELECT state,count(*) AS count FROM archive_jobs GROUP BY state')}
    if not result.get('phase'):result['phase']='STOPPED'
    return result


def start(cfg):
    if cfg.get('deployment_role')=='ai-client':
        from .ai_client_admin import start as start_client
        return start_client(cfg)
    current=status(cfg)
    if current['active']:return current
    from .windows_host import manifest,start_registered
    registered=manifest(cfg)
    if registered:return start_registered(cfg,registered)
    root=directory(cfg);root.mkdir(parents=True,exist_ok=True)
    env=os.environ.copy();env['PYTHONUTF8']='1'
    argv=[sys.executable,'-m','bench_gateway.deployment','--root',cfg['project_root'],'run']
    options={'creationflags':subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=='nt' else {'start_new_session':True}
    with (root/'supervisor.log').open('ab') as log:
        process=subprocess.Popen(argv,cwd=cfg['project_root'],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,**options)
    # Wait only for this new instance; a stale heartbeat must not count as startup success.
    for _ in range(40):
        current=status(cfg)
        if current.get('pid')==process.pid and current['active']:return current
        if process.poll() is not None:
            # A concurrent start may have won the process lock.
            if current['active']:return current
            raise GatewayError('后台启动失败，请查看 runtime/service/supervisor.log')
        time.sleep(.2)
    return {'phase':'STARTING','pid':process.pid,'log_directory':str(root)}


def stop(cfg,wait_seconds=12):
    if cfg.get('deployment_role')=='ai-client':
        from .ai_client_admin import stop as stop_client
        return stop_client(cfg,wait_seconds)
    current=status(cfg)
    if not current['active']:return current
    save(directory(cfg)/'stop.json',{'instance':current['instance']})
    until=time.monotonic()+wait_seconds
    while time.monotonic()<until:
        current=status(cfg)
        if not current['active']:return current
        time.sleep(.3)
    return {**current,'note':'正在排空当前任务；任务结束后停止，不强行终止台架进程。'}


def supervise(cfg):
    if cfg.get('deployment_role')=='ai-client':
        from .ephemeral_ai import serve
        return serve(cfg)
    root=directory(cfg);logs=root/'logs';logs.mkdir(parents=True,exist_ok=True)
    with worker_lock(root):
        state={'instance':uuid.uuid4().hex,'pid':os.getpid(),'phase':'STARTING','started':time.time(),'children':{}}
        children={};files={};retry={};offsets={}
        drain=root/'drain.json';drain.unlink(missing_ok=True)
        state_path=root/'status.json'
        heartbeat_writer=_HeartbeatWriter(state_path)
        def request_stop(*_):save(root/'stop.json',{'instance':state['instance']})
        signal.signal(signal.SIGINT,request_stop)
        signal.signal(signal.SIGTERM,request_stop)
        try:
            while True:
                now=time.time()
                stopping=stop_matches(read(root/'stop.json'),state)
                if stopping:
                    state['phase']='STOPPING'
                    save(drain,{'instance':state['instance']})
                for role in ('bridge','worker'):
                    child=children.get(role)
                    entry=state['children'].setdefault(role,{'restarts':0,'running':False,'ready':False})
                    if child is not None and child.poll() is not None:
                        entry.update(running=False,ready=False,last_exit=child.returncode)
                        if files.get(role):files.pop(role).close()
                        children.pop(role);child=None
                        retry[role]=now+min(30,2**min(entry['restarts'],5))
                    if child is None and not stopping and now>=retry.get(role,0):
                        log_path=logs/(role+'.log')
                        if log_path.exists() and log_path.stat().st_size>5*1024*1024:
                            previous=log_path.with_suffix('.previous.log');os.replace(log_path,previous)
                        offsets[role]=log_path.stat().st_size if log_path.exists() else 0
                        files[role]=log_path.open('ab')
                        try:
                            if role=='bridge':argv,env=launch_command(load(cfg['project_root']))
                            else:
                                argv=[sys.executable,'-m','bench_gateway.cli','--root',cfg['project_root'],'worker']
                                env=os.environ.copy();env.update(PYTHONUTF8='1',BENCH_GATEWAY_MANAGED='1')
                            options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
                            children[role]=subprocess.Popen(argv,cwd=cfg['project_root'],env=env,stdin=subprocess.DEVNULL,
                                                            stdout=files[role],stderr=subprocess.STDOUT,**options)
                            entry.update(pid=children[role].pid,running=True,ready=False,started=now,restarts=entry['restarts']+1)
                            entry.pop('error',None)
                        except Exception as exc:
                            # Keep the supervisor and the other component alive. Never log credential-bearing arguments.
                            entry.update(running=False,ready=False,error=type(exc).__name__)
                            files.pop(role).close();retry[role]=now+10
                    if entry['running'] and not entry['ready']:
                        with (logs/(role+'.log')).open('rb') as log:
                            log.seek(offsets[role]);recent=log.read()[-65536:]
                        marker=b'[connected to wss://' if role=='bridge' else b'"event": "ready"'
                        entry['ready']=marker in recent
                if stopping and 'worker' not in children:
                    bridge=children.get('bridge')
                    if bridge is not None:
                        bridge.terminate()
                        try:bridge.wait(timeout=8)
                        except subprocess.TimeoutExpired:bridge.kill();bridge.wait(timeout=5)
                    for entry in state['children'].values():entry.update(running=False,ready=False)
                    state.update(phase='STOPPED',heartbeat=time.time());save(state_path,state)
                    return
                if not stopping:state['phase']='RUNNING' if all(v['ready'] for v in state['children'].values()) else 'STARTING'
                state['heartbeat']=time.time();heartbeat_writer.write(state)
                time.sleep(1)
        finally:
            for f in files.values():f.close()
            drain.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description='台架统一入口本机后台部署管理')
    parser.add_argument('--root',type=Path)
    parser.add_argument('action',choices=['start','stop','status','run'])
    args=parser.parse_args();cfg=load(args.root) if args.root else load()
    try:
        if args.action=='run':supervise(cfg);return
        result={'start':start,'stop':stop,'status':status}[args.action](cfg)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except GatewayError as exc:
        print(str(exc));raise SystemExit(2)


if __name__=='__main__':main()
