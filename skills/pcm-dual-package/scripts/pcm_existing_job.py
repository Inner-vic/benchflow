"""Prepare and track two runs of the unchanged original PCM Jenkins job.

This helper never edits Jenkins, submits a job, uploads components, or sends IM.
The original Skill/browser workflow performs reviewed submissions.
"""
import argparse
import hashlib
import json
import pathlib
import re
import tarfile
from urllib.parse import urlsplit

JOB = 'TRANSIT/XCU/SCM/CCU-ACORE-RELEASE-BUILD'
JOB_URL = 'https://ci.ep.chehejia.com/job/TRANSIT/job/XCU/job/SCM/job/CCU-ACORE-RELEASE-BUILD/'
ARTIFACT_ROOT = 'https://artifactory.ep.chehejia.com/artifactory/xcu-acore-test-local/Acore/Civu/'
UROAD = 'https://gitlab.chehejia.com/livc-acore/chassis/uroad.git'
LABELS = {'OFF': '普通版 OFF', 'ON': '评测版 ON'}
SHA = re.compile(r'[0-9a-f]{64}\Z')
REF = re.compile(r'([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+)(?:@([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+))?\Z')


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def load(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field: ' + key)
            result[key] = value
        return result
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=unique)


def sha_file(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''): h.update(chunk)
    return h.hexdigest()


def ref(value):
    match = REF.fullmatch(value)
    if not match:
        raise ValueError('Use an exact component reference without a range or revision suffix')
    return match[1]


def references(values):
    result = {}
    for value in values:
        name = ref(value)
        if name in result:
            raise ValueError('Duplicate component name: ' + name)
        result[name] = value
    return result


def plan_hash(state):
    payload={k:state[k] for k in ['schema','pair_id','job','source','baseline_lock_sha256','common_recipe_evidence']}
    payload['builds']={mode:{k:item[k] for k in ['parameters','component']} for mode,item in state['builds'].items()}
    if 'input_confirmation' in state:
        payload['input_confirmation'] = state['input_confirmation']
    return hashlib.sha256(canonical(payload)).hexdigest()


