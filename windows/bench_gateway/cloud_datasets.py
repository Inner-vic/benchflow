"""Personal cloud data preparation, called only after the normal material confirmation."""
import json,queue,subprocess,threading,time
from pathlib import Path
from .core import GatewayError
from .cloud_dataset_paths import parse_root
from .config import save

def call(cfg,request,progress=None,timeout=180):
    process=subprocess.Popen([cfg['cloud_sdk_python'],'-B',str(Path(cfg['project_root'])/'bench_gateway/cloud_dataset_sdk.py')],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8')
    options={k:cfg[k] for k in ('workstation','cloud_dataset_volumes')}
    if cfg.get('ssh_binary'):options['ssh_binary']=cfg['ssh_binary']
    process.stdin.write(json.dumps({**request,'config':options}))
    process.stdin.close();events=queue.Queue()
    def read():
        try:
            for line in process.stdout:events.put(json.loads(line))
        except Exception:events.put({'kind':'invalid'})
        finally:events.put(None)
    thread=threading.Thread(target=read,daemon=True);thread.start();until=time.monotonic()+timeout;result=None
    try:
        while True:
            remaining=until-time.monotonic()
            if remaining<=0:raise GatewayError('数据准备等待超时；下载状态需核对，未提交台架，不自动重试。')
            try:event=events.get(timeout=min(remaining,5))
            except queue.Empty:continue
            if event is None:break
            if event.get('kind')=='progress':
                if progress:progress(event['value'])
            elif event.get('kind')=='result':result=event
            else:raise GatewayError('数据准备响应格式异常；未提交台架。')
        process.wait(timeout=10)
        if not result or not result.get('ok'):raise GatewayError((result or {}).get('message') or '数据准备连接中断，状态待核对；未提交台架。')
        return result['data']
    finally:
        if process.poll() is None:
            process.terminate()  # Ends this PC control process; partial inputs remain, never a hardware run.

class CloudDatasets:
    def __init__(self,cfg,store):self.cfg,self.store=cfg,store
    def plan(self,user,path,scope):
        self.store.require_user(user)
        if not self.cfg.get('cloud_dataset_enabled'):raise GatewayError('开发空间数据入口尚未启用；可继续提供工作站路径。')
        try:parse_root(path,self.cfg.get('cloud_dataset_volumes') or {})
        except ValueError:raise GatewayError('开发空间路径无效或数据卷尚未接入；请提供已挂载卷内的数据根目录。') from None
        from .preparation import scene_selection
        found=call(self.cfg,{'action':'discover','source_path':path})
        indices=scene_selection(scope,found['scene_count'])
        plan=call(self.cfg,{'action':'plan','source_path':path,'indices':indices},timeout=300)
        if plan['scene_names']!=found['scene_names']:raise GatewayError('准备期间云端场景清单发生变化；本次未下载。')
        if plan['download_bytes']>self.cfg.get('cloud_dataset_max_bytes',64*1024**3):raise GatewayError('本次回放数据超过下载上限，请缩小场景范围。')
        return plan
    def prepare(self,user,job,plan,progress=None):
        self.store.require_user(user)
        path=Path(self.store.user_root(user))/'cloud-inputs'/job['id']/'plan.json';save(path,plan)
        result=call(self.cfg,{'action':'prepare','source_path':plan['source']['requested_path'],'plan':plan,
            'owner_root':self.store.remote_root(user,self.cfg['remote_users_root']),
            'max_bytes':self.cfg.get('cloud_dataset_max_bytes',64*1024**3),'workers':8},progress=progress,
            timeout=self.cfg.get('cloud_dataset_timeout_seconds',3600))
        from .dataset_sources import DatasetSources
        data=result['data']
        if result.get('state')!='PREPARED' or result.get('gt_downloaded') is not False or data.get('cloud_source',{}).get('plan_id')!=plan['plan_id']:
            raise GatewayError('数据准备收据与本次来源不一致；未登记数据源。')
        sources=DatasetSources(self.cfg,self.store)
        asset=sources.call(user,'register',name='cloud-'+plan['plan_id'][:24],data=data,users_root=self.cfg['remote_users_root'],
            owner_root=self.store.remote_root(user,self.cfg['remote_users_root']))
        save(path.parent/'receipt.json',{'result':result,'asset':asset});return asset
