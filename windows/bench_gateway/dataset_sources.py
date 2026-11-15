"""Workstation dataset references. Dataset bytes stay at the approved source location."""
import base64
import json

from .core import GatewayError,name_check
from .transfer import SSH


VALIDATOR_CODE=r'''
import hashlib,json,os,pathlib
def check_source(data,inspect_frames=False):
    d=dict(data);root=pathlib.Path(d['remote_path']).resolve(strict=True)
    manifest=pathlib.Path(d['manifest']).resolve(strict=True)
    if not root.is_dir() or root==pathlib.Path(root.anchor):raise ValueError('invalid dataset root')
    digest=hashlib.sha256(manifest.read_bytes()).hexdigest()
    if d.get('sha256') and d['sha256']!=digest:raise ValueError('dataset manifest changed')
    value=json.loads(manifest.read_text(encoding='utf-8'));scenes=value.get('prepared_scenes')
    if not isinstance(scenes,list) or not scenes:raise ValueError('missing prepared scenes')
    if d.get('scene_count') and d['scene_count']!=len(scenes):raise ValueError('scene count changed')
    names=[s.get('scene') for s in scenes]
    if not all(isinstance(n,str) and n for n in names) or len(names)!=len(set(names)):raise ValueError('invalid scene names')
    def contained(raw,base):
        p=pathlib.Path(raw).resolve(strict=True)
        if p!=base and base not in p.parents:raise ValueError('dataset input escapes source')
        if not os.access(str(p),os.R_OK):raise ValueError('dataset input unreadable')
        return p
    config_roots=[pathlib.Path(p).resolve(strict=True) for p in d.get('replay_config_roots',[])]
    if any(not p.is_dir() or p==pathlib.Path(p.anchor) for p in config_roots):raise ValueError('invalid replay config root')
    for scene in scenes:
        values=scene.get('archive_stats',scene)
        for key in ('scene_root','truth_root','raw_root','jpeg_frames','camera_index','camera_video'):
            if values.get(key):contained(values[key],root)
        if values.get('replay_config'):
            p=pathlib.Path(values['replay_config']).resolve(strict=True)
            bases=[manifest.parent,*config_roots]
            base=next((b for b in bases if p!=b and b in p.parents),None)
            if base is None:raise ValueError('replay config escapes approved roots')
            contained(p,base)
    cameras=[]
    for index,raw in enumerate(d.get('camera_roots',[])):
        p=pathlib.Path(raw).resolve(strict=True)
        if not p.is_dir() or not os.access(str(p),os.R_OK|os.X_OK):raise ValueError('camera source unavailable')
        if index>=len(scenes) or type(scenes[index].get('camera_frames')) is not int or scenes[index]['camera_frames']<0:
            raise ValueError('invalid camera frame count')
        if inspect_frames:
            frames=list(p.iterdir())
            if len(frames)!=scenes[index]['camera_frames']:raise ValueError('camera frame count changed')
            # Full enumeration is an explicit diagnostic only. Registration
            # and routine reuse check scene entrances without walking frames.
            for frame in frames:contained(frame,root if frame.is_symlink() else p)
        cameras.append(str(p))
    if cameras and len(cameras)!=len(scenes):raise ValueError('incomplete camera sources')
    d.update(remote_path=str(root),manifest=str(manifest),sha256=digest,scene_count=len(scenes),camera_roots=cameras,read_only=True)
    if config_roots:d['replay_config_roots']=[str(p) for p in config_roots]
    return d

def check_registered_source(data):
    # Called only after a trusted catalog record and its owner are matched.
    # Frame contents/links are not reverified here; this is not a full audit.
    return check_source(data,inspect_frames=False)
'''