def make_plan(context):
    if context.get('delivery', 'app_pair') != 'app_pair':
        raise ValueError('The legacy pair tracker is only for explicit app_pair delivery; use pcm_workflow.py')
    pair_id = context['pair_id']
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{7,47}', pair_id):
        raise ValueError('Invalid pair ID')
    source = context['source']
    if source['repository'] != UROAD or not re.fullmatch(r'[0-9a-f]{40}', source['commit']):
        raise ValueError('A pinned uroad source is required')
    common = dict(context['common_parameters'])
    required = ['CONTROLLER','BASE_LINE','SOFTWARE_BRANCH','BUILDVARIANT','MANIFEST_NAME','BUILD_REPLAY','UPLOAD_ALL_PACKAGE','SILENT_BUILD']
    if any(name not in common for name in required) or any(name in common for name in ['APP_VERSION','LOCKED_LIBS']) or any(name.startswith('PCM_') for name in common):
        raise ValueError('Use existing Job parameters and separate component lists')
    if common['CONTROLLER'] != 'CCU_M100_AVMLIVC' or common['BUILDVARIANT'] not in ['release','debug']:
        raise ValueError('Unsupported PCM target')
    if (not re.fullmatch(r'[0-9A-Fa-f]{4}', common['BASE_LINE'])
            or not re.fullmatch(r'\d{4}', common['SOFTWARE_BRANCH'])):
        raise ValueError('Invalid software baseline or ID')
    if common['BUILD_REPLAY'] is not True or common['UPLOAD_ALL_PACKAGE'] is not False or type(common['SILENT_BUILD']) is not bool:
        raise ValueError('Use baseline replay and disable republication of existing Conan packages')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',common['MANIFEST_NAME']) or not SHA.fullmatch(context['baseline_lock_sha256']):
        raise ValueError('A verified successful baseline lock is required')
    apps = references(context['common_app_refs'])
    libraries = references(context['common_locked_refs'])
    if set(apps) & set(libraries) or 'uroad_m100' in apps or 'uroad_m100' in libraries:
        raise ValueError('Common APP and library inputs must not overlap or replace M100')
    components = context['components']
    if set(components) != {'OFF','ON'}:
        raise ValueError('Both component variants are required')
    if components['OFF']['ref'] == components['ON']['ref']:
        raise ValueError('Two builds must select different component references')
    builds = {}
    evidence=context.get('common_recipe_evidence',{})
    pending=[]
    for value in list(apps.values())+list(libraries.values()):
        if REF.fullmatch(value)[4]!='pcm-'+pair_id: continue
        item=evidence.get(value,{})
        if item.get('availability')!='remote_recipe' or not re.fullmatch(r'[0-9a-f]{32,64}',item.get('recipe_revision','')) or not SHA.fullmatch(item.get('recipe_sha256','')):
            pending.append(value)
    ready = not pending
    for mode in ['OFF','ON']:
        component = components[mode]
        if ref(component['ref']) != 'uroad_m100' or component['source_commit'] != source['commit'] or component['cmake'] != {'WITH_EVAL_DUMP':mode}:
            raise ValueError('Component does not bind the selected source and CMake mode')
        available = component.get('availability') == 'remote_recipe'
        if REF.fullmatch(component['ref'])[4] != 'pcm-' + pair_id and not (available and component.get('reused_existing') is True):
            raise ValueError('Use the task-specific component channel')
        if available and (not re.fullmatch(r'[0-9a-f]{32,64}', component.get('recipe_revision','')) or not SHA.fullmatch(component.get('recipe_sha256',''))):
            raise ValueError('Published recipe identity was not verified')
        ready = ready and available
        params = dict(common)
        params['APP_VERSION'] = '\n'.join(list(apps.values()) + [component['ref']]) + '\n'
        params['LOCKED_LIBS'] = '\n'.join(libraries.values()) + ('\n' if libraries else '')
        builds[mode] = {'parameters': params, 'component': component, 'status':'PLANNED'}
    plan = {'schema':'pcm-existing-job-pair/v1','pair_id':pair_id,'job':JOB,
            'source':source,'baseline_lock_sha256':context['baseline_lock_sha256'],
            'common_recipe_evidence':evidence,'pending_common_refs':pending,
            'builds':builds,'status':'READY_FOR_REVIEW' if ready else 'COMPONENT_RECIPES_REQUIRED',
            'shared_pipeline_changed':False}
    plan['plan_sha256'] = plan_hash(plan)
    return plan


def submit_intent(state, mode, reviewed_sha):
    if state.get('plan_sha256') != reviewed_sha or plan_hash(state)!=reviewed_sha or state.get('status') not in ['READY_FOR_REVIEW','IN_PROGRESS']:
        raise ValueError('Resolve component recipes and review this exact plan before submitting')
    item = state['builds'][mode]
    if item['status'] != 'PLANNED':
        raise ValueError('Submission already started; inspect queue/build evidence before any retry')
    item['status'] = 'SUBMISSION_UNCERTAIN'
    state['status'] = 'IN_PROGRESS'


def record_build(state, mode, build_url):
    if not re.fullmatch(re.escape(JOB_URL) + r'\d+/', build_url):
        raise ValueError('Record a real build of the original Job')
    item = state['builds'][mode]
    other = state['builds']['ON' if mode=='OFF' else 'OFF']
    if other.get('build_url') == build_url:
        raise ValueError('OFF and ON cannot refer to the same Jenkins build')
    if item.get('build_url') and item['build_url'] != build_url:
        raise ValueError('Build identity is immutable; no silent replacement build')
    if item['status'] not in ['SUBMISSION_UNCERTAIN','RUNNING']:
        raise ValueError('Record submission intent before binding a build')
    item.update(status='RUNNING', build_url=build_url)


def artifact_url(url):
    parsed = urlsplit(url)
    if not url.startswith(ARTIFACT_ROOT) or parsed.username or parsed.query or parsed.fragment or '..' in parsed.path.split('/'):
        raise ValueError('Unexpected PCM artifact URL')


