"""One factual material summary for intake, execution and delivery cards."""
import json,re
from pathlib import PurePosixPath
from urllib.parse import urlsplit,unquote

LIBRARIES=(('camera_so','lib/libxcamera.so'),('lidar_so','lib/libxlidar.so'),('m100_so','lib/liburoad_m100.so'))

def describe(store,user,*,task=None,job=None,summary=None,files=()):
    summary=summary or {};payload=job.get('payload',{}) if job else {};assets=payload.get('assets') or {};draft={}
    with store.connect() as c:
        if not task and job and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_requests'").fetchone():
            row=c.execute('SELECT * FROM task_requests WHERE owner=? AND job_id=?',(user,job['id'])).fetchone()
            if row:task=dict(row)
        if task:draft=json.loads(task['data']) if isinstance(task['data'],str) else task['data']
        # Only errors for this owner's exact current material snapshot apply.
        issues={}
        if task and task.get('prep_id') and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparations'").fetchone():
            prep=c.execute('SELECT result,fingerprint FROM preparations WHERE owner=? AND id=?',(user,task['prep_id'])).fetchone()
            from .preparation import fingerprint
            if prep and prep['fingerprint']==fingerprint(draft):
                for problem in json.loads(prep['result'] or '{}').get('issues',[]):
                    field=problem.get('field')
                    if field in ('camera_so','lidar_so','m100_so','dependencies'):
                        issues.setdefault(field,problem.get('message') or '检查未通过')
        rows=[]
        for field,target in LIBRARIES:
            source=draft.get(field,'')
            asset=next((a for a in assets.get('dependencies',[]) if a.get('owner')==user and a.get('data',{}).get('target')==target),None)
            if not asset and source:
                found=c.execute("SELECT * FROM assets WHERE id=? AND owner=? AND kind='dependency'",(source,user)).fetchone()
                if found:asset=dict(found);asset['data']=json.loads(asset['data'])
            data=asset.get('data',{}) if asset else {};filename=data.get('filename')
            if not filename and source:
                candidate=unquote(PurePosixPath(urlsplit(source).path if source.startswith('https://') else source.replace('\\','/')).name)
                if candidate.endswith('.so'):filename=candidate
            filename=filename or PurePosixPath(target).name
            compiled=field=='m100_so' and not source and draft.get('m100_mode')=='build_if_missing'
            status='系统编译' if compiled else '已提供' if source or asset else '未提供'
            pending=[f for f in files if json.loads(f.get('data') or '{}').get('target')==target and f['state'] not in ('ASSOCIATED','DUPLICATE','SUPERSEDED')]
            if pending:status='保存失败' if any(f['state']=='FAILED' for f in pending) else '接收中'
            provided=bool(source or asset)
            error=(issues.get(field) or issues.get('dependencies') or '') if provided or compiled else ''
            indicator='provided' if provided else 'missing'
            if pending:
                indicator='error' if status=='保存失败' else 'missing'
                error='附件保存失败，请重发。' if indicator=='error' else ''
            elif error:indicator='error'
            rows.append({'name':filename,'status':status,'indicator':indicator,'error':error})
        yaml_request=draft.get('yaml_request','').strip()
        configs=re.findall(r'(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.ya?ml)\b',yaml_request)
        if yaml_request and task and task.get('prep_id'):
            p=c.execute('SELECT result,fingerprint FROM preparations WHERE owner=? AND id=?',(user,task['prep_id'])).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparations'").fetchone() else None
            from .preparation import fingerprint
            if p and p['fingerprint']==fingerprint(draft):
                outputs=(json.loads(p['result'] or '{}').get('yaml_preparation') or {}).get('outputs') or []
                configs.extend(PurePosixPath(x['target']).name for x in outputs if x.get('target'))
        configs=list(dict.fromkeys(configs))
        dataset=assets.get('dataset') or {}
        if dataset and dataset.get('owner') not in (user,'shared'):dataset={}
        if not dataset and draft.get('dataset'):
            found=c.execute("SELECT * FROM assets WHERE id=? AND owner IN (?, 'shared') AND kind='dataset'",(draft['dataset'],user)).fetchone()
            if found:dataset=dict(found);dataset['data']=json.loads(dataset['data'])
        if not dataset and draft.get('dataset','').startswith('共享'):
            # Reuse only explicitly shared, catalog-origin metadata already
            # frozen in a real job. This never chooses or authorizes execution.
            name=draft['dataset'].removeprefix('共享').lstrip(':：')
            found=c.execute("SELECT json_extract(payload,'$.assets.dataset') FROM jobs WHERE json_extract(payload,'$.assets.dataset.owner')='shared' AND json_extract(payload,'$.assets.dataset.origin')='dataset_catalog' AND json_extract(payload,'$.assets.dataset.name')=? ORDER BY updated DESC LIMIT 1",(name,)).fetchone()
            if found:dataset=json.loads(found[0])
        count=(dataset.get('data') or {}).get('scene_count') or summary.get('dataset_scene_count')
        source=(dataset.get('data') or {}).get('remote_path') or summary.get('dataset_source') or ''
        if not source.startswith('/') or any(ch in source for ch in '\r\n\x00'):source='检查后显示工作站路径' if draft.get('dataset') else '待提供工作站路径'
        software=summary.get('software') or (assets.get('package') or {}).get('name') or draft.get('package') or '未提供'
        release=re.search(r'(SW\d+).*?\.(\d{8})-(\d+)\.app',summary.get('software_url') or software)
        if release:software=release[1]+' · 构建 '+release[3]
        scope=summary.get('scene_scope') or draft.get('scene_scope') or ('、'.join(map(str,payload.get('scenes') or []))) or '全部'
        if scope in ('全部','all'):scope='全部'+(str(count)+'个场景' if count else '场景')
        else:scope='场景 '+scope+((' / 共'+str(count)+'个') if count else '')
        mode=((payload.get('baseline') or {}).get('data',{}).get('execution_policy') or {}).get('replay_mode') or draft.get('replay_mode','stream')
        return {'software':software,'software_provided':bool(draft.get('package') or assets.get('package')),'rows':rows,'dataset_path':source,'scope':scope,'configs':configs,'yaml_requested':bool(yaml_request),'replay_mode':mode,
                'software_url':summary.get('software_url',''),'build_reference':summary.get('build_reference',''),
                'changes':summary.get('changes',''),'m100_method':rows[2]['status']}
