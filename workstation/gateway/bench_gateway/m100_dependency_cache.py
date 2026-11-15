"""Private locked Conan cache, with read-only links to reviewed shared packages."""
import hashlib,io,json,os,posixpath,re,shutil,tarfile,uuid,time
from pathlib import Path,PurePosixPath
from urllib.parse import quote
from .download_auth import load_authorization
from .m100_auto_inputs import _read,InputError

def unpack(body,root):
    root=Path(root);root.mkdir(parents=True,exist_ok=False);links=[];total=0;names=set()
    try:
        with tarfile.open(fileobj=io.BytesIO(body),mode='r:gz') as archive:
            for i,item in enumerate(archive):
                p=PurePosixPath(item.name)
                if i>=60000 or p.is_absolute() or '..' in p.parts or not p.parts:raise ValueError('unsafe dependency archive')
                if str(p) in names and not item.isdir():raise ValueError('duplicate archive file')
                names.add(str(p));target=root.joinpath(*p.parts)
                if item.isdir():target.mkdir(parents=True,exist_ok=True);continue
                if item.issym() or item.islnk():links.append(item);continue
                if not item.isfile() or item.size>1024**3:raise ValueError('special archive member')
                total+=item.size
                if total>4*1024**3:raise ValueError('expanded dependency limit')
                target.parent.mkdir(parents=True,exist_ok=True)
                with archive.extractfile(item) as src,target.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
                target.chmod(0o755 if item.mode&0o111 else 0o644)
        for item in links:
            path=root.joinpath(*PurePosixPath(item.name).parts)
            relative=posixpath.normpath(posixpath.join(str(PurePosixPath(item.name).parent) if item.issym() else '',item.linkname))
            if relative.startswith('../') or relative=='..' or PurePosixPath(relative).is_absolute():raise ValueError('escaping archive link')
            path.parent.mkdir(parents=True,exist_ok=True)
            if item.issym():path.symlink_to(item.linkname)
            else:
                target=root.joinpath(*PurePosixPath(relative).parts)
                if not target.is_file() or target.is_symlink():raise ValueError('unsafe hardlink')
                os.link(target,path)
        for item in links:
            if not root.resolve() in root.joinpath(*PurePosixPath(item.name).parts).resolve().parents:raise ValueError('escaping archive link chain')
    except BaseException:shutil.rmtree(root);raise

def verify_manifest(root,manifest,allow_missing=False):
    missing=[]
    for line in manifest.decode().splitlines()[1:]:
        if not line.strip():continue
        name,expected=line.rsplit(': ',1);p=PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or not re.fullmatch('[a-f0-9]{32}',expected):raise ValueError('invalid Conan manifest')
        target=Path(root).joinpath(*p.parts)
        if not target.is_file():
            if allow_missing:missing.append(name);continue
            raise ValueError('Conan manifest member missing')
        if hashlib.md5(target.read_bytes()).hexdigest()!=expected:raise ValueError('Conan manifest content differs')
    return missing