CATALOG_CODE=VALIDATOR_CODE+r'''
import base64,sys,time,uuid
r=json.loads(base64.b64decode(sys.argv[1]));path=pathlib.Path(r['catalog'])
def read_catalog():
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'schema':1,'datasets':[]}
if r['op']=='register':
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.lock').open('a+b') as lock:
        if os.fstat(lock.fileno()).st_size==0:lock.write(b'0');lock.flush()
        lock.seek(0)
        if os.name=='nt':
            import msvcrt;msvcrt.locking(lock.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl;fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        catalog=read_catalog();d=check_source(r['data']);owner=r['owner']
        personal=pathlib.Path(r['users_root']).resolve()
        allowed=pathlib.Path(r['owner_root']).resolve() if r.get('owner_root') else None
        for raw in [d['remote_path'],d['manifest'],*d['camera_roots'],*d.get('replay_config_roots',[])]:
            p=pathlib.Path(raw)
            if (p==personal or personal in p.parents) and (allowed is None or (p!=allowed and allowed not in p.parents)):
                raise ValueError('cannot register another user directory')
        previous=[a for a in catalog['datasets'] if a['owner']==owner and a['name']==r['name']]
        if previous and previous[-1]['data']==d:result=previous[-1]
        else:
            result={'id':'ds-'+uuid.uuid4().hex[:20],'owner':owner,'kind':'dataset','name':r['name'],
                    'origin':'dataset_catalog','version':previous[-1]['version']+1 if previous else 1,'created':time.time(),'data':d}
            catalog['datasets'].append(result)
            tmp=path.with_name('.datasets-'+uuid.uuid4().hex+'.json')
            tmp.write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(str(tmp),str(path))
else:
    visible=[a for a in read_catalog()['datasets'] if a['owner'] in ('shared',r['owner'])]
    if r['op']=='validate':
        matches=[a for a in visible if a['id']==r['id']]
        if len(matches)!=1:raise ValueError('dataset source not available to this user')
        result=matches[0]
        if check_registered_source(result['data'])!=result['data']:raise ValueError('dataset source changed')
    else:result=visible
print(json.dumps(result,ensure_ascii=False))
'''


def reference(asset):
    return '共享:'+asset['name'] if asset.get('owner')=='shared' else asset['name']


class DatasetSources:
    def __init__(self,cfg,store,ssh=None):self.cfg,self.store,self.ssh=cfg,store,ssh or SSH(cfg)

    def call(self,owner,op,**values):
        catalog=self.cfg.get('dataset_catalog')
        if not catalog:
            if op=='list':return []
            raise GatewayError('工作站数据源目录尚未配置')
        request={'catalog':catalog,'owner':owner,'op':op,**values}
        encoded=base64.b64encode(json.dumps(request,ensure_ascii=False).encode()).decode()
        try:return json.loads(self.ssh.python(CATALOG_CODE,(encoded,),timeout=120))
        except Exception as exc:raise GatewayError('工作站数据源核验失败，请检查目录、manifest 和共享配置；未复制数据') from exc

    def list(self,user):
        self.store.require_user(user)
        return self.call(user,'list')

    def validate(self,user,asset):
        self.store.require_user(user)
        current=self.call(user,'validate',id=asset['id'])
        if current!=asset:raise GatewayError('数据源登记发生变化')
        return current

    def register(self,owner,name,path,manifest,camera_prefix=None,expected_scenes=None,replay_config_roots=None,cloud_dataset_root=None,cloud_gt_root=None):
        # Only the local operator CLI exposes this method. Feishu cannot declare a source shared.
        name_check(name)
        if ':' in name:raise GatewayError('数据源名称不能包含冒号')
        if owner!='shared':self.store.require_user(owner)
        data={'remote_path':path,'manifest':manifest,'camera_roots':[]}
        if cloud_gt_root and not cloud_dataset_root:raise GatewayError('GT关联需要明确的开发空间数据集根目录。')
        if cloud_dataset_root:
            from .cloud_truth import source_spec
            try:data['evaluation_source']=source_spec(cloud_dataset_root,cloud_gt_root)
            except ValueError:raise GatewayError('开发空间数据集/GT路径必须是规范的挂载卷绝对路径。') from None
        if replay_config_roots:data['replay_config_roots']=replay_config_roots
        if expected_scenes:data['scene_count']=expected_scenes
        if camera_prefix:
            if not expected_scenes:raise GatewayError('相机前缀需要 --scene-count')
            data['camera_roots']=[camera_prefix+f'{i:03d}' for i in range(1,expected_scenes+1)]
        return self.call(owner,'register',name=name,data=data,users_root=self.cfg['remote_users_root'],
                         owner_root=None if owner=='shared' else self.store.remote_root(owner,self.cfg['remote_users_root']))
