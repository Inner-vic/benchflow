"""Personal catalog of immutable software archives proven by real replay runs.

This module performs no SSH, extraction, installation or evaluation. The fixed
remote adapter produces the archive and receipts; these checks bind that result
to one owner and the frozen material versions of a successful gateway replay.
"""
import json
from pathlib import PurePosixPath
import re
import time

from .core import GatewayError, name_check, owner_key


KIND='saved-assembly-v1'
ROLES={'camera_so':'lib/libxcamera.so','lidar_so':'lib/libxlidar.so','m100_so':'lib/liburoad_m100.so'}
SUFFIX={'package':'','camera_so':'-相机SO','lidar_so':'-雷达SO','m100_so':'-M100SO'}
SAFE=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
SHA=re.compile(r'^[a-f0-9]{64}$')


def _canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _clone(value):return json.loads(_canonical(value))


def _sha(value,label):
    if not isinstance(value,str) or not SHA.fullmatch(value):raise GatewayError('装配归档缺少有效哈希：'+label)
    return value


def _path(raw,root=None):
    if (not isinstance(raw,str) or len(raw)>4096 or any(ord(c)<32 for c in raw)
            or '\\' in raw):raise GatewayError('装配归档路径格式无效')
    path=PurePosixPath(raw)
    if not path.is_absolute() or '..' in path.parts or str(path)!=raw:raise GatewayError('装配归档路径必须规范且绝对')
    if root is not None and PurePosixPath(root) not in path.parents:raise GatewayError('装配工件只能保存在本人归档身份目录下')
    return str(path)