def prepare(cfg,inputs,fetch=None):
    fetch=fetch or _read;source=Path(inputs['source']);root=source.parent;cache=root/'conan-data';shared=Path(cfg['m100_build_environment']['cache'])
    if not shared.is_absolute() or not shared.exists() or shared.is_symlink():raise ValueError('invalid shared Conan cache')
    document=json.loads((source/'build/conan.lock').read_text());nodes=document['graph_lock']['nodes'];shared_mount='/root/.conan/shared-data'
    user=inputs.get('owner')
    if not user:raise ValueError('source input owner missing')
    host='artifactory.ep.chehejia.com';auth=load_authorization(cfg,user,host);headers={'Authorization':auth} if auth else {}
    repository=cfg.get('m100_conan_repository','liconan-release-local')
    if not re.fullmatch('[A-Za-z0-9_.-]+',repository):raise ValueError('invalid Conan repository')
    base='https://'+host+'/artifactory/api/conan/'+repository+'/v2/conans/'
    cache.mkdir(exist_ok=True);seen={};evidence=[]
    try:
        for node in nodes.values():
            if time.monotonic()>cfg.get('_m100_inputs_deadline',float('inf')):
                raise InputError('M100_INPUT_PREPARATION_TIMEOUT','M100源码和依赖准备达到本次时间上限，已保留完成的文件。','请管理员检查下载网络，或手动编译后提供本APP的liburoad_m100.so；不自动循环重试。')
            if not node.get('package_id'):continue
            match=re.fullmatch(r'([A-Za-z0-9_.+-]+)/([A-Za-z0-9_.+-]+)(?:@([A-Za-z0-9_.+-]+)/([A-Za-z0-9_.+-]+))?#([a-f0-9]{32,64})',node['ref'])
            if not match:raise ValueError('unpinned recipe in dependency lock')
            name,version,namespace,channel,revision=match.groups();namespace=namespace or '_';channel=channel or '_';pid=node['package_id'];prev=node.get('prev','')
            if not re.fullmatch('[a-f0-9]{40}',pid) or not re.fullmatch('[a-f0-9]{32,64}',prev):raise ValueError('unpinned binary dependency')
            relative=Path(name)/version/namespace/channel;key=str(relative)
            if key in seen and seen[key]!=revision:raise ValueError('multiple recipe revisions in Conan1 cache')
            seen[key]=revision;original=shared/relative;entry=cache/relative
            if any(p.is_symlink() for p in (entry,*entry.parents)):raise ValueError('linked cache root')
            entry.mkdir(parents=True,exist_ok=True)
            metadata_file=entry/'metadata.json'
            if metadata_file.exists():metadata=json.loads(metadata_file.read_text())
            elif (original/'metadata.json').is_file():metadata=json.loads((original/'metadata.json').read_text())
            else:metadata={'recipe':{'revision':revision,'remote':None,'properties':{}},'packages':{}}
            existing_recipe=metadata.get('recipe',{}).get('revision')
            recipe_url=base+'/'.join(quote(x,safe='') for x in [name,version,namespace,channel])+'/revisions/'+revision+'/files/'
            if not (entry/'export').exists():
                if existing_recipe==revision and (original/'export').is_dir():shutil.copytree(original/'export',entry/'export',ignore=shutil.ignore_patterns('__pycache__'))
                else:
                    export=entry/'export';export.mkdir()
                    listing=json.loads(fetch(recipe_url,headers,262144,20))['files']
                    for filename in ['conanfile.py','conanmanifest.txt']:(export/filename).write_bytes(fetch(recipe_url+filename,headers,262144,20))
                    if 'conan_export.tgz' in listing:
                        body=fetch(recipe_url+'conan_export.tgz',headers,64*1024**2,40);temp=entry/'export-extra';unpack(body,temp)
                        shutil.copytree(temp,export,dirs_exist_ok=True);shutil.rmtree(temp)
                    verify_manifest(export,(export/'conanmanifest.txt').read_bytes())
                    metadata={'recipe':{'revision':revision,'remote':None,'properties':{}},'packages':{}}
            if metadata.get('recipe',{}).get('revision')!=revision:
                locked_manifest=fetch(recipe_url+'conanmanifest.txt',headers,262144,20)
                if (entry/'export/conanmanifest.txt').read_bytes()!=locked_manifest:raise ValueError('private recipe revision differs')
                verify_manifest(entry/'export',locked_manifest)
                metadata={'recipe':{'revision':revision,'remote':None,'properties':{}},'packages':{}}
            metadata_file.write_text(json.dumps(metadata,sort_keys=True,indent=2))
            packages=entry/'package';packages.mkdir(exist_ok=True);target=packages/pid
            record=metadata.setdefault('packages',{}).get(pid,{})
            shared_metadata=json.loads((original/'metadata.json').read_text()) if (original/'metadata.json').is_file() else {}
            shared_record=shared_metadata.get('packages',{}).get(pid,{})
            if target.is_symlink() or target.exists():
                if record.get('revision')!=prev or record.get('recipe_revision')!=revision:raise ValueError('private package identity differs')
                if target.is_symlink() and os.readlink(target)!=shared_mount+'/'+str(relative/'package'/pid):raise ValueError('unexpected shared package link')
                source_kind='PRIVATE_CACHE'
            elif (shared_metadata.get('recipe',{}).get('revision')==revision and shared_record.get('revision')==prev and shared_record.get('recipe_revision')==revision and (original/'package'/pid).is_dir()):
                target.symlink_to(shared_mount+'/'+str(relative/'package'/pid));metadata['packages'][pid]=shared_record;source_kind='SHARED_READONLY'
            else:
                url=recipe_url[:-6]+'packages/'+pid+'/revisions/'+prev+'/files/'
                manifest=fetch(url+'conanmanifest.txt',headers,4*1024**2,20)
                info=fetch(url+'conaninfo.txt',headers,1024**2,20)
                pending=[p for p in packages.glob('.'+pid+'-*') if (p/'conanmanifest.txt').is_file() and (p/'conanmanifest.txt').read_bytes()==manifest and (p/'conaninfo.txt').is_file() and (p/'conaninfo.txt').read_bytes()==info]
                if len(pending)==1:stage=pending[0]
                else:
                    body=fetch(url+'conan_package.tgz',headers,512*1024**2,90)
                    stage=packages/('.'+pid+'-'+uuid.uuid4().hex[:8]);unpack(body,stage)
                for filename,content in [('conanmanifest.txt',manifest),('conaninfo.txt',info)]:
                    p=stage/filename
                    if p.exists() and p.read_bytes()!=content:raise ValueError('package metadata differs')
                    p.write_bytes(content)
                missing=verify_manifest(stage,manifest,allow_missing=True)
                if missing:
                    (stage/'.gateway-manifest-missing.json').write_text(json.dumps({'missing_entries':missing,'content_verification':'all-present-manifest-entries','complete_manifest_inventory_verified':False},indent=2))
                os.replace(stage,target)
                metadata['packages'][pid]={'revision':prev,'recipe_revision':revision,'remote':None,'properties':{}};source_kind='DOWNLOADED_EXACT'
            metadata_file.write_text(json.dumps(metadata,sort_keys=True,indent=2));evidence.append({'ref':node['ref'],'package_id':pid,'prev':prev,'source':source_kind})
        marker={'schema_version':1,'package_sha256':inputs['package_sha256'],'source_lock_sha256':inputs['source_lock_sha256'],'cache':str(cache),'shared_cache':str(shared),'nodes':evidence}
        (root/'dependency-cache.json').write_text(json.dumps(marker,ensure_ascii=False,indent=2));return marker
    except InputError:raise
    except Exception as e:
        (root/'dependency-cache-error.json').write_text(json.dumps({'ref':node.get('ref'),'error_type':type(e).__name__,'http_status':getattr(e,'code',None),'file':str(getattr(e,'filename','')) if str(getattr(e,'filename','')).startswith(str(root)) else None,'detail':str(e)[:300] if isinstance(e,ValueError) else ''},ensure_ascii=False,indent=2))
        raise InputError('M100_DEPENDENCY_PREPARATION_FAILED','源码与构建锁已取得，锁定编译依赖准备未完成（'+type(e).__name__+'）。','请管理员核对对应精确依赖制品或只读权限；输入已保留，不需要用户逐包提交源码。') from None
