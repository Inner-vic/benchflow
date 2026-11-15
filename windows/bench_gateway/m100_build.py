"""Fixed reviewed M100 build; data from chat cannot provide build commands."""
import base64
import hashlib
import json
from pathlib import Path
import re

from .core import GatewayError
from .recipes import load as load_recipes, select as select_recipe, sensor_match

# Compatibility exports for existing operator tools. Dispatch always selects
# from the installed catalog, rather than these default-release aliases.
_default = load_recipes()[0]
PACKAGE,OUTPUT,RECIPE,SENSORS = _default['package_sha256'][0],_default['m100']['sha256'],_default['id'],_default['sensors']

BUILD_GUIDE='''M100 编译说明：
已支持的 APP 与相机/雷达组合：本人已有匹配产物时复用；没有时使用工作站已配置的源码、锁定依赖和工具链编译。2737 已接入，无需另填编译参数。
新版本没有构建配置时，请提供匹配的 liburoad_m100.so，或补充源码仓库/工作站路径、准确提交号及未提交修改、构建 Profile、源码构建的依赖锁定文件；环境不适用时还需工具链或构建环境来源，由管理员接入。
APP 集成 Lockfile 用于追踪软件来源，不能替代源码构建所需的依赖锁定文件。凭据在本机配置，不发到聊天。
版本不匹配或编译失败会停止并提示缺项，不自动反复编译。'''


class BuildError(GatewayError):
    def __init__(self, code, message, next_action):
        super().__init__(message)
        self.code, self.next_action = code, next_action


def reusable(cfg,user,package,dependencies,assets):
    """A catalog lookup only. Caller must still inspect the exact file contents."""
    recipe=select_recipe(cfg,package.get('data',{}).get('sha256'))
    if not recipe or package.get('owner')!=user or not sensor_match(recipe,user,dependencies):return None,None
    matches=[a for a in assets if a.get('owner')==user and a.get('kind')=='dependency'
             and a.get('data',{}).get('target')=='lib/liburoad_m100.so'
             and a.get('data',{}).get('sha256')==recipe['m100']['sha256']]
    if not matches:return None,None
    asset=max(matches,key=lambda a:a.get('created',0))
    return asset,{'recipe':recipe['id'],'state':'REUSED','path':asset['data']['remote_path'],
                  'sha256':recipe['m100']['sha256'],'output_types':recipe['m100']['output_types'],
                  'absent_optional_outputs':recipe['m100']['absent_optional_outputs']}


REMOTE = r'''
import base64,fcntl,hashlib,json,os,pathlib,subprocess,sys,time
r=json.loads(base64.b64decode(sys.argv[1]));root=pathlib.Path(r['owner_root'])
assert root.is_absolute() and '..' not in root.parts
assert not any(p.is_symlink() for p in (root,*root.parents))
assert len(r['key'])==32 and all(c in '0123456789abcdef' for c in r['key'])
p=root/'builds'/('m100-'+r['key']);p.mkdir(parents=True,exist_ok=True)
assert p.resolve()==p and root.resolve() in p.parents
with (p/'launch.lock').open('a+b') as lock:
 fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
 request=p/'request.json'
 if request.exists():
  old=json.loads(request.read_text());assert old['identity']==r['identity'],'build identity changed'
 else:
  os.umask(0o077)
  request.write_text(json.dumps({'identity':r['identity'],'build_seconds':r['build_seconds']}))
  for name,body in r['files'].items():
   assert name in ('m100_build_remote.py','build_uroad_eval.py')
   data=base64.b64decode(body);assert hashlib.sha256(data).hexdigest()==r['identity']['scripts'][name]
   (p/name).write_bytes(data)
  with (p/'launcher.log').open('wb') as log:
   proc=subprocess.Popen(['python3',str(p/'m100_build_remote.py')],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
  (p/'pid').write_text(str(proc.pid))
deadline=time.monotonic()+r['wait_seconds']
while True:
 state=p/'status.json'
 if state.exists():
  assert state.stat().st_size<131072
  v=json.loads(state.read_text())
  if v.get('cleanup_finished'):
   if v.get('state')=='BUILT':
    assert v.get('container_stopped') is True,'build container cleanup not verified'
    output=pathlib.Path(v['output']);assert output==p/'artifact/liburoad_m100.so' and output.resolve()==output
    assert 0<output.stat().st_size<=128*1024**2
    h=hashlib.sha256()
    with output.open('rb') as f:
     for block in iter(lambda:f.read(1048576),b''):h.update(block)
    assert h.hexdigest()==v['sha256']==r['expected_output']
    print(json.dumps({'state':'BUILT','path':str(output),'sha256':v['sha256'],'size':output.stat().st_size,'elapsed_seconds':v['elapsed_seconds'],'build_root':str(p)}))
   else:print(json.dumps({'state':'BUILD_FAILED','build_root':str(p),'reason':v.get('error_type','BuildError')}))
   break
 if time.monotonic()>=deadline:
  print(json.dumps({'state':'NEEDS_REVIEW','build_root':str(p)}));break
 time.sleep(1)
'''