def _schema(store):
    with store.connect() as connection:
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS assemblies(
            owner TEXT NOT NULL REFERENCES users(id), id TEXT NOT NULL,
            name TEXT NOT NULL, version INTEGER NOT NULL, software_identity TEXT NOT NULL,
            data TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(owner,id), UNIQUE(owner,software_identity), UNIQUE(owner,name,version));
        CREATE TABLE IF NOT EXISTS assembly_sources(
            owner TEXT NOT NULL, source_job TEXT NOT NULL, assembly_id TEXT NOT NULL,
            data TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(owner,source_job),
            FOREIGN KEY(owner,assembly_id) REFERENCES assemblies(owner,id));
        ''')


def _archive(value,owner,expected_root=None):
    if (not isinstance(value,dict) or type(value.get('schema_version')) is not int or value['schema_version']!=1
            or value.get('kind')!=KIND or value.get('state')!='SAVED'):
        raise GatewayError('需要已实际保存的装配包收据，原包或静态检查不是成功装配归档')
    value=_clone(value)
    if value.get('configuration_policy')!='original-package-defaults' or value.get('requires_current_precheck') is not True:
        raise GatewayError('成功装配包只能保存原包基础配置，复用仍须执行本任务小场景预检')
    software=_sha(value.get('software_identity'),'software_identity')
    root=_path(value.get('owner_root'))
    if PurePosixPath(root).name!=owner_key(owner) or (expected_root is not None and root!=expected_root):
        raise GatewayError('装配归档不属于本人的工作站目录')
    scope=root+'/assemblies/'+software
    artifact=value.get('artifact')
    if not isinstance(artifact,dict) or type(artifact.get('bytes')) is not int or artifact['bytes']<=0:
        raise GatewayError('装配工件必须包含非空实际文件的体量')
    _path(artifact.get('path'),scope);_sha(artifact.get('sha256'),'artifact')
    dependencies=value.get('dependencies')
    if not isinstance(dependencies,list) or len(dependencies)!=3:raise GatewayError('装配归档必须包含相机、雷达、M100 三份依赖')
    paths={artifact['path']};targets=set()
    for item in dependencies:
        if not isinstance(item,dict) or item.get('target') not in ROLES.values() or item['target'] in targets:
            raise GatewayError('装配归档的三份 SO 角色或替换目标不完整')
        targets.add(item['target']);path=_path(item.get('path'),scope)
        _sha(item.get('sha256'),item['target'])
        if path in paths:raise GatewayError('装配归档工件路径重复')
        paths.add(path)
    for field in ('manifest','evidence'):
        path=_path(value.get(field+'_path'),scope);_sha(value.get(field+'_sha256'),field)
        if path in paths:raise GatewayError('装配归档证据与工件路径重复')
        paths.add(path)
    _sha(value.get('input_digest'),'input_digest');_sha(value.get('package_source_sha256'),'package_source')
    if not isinstance(value.get('source_run'),str) or not SAFE.fullmatch(value['source_run']):raise GatewayError('装配归档源 Run ID 无效')
    if value.get('owner') not in (None,owner):raise GatewayError('装配归档返回了其他用户的身份')
    value['dependencies']=sorted(dependencies,key=lambda item:item['target'])
    return value


def _software(value):
    """Run-specific evidence changes; the reusable executable bytes may not."""
    return {key:value[key] for key in ('schema_version','kind','state','software_identity','owner_root',
                                       'artifact','dependencies','manifest_path','manifest_sha256')}


def _validate_success(cfg,store,job,result):
    if not isinstance(job,dict) or not isinstance(result,dict):raise GatewayError('装配归档需要完整回放任务和收据')
    try:
        user=job['owner'];store.require_user(user)
        p=job['payload'];run=job['id'];saved=store.job(user,run)
        if _canonical(saved['payload'])!=_canonical(p):raise GatewayError('任务材料已变化，不能登记不同的装配归档')
        if (p.get('action')!='replay' or p.get('retained_result') or p.get('registration_only')
                or job.get('state')=='SIMULATED' or saved['state']=='SIMULATED' or result.get('simulated')
                or result.get('state')!='PASS' or result.get('run_id')!=run):
            raise GatewayError('仅真实成功回放可保存装配包，模拟或历史结果登记不属于装配成功')
        baseline=p['baseline']['data'];binding=baseline.get('binding') or {}
        if baseline.get('kind')!='candidate-v1':raise GatewayError('装配归档必须来自本次已冻结的候选材料')
        frozen=_sha(binding.get('input_digest'),'candidate input_digest')
        if result.get('input_digest')!=frozen:raise GatewayError('成功回放与候选输入身份不一致')
        smoke=result.get('smoke') or {}
        if smoke.get('state')!='PASS' or smoke.get('run_id')!=run+'-smoke' or smoke.get('input_digest')!=frozen:
            raise GatewayError('装配归档缺少本任务已通过的小场景预检')
        assets=p['assets'];package=assets['package'];dependencies=assets['dependencies']
        if package.get('kind')!='package' or package.get('owner')!=user:raise GatewayError('源 APP 必须属于本人')
        _sha(package['data'].get('sha256'),'source APP')
        if not isinstance(dependencies,list) or len(dependencies)!=3:raise GatewayError('本次候选未冻结完整三份 SO')
        original={}
        for item in dependencies:
            data=item['data'];target=data.get('target')
            if (item.get('kind')!='dependency' or item.get('owner')!=user or target not in ROLES.values()
                    or target in original):raise GatewayError('源 SO 角色错误或引用了其他用户资产')
            original[target]=_sha(data.get('sha256'),'source SO')
        archive=_archive(result.get('assembly_archive'),user,store.remote_root(user,cfg['remote_users_root']))
        if archive['source_run']!=run or archive.get('job_id',run)!=run or archive['input_digest']!=frozen:
            raise GatewayError('装配归档收据不属于本次成功回放')
        source_sha=(package['data'].get('package_source_sha256') if package.get('origin')=='assembly_catalog'
                    else package['data']['sha256'])
        if archive['package_source_sha256']!=_sha(source_sha,'original source APP'):
            raise GatewayError('装配归档源 APP 哈希不一致')
        if {item['target']:item['sha256'] for item in archive['dependencies']}!=original:
            raise GatewayError('装配归档三份 SO 与源任务冻结版本不同')
        name=(p.get('business_contract') or {}).get('software_name') or package['name']
        name_check(name)
        if not name.strip():raise GatewayError('装配软件规范名称不能为空白')
        return user,name,archive
    except (KeyError,TypeError,AttributeError):raise GatewayError('装配归档或源任务字段不完整，回放结果保留但尚未存档') from None


def _read(connection,row):
    record=dict(row);record['data']=json.loads(record['data'])
    record['kind']=KIND;record['origin']='assembly_catalog'
    record['sources']=[json.loads(item['data']) for item in connection.execute(
        'SELECT data FROM assembly_sources WHERE owner=? AND assembly_id=? ORDER BY created,source_job',
        (record['owner'],record['id']))]
    return record


def record_success(cfg,store,job,result):
    """Register archive identity after replay success; evaluation is independent."""
    user,name,archive=_validate_success(cfg,store,job,result);_schema(store)
    asm_id='asm-'+archive['software_identity'][:24];now=time.time()
    source={'source_job':job['id'],'source_run':archive['source_run'],'input_digest':archive['input_digest'],
            'evidence_path':archive['evidence_path'],'evidence_sha256':archive['evidence_sha256'],
            'package_source_sha256':archive['package_source_sha256'],'submitted_name':name,
            'input_package_sha256':job['payload']['assets']['package']['data']['sha256'],
            'archive':archive}
    with store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row=connection.execute('SELECT * FROM assemblies WHERE owner=? AND id=?',(user,asm_id)).fetchone()
        previous=connection.execute('SELECT * FROM assembly_sources WHERE owner=? AND source_job=?',(user,job['id'])).fetchone()
        if previous and (previous['assembly_id']!=asm_id or json.loads(previous['data'])!=source):
            raise GatewayError('同一源任务已登记不同的装配收据，拒绝覆盖成功证据')
        if row:
            data=json.loads(row['data'])
            if row['software_identity']!=archive['software_identity'] or _software(data)!=_software(archive):
                raise GatewayError('相同装配身份返回不同的工件或清单，拒绝覆盖')
        else:
            version=connection.execute('SELECT COALESCE(MAX(version),0)+1 FROM assemblies WHERE owner=? AND name=?',(user,name)).fetchone()[0]
            connection.execute('INSERT INTO assemblies(owner,id,name,version,software_identity,data,created) VALUES(?,?,?,?,?,?,?)',
                (user,asm_id,name,version,archive['software_identity'],_canonical(archive),now))
        if previous is None:
            connection.execute('INSERT INTO assembly_sources(owner,source_job,assembly_id,data,created) VALUES(?,?,?,?,?)',
                (user,job['id'],asm_id,_canonical(source),now))
        row=connection.execute('SELECT * FROM assemblies WHERE owner=? AND id=?',(user,asm_id)).fetchone()
        record=_read(connection,row)
    return {'assembly':{'id':record['id'],'name':record['name'],'version':record['version'],
                        'software_identity':record['software_identity'],'state':'SAVED','scope':'personal',
                        'remote_path':archive['artifact']['path'],'sha256':archive['artifact']['sha256'],
                        'source_run':archive['source_run'],'new_task_precheck_required':True}}


def list_assemblies(cfg,store,user):
    store.require_user(user);_schema(store)
    with store.connect() as connection:
        return [_read(connection,row) for row in connection.execute(
            'SELECT * FROM assemblies WHERE owner=? ORDER BY name,version,id',(user,))]


def resolve_assembly(cfg,store,user,reference):
    store.require_user(user)
    if not isinstance(reference,str) or not reference or len(reference)>256:raise GatewayError('请提供本人的装配包 ID 或明确名称')
    records=list_assemblies(cfg,store,user)
    if re.fullmatch(r'asm-[a-f0-9]{24}',reference):matches=[record for record in records if record['id']==reference]
    else:matches=[record for record in records if record['name']==reference]
    if not matches:raise GatewayError('本人尚未保存这个成功装配包；不会查询其他用户，也不会回退原始 APP')
    if len(matches)!=1:
        examples='、'.join(record['id'] for record in matches[:5])
        raise GatewayError('这个装配名称有多个已保存版本，请从“我的可复用软件”选择精确 ID：'+examples)
    record=matches[0];_archive(record['data'],user,store.remote_root(user,cfg['remote_users_root']))
    if record['software_identity']!=record['data']['software_identity'] or record['id']!='asm-'+record['software_identity'][:24]:
        raise GatewayError('装配目录索引与完整软件身份不一致')
    return record


def assembly_assets(record):
    try:
        owner=record['owner'];archive=_archive(record['data'],owner)
        if (record.get('kind')!=KIND or record['id']!='asm-'+archive['software_identity'][:24]
                or record.get('software_identity')!=archive['software_identity'] or type(record['version']) is not int
                or record['version']<1):raise GatewayError('装配目录记录身份不完整')
        name_check(record['name']);dependencies={item['target']:item for item in archive['dependencies']}
        output={}
        for role in ('package',*ROLES):
            item=archive['artifact'] if role=='package' else dependencies[ROLES[role]]
            data={'remote_path':item['path'],'sha256':item['sha256'],'filename':PurePosixPath(item['path']).name,
                  'assembly_id':record['id'],'software_identity':archive['software_identity']}
            if role=='package':data['size']=item['bytes'];data['package_source_sha256']=archive['package_source_sha256']
            else:data['target']=ROLES[role]
            output[role]={'id':record['id']+'-'+role,'owner':owner,'kind':'package' if role=='package' else 'dependency',
                          'name':record['name']+SUFFIX[role],'version':record['version'],'origin':'assembly_catalog',
                          'data':data,'created':record['created']}
        return output
    except (KeyError,TypeError,AttributeError):raise GatewayError('成功装配目录缺少完整工件或三份 SO') from None


def selection_inputs(record):
    assets=assembly_assets(record)
    if len(record['name'])>60:raise GatewayError('该历史软件名称超过60字符，请管理员设置较短规范名后再选用')
    return {'assembly_ref':record['id'],'software_name':record['name'],
            **{role:asset['id'] for role,asset in assets.items()},'m100_mode':'provided'}
