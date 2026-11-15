"""Short, owner-scoped input descriptions; no network or material resolution."""
import json
from pathlib import PurePosixPath
import re
from urllib.parse import unquote,urlsplit

from .incremental import SO_TARGETS,frozen_inputs
from .tasks import Tasks

NAMES={'camera_so':'相机','lidar_so':'雷达','m100_so':'M100'}

def public_url(value):
    try:
        parsed=urlsplit(str(value or ''))
        if (parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment and not any(c in str(value) for c in '\r\n<>')):
            return str(value)
    except ValueError:pass
    return ''


def label(value):
    text=str(value or '').strip()
    if not text:return '待提供 APP'
    if text.startswith(('https://','http://')):text=unquote(PurePosixPath(urlsplit(text).path).name)
    elif '/' in text or '\\' in text:text=PurePosixPath(text.replace('\\','/')).name
    text=re.sub(r'\.app\.tar\.gz$|\.tar\.gz$','',text,flags=re.I)
    if re.fullmatch(r'(?:a-|asm-|task-|gw-)[a-f0-9-]+(?:-package)?',text):return '本人已选软件'
    return text[-64:]


def dataset_details(value,asset,user):
    """Show the name and actual approved source without querying remote data."""
    value=str(value or '');asset=asset if isinstance(asset,dict) and asset.get('owner') in (user,'shared') else {}
    if asset:
        name=asset.get('name') or label(value)
        if asset.get('owner')=='shared':name='共享:'+name.removeprefix('共享:')
        source=str((asset.get('data') or {}).get('remote_path') or '')
        if not source.startswith('/') or any(c in source for c in '\r\n\x00'):source='工作站已接入数据源'
    elif value.startswith('/') and not any(c in value for c in '\r\n\x00'):
        name=label(value);source=value
    else:
        name='共享:18场景' if value in ('共享18场景','共享:18场景') else label(value) if value else '待提供数据集'
        source='工作站共享数据源' if name.startswith('共享:') else ''
    cloud=(asset.get('data') or {}).get('cloud_source') or {}
    return {'dataset':name,'dataset_source':source,'dataset_input':cloud.get('input_root') or source,
            'dataset_scene_count':cloud.get('source_scene_count') or (asset.get('data') or {}).get('scene_count')}


