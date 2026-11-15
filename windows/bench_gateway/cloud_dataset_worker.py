"""Read-only cloud originals -> one owner's immutable workstation input snapshot.

Python 3.8 and boto3; credentials exist only in the SSH process's stdin/memory.
No bench, GT downloads, shell execution, or dataset-provided code.
"""
import hashlib,json,os,re,shutil,stat,time
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from pathlib import Path,PurePosixPath

SCENE=re.compile(r'20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[A-Za-z0-9]{1,64}\Z')
MAX_FILES=300000
GROUPS=('VAL_Lidar_0_PointCloud','eps_mode_sts','ins_info','Val_Navigation_Ins','odom_data_for_pcm','images/f_ca_120','f_ca_120')

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def relative(raw):
    if not isinstance(raw,str) or not raw or '\\' in raw or any(ord(c)<32 for c in raw):raise ValueError('invalid source member')
    p=PurePosixPath(raw)
    if p.is_absolute() or str(p)!=raw or any(x in ('','..','.') for x in raw.split('/')):raise ValueError('unsafe source member')
    return raw

def owned(root,path):
    root=Path(root);path=Path(path)
    if not root.is_absolute() or not path.is_absolute() or '..' in root.parts or '..' in path.parts or root not in path.parents:raise ValueError('owner path required')
    for p in [*path.parents,path]:
        if p.is_symlink():raise ValueError('owner directory contains link')
    if root.exists() and root.resolve()!=root:raise ValueError('owner root changed')
    return path

def write_json(path,value):
    raw=canonical(value)+b'\n';temp=path.with_name(path.name+'.partial')
    if temp.exists():raise ValueError('incomplete metadata write requires inspection')
    with temp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    os.chmod(temp,0o600);os.replace(temp,path)

def listing(client,bucket,prefix,delimiter=None,limit=MAX_FILES):
    rows=[];folders=[];token=None
    while True:
        args={'Bucket':bucket,'Prefix':prefix,'MaxKeys':1000}
        if delimiter is not None:args['Delimiter']=delimiter
        if token:args['ContinuationToken']=token
        reply=client.list_objects_v2(**args)
        rows.extend(reply.get('Contents',[]));folders.extend(x['Prefix'] for x in reply.get('CommonPrefixes',[]))
        if len(rows)+len(folders)>limit:raise ValueError('source inventory limit exceeded; select fewer scenes')
        if not reply.get('IsTruncated'):return rows,folders
        next_token=reply.get('NextContinuationToken')
        if not next_token or next_token==token:raise ValueError('source listing did not advance')
        token=next_token

def discover(client,location,source,indices=None):
    root=relative(source['relative_root']).rstrip('/')+'/'
    base=location['prefix']+root;bucket=location['bucket'];_,children=listing(client,bucket,base,'/',2000)
    scenes={}
    for path in children:
        name=path[len(base):].rstrip('/')
        candidates=[path] if SCENE.fullmatch(name) else []
        if re.fullmatch(r'20\d{2}-\d{2}-\d{2}',name):
            _,dated=listing(client,bucket,path,'/',2000);candidates=[p for p in dated if SCENE.fullmatch(p[len(path):].rstrip('/'))]
        for p in candidates:
            scene=p.rstrip('/').split('/')[-1]
            if scene in scenes:raise ValueError('same scene exists under multiple dates; provide a narrower root')
            scenes[scene]=p
    names=sorted(scenes)
    if not names:raise ValueError('no canonical scenes found below supplied source root')
    chosen=list(range(1,len(names)+1)) if indices is None else indices
    if not chosen or chosen!=sorted(set(chosen)) or any(type(x) is not int or not 1<=x<=len(names) for x in chosen):raise ValueError('source scene selection invalid')
    inventory=[];selected=[]
    for number in chosen:
        scene=names[number-1];prefix=scenes[scene]+'original/';rows,_=listing(client,bucket,prefix)
        files=[]
        for row in rows:
            key=row['Key'];rel=key[len(prefix):]
            if not rel or key.endswith('/'):continue
            relative(rel)
            if not (rel in ('calib_cam_params.json','calib_lidar_params.json') or any(rel.startswith(g+'/') for g in GROUPS)):continue
            size=row.get('Size');etag=row.get('ETag')
            if type(size) is not int or size<=0 or not isinstance(etag,str) or not etag:raise ValueError('source object metadata missing')
            files.append({'key':key,'relative':scene+'/original/'+rel,'bytes':size,'etag':etag})
        rels=[r['relative'][len(scene+'/original/'):] for r in files]
        for name in ('calib_cam_params.json','calib_lidar_params.json'):
            if name not in rels:raise ValueError('missing replay calibration: '+scene+'/'+name)
        for group in ('VAL_Lidar_0_PointCloud','odom_data_for_pcm','eps_mode_sts'):
            if not any(r.startswith(group+'/') for r in rels):raise ValueError('missing replay signal: '+scene+'/'+group)
        camera=[g for g in ('images/f_ca_120','f_ca_120') if any(r.startswith(g+'/') for r in rels)]
        ins=[g for g in ('ins_info','Val_Navigation_Ins') if any(r.startswith(g+'/') for r in rels)]
        if len(camera)!=1 or len(ins)!=1:raise ValueError('camera or navigation source missing/ambiguous: '+scene)
        if not all(r.lower().endswith(('.jpg','.jpeg')) for r in rels if r.startswith(camera[0]+'/')):raise ValueError('cloud original requires timestamped JPEG inputs; packed video uses workstation prepared input')
        selected.append({'scene':scene,'source_index':number,'camera_group':camera[0],'ins_group':ins[0]})
        inventory.extend(files)
        if len(inventory)>MAX_FILES:raise ValueError('source inventory limit exceeded')
    total=sum(r['bytes'] for r in inventory)
    plan={'schema':1,'source':source,'scene_names':names,'source_indices':chosen,'scenes':selected,
          'files':sorted(inventory,key=lambda r:r['relative']),'download_bytes':total,'gt_downloaded':False}
    plan['plan_id']=digest(plan);return plan

