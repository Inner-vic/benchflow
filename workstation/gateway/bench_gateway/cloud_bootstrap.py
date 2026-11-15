"""A bounded fixed-instance lifecycle tool using the supported platform CLI.

No browser fallback, approval bypass, stop, restart, replay, or evaluation.
"""
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

from .cloud_environment import inspect_instance,diagnose
from .cloud_provider import transport
from .config import save
from .core import GatewayError,worker_lock

TARGET=('bench-eval-offline','vc-chassis','cnhb01-algorithm-bidu-p-03')
EXPECTED_UID='395dafd5-4701-4480-8fad-3c33e9d24e53'
PENDING={'REQUESTING','WAITING_APPROVAL','STARTING','START_RESULT_UNKNOWN'}
WAITABLE={'STARTING','STOPPING','RECEIVER_NOT_READY','UNKNOWN_PLATFORM_STATE'}


def request_start(binary,profile):
    try:
        p=subprocess.run([binary,'devspaces','start','--name',profile['instance'],
            '--namespace',profile['namespace'],'--cluster',profile['cluster'],'--timeout','15'],
            capture_output=True,text=True,encoding='utf-8',timeout=25)
        if len(p.stdout)>1024*1024:raise ValueError('oversized response')
        reply=json.loads(p.stdout)
        nested=reply.get('data') if isinstance(reply.get('data'),dict) else {}
        if reply.get('status')=='pending_approval' or nested.get('status')=='pending_approval':
            approval=reply.get('approval_id') or nested.get('approval_id')
            result={'state':'WAITING_APPROVAL','reason':'平台要求确认；未完成启动，不重复提交。'}
            if isinstance(approval,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',approval):result['approval_id']=approval
            return result
        if p.returncode or reply.get('status') in ('error','failed','denied'):
            return {'state':'START_REJECTED','reason':'平台拒绝启动，请管理员核对启动身份和权限。'}
        if reply.get('status')=='ok':return {'state':'STARTING'}
        return {'state':'START_RESULT_UNKNOWN','reason':'启动返回格式未知，仅查询同一实例，不重复提交。'}
    except Exception:
        return {'state':'START_RESULT_UNKNOWN','reason':'启动请求结果待核验，仅查询同一实例，不重复提交。'}


class EnvironmentTool:
    def __init__(self,cfg,profile,*,binary,control_root,backend='cli'):
        if tuple(profile.get(k) for k in ('instance','namespace','cluster'))!=TARGET:
            raise GatewayError('环境工具仅允许已授权的固定专用评测实例。')
        self.cfg,self.profile,self.binary=cfg,profile,binary
        if backend not in ('cli','browser'):raise GatewayError('未知环境启动后端。')
        self.backend=backend
        self.root=Path(control_root)
        self.path=self.root/'operation.json'

    def inspect(self,previous_generation=None):
        instance=inspect_instance(self.binary,self.profile)
        if instance.get('state')=='OBSERVED' and instance.get('uid')!=EXPECTED_UID:
            return {'state':'INSTANCE_ID_CHANGED','browser_action':'NONE'},instance,None
        try:h=transport(self.cfg,{'action':'heartbeat','remote_prefix':self.profile['volume_prefix']})
        except Exception:h=None
        return diagnose(instance,h,self.profile,previous_generation=previous_generation),instance,h

    def check(self):
        value,instance,_=self.inspect()
        return {**value,'instance':self.profile['instance'],'uid':instance.get('uid'),
                'platform_phase':instance.get('phase'),'checked_at':time.time()}

    def ensure(self,*,allow_start=False,wait_seconds=0):
        if type(wait_seconds) not in (int,float) or not 0<=wait_seconds<=180:
            raise GatewayError('环境等待时间必须在0至180秒之间。')
        # Local PC lock only. Never use or inspect the workstation bench.lock.
        with worker_lock(self.root):
            submitted=False
            operation=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else None
            if operation and (operation.get('target')!=list(TARGET) or operation.get('uid')!=EXPECTED_UID):
                raise GatewayError('环境控制记录身份不一致，保留原记录，需管理员核验。')
            previous=operation.get('previous_generation') if operation and operation.get('state') in PENDING else None
            value,instance,h=self.inspect(previous)
            if value['state']=='READY':
                if operation:
                    operation.update(state='READY',ready_generation=value['generation'],updated=time.time());save(self.path,operation)
                return {**value,'instance':self.profile['instance'],'start_submitted':False}
            if value['state']=='STOPPED':
                if operation and operation.get('state') in PENDING:
                    return {'state':operation['state'],'instance':self.profile['instance'],
                            'operation_id':operation['id'],'start_submitted':False,
                            **({'approval_id':operation['approval_id']} if operation.get('approval_id') else {})}
                if not allow_start:return {**value,'start_submitted':False}
                if operation and operation.get('state')=='START_REJECTED':
                    return {'state':'START_REJECTED','reason':'此前已明确拒绝；修复权限并由管理员核验记录后再提交。','start_submitted':False}
                operation={'id':'environment-'+uuid.uuid4().hex[:16],'target':list(TARGET),'uid':EXPECTED_UID,
                           'state':'REQUESTING','created':time.time(),'previous_generation':(h or {}).get('generation')}
                save(self.path,operation)  # Durable before invoking any write API.
                submitted=True
                if self.backend=='browser':
                    from .cloud_browser_start import request_browser_start
                    started=request_browser_start(self.cfg,self.profile,self.root)
                else:
                    started=request_start(self.binary,self.profile)
                operation.update(started,updated=time.time());save(self.path,operation)
                if started['state']!='STARTING':
                    return {**started,'operation_id':operation['id'],'start_submitted':True}
                value={'state':'STARTING','browser_action':'NONE'}
            if value['state'] not in WAITABLE or not wait_seconds:
                return {**value,'start_submitted':submitted}
            until=time.monotonic()+wait_seconds
            while time.monotonic()<until:
                time.sleep(min(3,max(0,until-time.monotonic())))
                previous=operation.get('previous_generation') if operation and operation['state'] in PENDING else None
                value,instance,h=self.inspect(previous)
                if value['state']=='READY':
                    if operation:
                        operation.update(state='READY',ready_generation=value['generation'],updated=time.time());save(self.path,operation)
                    return {**value,'start_submitted':submitted,'operation_id':operation['id'] if operation else None}
                if value['state'] not in WAITABLE:break
            return {**value,'start_submitted':submitted,'operation_id':operation['id'] if operation else None,
                    'reason':'本次等待结束，保留环境状态；未重复提交、未回灌或评测。'}
