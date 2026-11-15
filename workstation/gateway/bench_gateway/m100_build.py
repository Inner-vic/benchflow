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
默认根据本APP原始文件名、构建链接和集成Lockfile，自动获取准确源码提交、SDK及消息依赖锁，使用已配置工具链在工作站编译；不继承旧APP的M100或旧版本源码。
遵从本APP官方ON/OFF构建参数，不按任务目标切换WITH_EVAL_DUMP。仅回放跳过指标评测；回放后评测需要官方ON版及配套落盘配置，不能将OFF版自动改成ON版。
如编译失败或接口不匹配，停止并要求手动编译、提交配套 liburoad_m100.so；支持本人工作站路径、下载链接或允许大小内的附件。其他材料保留，不自动换依赖试编。
由Jenkins的APP_VERSION取得conan_shared基准锁，由APP_VERSION/LOCKED_LIBS取得SDK及消息库版本，与本APP集成锁核对后调用原build.sh；脚本和CMake原文件不改写。
Jenkins/GitLab/JFrog只读认证和工具链由管理员一次性配置；正常换包不要求用户额外提供源码或构建锁。'''




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
  request.write_text(json.dumps({'identity':r['identity'],'build_seconds':r['build_seconds'],
                                'credential_root':r.get('credential_root'),'credential_user':r.get('credential_user')}))
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
    assert h.hexdigest()==v['sha256']
    if r['expected_output']:assert v['sha256']==r['expected_output']
    with output.open('rb') as stream:header=stream.read(20)
    assert len(header)==20 and header[:6]==b'\x7fELF\x02\x01' and int.from_bytes(header[18:20],'little')==183
    print(json.dumps({'state':'BUILT','path':str(output),'sha256':v['sha256'],'size':output.stat().st_size,'elapsed_seconds':v['elapsed_seconds'],'build_root':str(p)}))
   else:print(json.dumps({'state':'BUILD_FAILED','build_root':str(p),'reason':v.get('error_type','BuildError'),'diagnostic':v.get('diagnostic',{}), 'container_stopped':v.get('container_stopped') is True}))
   break
 if time.monotonic()>=deadline:
  print(json.dumps({'state':'NEEDS_REVIEW','build_root':str(p)}));break
 time.sleep(1)
'''


