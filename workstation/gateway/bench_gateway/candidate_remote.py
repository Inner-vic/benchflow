"""Workstation-only candidate binding and planning. Binding never contacts a bench.

Loaded from administrator code over SSH stdin; no executable code comes from
assets, YAML, a binding receipt, or a chat message. Python 3.8 compatible.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import tarfile
import time

COMMON = 'etc/uroad/config/common_param.yaml'
TEMPLATE_FILES = ('job.json', 'profile.json', 'small_stream_adapter.py', 'sw0058_gate_probe_driver_reference.py')
CONTROL_HASHES = {
    'bin/bench-replay-mode':'cb0d617cfcf0a48cab87e8ff042cf50d3a5e26fd8dc78b3b1f6144b761aafcac',
    'bin/formal-replay-wrapper.sh':'4d97a9f376a4a50b91c13c5098426976a94e5bc165059eb43a4662f76c4d49ed',
    'sdf/infer_expert_pcm_replay.sdf':'7c0eb2c9bc53a188336522d047ac7dfb4caa1364ffc182a0465242b12d7dc225',
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1048576),b''):h.update(chunk)
    return h.hexdigest()


def read(path):
    p=Path(path)
    if p.stat().st_size>8*1024*1024:raise ValueError('candidate JSON exceeds limit')
    return json.loads(p.read_text(encoding='utf-8'))


def owned(root,path):
    root=Path(root);p=Path(path)
    if not p.is_absolute() or '..' in p.parts or p==root or root not in p.parents:
        raise ValueError('candidate path outside owner')
    for ancestor in [*p.parents,p]:
        if ancestor.is_symlink():raise ValueError('candidate path contains link')
    return p


def write_once(path,raw):
    path=Path(path)
    if path.exists():
        if path.is_symlink() or path.read_bytes()!=raw:raise ValueError('immutable candidate differs')
        return
    with path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    path.chmod(0o440)


def control_bundle(deploy=False):
    directory=Path('/home/lixiang/bench-gateway/adapter-tools')/digest(CONTROL_HASHES)
    if directory.resolve()!=directory:raise ValueError('shared control directory contains links')
    if deploy:directory.mkdir(parents=True,exist_ok=True,mode=0o750)
    controls=[]
    for target,expected in CONTROL_HASHES.items():
        path=directory/Path(target).name
        if deploy:
            source=Path('/home/lixiang/bench-eval/staging')/path.name
            if not source.is_file() or source.is_symlink() or sha(source)!=expected:raise ValueError('reviewed shared control source changed')
            write_once(path,source.read_bytes())
            path.chmod(0o755 if target.startswith('bin/') else 0o440)
        if not path.is_file() or path.is_symlink() or sha(path)!=expected:raise ValueError('shared controls not deployed or changed')
        if target.startswith('bin/') and (not path.stat().st_mode&0o111 or not os.access(path,os.X_OK)):
            raise ValueError('shared control executable permission is missing')
        controls.append({'target':target,'path':str(path),'sha256':expected})
    return controls


def qualify(m,source_run):
    """Qualify shared tools without contacting the bench.

    Recorder's reviewed 674 MB bundle uses an administrator cache of per-file
    content hashes. Every call inventories paths, modes, links and kernel file
    identities; changed inode/size/mtime/ctime forces a fresh content hash. This
    cache never updates the registered template or accepts a new tool identity.
    Small configuration files are always hashed. The returned identity contains
    content digests, not cache timestamps, so harmless metadata changes alone do
    not invalidate an otherwise byte-identical reviewed template.
    """
    import stat
    import subprocess
    started=time.monotonic()
    def budget():
        if time.monotonic()-started>45:raise ValueError('shared tool qualification exceeded 45 seconds')
    def stamp(value):
        return [value.st_dev,value.st_ino,value.st_mode,value.st_size,value.st_mtime_ns,value.st_ctime_ns]
    def regular_hash(path,cached=None):
        budget();path=Path(path)
        # Known top-level aliases are resolved before this helper. No newly
        # introduced file/directory link is accepted while opening content.
        if path.resolve(strict=True)!=path:raise ValueError('unexpected link in shared tool content')
        before=path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size>128*1024*1024:
            raise ValueError('shared tool file is special or exceeds size bound')
        signature=stamp(before)
        if (isinstance(cached,dict) and cached.get('stat')==signature
                and isinstance(cached.get('sha256'),str) and re.fullmatch('[0-9a-f]{64}',cached['sha256'])):
            return cached
        hasher=hashlib.sha256();total=0
        fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:
            if stamp(os.fstat(stream.fileno()))!=signature:raise ValueError('shared tool changed before hashing')
            while True:
                budget();chunk=stream.read(1024*1024)
                if not chunk:break
                total+=len(chunk)
                if total>before.st_size:raise ValueError('shared tool grew while hashing')
                hasher.update(chunk)
            if total!=before.st_size or stamp(os.fstat(stream.fileno()))!=signature:
                raise ValueError('shared tool changed while hashing')
        if stamp(path.lstat())!=signature:raise ValueError('shared tool replaced while hashing')
        return {'stat':signature,'sha256':hasher.hexdigest()}

    if not isinstance(source_run,str) or not m.SAFE.fullmatch(source_run):raise ValueError('invalid template id')
    job,profile,adapter,_,_=m.source_contract(source_run)
    base=m.BASE/'adapters'/source_run
    paths=[base/name for name in TEMPLATE_FILES]
    paths += [m.ROOT/'runs'/source_run/name for name in ('state.json','restore-verification.json','workstation-predictions.json')]
    paths += [m.ROOT/'cache/predictions'/source_run/'predictions.bench-eval-product.json',Path(job['replay_input']['prepared_manifest'])]
    # Shared executables are template tools, never another user's package/SO.
    paths += [Path('/home/lixiang/bench-eval/adapters/gt-toggle-off-stream-20260929-001/v18/controlled-replay/run_controlled_sweep.py'),
              Path('/mnt/data/bench-eval/adapters/controlled-replay-native-stream-20260929-006/recorder/lixplayCLI')]
    sweep=paths[-2].parent
    paths += [sweep/'adapter-manifest.json',sweep/'native_stream_replay.py']
    controls=control_bundle();paths += [Path(c['path']) for c in controls]
    snapshot={str(path):regular_hash(path.resolve(strict=True))['sha256'] for path in paths}

    # Only these two outer links are part of the existing reviewed deployment.
    # Internal library links may point to regular files inside _internal only;
    # their link text and resolved destination are included in the content tree.
    recorder=Path('/mnt/data/bench-eval/adapters/controlled-replay-native-stream-20260929-006/recorder')
    logical_bundle=Path('/home/lixiang/bench-eval/cache/tools/RecorderHMI-v0.2.4')
    real_bundle=Path('/mnt/data/bench-eval/cache/tools/RecorderHMI-v0.2.4')
    if logical_bundle.resolve(strict=True)!=real_bundle or real_bundle.resolve(strict=True)!=real_bundle:
        raise ValueError('reviewed Recorder bundle root changed')
    cache_dir=Path('/home/lixiang/bench-gateway/adapter-tools/template-cache')
    if cache_dir.resolve()!=cache_dir:raise ValueError('shared tool cache contains links')
    cache_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    cache_file=cache_dir/'recorder-hmi-v024-content-v1.json'
    cache={}
    if cache_file.exists() or cache_file.is_symlink():
        meta=cache_file.lstat()
        if (cache_file.is_symlink() or not stat.S_ISREG(meta.st_mode) or meta.st_size>8*1024*1024
                or (hasattr(os,'getuid') and (meta.st_uid!=os.getuid() or stat.S_IMODE(meta.st_mode)&0o022))):
            raise ValueError('shared tool hash cache is unsafe')
        try:
            prior=json.loads(cache_file.read_text(encoding='utf-8'))
            if prior.get('schema_version')==1 and isinstance(prior.get('files'),dict):cache=prior['files']
        except (ValueError,AttributeError):
            cache={}
    current_cache={};dependencies=[]
    for label in ('_internal','config'):
        link=recorder/label;expected=logical_bundle/label;root=real_bundle/label
        if not link.is_symlink() or os.readlink(link)!=str(expected) or link.resolve(strict=True)!=root:
            raise ValueError('reviewed Recorder dependency link changed: '+label)
        if not root.is_dir() or root.is_symlink():raise ValueError('invalid Recorder dependency directory')
        records=[];files=links=total=0
        for directory,dirs,names in os.walk(str(root),followlinks=False):
            budget();dirs.sort();names.sort();parent=Path(directory)
            if len(records)+len(dirs)+len(names)>10000:
                raise ValueError('Recorder dependency inventory exceeds reviewed bounds')
            if parent.resolve(strict=True)!=parent:raise ValueError('unexpected Recorder directory link')
            for name in dirs:
                child=parent/name
                if child.is_symlink():raise ValueError('Recorder directory symlinks are not reviewed')
                records.append({'path':str(child.relative_to(root)),'kind':'directory'})
            for name in names:
                path=parent/name;relative=str(path.relative_to(root));info=path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raw=os.readlink(path)
                    if os.path.isabs(raw):raise ValueError('absolute Recorder library link is not reviewed')
                    destination=path.resolve(strict=True)
                    if root not in destination.parents or not destination.is_file() or destination.is_symlink():
                        raise ValueError('Recorder library link escapes dependency directory')
                    records.append({'path':relative,'kind':'link','target':raw,'resolved':str(destination.relative_to(root))})
                    links+=1
                    if links>512:raise ValueError('Recorder link inventory exceeds reviewed bounds')
                else:
                    files+=1;total+=info.st_size
                    if files>6000 or total>1024*1024*1024 or len(records)>10000:
                        raise ValueError('Recorder dependency inventory exceeds reviewed bounds')
                    key=label+'/'+relative
                    content=regular_hash(path,cache.get(key) if label=='_internal' else None)
                    current_cache[key]=content
                    records.append({'path':relative,'kind':'file','bytes':info.st_size,
                                    'mode':stat.S_IMODE(info.st_mode),'sha256':content['sha256']})
        dependencies.append({'path':str(link),'target':str(expected),'resolved':str(root),'files':files,
                             'links':links,'bytes':total,'content_sha256':digest(sorted(records,key=lambda r:r['path']))})
    # Cache publication is atomic, private, and only reflects files just checked.
    # Qualifying changed bytes produces a different template_id; load_template
    # still refuses it until an administrator reviews/registers that new identity.
    raw=canonical({'schema_version':1,'files':current_cache})
    if len(raw)>8*1024*1024:raise ValueError('shared tool content cache exceeds limit')
    if current_cache!=cache:
        import tempfile
        fd,temporary=tempfile.mkstemp(prefix='.recorder-',suffix='.tmp',dir=str(cache_dir))
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,0o600);os.replace(temporary,str(cache_file))
        finally:
            if os.path.exists(temporary):os.unlink(temporary)

    python=Path('/home/lixiang/bench-eval/venvs/xcamera-replay/bin/python')
    python_links=[]
    for path,expected in ((python,'python3'),(python.parent/'python3','/usr/bin/python3'),(Path('/usr/bin/python3'),'python3.8')):
        if not path.is_symlink() or os.readlink(path)!=expected:raise ValueError('reviewed replay Python link changed')
        python_links.append({'path':str(path),'target':expected})
    executable=python.resolve(strict=True)
    if executable!=Path('/usr/bin/python3.8'):raise ValueError('reviewed replay Python executable changed')
    python_hash=regular_hash(executable)['sha256']
    pyvenv=python.parent.parent/'pyvenv.cfg'
    pyvenv_hash=regular_hash(pyvenv.resolve(strict=True))['sha256']
    version=subprocess.run([str(executable),'-I','-S','--version'],text=True,capture_output=True,timeout=5)
    if version.returncode or not re.fullmatch(r'Python 3\.8\.[0-9]+\s*',version.stdout):
        raise ValueError('reviewed replay Python version unavailable or incompatible')
    runtime={'links':python_links,'executable':str(executable),'sha256':python_hash,
             'version':version.stdout.strip(),'pyvenv_cfg_sha256':pyvenv_hash}
    inspected=subprocess.run(['docker','image','inspect','vehicle-ppo:v1.0','--format','{{.Id}}'],
                             text=True,capture_output=True,timeout=10)
    image_id=inspected.stdout.strip()
    if inspected.returncode or not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id):
        raise ValueError('reviewed Recorder Docker image identity unavailable')
    budget()
    value={'schema_version':1,'source_run':source_run,'protocol':'reviewed-sw0058-stream-v1','snapshot':snapshot,
           'controls':controls,'recorder_dependencies':dependencies,'python_runtime':runtime,
           'recorder_image':{'tag':'vehicle-ppo:v1.0','id':image_id}}
    value['template_id']=digest(value)
    return value


def load_template(m,registered):
    current=qualify(m,registered['source_run'])
    if current!=registered:raise ValueError('reviewed adapter template changed; review again before binding')
    base=m.BASE/'adapters'/registered['source_run']
    texts={name:(base/name).read_text(encoding='utf-8') for name in TEMPLATE_FILES}
    return {'id':registered['source_run'],'job':json.loads(texts['job.json']),
            'profile':json.loads(texts['profile.json']),'adapter':texts['small_stream_adapter.py'],
            'driver':texts['sw0058_gate_probe_driver_reference.py'],
            'job_text':texts['job.json'],'profile_text':texts['profile.json'],
            'hashes':{name:registered['snapshot'][str(base/name)] for name in TEMPLATE_FILES}}


def package_inventory(package):
    """Read the new package, not the historical template's installed image."""
    start=time.monotonic();entries={};common=None;expanded=0;seen=set()
    with tarfile.open(package,'r:gz') as archive:
        for number,item in enumerate(archive):
            if time.monotonic()-start>120 or number>=100000:raise ValueError('package inventory exceeded budget')
            if len(item.name)>4096 or len(canonical(item.pax_headers))>262144:raise ValueError('archive metadata exceeds limit')
            if item.name.startswith('/') or '\\' in item.name or '..' in PurePosixPath(item.name).parts:
                raise ValueError('unsafe archive path')
            parts=[v for v in item.name.split('/') if v not in ('','.')] 
            if not parts and item.isdir():continue
            if not parts or parts[0]!='livc':raise ValueError('APP must have one livc root')
            rel='/'.join(parts[1:])
            if not rel and item.isdir():continue
            if rel in seen:raise ValueError('duplicate archive member')
            seen.add(rel)
            if item.isdir():continue
            if item.isfile():
                if rel in CONTROL_HASHES and rel.startswith('bin/') and not item.mode & 0o111:
                    raise ValueError('APP control target is not executable: '+rel)
                expanded+=item.size
                if expanded>32*1024**3 or getattr(item,'sparse',None):raise ValueError('expanded archive exceeds supported limit')
                if rel==COMMON and item.size>1024*1024:raise ValueError('common YAML exceeds limit')
                h=hashlib.sha256();chunks=[];total=0
                with archive.extractfile(item) as stream:
                    for chunk in iter(lambda:stream.read(1048576),b''):
                        if time.monotonic()-start>120:raise ValueError('package inventory exceeded budget')
                        total+=len(chunk);h.update(chunk)
                        if rel==COMMON:chunks.append(chunk)
                if total!=item.size:raise ValueError('truncated archive')
                entries[rel]={'kind':'file','sha256':h.hexdigest()}
                if rel==COMMON:common=b''.join(chunks)
            elif item.issym():
                target=posixpath.normpath(posixpath.join(posixpath.dirname(rel),item.linkname))
                if item.linkname.startswith('/') or '\\' in item.linkname or target=='..' or target.startswith('../'):
                    raise ValueError('archive link escapes APP')
                entries[rel]={'kind':'link','target':item.linkname}
            else:raise ValueError('unsupported APP archive member')
    if common is None:raise ValueError('supported common_param.yaml is missing')
    for name in entries:
        if any(entries.get(str(parent),{}).get('kind')=='link' for parent in PurePosixPath(name).parents):
            raise ValueError('archive member descends through a link')
    return entries,common


