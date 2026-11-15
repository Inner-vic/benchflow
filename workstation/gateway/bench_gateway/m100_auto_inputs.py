"""Derive immutable M100 inputs from an APP integration lock and Conan recipe."""
import ast
import copy
import difflib
import hashlib
import io
import json
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import tarfile
import time
from urllib.parse import quote,urlsplit
from urllib.request import Request,build_opener,HTTPRedirectHandler
from .core import GatewayError
from .download_auth import load_authorization
from .m100_source_auth import load as source_token,HOST,PROJECT,REPOSITORY

class InputError(GatewayError):
    def __init__(self,code,message,next_action):
        super().__init__(message);self.code=code;self.next_action=next_action

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def sha(data):return hashlib.sha256(data).hexdigest()

def locked_node(document):
    graph=document.get('graph_lock',{});nodes=graph.get('nodes',{})
    found=[(key,value) for key,value in nodes.items() if value.get('ref','').startswith('uroad_m100/') and value.get('context','host')=='host']
    if len(found)!=1:raise ValueError('uroad node missing or ambiguous')
    key,node=found[0]
    match=re.fullmatch(r'uroad_m100/([A-Za-z0-9_.+-]+)@([A-Za-z0-9_.+-]+)/([A-Za-z0-9_.+-]+)#([a-f0-9]{32,64})',node['ref'])
    if not match:raise ValueError('recipe revision is not pinned')
    return key,node,match.groups()

def source_lock(document):
    """Select the exact dependency closure; convert only its root to a local recipe."""
    root,_,_=locked_node(document);nodes=document['graph_lock']['nodes'];pending=[root];seen=set()
    while pending:
        key=pending.pop()
        if key in seen:continue
        if key not in nodes:raise ValueError('incomplete locked dependency graph')
        seen.add(key);item=nodes[key]
        for group in ['requires','build_requires']:
            children=item.get(group,[])
            if not isinstance(children,list) or any(not isinstance(x,str) for x in children):raise ValueError('invalid graph edges')
            pending.extend(children)
    result=copy.deepcopy(document);result['graph_lock']['nodes']={k:copy.deepcopy(v) for k,v in nodes.items() if k in seen}
    item=result['graph_lock']['nodes'][root]
    for key in ['package_id','prev','modified','python_requires']:item.pop(key,None)
    item.update(ref='uroad_m100/0.1.0',path='../conanfile.py')
    return result

def recipe_source(body):
    tree=ast.parse(body.decode('utf-8'));classes=[x for x in tree.body if isinstance(x,ast.ClassDef)]
    matches=[]
    for cls in classes:
        values={}
        for item in cls.body:
            if isinstance(item,ast.Assign) and len(item.targets)==1 and isinstance(item.targets[0],ast.Name):
                try:values[item.targets[0].id]=ast.literal_eval(item.value)
                except (ValueError,TypeError):pass
        if values.get('name')!='uroad_m100':continue
        commit=values.get('branch','')
        method=next((x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='source'),None)
        text=ast.unparse(method) if method else ''
        if not re.fullmatch('[a-f0-9]{40}',commit) or 'gitlab.chehejia.com/livc-acore/chassis/uroad.git' not in text or 'self.branch' not in text:
            raise ValueError('source repository or commit is not immutable')
        matches.append(commit)
    if len(matches)!=1:raise ValueError('source identity ambiguous')
    return matches[0]

def enable_eval(body,enabled=True):
    text=body.decode('utf-8');tree=ast.parse(text);lines=text.splitlines(keepends=True)
    candidates=[];anchors=[]
    for item in ast.walk(tree):
        if not isinstance(item,ast.Assign) or len(item.targets)!=1:continue
        target=item.targets[0]
        if not isinstance(target,ast.Subscript) or ast.unparse(target.value)!='self._cmake.definitions':continue
        if isinstance(target.slice,ast.Constant):
            if target.slice.value=='WITH_EVAL_DUMP':candidates.append(item)
            if target.slice.value=='WITH_EDS':anchors.append(item)
    if len(candidates)>1:raise ValueError('ambiguous eval macro')
    if candidates:
        item=candidates[0]
        if item.lineno!=item.end_lineno:raise ValueError('multiline eval macro unsupported')
        line=lines[item.lineno-1];lines[item.lineno-1]=line[:item.value.col_offset]+str(enabled)+line[item.value.end_col_offset:]
    else:
        if len(anchors)!=1:raise ValueError('eval macro insertion point missing')
        item=anchors[0];indent=' '*item.col_offset
        lines.insert(item.end_lineno,indent+'self._cmake.definitions["WITH_EVAL_DUMP"] = '+str(enabled)+'\n')
    patched=''.join(lines).encode();ast.parse(patched)
    diff=''.join(difflib.unified_diff(text.splitlines(True),patched.decode().splitlines(True),fromfile='original/conanfile.py',tofile='eval/conanfile.py'))
    return patched,diff.encode()