def checked_artifacts(build_url, result):
    if not build_url or result['build_url'] != build_url:
        raise ValueError('Artifact result belongs to another build')
    for kind in ['app','lock']:
        artifact_url(result[kind+'_url'])
        if not SHA.fullmatch(result[kind+'_sha256']):
            raise ValueError('Verify each downloaded artifact checksum')
    app_name=result['app_url'].rsplit('/',1)[-1]
    lock_name=result['lock_url'].rsplit('/',1)[-1]
    number=build_url.rstrip('/').rsplit('/',1)[-1]
    if not app_name.endswith('-'+number+'.app.tar.gz') or lock_name!=app_name[:-len('.app.tar.gz')]+'.lock':
        raise ValueError('APP and lock must belong to this real build number')
    return {key: result[key] for key in ['app_url','app_sha256','lock_url','lock_sha256']}


def record_result(state, mode, result):
    item = state['builds'][mode]
    if item['status'] != 'RUNNING' or result['build_url'] != item['build_url']:
        raise ValueError('Result must belong to the recorded build')
    if result['result'] != 'SUCCESS':
        item.update(status='FAILED',result=result)
        state['status']='PARTIAL_OR_FAILED'
        return
    checked_artifacts(item['build_url'], result)
    item.update(status='UPLOADED', result=result)
    state['status']='PARTIAL_OR_FAILED' if any(v['status']=='FAILED' for v in state['builds'].values()) else ('AWAITING_COMPARISON' if all(v['status']=='UPLOADED' for v in state['builds'].values()) else 'IN_PROGRESS')


def compare_locks(off, on, expected, common_evidence=None):
    component_options={}
    def signature(lock, mode):
        if 'graph_lock' not in lock or 'schema' in lock:
            raise ValueError('Original Jenkins must produce a standard Conan lock')
        nodes = lock['graph_lock']['nodes']
        roots=[n for n in nodes.values() if n.get('ref','').startswith('uroad_m100/') and n.get('context','host')=='host']
        if len(roots)!=1 or roots[0]['ref'].split('#')[0]!=expected[mode]['ref']:
            raise ValueError('Actual lock selected the wrong M100 variant')
        if roots[0]['ref'].split('#')[-1]!=expected[mode]['recipe_revision']:
            raise ValueError('M100 recipe changed after review')
        options=dict(line.split('=',1) for line in roots[0].get('options','').splitlines() if line)
        dump=options.pop('with_eval_dump',None)
        if dump is not None and dump.lower()!=('false' if mode=='OFF' else 'true'):
            raise ValueError('Effective M100 Conan option contradicts the variant')
        component_options[mode]=options
        def identity(key):
            node=nodes[key]
            return '|'.join(str(node.get(k,'')) for k in ['context','ref','package_id','prev'])
        values=[]
        for node in nodes.values():
            name=node.get('ref','').split('/')[0]
            if name in ['Livc','uroad_m100']: continue
            if not re.search(r'#[0-9a-f]{32,64}$',node.get('ref','')) or not node.get('package_id') or not node.get('prev'):
                raise ValueError('Dependency binary identity is incomplete')
            component_ref=node['ref'].split('#')[0]
            if common_evidence and component_ref in common_evidence and node['ref'].split('#')[-1]!=common_evidence[component_ref]['recipe_revision']:
                raise ValueError('Common private recipe changed after review')
            value={k:node.get(k) for k in ['ref','package_id','prev','options','context']}
            for edge in ['requires','build_requires']:
                value[edge]=sorted(identity(child) for child in node.get(edge,[]))
            values.append(value)
        return sorted(values,key=canonical)
    left,right=signature(off,'OFF'),signature(on,'ON')
    if any(off.get(k)!=on.get(k) for k in ['profile_host','profile_build']) or left!=right or component_options['OFF']!=component_options['ON']:
        raise ValueError('OFF and ON resolved different non-M100 dependencies')
    return {'status':'PASS','non_m100_binary_identities_equal':True}