def cached_package_inventory(package,cache=None):
    """Only unmodified base inventory is cached, by live SHA and implementation."""
    if not cache:return package_inventory(package)
    import base64
    import uuid
    root=Path(cache['root']);package=owned(root,package)
    expected=cache['sha256'];version=cache['version']
    if not re.fullmatch('[a-f0-9]{64}',expected) or not re.fullmatch('[a-f0-9]{64}',version):raise ValueError('invalid inventory cache identity')
    before=package.stat();stamp=(before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
    if sha(package)!=expected:raise ValueError('APP differs from frozen hash')
    directory=owned(root,root/'.material-cache');path=directory/('binding-'+expected+'-'+version+'.json')
    def unchanged():
        now=package.stat()
        if (now.st_ino,now.st_size,now.st_mtime_ns,now.st_ctime_ns)!=stamp:raise ValueError('APP changed during inventory')
    try:
        saved=read(path)
        if saved['sha256']==expected and saved['version']==version and saved['value_sha256']==digest(saved['value']):
            value=saved['value'];common=base64.b64decode(value['common'],validate=True)
            if len(common)<=1024*1024 and isinstance(value['entries'],dict):
                unchanged();return value['entries'],common
    except (OSError,ValueError,KeyError,TypeError):pass
    entries,common=package_inventory(package);unchanged()
    try:
        directory.mkdir(exist_ok=True)
        value={'entries':entries,'common':base64.b64encode(common).decode()}
        saved={'sha256':expected,'version':version,'value':value,'value_sha256':digest(value)}
        raw=canonical(saved)
        if len(raw)<=8*1024**2:
            temp=directory/('.binding-'+uuid.uuid4().hex)
            with temp.open('xb') as f:f.write(raw)
            temp.chmod(0o600);os.replace(temp,path)
    except OSError:pass
    return entries,common


def inventory(package,replacements,original_entries=None,cache=None):
    entries,common=cached_package_inventory(package,cache)
    original=common
    if original_entries is not None:
        original_entries.update(json.loads(canonical(entries)))
    for replacement in replacements:
        target=replacement['target']
        if target not in entries or entries[target]['kind']!='file':raise ValueError('replacement target must be an existing regular APP file')
        entries[target]={'kind':'file','sha256':replacement['sha256']}
        if target==COMMON:common=Path(replacement['path']).read_bytes()
    return entries,original,common


def make_plan(m,render,template,candidate,run_id,actor,indices,resume=False):
    if not m.SAFE.fullmatch(run_id) or not m.SAFE.fullmatch(actor) or len(run_id)>86:raise ValueError('invalid candidate run identity')
    prepared=candidate['dataset']['prepared_scenes'];cameras=candidate['dataset']['camera_roots']
    if not indices or indices!=sorted(set(indices)) or any(type(i) is not int or not 1<=i<=len(prepared) for i in indices):
        raise ValueError('invalid candidate scene selection')
    if len(cameras)!=len(prepared):raise ValueError('dataset needs explicit timestamped camera roots')
    selections=[]
    for i in indices:
        item=prepared[i-1];name=item['scene'];camera=Path(cameras[i-1])
        if not m.SCENE.fullmatch(name):raise ValueError('unsafe scene name')
        if not resume and (not camera.is_dir() or camera.is_symlink()
                or type(item.get('camera_frames')) is not int or item['camera_frames']<=0):
            raise ValueError('prepared camera source changed')
        selections.append({'index':i,'scene':name,'camera':str(camera),'camera_frames':item['camera_frames'],'archive_stats':item})
    if not resume and any(path.exists() for path in (m.BASE/'adapters'/run_id,m.ROOT/'runs'/run_id,m.ROOT/'tasks'/run_id)):
        raise ValueError('candidate run identity is already used')
    rendered=render(template,candidate,run_id,actor,indices)
    p={'source_run':template['id'],'run_id':run_id,'task_id':run_id,'actor':actor,'scene_count':len(indices),
       'scenes':selections,'resume_delivery':resume,**{k:rendered[k] for k in ('job','profile','adapter','driver')},
       'required_output_types':rendered['identity']['required_output_types'],'binding_identity':candidate['binding_identity'],
       'replay_only':candidate.get('replay_only',False),'replay_owner_root':candidate['owner_root']}
    if resume:
        existing=m.BASE/'adapters'/run_id
        for name,value in (('job.json',p['job']),('profile.json',p['profile']),('gateway-binding.json',p['binding_identity'])):
            if read(existing/name)!=value:raise ValueError('existing run differs from frozen candidate')
        for name,value in (('small_stream_adapter.py',p['adapter']),('sw0058_gate_probe_driver_reference.py',p['driver'])):
            if (existing/name).read_text(encoding='utf-8')!=value:raise ValueError('existing adapter code differs from frozen candidate')
    return p


def configure_batch(m):
    m.derive=lambda p:(p['job'],p['profile'],p['adapter'])
    def prepare(p):
        target=m.BASE/'adapters'/p['run_id'];target.mkdir(mode=0o750)
        for name,value in (('job.json',p['job']),('profile.json',p['profile']),('gateway-binding.json',p['binding_identity'])):
            m.write(target/name,value)
        (target/'small_stream_adapter.py').write_text(p['adapter'],encoding='utf-8')
        (target/'sw0058_gate_probe_driver_reference.py').write_text(p['driver'],encoding='utf-8')
        return target/'job.json',target/'profile.json'
    m.prepare=prepare


def bind(req,m,files,adapter,dataset_input):
    preparation=req['preparation'];result=preparation['result'];root=req['owner_root']
    if preparation['owner']!=req['owner'] or result['status']!='STATIC_PASSED':raise ValueError('own static preparation required')
    materials=result['assets']
    replay_only=result.get('replay_only') is True
    if replay_only and preparation['payload'].get('goal')!='replay':raise ValueError('replay-only goal differs from confirmed material')
    prepared={'package':materials['package'],'dataset':result['dataset'],
              'dependencies':[materials[key] for key in ('camera_so','lidar_so','m100_so')]}
    if canonical(prepared)!=canonical(req['assets']):raise ValueError('inputs differ from statically checked preparation')
    if any(a.get('owner')!=req['owner'] for a in [prepared['package'],*prepared['dependencies']]):raise ValueError('foreign candidate software')
    template=load_template(m,req['template'])
    manifest,_=dataset_input(req,root,req['assets']['dataset'])
    frozen=files.freeze_inputs(root,preparation['id'],req['assets'],result.get('yaml_preparation') or None)
    selected=result['scenes'];data={**req['assets']['dataset']['data'],'prepared_scenes':read(manifest)['prepared_scenes']}
    embedded={}
    entries,original,common=inventory(frozen['package']['path'],frozen['replacements'],embedded,cache={'root':root,'sha256':frozen['package']['sha256'],'version':req['candidate_implementation']['candidate_remote']})
    if result.get('follow_official_build') is True:
        # Goal controls metrics, not a rewrite of the official runtime switch.
        compiled=materials['m100_so'].get('data',{})
        mode=compiled.get('eval_dump') if compiled.get('eval_mode_policy')=='official-v1' else None
        replay_only=adapter.official_replay_without_dump(preparation['payload'].get('goal','replay_and_evaluate'),
                                                        adapter.config_fields(common.decode('utf-8')),mode)
    package_origin=materials['package'].get('origin','personal')
    package_source_sha256=(materials['package']['data'].get('package_source_sha256',frozen['package']['sha256'])
                          if package_origin=='assembly_catalog' else frozen['package']['sha256'])
    m100=next(r['sha256'] for r in frozen['replacements']
              if r['kind']=='dependency' and r['target']=='lib/liburoad_m100.so')
    normalized,normalization=(common.decode('utf-8'),None) if replay_only else adapter.protocol_normalize_common(original.decode('utf-8'),common.decode('utf-8'),
        package_sha256=frozen['package']['sha256'],package_source_sha256=package_source_sha256,
        package_origin=package_origin,m100_sha256=m100,template_id=template['id'],
        user_operations=(result.get('yaml_preparation') or {}).get('changes',[]),replacements=frozen['replacements'])
    if normalization:
        common=normalized.encode('utf-8')
        entries[COMMON]={'kind':'file','sha256':normalization['after_sha256']}
    controls=req['template']['controls']
    for item in controls:
        if sha(item['path'])!=CONTROL_HASHES.get(item['target']):raise ValueError('shared control identity mismatch')
        entries[item['target']]={'kind':'file','sha256':item['sha256'],'executable':item['target'].startswith('bin/')}
    mode=(preparation.get('payload') or {}).get('replay_mode','stream')
    if mode not in ('stream','frame_by_frame'):raise ValueError('unsupported replay mode')
    identity={'input_digest':frozen['input_digest'],'template_id':req['template']['template_id'],
              'implementation_hashes':req['candidate_implementation'],'scenes':selected,'replay_mode':mode}
    if replay_only:identity['replay_only']=True
    if normalization:identity['protocol_normalization_sha256']=adapter.normalization_digest(normalization)
    binding_id=digest(identity)
    directory=owned(root,str(Path(root)/'preparations'/preparation['id']/'bindings'/binding_id))
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    owned(root,str(directory))
    config=directory/'common_param.yaml';write_once(config,common)
    candidate={'owner_root':root,'replay_mode':mode,'replay_only':replay_only,'package':{'remote_path':frozen['package']['path'],'sha256':frozen['package']['sha256']},
               'package_source_sha256':package_source_sha256,'package_origin':package_origin,
               'replacements':[{**r,'remote_path':r['path']} for r in frozen['replacements']],
               'configuration':{'remote_path':str(config),'sha256':sha(config),'text':common.decode('utf-8')},
               'original_configuration_text':original.decode('utf-8'),
               'formal_manifest':entries,'dataset':data,'yaml_preparation':result.get('yaml_preparation') or {},
               'controls':controls,
               'embedded_dependencies':{r['target']:r['sha256'] for r in frozen['replacements']
                    if r['kind']=='dependency' and embedded.get(r['target'])=={'kind':'file','sha256':r['sha256']}},
               'yaml_plan':{'operations':(result.get('yaml_preparation') or {}).get('changes',[])},
               'binding_identity':identity}
    if normalization:
        candidate['protocol_normalization']=normalization
        candidate['replacements'].append({'kind':'protocol_normalization','target':COMMON,
            'path':str(config),'remote_path':str(config),'sha256':normalization['after_sha256']})
    configure_batch(m)
    if replay_only:adapter.configure_replay_only_batch(m)
    check_id='candidate-'+binding_id[:24]
    smoke=make_plan(m,adapter.render,template,candidate,check_id+'-smoke',req['actor'],[selected[0]])
    full=make_plan(m,adapter.render,template,candidate,check_id,req['actor'],selected)
    validation={'smoke':m.offline_validate(smoke),'replay':m.offline_validate(full)}
    files.validate_inputs(root,frozen)
    policy={'protocol':req['template'].get('protocol'),'replay_mode':mode,'fixed_configuration':adapter.configuration_overrides('<run_id>',not replay_only),'replay_only':replay_only,
            'required_output_types':full['required_output_types'],'selected_scenes':selected,'precheck_scenes':selected[:1],
            'dependency_targets':[r['target'] for r in frozen['replacements'] if r['kind']=='dependency'],
            'yaml_targets':[r['target'] for r in frozen['replacements'] if r['kind']=='yaml'],
            'runtime_compatibility':'NOT_VERIFIED'}
    if normalization:policy['protocol_normalization']=normalization
    receipt={'schema_version':1,'state':'READY_FOR_GATE','owner':req['owner'],'preparation_id':preparation['id'],
             'preparation_fingerprint':preparation['fingerprint'],'template':req['template'],'frozen':frozen,
             'candidate':candidate,'scenes':selected,'binding_identity':identity,'offline_validation':validation,
             'execution_policy':policy,'gate_verified':False,'hardware_actions':'NOT_RUN'}
    for label,p in (('smoke',smoke),('replay',full)):
        for name,value in (('job.json',p['job']),('profile.json',p['profile'])):write_once(directory/(label+'-'+name),canonical(value))
        for name,key in (('adapter.py','adapter'),('driver.py','driver')):write_once(directory/(label+'-'+name),p[key].encode())
    path=directory/'binding.json';write_once(path,canonical(receipt))
    return {'state':'READY_FOR_GATE','path':str(path),'sha256':sha(path),'input_digest':frozen['input_digest'],
            'template_id':req['template']['template_id'],'implementation_hashes':req['candidate_implementation'],
            'execution_policy':policy,'hardware_actions':'NOT_RUN'}


def binding_receipt(req):
    """Read the owner-scoped receipt without re-inspecting software or data."""
    b=req['baseline'];ref=b['binding'];path=owned(req['owner_root'],ref['path'])
    if sha(path)!=ref['sha256']:raise ValueError('candidate binding receipt changed')
    receipt=read(path)
    if (receipt.get('state')!='READY_FOR_GATE' or receipt.get('gate_verified') is not False
            or receipt['owner']!=req['owner'] or receipt['preparation_id']!=b['preparation_id']
            or receipt['preparation_fingerprint']!=b['preparation_fingerprint']
            or receipt['frozen']['input_digest']!=ref['input_digest']):raise ValueError('candidate binding identity mismatch')
    return receipt


def load_binding(req,m,files,dataset_input,adapter=None):
    receipt=binding_receipt(req)
    if receipt['binding_identity']['implementation_hashes']!=req['candidate_implementation']:raise ValueError('candidate implementation changed; rebind materials')
    template=load_template(m,receipt['template'])
    # Re-freeze the queued assets to reject substituted metadata, versions and paths.
    current=files.freeze_inputs(req['owner_root'],receipt['preparation_id'],req['assets'],receipt['candidate']['yaml_preparation'] or None)
    if current!=receipt['frozen']:raise ValueError('queued inputs differ from frozen binding')
    dataset_input(req,req['owner_root'],req['assets']['dataset'])
    config=owned(req['owner_root'],receipt['candidate']['configuration']['remote_path'])
    if not config.is_file() or config.stat().st_nlink!=1:raise ValueError('bound configuration is not a regular private file')
    if sha(config)!=receipt['candidate']['configuration']['sha256']:raise ValueError('bound configuration changed')
    candidate=receipt['candidate'];normalization=candidate.get('protocol_normalization')
    if (normalization or 'protocol_normalization_sha256' in receipt['binding_identity']
            or receipt.get('execution_policy',{}).get('protocol_normalization')
            or any(r.get('kind')=='protocol_normalization' for r in candidate['replacements'])):
        if adapter is None:raise ValueError('protocol normalization validator is required')
        package=req['assets']['package'];origin=package.get('origin','personal')
        source=(package['data'].get('package_source_sha256',current['package']['sha256'])
                if origin=='assembly_catalog' else current['package']['sha256'])
        if (candidate.get('package_origin')!=origin or candidate.get('package_source_sha256')!=source
                or candidate['package']!={'remote_path':current['package']['path'],'sha256':current['package']['sha256']}):
            raise ValueError('protocol normalization package lineage differs from frozen owner asset')
        entries,original,common=inventory(current['package']['path'],current['replacements'])
        m100=next(r['sha256'] for r in current['replacements']
                  if r['kind']=='dependency' and r['target']=='lib/liburoad_m100.so')
        text,proof=adapter.protocol_normalize_common(original.decode('utf-8'),common.decode('utf-8'),
            package_sha256=current['package']['sha256'],package_source_sha256=source,package_origin=origin,
            m100_sha256=m100,template_id=template['id'],
            user_operations=candidate.get('yaml_plan',{}).get('operations',[]),replacements=current['replacements'])
        derived={'kind':'protocol_normalization','target':COMMON,'path':str(config),'remote_path':str(config),
                 'sha256':(proof or {}).get('after_sha256')}
        identity={'input_digest':current['input_digest'],'template_id':receipt['template']['template_id'],
                  'implementation_hashes':req['candidate_implementation'],'scenes':receipt['scenes'],
                  'protocol_normalization_sha256':adapter.normalization_digest(proof)}
        if 'replay_mode' in receipt['binding_identity']:
            identity['replay_mode']=candidate.get('replay_mode','stream')
        if (not proof or proof!=normalization or receipt.get('execution_policy',{}).get('protocol_normalization')!=proof
                or candidate['original_configuration_text']!=original.decode('utf-8')
                or config.read_bytes()!=text.encode('utf-8') or candidate['configuration'].get('text')!=text
                or candidate['replacements']!=[{**r,'remote_path':r['path']} for r in current['replacements']]+[derived]
                or candidate['controls']!=receipt['template']['controls']
                or candidate['binding_identity']!=identity or receipt['binding_identity']!=identity):
            raise ValueError('protocol normalization differs from frozen derivation')
        entries[COMMON]={'kind':'file','sha256':proof['after_sha256']}
        for item in receipt['template']['controls']:
            if sha(item['path'])!=CONTROL_HASHES.get(item['target']):raise ValueError('shared control identity mismatch')
            entries[item['target']]={'kind':'file','sha256':item['sha256'],'executable':item['target'].startswith('bin/')}
        if entries!=candidate['formal_manifest']:raise ValueError('normalized formal inventory changed')
        adapter.validate_protocol_normalization(candidate,template['id'])
    if req['operation'] not in ('diagnose',) and any(i not in receipt['scenes'] for i in req.get('scenes',[])):
        raise ValueError('requested scenes outside bound preparation')
    return receipt,template


def recovered_precheck(req,m,p,receipt):
    """Verify an already delivered precheck; never run, install or clean a bench."""
    parent=req.get('parent_run_id')
    if (not isinstance(parent,str) or not m.SAFE.fullmatch(parent)
            or req['run_id']!=parent+'-smoke' or req['scenes']!=receipt['scenes'][:1]):
        raise ValueError('precheck recovery must identify this task and first selected scene')
    return recovered_retained(req,m,p,receipt)


def recovered_replay(req,m,p,receipt):
    """Verify a complete retained full run with the same positive proof as precheck."""
    if req['run_id']!=req.get('parent_run_id') or req['scenes']!=receipt['scenes']:
        raise ValueError('replay recovery must identify the exact full task and selected scenes')
    return recovered_retained(req,m,p,receipt)


def recovered_retained(req,m,p,receipt):
    lock=m.BASE/'locks/bench.lock'
    queue=m.command('queue')
    if (not isinstance(queue,dict) or not {'active','queued','waiting'}.issubset(queue)
            or queue['active'] or queue['queued'] or queue['waiting'] or lock.exists() or lock.is_symlink()):
        raise ValueError('Runner is not independently verified idle; recovery remains pending')
    run=req['run_id'];run_dir=m.ROOT/'runs'/run;task=m.ROOT/'tasks'/run
    # validate_and_retain is read-only only when the complete retained task exists.
    # An incomplete delivery is not permission to run cleanup or the APP again.
    if not task.is_dir() or task.is_symlink() or not (task/'index.json').is_file():
        raise ValueError('precheck retained task is missing; do not automatically rerun')
    exit_path=run_dir/'logs/worker-exit-code.txt'
    if not exit_path.is_file() or exit_path.is_symlink() or exit_path.read_text().strip()!='0':
        raise ValueError('precheck worker completion is not verified')
    checked_task,product_receipt=m.validate_and_retain(p,0)
    if Path(checked_task)!=task:raise ValueError('precheck retained task identity differs')
    cleanup_id=run+'-cleanup';cleanup=m.ROOT/'maintenance'/(cleanup_id+'.json')
    copied=task/'logs/bench-output-cleanup.json'
    if (not cleanup.is_file() or cleanup.is_symlink() or not copied.is_file() or copied.is_symlink()
            or sha(cleanup)!=sha(copied)):
        raise ValueError('precheck cleanup is not independently verified')
    producer=read(product_receipt);data=read(cleanup);accepted=data.get('accepted')
    product=str(Path(product_receipt).parent/'predictions')
    if (data.get('batch_id')!=cleanup_id or not isinstance(accepted,list) or len(accepted)!=1
            or accepted[0].get('run_id')!=run or accepted[0].get('source')!=producer.get('source')
            or accepted[0].get('workstation_product')!=product
            or accepted[0].get('product_receipt')!=str(product_receipt)
            or accepted[0].get('inventory_sha256')!=producer.get('inventory_sha256')
            or not isinstance(producer.get('source'),str) or not producer['source']
            or producer['source'] not in data.get('removed',[])):
        raise ValueError('precheck cleanup receipt differs from this run')
    # Recheck after the potentially long product hash pass.
    after=m.command('queue')
    if after!=queue or lock.exists() or lock.is_symlink():raise ValueError('Runner changed during recovery')
    evidence={str(path):sha(path) for path in (task/'index.json',product_receipt,cleanup,copied,
              run_dir/'state.json',run_dir/'restore-verification.json',exit_path)}
    return {'state':'PASS','run_id':run,'input_digest':receipt['frozen']['input_digest'],
            'task':str(task),'product':product,'receipt':str(product_receipt),'cleanup_receipt':str(cleanup),
            'evidence_only':True,'resource_idle':True,'restore_verified':True,'cleanup_verified':True,
            'binding_verified':True,'hardware_actions':'NOT_RUN','evidence':evidence}


def no_previous_attempt(req,m):
    """Positive evidence for rejecting a new precheck before any transaction.

    No such proof is issued after a mutation boundary, or for an uncertain old
    attempt. Those cases retain the existing manual-reconciliation barrier.
    """
    run=req.get('run_id','')
    if (req.get('operation')!='smoke' or req.get('_dispatch_started')
            or not isinstance(run,str) or not run.endswith('-smoke') or not m.SAFE.fullmatch(run)):
        return None
    parent=run[:-6]
    if not parent or not m.SAFE.fullmatch(parent):return None
    for identity in (run,parent,run+'-install',parent+'-install'):
        for path in (m.ROOT/'runs'/identity,m.BASE/'runs'/identity,
                     m.BASE/'adapters'/identity,m.ROOT/'tasks'/identity):
            if path.exists() or path.is_symlink():return None
    lock=m.BASE/'locks/bench.lock'
    if lock.exists() or lock.is_symlink():return None
    queue=m.command('queue')
    if (not isinstance(queue,dict) or not {'active','queued','waiting'}.issubset(queue)
            or queue['active'] or queue['queued'] or queue['waiting']):return None
    return {'schema_version':1,'code':'PREDISPATCH_REJECTED','run_id':run,
            'hardware_actions':'NOT_RUN','no_previous_attempt':True,'runner_idle':True}


def bounded_failure_diagnostics(run_dir, adapter_dir):
    """Read a fixed, bounded set of this run's logs; never probe or repair hardware.

    Categories describe observed log messages, not a proven root cause. In
    particular, timeout is not evidence that a package or SO is incompatible.
    Caller-supplied roots must identify the same safe run under runs/adapters.
    """
    import stat
    from urllib.parse import urlsplit, urlunsplit

    run_dir, adapter_dir = Path(run_dir), Path(adapter_dir)
    safe_id = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
    paths = ((run_dir, 'logs/worker-cli.log'), (run_dir, 'probe.log'),
             (adapter_dir, 'evidence/' + run_dir.name + '/batch-result.json'))
    maximum = 64 * 1024
    logs, observed = [], []

    def unlinked(path):
        for entry in (path, *path.parents):
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                return False
        return True

    valid_roots = (run_dir.is_absolute() and adapter_dir.is_absolute()
                   and '..' not in run_dir.parts and '..' not in adapter_dir.parts
                   and run_dir.parent.name == 'runs' and adapter_dir.parent.name == 'adapters'
                   and run_dir.name == adapter_dir.name and bool(safe_id.fullmatch(run_dir.name)))
    try:
        valid_roots = valid_roots and unlinked(run_dir) and unlinked(adapter_dir)
    except OSError:
        valid_roots = False

    def tail(root, relative):
        path = root / relative
        if root not in path.parents or not unlinked(path):
            return None
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        descriptor = os.open(str(path), flags)
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                return None
            os.lseek(descriptor, max(0, opened.st_size - maximum), os.SEEK_SET)
            raw = os.read(descriptor, maximum)
            after = os.fstat(descriptor)
            current = path.lstat()
            if (not unlinked(path)
                    or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
                return None
        finally:
            os.close(descriptor)
        logs.append(str(path))
        if opened.st_size > maximum:
            # A truncated first line may have lost a credential key or an error
            # prefix. Never quote or classify that incomplete log fragment.
            _, separator, raw = raw.partition(b'\n')
            if not separator:
                raw = b''
        return raw.decode('utf-8', errors='replace'), opened.st_size <= maximum

    def redact(text):
        # Remove terminal control sequences before displaying untrusted logs.
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
        text = ''.join(char for char in text if char >= ' ' or char == '\t')
        def url(match):
            try:
                parts = urlsplit(match.group(0))
                host = parts.netloc.rsplit('@', 1)[-1]
                return urlunsplit((parts.scheme, host, parts.path,
                                   '[REDACTED]' if parts.query else '', ''))
            except ValueError:
                return '[REDACTED_URL]'
        text = re.sub(r'https?://[^\s<>\"\']+', url, text, flags=re.I)
        text = re.sub(r'(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+', r'\1[REDACTED]', text)
        text = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [REDACTED]', text)
        keys = r'(?:[\w.-]*[_-])?(?:token|password|passwd|secret|cookie|authorization|api[_-]?key|credential|private[_-]?key)[\w.-]*'
        quoted = r'(?:\"(?:\\.|[^\"\\])*(?:\"|$)|\'(?:\\.|[^\'\\])*(?:\'|$)|[^\s,;]+)'
        text = re.sub(r'(?i)([\"\']?' + keys + r'[\"\']?\s*[:=]\s*)' + quoted, r'\1[REDACTED]', text)
        text = re.sub(r'(?i)(--' + keys + r'\s+)(?:\"[^\"]*\"|\'[^\']*\'|[^\s,;]+)', r'\1[REDACTED]', text)
        return text.strip()[:400]

    patterns = (
        ('APPLICATION_CRASH', re.compile(r'candidate startup (?:APPLICATION_FATAL|RESTART_LOOP)|\bSIGABRT\b|__stack_chk_fail|stack smashing detected', re.I)),
        ('ABI_SYMBOL_MISMATCH', re.compile(r'undefined symbol\s*:|symbol lookup error\s*:|wrong ELF class|version\s+[\"\'`]?\b(?:GLIBCXX|CXXABI|GLIBC)_[^\s\"\'`]+[\"\'`]?\s+not found', re.I)),
        ('MISSING_LIBRARY', re.compile(r'error while loading shared libraries\s*:|cannot open shared object file|\.so(?:\.[\w.]+)?[^\n]{0,180}(?:No such file or directory|not found)', re.I)),
        ('ENDPOINT_UNAVAILABLE', re.compile(r'connection refused|no route to host|failed to connect to all addresses|(?:connect|connection)[^\n]{0,120}(?:network is unreachable|host is unreachable)', re.I)),
        ('DATA_MISSING', re.compile(r'(?:FileNotFoundError|No such file or directory|missing|not found)[^\n]{0,160}(?:\.(?:pack|pcap|jpeg|jpg|png|mp4|npy)\b|manifest\.json\b)|(?:\.(?:pack|pcap|jpeg|jpg|png|mp4|npy)\b|manifest\.json\b)[^\n]{0,160}(?:No such file or directory|not found|missing)', re.I)),
        ('TIMEOUT', re.compile(r'\b(?:TimeoutExpired|TimeoutError)\b|\btimed out\b|\bdeadline exceeded\b|\btimeout\s+(?:expired|exceeded|error)\b', re.I)),
    )

    def record(path, text, explicit_error=False):
        # Inspect only bounded error lines; do not send successful log chatter.
        for line in text.splitlines():
            for category, pattern in patterns:
                if pattern.search(line):
                    observed.append((category, str(path), redact(line)))
                    break
            else:
                if explicit_error or re.search(r'\b(?:ERROR|FATAL|Traceback|Exception|failed)\b', line, re.I):
                    observed.append(('UNKNOWN', str(path), redact(line)))

    def json_errors(value):
        errors, remaining = [], [64]
        def visit(item, failed=False, depth=0):
            if remaining[0] <= 0 or depth > 6:
                return
            remaining[0] -= 1
            if isinstance(item, dict):
                state = str(item.get('status', item.get('state', ''))).upper()
                if state in ('PASS', 'PASSED', 'SUCCESS', 'SUCCEEDED') or item.get('success') is True:
                    failed = False
                else:
                    failed = failed or state in ('FAILED', 'FAIL', 'ERROR', 'BLOCKED') or item.get('success') is False
                for key in ('error', 'exception'):
                    if isinstance(item.get(key), str) and item[key].strip():
                        errors.append(item[key])
                if failed:
                    for key in ('message', 'reason', 'detail', 'stderr'):
                        if isinstance(item.get(key), str) and item[key].strip():
                            errors.append(item[key])
                for key, child in list(item.items())[:64]:
                    if isinstance(child, (dict, list)):
                        visit(child, failed or key in ('errors', 'failed', 'failures'), depth + 1)
            elif isinstance(item, list):
                for child in item[:64]:
                    if isinstance(child, str) and failed:
                        errors.append(child)
                    elif isinstance(child, (dict, list)):
                        visit(child, failed, depth + 1)
        visit(value)
        return errors

    if valid_roots:
        for root, relative in paths:
            try:
                result = tail(root, relative)
                if result is None:
                    continue
                text, complete = result
                path = root / relative
                if relative.endswith('.json'):
                    if complete:
                        for error in json_errors(json.loads(text)):
                            record(path, error, explicit_error=True)
                else:
                    record(path, text)
            except (OSError, ValueError, RuntimeError, RecursionError):
                continue
    priorities = {category: index for index, (category, _) in enumerate(patterns)}
    priorities['UNKNOWN'] = len(patterns)
    category = min((entry[0] for entry in observed), key=lambda value: priorities[value]) if observed else 'UNKNOWN'
    evidence, seen = [], set()
    for matched, path, excerpt in sorted(observed, key=lambda entry: priorities[entry[0]]):
        if (path, excerpt) not in seen:
            evidence.append({'path': path, 'excerpt': excerpt})
            seen.add((path, excerpt))
        if len(evidence) == 3:
            break
    messages = {
        'APPLICATION_CRASH': ('日志出现应用致命错误或启动期间重启。', '启动等待已提前结束，请排查本次软件包及依赖的运行日志；不自动重启、换库或重跑。'),
        'MISSING_LIBRARY': ('日志出现共享库加载或缺失错误。', '请核对本次软件包及三项 SO 的替换目标和完整性，补充匹配成品后重新检查材料；不自动换库或重试。'),
        'ABI_SYMBOL_MISMATCH': ('日志出现动态符号或 ELF/运行库版本错误。', '请提供与本次软件包接口和运行环境匹配的 SO，并结合摘录排查构建版本；不自动修改算法或反复编译。'),
        'ENDPOINT_UNAVAILABLE': ('日志出现回放端点连接失败；具体连接原因仍需核验。', '请核对本次相机/雷达端点和进程日志，并检查台架连接；不凭此断言物理断线或自动猜端口。'),
        'DATA_MISSING': ('日志出现回放数据文件或 manifest 缺失错误。', '请核对已绑定数据源、文件完整性及读取权限；修复来源后重新检查材料。'),
        'TIMEOUT': ('日志出现超时，尚不能据此判断软件包或 SO 不兼容。', '请结合超时阶段、进程及数据日志核查原因；先核验恢复收据，不自动重试。'),
        'UNKNOWN': ('已读取有限日志，未发现可可靠分类的错误证据。' if logs else '本任务没有可安全读取的固定日志，原因尚未确定。', '请核对本任务日志、软件包和依赖，并补充错误证据；不会自动换库、猜端口或重试。'),
    }
    summary, next_action = messages[category]
    return {'category': category, 'summary': summary, 'evidence': evidence,
            'next_action': next_action, 'logs': logs, 'diagnostic_scope': 'bounded-local-logs'}


def execute(req,m,files,adapter,dataset_input,runtime_installer,formal_matches):
    operation=req['operation']
    if operation=='diagnose':
        # A failure is diagnosable even if an input was removed or the adapter
        # was upgraded. Authenticate the binding, then read only this run's logs.
        binding_receipt(req)
        state=m.ROOT/'runs'/req['run_id']/'restore-verification.json'
        restored=read(state) if state.exists() else {}
        lock=m.BASE/'locks/bench.lock'
        diagnostics=bounded_failure_diagnostics(m.ROOT/'runs'/req['run_id'],m.BASE/'adapters'/req['run_id'])
        return {**diagnostics,'state':'DIAGNOSED',
                'safe_to_continue':restored.get('restore_ok') is True and not lock.exists() and not lock.is_symlink(),
                'restore_verified':restored.get('restore_ok') is True}
    receipt,template=load_binding(req,m,files,dataset_input,adapter=adapter)
    candidate=receipt['candidate'];configure_batch(m)
    if candidate.get('replay_only'):adapter.configure_replay_only_batch(m)
    p=make_plan(m,adapter.render,template,candidate,req['run_id'],req['actor'],req['scenes'],operation in ('recover_delivery','reconcile_precheck','reconcile_replay'))
    if operation=='reconcile_precheck':return recovered_precheck(req,m,p,receipt)
    if operation=='reconcile_replay':return recovered_replay(req,m,p,receipt)
    if operation=='recover_delivery':return {**m.finish_delivery(p,0),'input_digest':receipt['frozen']['input_digest']}
    if operation not in ('smoke','replay'):raise ValueError('unsupported candidate operation')
    if operation=='smoke' and req['scenes']!=receipt['scenes'][:1]:raise ValueError('precheck must use the first selected scene')
    if operation=='replay':
        if req['scenes']!=receipt['scenes'] or req.get('smoke_run')!=req['run_id']+'-smoke' or req.get('smoke_scenes')!=receipt['scenes'][:1]:
            raise ValueError('full replay is missing this task\'s selected-scene precheck')
        smoke=make_plan(m,adapter.render,template,candidate,req['smoke_run'],req['actor'],req['smoke_scenes'],True)
        m.validate_and_retain(smoke,0)
    def activate_candidate():
        install=runtime_installer(req.get('runtime_contract'))
        if formal_matches(m,candidate['formal_manifest']):
            req['_dispatch_started']=True
            m.progress_event(req['run_id'],'SOFTWARE_READY')
            return
        argv=['python3',str(install),'--root',str(m.BASE),'--run-id',req['run_id']+'-install',
              '--package',candidate['package']['remote_path'],'--expected-sha256',candidate['package']['sha256'],'--clean-formal','--execute']
        for item in candidate['replacements']+candidate['controls']:
            # The installer always requires an explicit M100 input, including
            # canonical APPs that already embed those exact bytes.
            if item['target']!='lib/liburoad_m100.so' and candidate.get('embedded_dependencies',{}).get(item['target'])==item['sha256']:
                continue
            argv += ['--m100-so',item['path']] if item['target']=='lib/liburoad_m100.so' else ['--replace',item['target']+'='+item['path']]
        import subprocess
        req['_dispatch_started']=True
        result=subprocess.run(argv,text=True,capture_output=True)
        if result.returncode:raise ValueError('candidate installation failed; inspect installer receipts')
        if not formal_matches(m,candidate['formal_manifest']):raise ValueError('installed candidate files do not match binding')
        m.progress_event(req['run_id'],'SOFTWARE_INSTALLED')
    # Batch preflight happens once, before any installer mutation. Its native
    # queue/lock still protects against another workstation client racing us.
    result=m.execute(p,before_submit=activate_candidate)
    if result.get('state')=='RESOURCE_BUSY':return result
    if result.get('state')!='PASS':raise ValueError('candidate replay receipt did not pass')
    return {**result,'input_digest':receipt['frozen']['input_digest'],'workstation':'ubuntu-workstation'}