def official_eval_mode(recipe, cmake):
    """Read the locked upstream setting; never infer a variant from a task goal."""
    values=[]
    for item in ast.walk(ast.parse(recipe.decode('utf-8'))):
        if not isinstance(item,ast.Assign) or len(item.targets)!=1:continue
        target=item.targets[0]
        if (isinstance(target,ast.Subscript) and ast.unparse(target.value)=='self._cmake.definitions'
                and isinstance(target.slice,ast.Constant) and target.slice.value=='WITH_EVAL_DUMP'):
            if not isinstance(item.value,ast.Constant):
                raise ValueError('official eval setting is not an explicit boolean')
            value=item.value.value
            if isinstance(value,str) and value.upper() in ('ON','OFF','TRUE','FALSE'):
                value=value.upper() in ('ON','TRUE')
            if type(value) is not bool:raise ValueError('official eval setting is not an explicit boolean')
            values.append(value)
    if len(values)>1:raise ValueError('ambiguous official eval setting')
    text=cmake.decode('utf-8')
    active='\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
    if re.search(r'\bset\s*\(\s*WITH_EVAL_DUMP\b',active,re.I):
        raise ValueError('official CMake overrides eval option; manual review required')
    options=re.findall(r'\boption\s*\(\s*WITH_EVAL_DUMP\s+"[^"\n]*"\s+(ON|OFF|TRUE|FALSE)\s*\)',active,re.I)
    if len(options)!=1:raise ValueError('official CMake eval default missing or ambiguous')
    return (values[0], 'locked-conan-definition') if values else (options[0].upper() in ('ON','TRUE'),'locked-cmake-default')

def align_build_requirements(body,document,names=('infer_expert_sdk','function-message')):
    """Apply the document's SDK/message pins from this APP, not old defaults."""
    pins={}
    for name in names:
        found={v['ref'].split('#',1)[0] for v in document['graph_lock']['nodes'].values()
               if v.get('ref','').startswith(name+'/') and v.get('context','host')=='host'}
        if len(found)!=1:raise ValueError('SDK/message dependency missing or ambiguous')
        pins[name]=found.pop()
    text=body.decode();tree=ast.parse(text);lines=text.splitlines(keepends=True);edits=[];counts={k:0 for k in pins}
    for item in ast.walk(tree):
        if not isinstance(item,ast.Call) or not isinstance(item.func,ast.Attribute) or ast.unparse(item.func)!='self.requires' or not item.args:continue
        arg=item.args[0]
        if isinstance(arg,ast.Constant) and isinstance(arg.value,str):value=arg.value
        elif isinstance(arg,ast.JoinedStr) and all(isinstance(v,ast.Constant) and isinstance(v.value,str) for v in arg.values):
            value=''.join(v.value for v in arg.values)
        else:continue
        name=value.split('/',1)[0]
        if name not in pins:continue
        if arg.lineno!=arg.end_lineno:raise ValueError('multiline dependency unsupported')
        counts[name]+=1;edits.append((arg.lineno,arg.col_offset,arg.end_col_offset,json.dumps(pins[name])))
    if any(n!=1 for n in counts.values()):raise ValueError('SDK/message declaration missing or ambiguous')
    for lineno,start,end,value in sorted(edits,reverse=True):lines[lineno-1]=lines[lineno-1][:start]+value+lines[lineno-1][end:]
    patched=''.join(lines).encode();ast.parse(patched);return patched,pins