def inventory(path):
    result={}
    with tarfile.open(path,'r:gz') as source:
        for member in source:
            p=pathlib.PurePosixPath(member.name)
            if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]!='livc' or member.name in result:
                raise ValueError('Unsafe or duplicate APP archive path')
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError('Unsupported APP archive member')
            value={'type':member.type.decode(),'mode':member.mode,'uid':member.uid,'gid':member.gid,'link':member.linkname}
            if member.isfile():
                h=hashlib.sha256()
                with source.extractfile(member) as stream:
                    for chunk in iter(lambda: stream.read(4*1024*1024), b''): h.update(chunk)
                value.update(sha256=h.hexdigest(),size=member.size)
            result[member.name]=value
    return result


def compare_apps(off, on, off_lock_name, on_lock_name, lock_hashes=None):
    left,right=inventory(off),inventory(on)
    removed=[]
    for values,name in [(left,off_lock_name),(right,on_lock_name)]:
        path='livc/'+name
        if path not in values: raise ValueError('Expected embedded build lock is missing')
        removed.append(values.pop(path))
    if lock_hashes and [item.get('sha256') for item in removed]!=lock_hashes:
        raise ValueError('Embedded locks differ from the downloaded original locks')
    m100='livc/lib/liburoad_m100.so'
    if m100 not in left or m100 not in right or left[m100].get('sha256')==right[m100].get('sha256'):
        raise ValueError('OFF and ON must contain different M100 binaries')
    changes=sorted(k for k in set(left)|set(right) if left.get(k)!=right.get(k))
    if set(changes)-{m100,'livc/etc/VERSION'} or set(left)!=set(right):
        raise ValueError('Unexpected APP differences: '+', '.join(changes))
    for name in changes:
        if {k:v for k,v in left[name].items() if k not in ['sha256','size']}!={k:v for k,v in right[name].items() if k not in ['sha256','size']}:
            raise ValueError('Runtime file metadata differs')
    return {'status':'PASS','differences':changes,'build_metadata':['embedded Conan lock','livc/etc/VERSION'],'runtime_verified':False}


def delivery_manifest(state):
    """Explicit mode/build/artifact mapping. Never relabel or repack remote APPs."""
    if state.get('plan_sha256') != plan_hash(state):
        raise ValueError('Plan changed after review; cannot export an identity mapping')
    entries, build_urls = {}, set()
    for mode in ['OFF', 'ON']:
        item = state['builds'][mode]
        component = item['component']
        if component.get('cmake') != {'WITH_EVAL_DUMP': mode}:
            raise ValueError('Component mode contradicts the delivery label')
        entry = {'mode': mode, 'label': LABELS[mode], 'with_eval_dump': mode,
                 'component_ref': component['ref'], 'source_commit': component['source_commit'],
                 'status': item['status'], 'download_subdirectory': mode,
                 'build_url': item.get('build_url'), 'artifacts': None}
        if entry['build_url']:
            if (not re.fullmatch(re.escape(JOB_URL) + r'\d+/', entry['build_url'])
                    or entry['build_url'] in build_urls):
                raise ValueError('Two modes must bind distinct builds of the original Job')
            build_urls.add(entry['build_url'])
            entry['build_number'] = int(entry['build_url'].rstrip('/').rsplit('/', 1)[1])
        result = item.get('result', {})
        if item['status'] == 'UPLOADED':
            if result.get('result') != 'SUCCESS':
                raise ValueError('Uploaded status requires a successful recorded result')
            entry['artifacts'] = checked_artifacts(entry['build_url'], result)
        entries[mode] = entry
    return {'schema': 'pcm-dual-delivery/v1', 'pair_id': state['pair_id'],
            'source': state['source'], 'status': state['status'],
            'plan_sha256': state['plan_sha256'], 'packages': entries,
            'comparison_error': state.get('comparison_error'), 'runtime_verified': False}