def validate_plan(plan,location,source,max_bytes):
    if plan.get('schema')!=1 or plan.get('source')!=source or plan.get('gt_downloaded') is not False:raise ValueError('source plan identity mismatch')
    if digest({k:v for k,v in plan.items() if k!='plan_id'})!=plan.get('plan_id'):raise ValueError('source plan changed')
    files=plan.get('files');names=[s['scene'] for s in plan.get('scenes',[])]
    if not files or len(files)>MAX_FILES or len(set(names))!=len(names) or any(not SCENE.fullmatch(n) for n in names):raise ValueError('invalid selected scenes')
    source_prefix=location['prefix']+source['relative_root'].rstrip('/')+'/'
    seen=set()
    for r in files:
        rel=relative(r['relative']);parts=rel.split('/')
        if rel in seen or parts[0] not in names or parts[1]!='original' or not r['key'].startswith(source_prefix):raise ValueError('source download scope differs')
        suffix='/'+parts[0]+'/original/'+'/'.join(parts[2:])
        if not r['key'].endswith(suffix):raise ValueError('GT or different member requested')
        if type(r.get('bytes')) is not int or r['bytes']<=0 or not isinstance(r.get('etag'),str):raise ValueError('invalid transfer inventory')
        seen.add(rel)
    if plan['download_bytes']!=sum(r['bytes'] for r in files) or not 0<plan['download_bytes']<=max_bytes:raise ValueError('source exceeds configured transfer size')

def timestamp(name):
    values=re.findall(r'\d+',Path(name).stem)
    stamps=[s for s in values if len(s) in (13,16,19)]
    if len(stamps)!=1:raise ValueError('camera timestamp missing/ambiguous: '+name)
    return int(stamps[0])*10**(19-len(stamps[0]))

def prepare_manifest(directory,plan):
    scenes=[];camera_roots=[]
    config_dir=directory/'replay-configs';config_dir.mkdir(exist_ok=True)
    for index,s in enumerate(plan['scenes'],1):
        scene=s['scene'];original=directory/scene/'original';camera=directory/('camera-s%03d'%index);camera.mkdir(exist_ok=True)
        entries=[r for r in plan['files'] if r['relative'].startswith(scene+'/original/'+s['camera_group']+'/')]
        stamps={}
        for row in entries:
            stamp=timestamp(row['relative'])
            if stamp in stamps:raise ValueError('duplicate camera timestamp')
            src=owned(directory,directory/row['relative']);link=camera/('camera_%d.jpg'%stamp)
            if link.exists() or link.is_symlink():
                if not link.is_symlink() or link.resolve()!=src.resolve():raise ValueError('camera view differs')
            else:link.symlink_to(src)
            stamps[stamp]=src
        ordered=sorted(stamps)
        if not ordered:raise ValueError('no camera images')
        index_file=config_dir/(scene+'-camera-index.json')
        write_json(index_file,{'camera_id':2,'frame_count':len(ordered),'timestamps_ns':ordered})
        config={'version':'1.0','type':'replay_config','fps':10,'cameras':[{'camera_id':'2','folder':str(camera)}],
          'lidars':[{'lidar_id':'0','folder':str(original/'VAL_Lidar_0_PointCloud')}],
          'topics':[{'topic_name':topic,'idl_file':idl,'bin_folder':str(original/folder),'struct_name':struct}
           for topic,idl,folder,struct in [('/soa/adpositioninfo','parse_vbs_topic/vbs_idl/ADPositionInfo.idl',s['ins_group'],'GNSSLOCInfo'),
            ('OdomDataForPCM','parse_vbs_topic/vbs_idl/IMUAndOdomDataService.idl','odom_data_for_pcm','OdomDataForPCM'),
            ('EPSModeSts','parse_vbs_topic/vbs_idl/SteeringSystem.idl','eps_mode_sts','EPSModeSts')]]}
        config_file=config_dir/(scene+'.json');write_json(config_file,config)
        lidar=[r for r in plan['files'] if r['relative'].startswith(scene+'/original/VAL_Lidar_0_PointCloud/') and r['relative'].lower().endswith('.zst')]
        if not lidar:raise ValueError('cloud original requires LiDAR zst frames')
        scenes.append({'scene':scene,'scene_root':str(directory/scene),'raw_root':str(original),'camera_index':str(index_file),
           'camera_frames':len(ordered),'jpeg_frames':str(camera),'replay_config':str(config_file),
           'lidar_frames':len(lidar),'replay_input_format':'cloud-original-v1','source_scene_index':s['source_index']})
        camera_roots.append(str(camera))
    manifest=directory/'manifest.json';write_json(manifest,{'schema_version':1,'prepared_scenes':scenes,'source_plan_id':plan['plan_id'],'gt_downloaded':False})
    return {'remote_path':str(directory),'manifest':str(manifest),'sha256':hashlib.sha256(manifest.read_bytes()).hexdigest(),
       'camera_roots':camera_roots,'scene_count':len(scenes),'read_only':True,
       'evaluation_source':{'schema':1,'dataset_root':plan['source']['evaluation_path'],'gt_root':plan['source']['evaluation_path']},
       'cloud_source':{'input_root':plan['source']['evaluation_path'],'source_scene_count':len(plan['scene_names']),
          'source_indices':plan['source_indices'],'scene_names':[s['scene'] for s in plan['scenes']],'plan_id':plan['plan_id']}}