def extract_source(body,destination):
    """Extract a bounded GitLab source archive; no archive links or special files."""
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=False)
    total=0;count=0;prefix=None
    try:
        with tarfile.open(fileobj=io.BytesIO(body),mode='r:gz') as archive:
            for member in archive:
                count+=1
                if count>60000:raise ValueError('source archive file count exceeded')
                p=PurePosixPath(member.name)
                if p.is_absolute() or '..' in p.parts or not p.parts:raise ValueError('unsafe source archive path')
                if prefix is None:prefix=p.parts[0]
                if p.parts[0]!=prefix:raise ValueError('multiple source archive roots')
                if len(p.parts)==1:
                    if not member.isdir():raise ValueError('invalid archive root')
                    continue
                path=destination.joinpath(*p.parts[1:])
                if member.isdir():path.mkdir(parents=True,exist_ok=True);continue
                if not member.isfile() or member.size>64*1024**2:raise ValueError('source archive links or special members unsupported')
                total+=member.size
                if total>768*1024**2:raise ValueError('expanded source limit exceeded')
                path.parent.mkdir(parents=True,exist_ok=True)
                with archive.extractfile(member) as src,path.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
                path.chmod(0o755 if member.mode&0o111 else 0o644)
    except BaseException:
        shutil.rmtree(destination);raise
    return {'files':count,'expanded_bytes':total}

def _read(url,headers,limit,timeout):
    request=Request(url,headers=headers)
    deadline=time.monotonic()+timeout;blocks=[];total=0
    with build_opener(NoRedirect()).open(request,timeout=timeout) as response:
        expected=response.headers.get('X-Checksum-Sha256')
        while True:
            if time.monotonic()>=deadline:raise TimeoutError('source transfer time budget exceeded')
            block=response.read(min(1024*1024,limit+1-total))
            if not block:break
            total+=len(block)
            if total>limit:raise ValueError('source response too large')
            blocks.append(block)
    body=b''.join(blocks)
    if expected and re.fullmatch('[a-fA-F0-9]{64}',expected) and sha(body)!=expected.lower():raise ValueError('source artifact checksum differs')
    return body