def describe(store,user,*,task=None,assets=None,provided=None):
    store.require_user(user);task=task or Tasks(store).current(user)
    if not task:return {}
    draft=task['data'];base={};base_assets={};kind='initial'
    with store.connect() as c:
        lineage=c.execute('SELECT * FROM task_lineage WHERE task_id=? AND owner=?',(task['id'],user)).fetchone()
        if lineage:
            kind=lineage['change_kind']
            prior=c.execute('SELECT data FROM task_requests WHERE id=? AND owner=?',(lineage['base_task_id'],user)).fetchone()
            job=c.execute('SELECT payload FROM jobs WHERE id=? AND owner=?',(lineage['base_job_id'],user)).fetchone()
            if job:
                payload=json.loads(job['payload']);base_assets=payload.get('assets') or {}
                base=frozen_inputs(c,user,{'job_id':lineage['base_job_id']},json.loads(prior['data']) if prior else {})
    assets=assets or {};current_package=assets.get('package') or {}
    same_package=draft.get('package') and draft.get('package')==base.get('package')
    if not current_package and same_package:current_package=base_assets.get('package') or {}
    if not isinstance(current_package,dict):current_package={}
    software=current_package.get('data',{}).get('filename') or draft.get('software_name') or current_package.get('name') or draft.get('package')
    if current_package.get('data',{}).get('filename') and label(software)=='本人已选软件':software=current_package['data']['filename']
    source_url=public_url(draft.get('package'));ancestor=task['id'];visited=set()
    with store.connect() as c:
        while not source_url and ancestor and ancestor not in visited and len(visited)<20:
            visited.add(ancestor)
            row=c.execute('SELECT t.data,l.base_task_id,l.change_kind FROM task_requests t LEFT JOIN task_lineage l ON l.task_id=t.id AND l.owner=t.owner WHERE t.id=? AND t.owner=?',(ancestor,user)).fetchone()
            if not row:break
            ancestor_data=json.loads(row['data'])
            source_url=public_url(ancestor_data.get('package'))
            if row['change_kind']=='new_package':break
            ancestor=row['base_task_id']
    if source_url and label(software)=='本人已选软件':software=source_url
    changes=[];fields=provided if provided is not None else draft
    if kind=='new_package' or fields.get('package') and (not base or not same_package):changes.append('软件版本')
    for role,name in NAMES.items():
        if fields.get(role) and fields.get(role)!=base.get(role):changes.append(name+' SO')
    if fields.get('m100_mode')=='build_if_missing' and not draft.get('m100_so') and 'M100 SO' not in changes:changes.append('M100 准备')
    yaml=fields.get('yaml_request','')
    if yaml and yaml!=base.get('yaml_request'):
        if yaml=='保留原配置':changes.append('恢复原配置')
        else:
            changed=yaml[len(base['yaml_request']):] if base.get('yaml_request') and yaml.startswith(base['yaml_request']) else yaml
            targets=[]
            for clause in changed.split('；'):
                targets.extend(re.findall(r'替换 APP 内 ([A-Za-z0-9_./-]+\.ya?ml)',clause) or re.findall(r'(?<![A-Za-z0-9_])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.ya?ml\b',clause))
            names=list(dict.fromkeys(PurePosixPath(x).name for x in targets))[:3]
            from .yaml_plan import quick_plan
            planned=quick_plan(changed.strip('；'))
            fields_summary=[PurePosixPath(op['target']).name+'：'+'.'.join(op['path'])+'='+op['value_json']
                            for op in (planned or {}).get('operations',[]) if op['operation']=='set']
            changes.append('配置 '+('；'.join(fields_summary[:2]) if fields_summary else '、'.join(names) if names else 'YAML'))
    for field,name in (('dataset','数据集'),('scene_scope','场景范围'),('goal','执行目标'),('lpai_target','交付方式')):
        if fields.get(field) and base and fields[field]!=base.get(field):changes.append(name)
    if kind=='repeat':changes=['按当前版本重新回放']
    inherited=[];supplied=[]
    for role,name in NAMES.items():
        current=next((a for a in (assets.get('dependencies') or []) if isinstance(a,dict) and a.get('owner')==user and (a.get('data') or {}).get('target')==SO_TARGETS[role]),None)
        old=next((a for a in (base_assets.get('dependencies') or []) if isinstance(a,dict) and a.get('owner')==user and (a.get('data') or {}).get('target')==SO_TARGETS[role]),None)
        same=bool(draft.get(role) and draft[role]==base.get(role))
        if current and old:same=(current['data'].get('sha256')==old['data'].get('sha256'))
        if same:inherited.append(name)
        elif draft.get(role):supplied.append(name)
    dependencies=[]
    if inherited:dependencies.append('沿用本人'+ '、'.join(inherited))
    if supplied:dependencies.append('本次提供'+ '、'.join(supplied))
    if not draft.get('m100_so') and draft.get('m100_mode')=='build_if_missing':
        dependencies.append('M100工作站编译' if assets.get('dependencies') else 'M100自动准备')
    dataset=assets.get('dataset') or (base_assets.get('dataset') if draft.get('dataset')==base.get('dataset') else {})
    build=draft.get('build_reference','')
    if build.startswith(('https://','http://')):build=public_url(build)
    return {'software':label(software),'software_url':source_url,'build_reference':build,'changes':'、'.join(dict.fromkeys(changes)) or '沿用当前材料',
            'dependencies':'；'.join(dependencies) or '待补齐',
            **dataset_details(draft.get('dataset'),dataset,user),
            'scene_scope':draft.get('scene_scope') or '待明确'}


def text(value):
    if not value:return ''
    return ('软件：'+value['software']+('\n软件包链接：'+value['software_url'] if value.get('software_url') else '')+
            '\n本次：'+value['changes']+'\n依赖：'+value['dependencies']+'\n数据：'+value['dataset']+' · '+value['scene_scope']+
            ('\n数据来源：'+value['dataset_source'] if value.get('dataset_source') else ''))
