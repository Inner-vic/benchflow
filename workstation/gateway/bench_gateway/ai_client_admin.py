"""PC program lifecycle; all business health/state is read from the workstation."""
import json,os,subprocess
from pathlib import Path
from .core import GatewayError


def remote(cfg,action='status',wait_seconds=12):
    from .transfer import SSH
    code='''import sys,json
sys.path.insert(0,sys.argv[1])
from bench_gateway.config import load
from bench_gateway.deployment import status,stop
from bench_gateway.ai_broker import health
cfg=load(sys.argv[1]);result=stop(cfg,int(sys.argv[3])) if sys.argv[2]=='stop' else status(cfg)
result['ai_client']=health(cfg)
print(json.dumps(result))
'''
    return json.loads(SSH(cfg).python(code,(cfg['remote_project_root'],action,int(wait_seconds)),timeout=max(20,wait_seconds+15)))


def status(cfg):return remote(cfg)


def native(cfg,action):
    from .windows_host import manifest
    registered=manifest(cfg)
    if os.name!='nt' or not registered:raise GatewayError('个人电脑解析器尚未登记Windows后台托管。')
    executable=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/schtasks.exe'
    result=subprocess.run([str(executable),action,'/TN',registered['task_name']],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise GatewayError('Windows解析器托管请求未确认，请检查原生任务。')


def start(cfg):
    native(cfg,'/Run')
    return {'hosting':'windows-task','role':'memory-only-ai-client','state':'START_REQUESTED'}


def stop(cfg,wait_seconds=12):
    result=remote(cfg,'stop',wait_seconds)
    if result.get('active'):return result
    native(cfg,'/End')
    return result
