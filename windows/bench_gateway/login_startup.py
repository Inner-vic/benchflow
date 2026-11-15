"""Current-user login launcher; reuses the existing single supervisor."""
import time
from pathlib import Path

from .config import save
from .deployment import status,start


def run(cfg,wait_seconds=45):
    if cfg.get('deployment_role')=='ai-client':
        return start(cfg)
    before=status(cfg)
    if before.get('active'):
        result={'state':'ALREADY_RUNNING','service_instance':before.get('instance')}
    else:
        start(cfg)
        until=time.monotonic()+wait_seconds
        while True:
            health=status(cfg)
            if health.get('active') and health.get('phase')=='RUNNING' and all(
                    health.get('children',{}).get(role,{}).get('ready') for role in ('bridge','worker')):
                result={'state':'RUNNING','service_instance':health.get('instance')};break
            if time.monotonic()>=until:
                result={'state':'NOT_READY','reason':'入口启动未就绪，请查看现有服务日志。'};break
            time.sleep(.5)
    save(Path(cfg['project_root'])/'runtime/service/login-start.json',dict(result,checked_at=time.time()))
    return result
