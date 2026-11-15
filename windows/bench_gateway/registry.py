"""Authoritative assets live on the workstation. SQLite is only an index cache."""
import base64
import json
from pathlib import Path,PurePosixPath

from .core import GatewayError,digest,within
from .transfer import SSH


CATALOG_SCRIPT=r'''
import base64,hashlib,json,os,pathlib,sys,time,uuid
r=json.loads(base64.b64decode(sys.argv[1]));root=pathlib.Path(r['root'])
assert not root.is_symlink(),'owner root cannot be a link'
root.mkdir(parents=True,exist_ok=True);root=root.resolve(strict=True)
catalog=root/'catalog.json';lock=root/'.catalog.lock'
assert not catalog.is_symlink() and not lock.is_symlink(),'catalog cannot be a link'
def h(p):
    d=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):d.update(b)
    return d.hexdigest()
def checked(raw):
    p=pathlib.Path(raw).resolve(strict=True)
    assert root in p.parents,'asset outside owner directory'
    return p
with lock.open('a+b') as f:
    if os.fstat(f.fileno()).st_size==0:f.write(b'0');f.flush()
    f.seek(0)
    if os.name=='nt':
        import msvcrt;msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1)
    else:
        import fcntl;fcntl.flock(f.fileno(),fcntl.LOCK_EX)
    v=json.loads(catalog.read_text(encoding='utf-8')) if catalog.exists() else {'schema':1,'owner':r['owner'],'assets':[]}
    assert v['owner']==r['owner'],'catalog owner differs'
    if r['op']=='register':
        a=r['asset'];data=a['data'];p=checked(data['remote_path'])
        if a['kind']=='dataset':
            manifest=checked(data['manifest']);assert p in manifest.parents
            assert h(manifest)==data['sha256'],'manifest changed'
        else:
            assert p.is_file() and h(p)==data['sha256'],'asset content differs'
        previous=[x for x in v['assets'] if x['kind']==a['kind'] and x['name']==a['name']]
        if previous and previous[-1]['data']==data:result=previous[-1]
        else:
            result={'id':'a-'+uuid.uuid4().hex[:20],'owner':r['owner'],'kind':a['kind'],'name':a['name'],
                    'version':previous[-1]['version']+1 if previous else 1,'data':data,'created':time.time()}
            v['assets'].append(result)
            tmp=catalog.with_name('.catalog-'+uuid.uuid4().hex)
            tmp.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8');os.chmod(tmp,0o600);os.replace(tmp,catalog)
    else:result=v['assets']
print(json.dumps(result,ensure_ascii=False))
'''


class Registry:
    def __init__(self,cfg,store,ssh=None):
        self.cfg,self.store=cfg,store
        self.ssh=ssh or SSH(cfg)

    def call(self,user,op,**kwargs):
        self.store.require_user(user)
        request={'root':self.store.remote_root(user,self.cfg['remote_users_root']),'owner':user,'op':op,**kwargs}
        encoded=base64.b64encode(json.dumps(request,ensure_ascii=False).encode()).decode()
        try:out=json.loads(self.ssh.python(CATALOG_SCRIPT,(encoded,)))
        except Exception as exc:raise GatewayError('无法读取本人工作站资产目录，请检查 SSH 或目录权限；不会回退到别人目录') from exc
        items=out if isinstance(out,list) else [out]
        for a in items:self.cache(user,a)
        return out

    def cache(self,user,a):
        if a['owner']!=user:raise GatewayError('工作站目录返回了其他用户资产')
        with self.store.connect() as c:
            c.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?,?)',(a['id'],user,a['kind'],a['name'],a['version'],json.dumps(a['data'],ensure_ascii=False),a['created']))

    def list(self,user):return self.call(user,'list')

    def get(self,user,kind,name):
        matching=[a for a in self.list(user) if a['kind']==kind and a['name']==name]
        if not matching:raise GatewayError(f'本人工作站目录未登记 {kind}：{name}。不会查询其他用户目录。')
        return max(matching,key=lambda a:a['version'])

    def record(self,user,kind,name,data):
        from .core import name_check,remote_within
        name_check(name)
        if kind not in ('package','dependency','dataset'):raise GatewayError('不登记配置资产')
        root=self.store.remote_root(user,self.cfg['remote_users_root']);remote_within(root,data['remote_path'])
        if kind=='dependency':
            target=data.get('target','');p=PurePosixPath(target)
            if not target or p.is_absolute() or '..' in p.parts or '\\' in target:raise GatewayError('依赖需要 APP 内相对目标路径')
        return self.call(user,'register',asset={'kind':kind,'name':name,'data':data})

    def import_file(self,user,kind,name,source,target=None):
        self.store.require_user(user)
        if kind not in ('package','dependency'):raise GatewayError('数据集通过工作站路径登记')
        if kind=='dependency' and not target:raise GatewayError('依赖需要 APP 内目标路径')
        source=Path(source).resolve(strict=True)
        if (self.store.root/'users').resolve() in source.parents and self.store.user_root(user).resolve() not in source.parents:
            raise GatewayError('不能从其他用户本机目录取文件')
        # Local file is a staging source, not a permanent asset. Transfer once, then register remotely.
        data={'local_path':str(source),'sha256':digest(source),'size':source.stat().st_size}
        if target:data['target']=target
        asset={'owner':user,'kind':kind,'data':data}
        remote=self.ssh.upload(self.store,asset,trusted_source=source)
        data.pop('local_path');data['remote_path']=remote;data['filename']=source.name
        return self.record(user,kind,name,data)

    def remote_file(self,user,kind,name,path,target=None):
        from .core import remote_within
        root=self.store.remote_root(user,self.cfg['remote_users_root']);remote_within(root,path)
        code='''import pathlib,sys,json,hashlib
p=pathlib.Path(sys.argv[2]).resolve(strict=True);root=pathlib.Path(sys.argv[1]).resolve(strict=True)
assert root in p.parents and p.is_file()
h=hashlib.sha256()
with p.open('rb') as f:
    for b in iter(lambda:f.read(1048576),b''):h.update(b)
print(json.dumps({'remote_path':str(p),'sha256':h.hexdigest(),'size':p.stat().st_size,'filename':p.name}))
'''
        data=json.loads(self.ssh.python(code,(root,path)))
        if target:data['target']=target
        return self.record(user,kind,name,data)