def summary(state):
    manifest = delivery_manifest(state)
    lines=['PCM 普通版 / 评测版：'+state['status'], '本次双包 ID：'+state['pair_id'],
           '源码提交：'+state['source']['commit']]
    for mode, entry in manifest['packages'].items():
        lines += ['', entry['label']+' WITH_EVAL_DUMP='+mode+'：'+entry['status'],
                  'M100 组件：'+entry['component_ref']]
        if entry.get('build_number'):
            lines.append('原 Jenkins 卡片构建编号：#'+str(entry['build_number']))
        result = entry.get('artifacts') or {}
        for label,key in [('LIVC APP','app_url'),('集成Lockfile','lock_url')]:
            if result.get(key): lines.append(label+'：['+result[key].rsplit('/',1)[-1]+']('+result[key]+')')
        if result:
            lines += ['APP SHA-256：'+result['app_sha256'], 'Lockfile SHA-256：'+result['lock_sha256']]
        if entry['build_url']: lines.append('构建链接：'+entry['build_url'])
    if state.get('comparison_error'):
        lines += ['', '双包比较未通过：'+state['comparison_error']]
    lines += ['', '原 Jenkins 文件名保持不变；请按模式、构建编号和此清单区分，不能只看包名。']
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','submit-intent','record-build','record-result','compare','summary','export-delivery'])
    parser.add_argument('--context',type=pathlib.Path)
    parser.add_argument('--state',type=pathlib.Path,required=True)
    parser.add_argument('--mode',choices=['OFF','ON'])
    parser.add_argument('--reviewed-sha')
    parser.add_argument('--build-url')
    parser.add_argument('--result',type=pathlib.Path)
    parser.add_argument('--off-app',type=pathlib.Path)
    parser.add_argument('--on-app',type=pathlib.Path)
    parser.add_argument('--off-lock',type=pathlib.Path)
    parser.add_argument('--on-lock',type=pathlib.Path)
    parser.add_argument('--output-dir',type=pathlib.Path)
    args=parser.parse_args()
    if args.action=='plan':
        if args.state.exists(): raise ValueError('Pair state already exists')
        if any(args.state.with_name(mode+'.parameters.json').exists() for mode in ['OFF','ON']):
            raise ValueError('Use a fresh pair directory; parameter files already exist')
        state=make_plan(load(args.context))
        args.state.parent.mkdir(parents=True,exist_ok=True)
        for mode,item in state['builds'].items():
            args.state.with_name(mode+'.parameters.json').write_bytes(canonical(item['parameters']))
    else:
        state=load(args.state)
        if args.action=='summary': print(summary(state),end=''); return
        if args.action=='export-delivery':
            if args.output_dir is None: raise ValueError('Use a new output directory')
            manifest = delivery_manifest(state)
            args.output_dir.mkdir(parents=True,exist_ok=False)
            (args.output_dir/'package-index.json').write_bytes(canonical(manifest))
            (args.output_dir/'package-index.md').write_text(summary(state),encoding='utf-8')
            print(json.dumps({'status':state['status'],'output':str(args.output_dir)},ensure_ascii=False)); return
        if args.action=='submit-intent': submit_intent(state,args.mode,args.reviewed_sha)
        if args.action=='record-build': record_build(state,args.mode,args.build_url)
        if args.action=='record-result': record_result(state,args.mode,load(args.result))
        if args.action=='compare':
            if state['status']!='AWAITING_COMPARISON': raise ValueError('Both real uploads are required before pair acceptance')
            for mode,app,lock in [('OFF',args.off_app,args.off_lock),('ON',args.on_app,args.on_lock)]:
                for kind,file in [('app',app),('lock',lock)]:
                    if sha_file(file)!=state['builds'][mode]['result'][kind+'_sha256']:
                        raise ValueError('Downloaded artifact does not match recorded checksum')
            try:
                state['lock_comparison']=compare_locks(load(args.off_lock),load(args.on_lock),{k:v['component'] for k,v in state['builds'].items()},state['common_recipe_evidence'])
                state['app_comparison']=compare_apps(args.off_app,args.on_app,args.off_lock.name,args.on_lock.name,[sha_file(args.off_lock),sha_file(args.on_lock)])
                state['status']='COMPLETE'
            except ValueError as error:
                state.update(status='COMPARISON_FAILED',comparison_error=str(error))
                args.state.write_bytes(canonical(state))
                raise
    args.state.write_bytes(canonical(state))
    print(json.dumps({'status':state['status'],'state':str(args.state)},ensure_ascii=False))


if __name__=='__main__':
    main()
