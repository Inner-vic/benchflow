"""Bot-only message provenance and bounded resource download; never log credentials."""
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import quote,urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from .core import GatewayError

BASE='https://open.feishu.cn/open-apis'


class FeishuFiles:
    def __init__(self,cfg):
        self.cfg=cfg;self.token=None;self.expires=0

    def auth(self,timeout=25):
        if self.token and time.monotonic()<self.expires:return self.token
        secret=os.environ.get('BENCH_FEISHU_APP_SECRET')
        if not secret:
            value=json.loads((Path(self.cfg['project_root'])/'runtime/credentials.json').read_text(encoding='utf-8'))
            if value.get('app_id')!=self.cfg['feishu_app_id']:raise GatewayError('附件接收的机器人身份不一致')
            secret=value['app_secret']
        body=json.dumps({'app_id':self.cfg['feishu_app_id'],'app_secret':secret}).encode()
        value=self._json('/auth/v3/tenant_access_token/internal',body=body,authenticated=False,**({'timeout':timeout} if timeout!=25 else {}))
        self.token=value['tenant_access_token'];self.expires=time.monotonic()+max(1,int(value.get('expire',3600))-60)
        return self.token

    def _json(self,path,body=None,authenticated=True,method=None,timeout=25):
        headers={'Content-Type':'application/json; charset=utf-8'}
        if authenticated:headers['Authorization']='Bearer '+(self.auth() if timeout==25 else self.auth(timeout=timeout))
        try:
            with urlopen(Request(BASE+path,data=body,headers=headers,method=method),timeout=timeout) as response:
                raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:raise ValueError('oversized response')
            value=json.loads(raw)
            if value.get('code')!=0:raise GatewayError('飞书附件接口未授权或返回失败，错误码：'+str(value.get('code'))+'。请管理员检查该机器人的消息读取和资源下载权限。')
            return value
        except HTTPError as exc:
            try:code=json.loads(exc.read(65536)).get('code','UNKNOWN')
            except Exception:code='UNKNOWN'
            raise GatewayError(f'飞书附件接口 HTTP {exc.code}，错误码 {code}；请管理员核对本机器人的消息读取和资源下载权限。') from None
        except GatewayError:raise
        except Exception:raise GatewayError('飞书附件接口连接失败，请稍后重试；文件尚未登记为可用材料。') from None

    def message(self,mid,timeout=25):
        value=self._json('/im/v1/messages/'+quote(mid,safe='')+'?user_id_type=open_id',timeout=timeout)
        items=value.get('data',{}).get('items',[])
        if len(items)!=1:raise GatewayError('无法核验附件原消息')
        return items[0]

    def upload_report(self,path,timeout=25):
        """Small generated report upload under this bot, not a generic data pipe."""
        import uuid
        path=Path(path)
        if path.name not in ('arrival-accuracy.xlsx','analysis_plan.json') or not 0<path.stat().st_size<=4*1024*1024:
            raise GatewayError('到点报告文件不符合上传要求。')
        boundary='bench-report-'+uuid.uuid4().hex
        fields=[]
        for name,value in (('file_type','stream'),('file_name',path.name)):
            fields.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+name+'"\r\n\r\n'+value+'\r\n').encode())
        fields.extend([('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="'+path.name+'"\r\nContent-Type: application/octet-stream\r\n\r\n').encode(),path.read_bytes(),('\r\n--'+boundary+'--\r\n').encode()])
        try:
            req=Request(BASE+'/im/v1/files',data=b''.join(fields),headers={
                'Authorization':'Bearer '+self.auth(timeout=timeout),'Content-Type':'multipart/form-data; boundary='+boundary})
            with urlopen(req,timeout=timeout) as response:value=json.loads(response.read(65536))
            if value.get('code')!=0 or not value.get('data',{}).get('file_key'):raise ValueError('upload rejected')
            return value['data']['file_key']
        except Exception:raise GatewayError('到点报告上传未完成；计算结果已保留。') from None

    def reply_report(self,mid,file_key,key,timeout=25):
        value=self._json('/im/v1/messages/'+quote(mid,safe='')+'/reply',body=json.dumps({
            'msg_type':'file','content':json.dumps({'file_key':file_key}),'uuid':key}).encode(),timeout=timeout)
        return value['data']['message_id']

    def messages(self,chat,page_size=50):
        page_size=max(1,min(50,int(page_size)))
        value=self._json('/im/v1/messages?'+urlencode({'container_id_type':'chat','container_id':chat,'page_size':page_size,'sort_type':'ByCreateTimeDesc'}))
        return value.get('data',{}).get('items',[])

    def download(self,mid,key,path,maximum=128*1024*1024):
        path=Path(path);temp=path.with_suffix('.partial');path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() or temp.exists():raise GatewayError('测试下载路径已存在，不覆盖原文件')
        started=time.monotonic();total=0;digest=hashlib.sha256()
        url=BASE+'/im/v1/messages/'+quote(mid,safe='')+'/resources/'+quote(key,safe='')+'?type=file'
        try:
            with urlopen(Request(url,headers={'Authorization':'Bearer '+self.auth()}),timeout=60) as response,temp.open('xb') as out:
                if int(response.headers.get('Content-Length','0') or 0)>maximum:raise GatewayError('附件超过本入口大小上限')
                while True:
                    chunk=response.read(1024*1024)
                    if not chunk:break
                    total+=len(chunk)
                    if total>maximum:raise GatewayError('附件超过本入口大小上限')
                    digest.update(chunk);out.write(chunk)
            if not total:raise GatewayError('附件下载为空')
            os.replace(temp,path)
            elapsed=time.monotonic()-started
            return {'bytes':total,'sha256':digest.hexdigest(),'seconds':round(elapsed,3),'mib_per_second':round(total/1024**2/max(elapsed,.001),3)}
        except GatewayError:raise
        except Exception:raise GatewayError('飞书资源下载失败，未完成的文件不会登记') from None
        finally:temp.unlink(missing_ok=True)
