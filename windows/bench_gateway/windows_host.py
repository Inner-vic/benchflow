"""Current-user Windows Task Scheduler hosting for the existing supervisor."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .core import GatewayError


def task_name(cfg):
    root=str(Path(cfg['project_root']).resolve()).casefold()
    return 'BenchGateway-'+hashlib.sha256(root.encode()).hexdigest()[:16]


def manifest_path(cfg):return Path(cfg['project_root'])/'runtime/service/windows-task.json'


def manifest(cfg):
    path=manifest_path(cfg)
    if not path.exists():return None
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        valid=(value.get('schema_version')==1 and value.get('enabled') is True
               and value.get('task_name')==task_name(cfg)
               and Path(value['project_root']).resolve()==Path(cfg['project_root']).resolve())
    except (OSError,ValueError,KeyError,TypeError):valid=False
    if not valid:raise GatewayError('Windows入口托管登记无效，请管理员检查；未改为临时进程启动。')
    return value


def start_registered(cfg,registration):
    """Ask Windows to launch it; never detach from or alter a tool job object."""
    from .deployment import status,read,directory,stop_matches
    from .config import save
    if os.name!='nt':raise GatewayError('已登记Windows托管，但当前不是Windows环境。')
    if registration!=manifest(cfg):raise GatewayError('Windows入口托管登记已变化，请重新核对。')
    current=status(cfg);stop_path=directory(cfg)/'stop.json'
    if stop_matches(read(stop_path),current):
        if current.get('phase')!='STOPPED':
            raise GatewayError('此前停止请求尚未确认完成，请先核验原监督器；未取消停止或重复启动。')
        # Only an explicit standard start resumes a completed administrator
        # stop. Periodic native triggers never clear this intent.
        save(stop_path,{'instance':''})
    executable=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/schtasks.exe'
    try:
        reply=subprocess.run([str(executable),'/Run','/TN',registration['task_name']],
                             capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError,subprocess.TimeoutExpired):
        raise GatewayError('Windows托管入口启动请求未确认，请检查原生任务状态；不另启临时监督器。') from None
    if reply.returncode:raise GatewayError('Windows托管入口未接受启动请求，请管理员检查任务权限或启用状态。')
    until=time.monotonic()+8
    while True:
        current=status(cfg)
        if current.get('active'):return current
        if time.monotonic()>=until:return {'phase':'STARTING','hosting':'windows-task','task_name':registration['task_name']}
        time.sleep(.2)


def run_host(cfg):
    """Remain the scheduled action while the original supervisor runs below it."""
    if cfg.get('deployment_role')=='ai-client':
        # Task Scheduler IgnoreNew provides the PC singleton. Prompts, results,
        # heartbeat and errors are held by the workstation, not a local log.
        from .ephemeral_ai import serve
        serve(cfg)
        return 0
    from .deployment import status,read,directory,stop_matches
    root=Path(cfg['project_root']);folder=root/'runtime/service';folder.mkdir(parents=True,exist_ok=True)
    log_path=folder/'windows-host.log'
    with log_path.open('a',encoding='utf-8',buffering=1) as output:
        def notice(event,**fields):
            output.write(json.dumps({'time':time.time(),'component':'windows-host','event':event,**fields})+'\n');output.flush()
        try:
            # Existing single-supervisor and worker locks remain authoritative.
            # A duplicate launcher does not terminate or take over an owner.
            current=status(cfg)
            if current.get('active'):
                notice('already_running',instance=current.get('instance'));return 0
            if stop_matches(read(directory(cfg)/'stop.json'),current):
                notice('intentional_stop_preserved',instance=current.get('instance'));return 0
            interpreter=Path(sys.executable)
            if interpreter.name.casefold()=='pythonw.exe':interpreter=interpreter.with_name('python.exe')
            if not interpreter.is_file():raise FileNotFoundError('Console Python unavailable')
            argv=[str(interpreter),'-B','-m','bench_gateway.deployment','--root',str(root),'run']
            env=os.environ.copy();env['PYTHONUTF8']='1'
            options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            child=subprocess.Popen(argv,cwd=str(root),env=env,stdin=subprocess.DEVNULL,
                                   stdout=output,stderr=subprocess.STDOUT,**options)
            notice('supervisor_started',host_pid=os.getpid(),supervisor_pid=child.pid)
            code=child.wait()
            notice('supervisor_exited',exit_code=code)
            # Native periodic triggers can retry later; the matching stop
            # record above keeps an intentional normal drain stopped.
            return 0 if code==0 else 1
        except Exception as error:
            notice('hosting_failed',error_type=type(error).__name__);return 1