def prepare(cfg,store,user,package,*,build_reference='',fetch=None,eval_dump=True,follow_official=False):
    if not cfg.get('workstation_local_execution'):raise InputError('M100_WORKSTATION_REQUIRED','M100源码准备仅在工作站执行。','请管理员核对工作站服务部署。')
    store.require_user(user)
    fetch=fetch or _read;started=time.monotonic();data=package.get('data',{})
    if package.get('owner')!=user or not re.fullmatch('[a-f0-9]{64}',data.get('sha256','')):raise ValueError('invalid owned APP identity')
    filename=data.get('filename','')
    if filename.endswith('-prepared') and data.get('release_assembly'):
        from .recipes import select
        registered=select(cfg,data['sha256'])
        if registered and registered.get('assembly',{}).get('source_sha256')==data['release_assembly'].get('source_sha256'):
            filename=filename[:-len('-prepared')]
    match=re.fullmatch(r'(CCU_LIVC_AVM-[A-Za-z0-9_.+-]+-(\d+))\.app\.tar\.gz',filename)
    if not match:raise InputError('M100_APP_IDENTITY_REQUIRED','软件包登记中缺少可识别的APP原文件名。','请提供原始LIVC APP链接，不使用改名文件定位源码。')
    if build_reference:
        reference=urlsplit(build_reference)
        if (reference.scheme!='https' or reference.hostname!='ci.ep.chehejia.com' or reference.username or reference.password
                or reference.port not in (None,443) or reference.query or reference.fragment or reference.path.rstrip('/').split('/')[-1]!=match[2]):
            raise InputError('M100_BUILD_REFERENCE_MISMATCH','构建链接与APP构建编号不一致。','请核对APP链接和构建链接属于同一次构建。')
    base=urlsplit(cfg.get('livc_app_base_url',''))
    if base.scheme!='https' or base.hostname!='artifactory.ep.chehejia.com' or base.username or base.password or base.port not in (None,443) or not base.path.endswith('/app/'):
        raise InputError('M100_ARTIFACT_SOURCE_UNCONFIGURED','尚未配置受控APP制品根目录。','请管理员配置LIVC APP制品根目录。')
    root=Path(store.remote_root(user,cfg['remote_users_root']))/'build-inputs'/data['sha256']
    if any(p.is_symlink() for p in (root,*root.parents)):raise ValueError('linked input root')
    root.mkdir(parents=True,exist_ok=True)
    buildsh=bool(follow_official and cfg.get('m100_buildsh_enabled'))
    source_name='source-buildsh-v5' if buildsh else ('source-official-v3' if follow_official else ('source-v2' if eval_dump else 'source-replay-only-v2'))
    marker=root/('inputs-buildsh-v5.json' if buildsh else ('inputs-official-v3.json' if follow_official else ('inputs-v2.json' if eval_dump else 'inputs-replay-only-v2.json')))
    if marker.exists():
        cached=json.loads(marker.read_text())
        if cached.get('package_sha256')!=data['sha256']:raise ValueError('input identity changed')
        if follow_official:
            if cached.get('eval_mode_policy')!='official-v1' or type(cached.get('eval_dump')) is not bool:raise ValueError('official mode evidence missing')
        elif cached.get('eval_dump') is not eval_dump:raise ValueError('input mode changed')
        source=Path(cached['source'])
        if source!=root/source_name or source.resolve()!=source:
            raise ValueError('cached source path differs')
        proof=json.loads((source/'source-snapshot.json').read_text())
        if sha((source/'source-snapshot.json').read_bytes())!=cached['source_snapshot_sha256']:raise ValueError('snapshot changed')
        if sha((source/'build/conan.lock').read_bytes())!=cached['source_lock_sha256']:raise ValueError('source lock changed')
        if follow_official and any(cached.get(k)!=proof.get(k) for k in ('eval_dump','eval_mode_policy','eval_mode_origin','recipe_patch_sha256','source_commit')):
            raise ValueError('official input metadata differs from source proof')
        if buildsh:
            from .m100_ci import build_url
            if (cached.get('build_entry')!='upstream-buildsh-v1'
                    or cached.get('ci')!=proof.get('ci')
                    or cached.get('build_script_sha256')!=proof.get('build_script_sha256')
                    or cached['ci']['build_reference']!=build_url(build_reference,match[2])):
                raise ValueError('build.sh input proof differs')
        return dict(cached,owner=user)
    authorization=load_authorization(cfg,user,base.hostname)
    artifact_headers={'Authorization':authorization} if authorization else {}
    lock_url='https://'+base.netloc+base.path[:-4]+quote(match[1],safe='')+'.lock'
    try:
        lock_file=root/'integration.lock'
        if not lock_file.exists():lock_file.write_bytes(fetch(lock_url,artifact_headers,4*1024**2,25))
        lock_body=lock_file.read_bytes();document=json.loads(lock_body);key,node,identity=locked_node(document)
        ci=None
        if buildsh:
            from .m100_ci import read as read_ci,verify_pins
            ci=read_ci(cfg,build_reference,match[2],fetch)
            verify_pins(ci,document)
        version,namespace,channel,revision=identity
        # The upstream build.sh uses liconan-dev-local. ON/OFF development
        # recipes may exist there only; this changes the repository, never a
        # locked version/revision. Keep legacy exports separate.
        repository='liconan-dev-local' if buildsh else cfg.get('m100_conan_repository','liconan-release-local')
        if not re.fullmatch('[A-Za-z0-9_.-]+',repository):raise ValueError('invalid Conan repository')
        recipe_base='https://'+base.netloc+'/artifactory/api/conan/'+repository+'/v2/conans/uroad_m100/'+quote(version,safe='')+'/'+quote(namespace,safe='')+'/'+quote(channel,safe='')+'/revisions/'+revision+'/files/'
        original=root/('conanfile.buildsh.original.py' if buildsh else 'conanfile.original.py');manifest=root/('conanmanifest.buildsh.txt' if buildsh else 'conanmanifest.txt')
        if not original.exists():original.write_bytes(fetch(recipe_base+'conanfile.py',artifact_headers,256*1024,20))
        if not manifest.exists():manifest.write_bytes(fetch(recipe_base+'conanmanifest.txt',artifact_headers,128*1024,20))
        recipe=original.read_bytes()
        expected=re.findall(r'^conanfile\.py: ([a-f0-9]{32})$',manifest.read_text(),re.M)
        if len(expected)!=1 or hashlib.md5(recipe).hexdigest()!=expected[0]:raise ValueError('recipe manifest mismatch')
        commit=recipe_source(recipe);aligned,pins=align_build_requirements(recipe,document)
        patched=aligned if follow_official else enable_eval(aligned,eval_dump)[0]
        derived=source_lock(document)
        diff=''.join(difflib.unified_diff(recipe.decode().splitlines(True),patched.decode().splitlines(True),fromfile='original/conanfile.py',tofile='eval/conanfile.py')).encode()
        metadata={'schema_version':1,'input_policy':2,'eval_dump':eval_dump,'owner':user,'package_sha256':data['sha256'],'app_filename':filename,'build_reference':build_reference,'integration_lock_url':lock_url,'integration_lock_sha256':sha(lock_body),'uroad_ref':node['ref'],'recipe_sha256':sha(recipe),'source_repository':REPOSITORY,'source_commit':commit,'recipe_patch_sha256':sha(diff),'build_requirement_pins':pins}
        if not follow_official:
            suffix='on' if eval_dump else 'off'
            (root/('discovery-v2-'+suffix+'.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2));(root/('recipe-v2-'+suffix+'.patch')).write_bytes(diff)
    except InputError:raise
    except Exception as e:
        raise InputError('M100_INPUT_DISCOVERY_FAILED','未能从本APP集成锁和精确Conan配方完成源码关联（'+type(e).__name__+'）。','请管理员检查JFrog只读认证或该构建的制品完整性；不要求用户逐版本手工补源码。') from None
    token=source_token(cfg)
    if not token:raise InputError('M100_SOURCE_AUTH_REQUIRED','已定位本APP源码提交和依赖锁，尚未配置GitLab只读认证。','请管理员一次性配置源码仓库只读Token；无需重新提供APP、源码提交或构建锁。')
    try:
        git_headers={'PRIVATE-TOKEN':token};api='https://'+HOST+'/api/v4/projects/'+quote(PROJECT,safe='')+'/repository/'
        commit_info=json.loads(fetch(api+'commits/'+commit,git_headers,1024**2,20))
        if commit_info.get('id')!=commit:raise ValueError('source commit mismatch')
        archive_path=root/'source.tar.gz'
        if not archive_path.exists():archive_path.write_bytes(fetch(api+'archive.tar.gz?sha='+commit,git_headers,256*1024**2,90))
        archive=archive_path.read_bytes();source=root/source_name
        if source.exists():
            raise InputError('M100_INPUTS_INCOMPLETE','上次源码准备未完整结束，已保留文件。','请管理员核对本任务build-inputs；不覆盖未确认的源码目录。')
        if buildsh:
            inventory=extract_source(archive,source)
        else:
            source.mkdir();inventory=extract_source(archive,source/'uroad')
        if follow_official:
            try:eval_dump,mode_origin=official_eval_mode(recipe,(source/('CMakeLists.txt' if buildsh else 'uroad/CMakeLists.txt')).read_bytes())
            except (OSError,ValueError,SyntaxError):
                raise InputError('M100_OFFICIAL_MODE_UNCONFIRMED','无法从本APP锁定配方与源码确认官方ON/OFF构建参数，未编译。','请提供配套M100成品或明确的官方构建参数；不会按任务目标猜测或切换编译宏。') from None
            # Materialize the SAME upstream value for the existing build verifier.
            # This does not flip CMake defaults or change an explicit recipe value.
            if buildsh:
                # The original script builds the repository's local recipe,
                # not the exported packaging recipe (which has a nested tree).
                local_original=(source/'conanfile.py').read_bytes()
                expected_pins={**ci['build_requirement_pins'],**ci.get('sensor_development_pins',{})}
                local_aligned,local_pins=align_build_requirements(local_original,document,names=tuple(expected_pins))
                if local_pins!=expected_pins:raise ValueError('CI/local recipe dependency mismatch')
                patched,_=enable_eval(local_aligned,eval_dump)
                before=local_original
                script_sha=sha((source/'build.sh').read_bytes())
                reviewed=cfg.get('m100_reviewed_buildsh_sha256',[])
                if script_sha not in reviewed:
                    raise InputError('M100_BUILD_SCRIPT_REVIEW_REQUIRED','本APP的原 build.sh 与已审查版本不同，尚未编译。','请管理员核对脚本差异；不会用未知脚本读取构建认证。')
                metadata.update(input_policy=5,build_entry='upstream-buildsh-v1',ci=ci,build_script_sha256=script_sha,conan_repository=repository,source_requirement_pins=local_pins)
            else:
                patched,_=enable_eval(aligned,eval_dump)
                before=recipe
            diff=''.join(difflib.unified_diff(before.decode().splitlines(True),patched.decode().splitlines(True),fromfile='original/conanfile.py',tofile='official/conanfile.py')).encode()
            metadata.update(input_policy=3,eval_dump=eval_dump,eval_mode_policy='official-v1',eval_mode_origin=mode_origin,recipe_patch_sha256=sha(diff))
            if buildsh:metadata['input_policy']=5
            suffix='buildsh-v5' if buildsh else 'official-v3'
            (root/('discovery-'+suffix+'.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2));(root/('recipe-'+suffix+'.patch')).write_bytes(diff)
        (source/'conanfile.py').write_bytes(patched);(source/'build').mkdir()
        lock_bytes=(json.dumps(derived,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode();(source/'build/conan.lock').write_bytes(lock_bytes)
        files={str(p.relative_to(source)):sha(p.read_bytes()) for p in source.rglob('*') if p.is_file() and 'build' not in p.relative_to(source).parts[:1]}
        snapshot={**metadata,'source_archive_sha256':sha(archive),'source_lock_sha256':sha(lock_bytes),'files':files,'inventory':inventory}
        snapshot_bytes=(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode();(source/'source-snapshot.json').write_bytes(snapshot_bytes)
        ready={**metadata,'source':str(source),'source_snapshot':str(source/'source-snapshot.json'),'source_snapshot_sha256':sha(snapshot_bytes),'source_lock_sha256':sha(lock_bytes),'state':'READY','elapsed_seconds':round(time.monotonic()-started,3)}
        marker.write_text(json.dumps(ready,ensure_ascii=False,indent=2));return ready
    except InputError:raise
    except Exception as e:
        raise InputError('M100_SOURCE_FETCH_FAILED','已定位源码提交，但GitLab读取或源码快照准备未完成（'+type(e).__name__+'）。','请管理员检查一次性的GitLab只读授权与网络；准确源码提交和依赖锁已保留，不需要用户重新提供。') from None

def automatic_profile(cfg, inputs, fetch=None):
    """A generic reviewed toolchain; source and lock always come from this APP."""
    environment = cfg.get('m100_build_environment') or {}
    required = ('image','cache','settings_file','settings_sha256','compiler_version')
    if any(not environment.get(k) for k in required):
        raise InputError('M100_TOOLCHAIN_UNCONFIGURED','本APP源码和依赖锁已自动取得，尚未配置通用编译环境。','请管理员一次性接入对应schu工具链；无需用户补源码。')
    lock = json.loads((Path(inputs['source'])/'build/conan.lock').read_text())
    host = lock.get('profile_host','')
    settings = dict(re.findall(r'^([A-Za-z_.]+)=([^\r\n]+)$',host,re.M))
    if settings.get('arch') != 'armv8' or settings.get('os') != 'Linux' or settings.get('compiler.version') != environment['compiler_version']:
        raise InputError('M100_TOOLCHAIN_MISMATCH','本APP锁定的编译器与已配置环境不同。','请管理员接入锁中要求的工具链；不会套用旧环境。')
    build = {k:environment[k] for k in ('image','cache','settings_file','settings_sha256')}
    build.update(eval_dump=inputs.get('eval_dump',True),source=inputs['source'],commit=inputs['source_commit'],source_diff_sha256=inputs['recipe_patch_sha256'],
                 source_lock_sha256=inputs['source_lock_sha256'],source_snapshot=inputs['source_snapshot'],
                 source_snapshot_sha256=inputs['source_snapshot_sha256'],package_sha256=inputs['package_sha256'])
    if inputs.get('eval_mode_policy')=='official-v1':
        build.update(eval_mode_policy='official-v1',eval_mode_origin=inputs['eval_mode_origin'])
    if inputs.get('build_entry')=='upstream-buildsh-v1':
        build.update(build_entry=inputs['build_entry'],baselock=inputs['ci']['baselock'],
                     ci=inputs['ci'],build_script_sha256=inputs['build_script_sha256'])
        # The original script configures remotes and resolves its own baselock.
        # Reuse existing binaries read-only, with mutable metadata in a private
        # per-attempt home; do not pre-resolve a different offline dependency graph.
        build['shared_cache']=environment['cache']
    else:
        from .m100_dependency_cache import prepare as prepare_cache
        cached=prepare_cache(cfg,inputs,fetch=fetch)
        build['cache']=cached['cache'];build['shared_cache']=cached['shared_cache']
    return {'id':'auto-m100-'+inputs['package_sha256'][:20],'profile':'schu','build_mode':'upstream-buildsh-v1' if build.get('build_entry') else 'offline-eval-v1',
            'package_sha256':[inputs['package_sha256']],'build':build}
