"""Select a reviewed release assembly, retaining the user's frozen raw input."""
import base64
import json
from pathlib import Path

from .core import GatewayError
from .recipes import select, sensor_match, identity


def prepare(cfg,store,registry,user,package,dependencies):
    recipe=select(cfg,package.get('data',{}).get('sha256'),raw=True)
    if not recipe or package['data']['sha256'] in recipe['package_sha256']:return package,{}
    if package.get('owner')!=user or not sensor_match(recipe,user,dependencies):
        raise GatewayError('原始 APP 的已核验装配方案需要对应版本的相机和雷达 SO；请补充配套依赖，本次未装配、未编译。')
    m100=dependencies.get('m100_so')
    if m100 and (m100.get('owner')!=user or m100['data']['sha256'] not in recipe['assembly']['compatible_m100_sha256']):
        raise GatewayError('所提供的 M100 尚未与本包装配方案核验；请提供配套 M100，或明确改为自动编译，不会自行换库。')
    assembly=recipe['assembly']
    if not assembly.get('output_sha256'):raise GatewayError('该版本装配配方尚未验收；请管理员核验后接入。')
    root=store.remote_root(user,cfg['remote_users_root'])
    request={'owner_root':root,'package':package['data'],'assembly':assembly,'recipe':recipe['id'],'key':identity(assembly)[:32]}
    # Arguments and commands come only from installed administrator sources.
    code=(Path(cfg['project_root'])/'bench_gateway/package_assembly_remote.py').read_text(encoding='utf-8')
    encoded=base64.b64encode(json.dumps(request,ensure_ascii=True).encode()).decode()
    code='import base64\n'+code.replace("run(json.load(sys.stdin))",'run(json.loads(base64.b64decode('+repr(encoded)+')))')
    try:value=json.loads(registry.ssh.python(code,timeout=150))
    except Exception:raise GatewayError('APP 装配连接中断，状态待核对；原包保留，不自动重复装配。') from None
    expected=root+'/prepared-packages/'+request['key']+'/package.app.tar.gz'
    # Historical receipts retain their original path. Only an administrator's
    # owner-scoped migration mapping can translate it, after the remote helper
    # has verified the actual current file against the recipe's exact hash.
    if value.get('remote_path')!=expected:
        from .incremental import relocated_path
        original_path=value.get('remote_path')
        with store.connect() as connection:
            mapped=relocated_path(connection,user,original_path)
        if mapped==expected:
            value={**value,'remote_path':expected,'receipt_original_path':original_path,
                   'path_resolution':'owner-storage-mapping-v1'}
    if (value.get('state')!='PREPARED' or value.get('remote_path')!=expected or value.get('sha256')!=assembly['output_sha256']
            or value.get('source_sha256')!=package['data']['sha256'] or value.get('original_modified') is not False):
        raise GatewayError('APP 装配收据未通过，已停止；请管理员检查本人 prepared-packages 记录，不反复重试。')
    data={'remote_path':expected,'sha256':value['sha256'],'size':value['size'],
          'filename':package['data'].get('filename','APP')+'-prepared',
          'release_assembly':{'recipe':recipe['id'],'source_sha256':value['source_sha256'],'changed_members':value['changed_members']}}
    asset=registry.record(user,'package',recipe['id']+'-已装配APP',data)
    return asset,value