def build(cfg, store, registry, user, package, *, seconds=300, dependencies=None):
    if not cfg.get('m100_build_enabled'):
        raise BuildError('M100_BUILD_DISABLED', 'M100 自动编译尚未启用。', '请管理员启用已验收配方，或提供匹配的 liburoad_m100.so。')
    recipe=select_recipe(cfg,package.get('data',{}).get('sha256'))
    if package.get('owner') != user or not recipe:
        raise BuildError('M100_RECIPE_MISMATCH', '当前软件包内容未匹配已验收的 M100 编译配置，已停止；未尝试编译。',
                         '请提供匹配的 liburoad_m100.so，或补充源码路径/仓库、准确提交号及修改、构建 Profile 和源码依赖锁定文件，由管理员接入。发送“M100编译说明”查看要求。')
    if not sensor_match(recipe,user,dependencies):
        raise BuildError('M100_SENSOR_UNVERIFIED', '当前相机/雷达库尚未与这份 M100 构建配置核验匹配，未启动编译。',
                         '请提供2737已验证的配套相机/雷达库，或提供与本次依赖匹配的 M100 成品；不会自动换库。')
    if type(seconds) is not int or seconds < 30:
        raise BuildError('M100_BUILD_BUDGET', '本轮材料检查时间不足，尚未启动编译。', '请先准备好其他材料后重新检查。')
    seconds = min(seconds, 300)
    root = Path(cfg['project_root'])
    files = {'m100_build_remote.py': (root/'bench_gateway/m100_build_remote.py').read_bytes(),
             'build_uroad_eval.py': (root/'vendor/build_uroad_eval.py').read_bytes()}
    output=recipe['m100']['sha256']
    identity = {'recipe': recipe['id'], 'build':recipe['build'], 'expected_output': output,
                'conan_settings':base64.b64encode((root/recipe['build']['settings_file']).read_bytes()).decode(),
                'scripts': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    owner_root = store.remote_root(user, cfg['remote_users_root'])
    request = {'identity': identity, 'key': key, 'owner_root': owner_root, 'expected_output': output,
               'build_seconds': seconds, 'wait_seconds': seconds+30,
               'files': {name: base64.b64encode(data).decode() for name, data in files.items()}}
    encoded = base64.b64encode(json.dumps(request).encode()).decode()
    try:
        # Serialized recipes/settings can exceed Windows' command-line limit.
        # Keep the request in SSH stdin, as with package download/binding.
        code='import sys\nsys.argv=["m100",'+repr(encoded)+']\n'+REMOTE
        reply = json.loads(registry.ssh.python(code, timeout=seconds+45))
    except Exception:
        raise BuildError('M100_BUILD_UNKNOWN', '编译连接中断或收据未能核验，远端状态待核对；不自动重编。',
                         '请管理员检查本人 builds/m100-'+key+' 的状态；若不想等待，可提供匹配成品。') from None
    directory = owner_root+'/builds/m100-'+key
    if reply.get('state') != 'BUILT':
        raise BuildError('M100_BUILD_FAILED' if reply.get('state')=='BUILD_FAILED' else 'M100_BUILD_UNKNOWN',
                         'M100 编译未完成，已停止本轮准备，不自动重试。',
                         '请提供匹配成品，或请管理员检查 '+directory+'/artifact/build.log；相同配方不会重复启动。')
    if (reply.get('path') != directory+'/artifact/liburoad_m100.so' or reply.get('sha256') != output
            or type(reply.get('size')) is not int or not 0 < reply['size'] <= 128*1024**2):
        raise BuildError('M100_BUILD_RECEIPT', '编译产物收据与固定配方不一致，未使用该产物。', '请管理员核对构建记录，或提供匹配成品。')
    data={'remote_path':reply['path'],'sha256':output,'size':reply['size'],'filename':'liburoad_m100.so','target':'lib/liburoad_m100.so'}
    # The preceding remote receipt already hashed and verified this exact file.
    # record still performs its authoritative content check; avoid remote_file's
    # extra hash/SSH round trip before that check.
    asset = registry.record(user, 'dependency', recipe['id'], data)
    if asset.get('owner') != user or asset.get('data', {}).get('sha256') != output:
        raise BuildError('M100_BUILD_RECEIPT', '编译产物登记未通过。', '请管理员核对本人资产登记。')
    return asset, {'recipe': recipe['id'], 'state': 'BUILT', **reply,
                   'output_types': recipe['m100']['output_types'],
                   'absent_optional_outputs': recipe['m100']['absent_optional_outputs']}