def download(client,location,source,plan,owner_root,max_bytes=64*1024**3,workers=8,progress=None):
    validate_plan(plan,location,source,max_bytes)
    directory=owned(owner_root,Path(owner_root)/'datasets'/('cloud-'+plan['plan_id'][:24]))
    directory.mkdir(parents=True,exist_ok=True,mode=0o700);owned(owner_root,directory)
    receipt=directory/'import-receipt.json'
    if receipt.is_file():
        saved=json.loads(receipt.read_text())
        if saved['plan_id']!=plan['plan_id'] or hashlib.sha256(Path(saved['data']['manifest']).read_bytes()).hexdigest()!=saved['data']['sha256']:raise ValueError('import snapshot changed')
        return {**saved,'reused':True}
    if shutil.disk_usage(directory).free<plan['download_bytes']+5*1024**3:raise ValueError('insufficient workstation space for selected replay inputs')
    completed={};index_path=directory/'download-index.json'
    if index_path.is_file():completed=json.loads(index_path.read_text())
    done_bytes=0;done_files=0
    def one(row):
        dst=owned(owner_root,directory/relative(row['relative']));dst.parent.mkdir(parents=True,exist_ok=True);owned(owner_root,dst)
        identity={'bytes':row['bytes'],'etag':row['etag']}
        if completed.get(row['relative'])==identity and dst.is_file() and dst.stat().st_size==row['bytes']:return row,identity
        if dst.exists():raise ValueError('unregistered input file already exists; inspect before resuming')
        temp=dst.with_name(dst.name+'.download-partial')
        if temp.exists():
            if temp.is_symlink() or not stat.S_ISREG(temp.stat().st_mode):raise ValueError('partial input is not regular')
            temp.unlink()  # One known, uncommitted transfer file inside this exact snapshot.
        body=client.get_object(Bucket=location['bucket'],Key=row['key'],IfMatch=row['etag'])['Body']
        try:
            with temp.open('xb') as output:
                while True:
                    chunk=body.read(1024*1024)
                    if not chunk:break
                    output.write(chunk)
            if temp.stat().st_size!=row['bytes']:raise ValueError('cloud replay file transfer truncated')
            temp.chmod(0o440);os.replace(temp,dst)
        finally:body.close()
        return row,identity
    concurrency=min(16,max(1,workers))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        rows=iter(plan['files']);pending=set()
        def refill():
            while len(pending)<concurrency*2:
                row=next(rows,None)
                if row is None:break
                pending.add(pool.submit(one,row))
        refill()
        last=0
        try:
            while pending:
                finished,pending=wait(pending,return_when=FIRST_COMPLETED)
                for future in finished:
                    row,identity=future.result();completed[row['relative']]=identity;done_files+=1;done_bytes+=row['bytes']
                if time.monotonic()-last>2:
                    write_json(index_path,completed)
                    if progress:progress({'state':'DOWNLOADING','files':done_files,'total_files':len(plan['files']),'bytes':done_bytes,'total_bytes':plan['download_bytes']})
                    last=time.monotonic()
                refill()
        finally:write_json(index_path,completed)
    data=prepare_manifest(directory,plan)
    result={'state':'PREPARED','plan_id':plan['plan_id'],'data':data,'download_bytes':plan['download_bytes'],
        'file_count':len(plan['files']),'reused':False,'gt_downloaded':False,'hardware_actions':'NOT_RUN'}
    write_json(receipt,result);return result