def build(cfg, store, registry, user, package, *, seconds=300, dependencies=None, build_context=None, build_reference=None, progress=None, goal="replay_and_evaluate"):
    if not cfg.get('m100_build_enabled'):
        raise BuildError('M100_BUILD_DISABLED', 'M100 自动编译尚未启用。', '请管理员启用已验收配方，或提供匹配的 liburoad_m100.so。')
    from .m100_profiles import resolve
    from .m100_policy import enabled as official_enabled
    follow_official=official_enabled(cfg)
    recipe=resolve(cfg,package.get('data',{}).get('sha256'))
    eval_dump=not (cfg.get('replay_only_no_dump_enabled') and goal=='replay')
    if follow_official:
        # Old approved eval variants are not proof of an official APP variant.
        if recipe and recipe['build'].get('eval_mode_policy')!='official-v1':recipe=None
        if cfg.get('m100_buildsh_enabled') and recipe and recipe['build'].get('build_entry')!='upstream-buildsh-v1':recipe=None
    elif recipe and recipe['build'].get('eval_dump',True)!=eval_dump:recipe=None
    if recipe is None and package.get('owner')==user and cfg.get('m100_auto_inputs_enabled'):
        from .m100_auto_inputs import prepare,automatic_profile,InputError,_read
        import time
        if type(seconds) is not int or seconds<30:raise BuildError('M100_BUILD_BUDGET','本轮材料检查时间不足，尚未启动编译。','请准备好其他材料后重新检查。')
        started=time.monotonic();budget=min(int(cfg.get('m100_inputs_timeout_seconds',120)),max(1,seconds-30))
        input_cfg=dict(cfg,_m100_inputs_deadline=started+budget)
        def bounded_fetch(url,headers,limit,timeout):
            remaining=input_cfg['_m100_inputs_deadline']-time.monotonic()
            if remaining<=0:raise TimeoutError('M100 input preparation deadline exceeded')
            return _read(url,headers,limit,min(timeout,remaining))
        if progress:progress('获取M100源码与构建锁')
        try:recipe=automatic_profile(input_cfg,prepare(input_cfg,store,user,package,build_reference=build_reference,fetch=bounded_fetch,eval_dump=eval_dump,follow_official=follow_official),fetch=bounded_fetch)
        except InputError as exc:raise BuildError(exc.code,str(exc),exc.next_action) from None
        except Exception as exc:raise BuildError('M100_INPUT_PREPARATION_FAILED','本APP编译输入准备未完成（'+type(exc).__name__+'），已停止，未使用旧版本源码。','请管理员检查本次源码准备和依赖下载；也可提供配套 liburoad_m100.so，其余材料保留。') from None
        seconds-=int(time.monotonic()-started)
    if package.get('owner') != user or not recipe:
        raise BuildError('M100_RECIPE_MISMATCH', '本APP尚缺配套M100源码和构建信息，未启动编译；未复用旧M100或旧版本配置。',
                         '请提供匹配的 liburoad_m100.so，或补充源码路径/仓库、准确提交号及修改、构建 Profile 和源码依赖锁定文件，由管理员接入。发送“M100编译说明”查看要求。')
    if follow_official:
        eval_dump=recipe['build'].get('eval_dump')
        if type(eval_dump) is not bool or recipe['build'].get('eval_mode_policy')!='official-v1':
            raise BuildError('M100_OFFICIAL_MODE_UNCONFIRMED','本APP官方ON/OFF构建参数尚未确认，未启动编译。','请核对配套官方构建信息或提供配套M100；不按任务目标切换宏。')
        if goal!='replay' and not eval_dump:
            raise BuildError('M100_OFFICIAL_OFF_FOR_EVALUATION','本APP官方M100构建为OFF，不能用于回放后评测；未将其改成ON。','请改用官方ON版APP及配套依赖，或将本次目标设为仅回放后重新确认。')
    if not all((dependencies or {}).get(role,{}).get('owner')==user and re.fullmatch('[a-f0-9]{64}',(dependencies or {}).get(role,{}).get('data',{}).get('sha256','')) for role in ('camera_so','lidar_so')):
        raise BuildError('M100_SENSOR_UNVERIFIED', '相机或雷达SO的本人来源信息不完整，未启动编译。',
                         '请提供本人的相机、雷达SO。系统先核对架构，M100编译后再核对接口；不匹配时要求替换对应文件。')
    if type(seconds) is not int or seconds < 30:
        raise BuildError('M100_BUILD_BUDGET', '本轮材料检查时间不足，尚未启动编译。', '请先准备好其他材料后重新检查。')
    # Original -s may build missing SDK/sensor packages on a cold cache. Keep
    # that known work bounded, without treating a normal C++ build as a hang.
    seconds = min(seconds, 600 if recipe['build'].get('build_entry')=='upstream-buildsh-v1' else 300)
    if progress:progress('准备M100编译产物')
    root = Path(cfg['project_root'])
    files = {'m100_build_remote.py': (root/'bench_gateway/m100_build_remote.py').read_bytes(),
             'build_uroad_eval.py': (root/'vendor/build_uroad_eval.py').read_bytes()}
    output=recipe.get('m100',{}).get('sha256')
    identity = {'recipe': recipe['id'], 'build':recipe['build'], 'expected_output': output,
                'conan_settings':base64.b64encode((root/recipe['build']['settings_file']).read_bytes()).decode(),
                'scripts': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    identity['package_sha256']=package['data']['sha256']
    identity['sensors']={k:dependencies[k]['data']['sha256'] for k in ('camera_so','lidar_so')}
    identity['material_context']=build_context
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    owner_root = store.remote_root(user, cfg['remote_users_root'])
    request = {'identity': identity, 'key': key, 'owner_root': owner_root, 'expected_output': output,
               'build_seconds': seconds, 'wait_seconds': seconds+30,
               'credential_root':str(root),'credential_user':user,
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
    if reply.get('state')=='BUILD_FAILED' and reply.get('container_stopped'):
        from .m100_compile_diagnostics import MANUAL_ACTION
        diagnostic=reply.get('diagnostic') or {}
        code=diagnostic.get('code','M100_BUILD_FAILED')
        if code not in ('M100_COMPILER_API_MISMATCH','M100_DEPENDENCY_LOCK_MISMATCH','M100_BUILD_FAILED'):code='M100_BUILD_FAILED'
        provider=diagnostic.get('provider');symbol=diagnostic.get('symbol','')
        message='本APP自动编译失败，已停止，不自动换依赖或反复重编。'
        if code=='M100_COMPILER_API_MISMATCH' and provider in ('XLidar','XCamera','依赖库') and re.fullmatch('[A-Za-z_][A-Za-z_0-9]{0,100}',symbol):message='本APP自动编译失败：'+provider+'开发头文件缺少接口 '+symbol+'。'
        if code=='M100_DEPENDENCY_LOCK_MISMATCH':message='本APP编译依赖无法与构建锁对齐，已停止。'
        raise BuildError(code,message,MANUAL_ACTION)
    if reply.get('state') != 'BUILT':
        raise BuildError('M100_BUILD_FAILED' if reply.get('state')=='BUILD_FAILED' else 'M100_BUILD_UNKNOWN',
                         'M100 编译未完成，已停止本轮准备，不自动重试。',
                         '请提供匹配成品，或请管理员检查 '+directory+'/artifact/build.log；相同配方不会重复启动。')
    if (reply.get('path') != directory+'/artifact/liburoad_m100.so' or (not re.fullmatch('[a-f0-9]{64}',str(reply.get('sha256',''))) or output and reply['sha256']!=output)
            or type(reply.get('size')) is not int or not 0 < reply['size'] <= 128*1024**2):
        raise BuildError('M100_BUILD_RECEIPT', '编译产物收据与固定配方不一致，未使用该产物。', '请管理员核对构建记录，或提供匹配成品。')
    data={'m100_automatic_build':True,'eval_dump':eval_dump,'eval_mode_policy':'official-v1' if follow_official else 'legacy-goal-v2','compiled_for_package_sha256':package['data']['sha256'],
          'compiled_sensor_sha256':{k:dependencies[k]['data']['sha256'] for k in ('camera_so','lidar_so')},'remote_path':reply['path'],'sha256':reply['sha256'],'size':reply['size'],'filename':'liburoad_m100.so','target':'lib/liburoad_m100.so'}
    # The preceding remote receipt already hashed and verified this exact file.
    # record still performs its authoritative content check; avoid remote_file's
    # extra hash/SSH round trip before that check.
    asset = registry.record(user, 'dependency', recipe['id'], data)
    if asset.get('owner') != user or asset.get('data', {}).get('sha256') != reply['sha256']:
        raise BuildError('M100_BUILD_RECEIPT', '编译产物登记未通过。', '请管理员核对本人资产登记。')
    return asset, {'recipe': recipe['id'], 'state': 'BUILT', **reply,
                   'output_types': recipe.get('m100',{}).get('output_types',[]),
                   'absent_optional_outputs': recipe.get('m100',{}).get('absent_optional_outputs',[])}
