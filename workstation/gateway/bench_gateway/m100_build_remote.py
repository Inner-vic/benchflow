"""Build the selected APP's M100 in a private copy/container; never operate a bench."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


# The original script is unchanged. The launcher supplies authentication without
# credentials in docker argv, environment inspection, source or saved logs.
LAUNCHER = r'''import json,os,subprocess,sys
from pathlib import Path
a=json.loads(Path('/run/m100-auth/credentials.json').read_text())
env=dict(os.environ,CONAN_LOGIN_USERNAME=a['username'],CONAN_PASSWORD=a['password'],
         GITLAB_ACCESS_TOKEN=a['gitlab_token'],
         GIT_TERMINAL_PROMPT='0',GIT_CONFIG_GLOBAL='/run/m100-auth/gitconfig')
secrets=[a['password'],a['gitlab_token']]
p=subprocess.Popen(sys.argv[1:],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env)
for raw in iter(p.stdout.readline,b''):
 text=raw.decode('utf-8','replace')
 for value in secrets:
  if value:text=text.replace(value,'[REDACTED]')
 sys.stdout.write(text);sys.stdout.flush()
raise SystemExit(p.wait())
'''

GIT_CREDENTIAL = r'''#!/usr/bin/env python3
import json,sys
from pathlib import Path
v=dict(line.rstrip('\n').split('=',1) for line in sys.stdin if '=' in line)
if (len(sys.argv)>1 and sys.argv[1]=='get' and v.get('protocol')=='https'
    and v.get('host')=='gitlab.chehejia.com'
    and v.get('path') in ('liconan/conan-config.git','livc-acore/chassis/uroad.git')):
 a=json.loads(Path('/run/m100-auth/credentials.json').read_text())
 print('username=oauth2\npassword='+a['gitlab_token'])
'''


def private_build_environment(directory, build, request):
    """Mutable cache/config are per attempt; shared binary packages stay read-only."""
    auth_root=Path(request['credential_root'])
    assert auth_root.is_absolute() and auth_root.resolve()==auth_root
    loader='''import sys,json,base64
from pathlib import Path
r=Path(sys.argv[1]);sys.path.insert(0,str(r))
from bench_gateway.download_auth import load_authorization
from bench_gateway.m100_source_auth import load
c=json.loads((r/'config/gateway.json').read_text());c['project_root']=str(r)
h=load_authorization(c,sys.argv[2],'artifactory.ep.chehejia.com')
assert h and h.startswith('Basic ')
u,p=base64.b64decode(h[6:],validate=True).decode().split(':',1)
t=load(c);assert t
print(json.dumps({'username':u,'password':p,'gitlab_token':t}))
'''
    secret_result=subprocess.run([sys.executable,'-B','-c',loader,str(auth_root),request['credential_user']],
                                 capture_output=True,text=True,timeout=30)
    if secret_result.returncode:raise ValueError('private build authentication unavailable')
    credentials=json.loads(secret_result.stdout)
    assert set(credentials)=={'username','password','gitlab_token'} and all(credentials.values())
    auth=directory/'transient-auth';auth.mkdir(mode=0o700)
    for name,data,mode in [('credentials.json',json.dumps(credentials).encode(),0o600),
                           ('launch.py',LAUNCHER.encode(),0o600),('git-credential',GIT_CREDENTIAL.encode(),0o700),
                           ('gitconfig',b'[credential]\n\thelper =\n\thelper = /run/m100-auth/git-credential\n\tuseHttpPath = true\n',0o600)]:
        fd=os.open(auth/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
        with os.fdopen(fd,'wb') as out:out.write(data)
    conan=directory/'conan-home';conan.mkdir(mode=0o700);data=conan/'data';data.mkdir()
    cache=Path(build['cache'])
    locked=json.loads((Path(build['source'])/'build/conan.lock').read_text())
    refs={n['ref'].split('#')[0] for n in locked['graph_lock']['nodes'].values() if n.get('ref')}
    refs.add(build['baselock'])
    for ref in sorted(refs):
        match=re.fullmatch(r'([A-Za-z0-9_.+-]+)/([A-Za-z0-9_.+-]+)(?:@([A-Za-z0-9_.+-]+)/([A-Za-z0-9_.+-]+))?',ref)
        assert match,'invalid cache seed reference'
        name,version,user,channel=match.groups();entry=cache/name/version/(user or '_')/(channel or '_')
        if not entry.exists():continue
        assert not entry.is_symlink() and cache.resolve() in entry.resolve().parents,'linked seed cache entry'
        target=data/entry.relative_to(cache);target.mkdir(parents=True)
        for p in entry.iterdir():
            if p.name=='package':
                (target/'package').mkdir()
                for package in p.iterdir():
                    # Conan marks interrupted downloads/builds with .dirty.
                    # Such entries must be resolved afresh in the private home.
                    if (not re.fullmatch('[a-f0-9]{40}',package.name)
                            or (p/(package.name+'.dirty')).exists()):continue
                    link=os.readlink(package) if package.is_symlink() else '/root/.conan/seed-data/'+str(package.relative_to(cache))
                    assert link.startswith(('/root/.conan/shared-data/','/root/.conan/seed-data/'))
                    (target/'package'/package.name).symlink_to(link)
            elif p.name in ('export','export_source') and p.is_dir():shutil.copytree(p,target/p.name,symlinks=True)
            elif p.name=='metadata.json' and p.is_file():shutil.copy2(p,target/p.name)
    return ['--mount','type=bind,src='+str(conan)+',dst=/root/.conan',
            '--mount','type=bind,src='+str(auth)+',dst=/run/m100-auth,readonly',
            '--mount','type=bind,src='+str(cache)+',dst=/root/.conan/seed-data,readonly']


def clear_private_auth(directory):
    """Call only after the attempt's container has stopped, never on shared homes."""
    auth=directory/'transient-auth'
    if auth.exists():shutil.rmtree(auth)
    # Conan 1 stores a remote authentication token here after conan user.
    for name in ('.conan.db','.conan.db-journal','.conan.db-wal','.conan.db-shm'):
        (directory/'conan-home'/name).unlink(missing_ok=True)
    return not auth.exists() and not (directory/'conan-home/.conan.db').exists()

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1048576), b''):
            h.update(part)
    return h.hexdigest()


def capture(argv, timeout=60):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


"""Bounded compiler diagnostics; no commands or arbitrary log text reach chat."""
import re

MANUAL_ACTION='请手动编译与本APP及本次相机、雷达配套的 liburoad_m100.so，再提供本人工作站路径、下载链接或允许大小内的附件；其余材料保留，不自动进入台架。'

def diagnose(text):
    for line in text[-2*1024*1024:].splitlines():
        if 'error:' not in line:continue
        match=re.search(r'has no member named\s+[\x27\x22‘’]([A-Za-z_][A-Za-z_0-9]{0,100})',line)
        if match:
            provider='XLidar' if 'XLidar::' in line else 'XCamera' if 'XCamera::' in line else '依赖库'
            return {'code':'M100_COMPILER_API_MISMATCH','provider':provider,'symbol':match[1],
                    'message':'本APP自动编译失败：'+provider+'开发头文件缺少接口 '+match[1]+'；未修改业务代码或自动替换依赖。','next_action':MANUAL_ACTION}
    if "doesn't match the locked one" in text or 'does not match the locked' in text:
        return {'code':'M100_DEPENDENCY_LOCK_MISMATCH','message':'本APP自动编译的依赖版本或包ID无法与构建锁对齐，已停止。','next_action':MANUAL_ACTION}
    return {'code':'M100_BUILD_FAILED','message':'本APP自动编译未完成，已停止，不自动换版本或反复重编。','next_action':MANUAL_ACTION}


def main():
    directory = Path(__file__).resolve().parent
    assert directory.parent.name == 'builds' and directory.parent.parent.name.startswith('u_')
    assert not (directory / 'status.json').exists(), 'existing build must be inspected, never retried in place'
    request = json.loads((directory / 'request.json').read_text())
    # Only the installed PC-side catalog supplies this validated recipe.
    build=request['identity']['build']
    SOURCE=Path(build['source']);COMMIT=build['commit'];DIFF=build['source_diff_sha256']
    LOCK=build['source_lock_sha256'];IMAGE=build['image'];CACHE=build['cache']
    buildsh=build.get('build_entry')=='upstream-buildsh-v1'
    limit = request['build_seconds']
    assert type(limit) is int and 30 <= limit <= (600 if buildsh else 300)
    started = time.time()
    container = 'gateway-m100-' + directory.name
    receipt = {'state': 'PREPARING', 'started_at': started, 'source': str(SOURCE),
               'commit': COMMIT, 'source_diff_sha256': DIFF, 'source_lock_sha256': LOCK,
               'image_id': IMAGE, 'container': container, 'network': 'bridge' if buildsh else 'none',
               'bench_actions': 'NOT_RUN', 'source_modified': False}

    def state(**values):
        receipt.update(values)
        temp = directory / 'status.tmp'
        temp.write_text(json.dumps(receipt, indent=2) + '\n')
        os.replace(temp, directory / 'status.json')

    made_container = False
    try:
        state()
        if build.get('source_snapshot'):
            from build_uroad_eval import validate_source_snapshot
            assert sha(Path(build['source_snapshot']))==build['source_snapshot_sha256']
            proof=validate_source_snapshot(SOURCE,Path(build['source_snapshot']))
            assert proof['source_commit']==COMMIT and proof['package_sha256']==build['package_sha256']
            assert proof['recipe_patch_sha256']==DIFF
        else:
            assert capture(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD']) == COMMIT
            diff = capture(['git', '-C', str(SOURCE), 'diff', '--binary'])
            assert hashlib.sha256(diff.encode()).hexdigest() == DIFF
        assert sha(SOURCE / 'build/conan.lock') == LOCK
        if buildsh:
            assert build.get('eval_mode_policy')=='official-v1'
            assert sha(SOURCE/'build.sh')==build['build_script_sha256']
            assert build['baselock']==proof['ci']['baselock'] and build['ci']==proof['ci']
        assert capture(['docker', 'image', 'inspect', '--format', '{{.Id}}', IMAGE]) == IMAGE
        mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        assert int(mem['MemAvailable'].strip().split()[0]) > 10 * 1024 * 1024, 'less than 10GiB available memory'
        assert shutil.disk_usage(directory).free > 10 * 1024**3
        target = directory / 'source'
        shutil.copytree(SOURCE, target, symlinks=True,
                        ignore=lambda path, names: {'build', 'install'} if Path(path) == SOURCE else set())
        (target / 'build').mkdir()
        shutil.copy2(SOURCE / 'build/conan.lock', target / 'build/conan.lock')
        settings=base64.b64decode(request['identity']['conan_settings'],validate=True)
        assert hashlib.sha256(settings).hexdigest()==build['settings_sha256'],'Conan schema differs'
        (directory / 'settings.yml').write_bytes(settings)
        extra_mounts=['--mount','type=bind,src='+build['shared_cache']+',dst=/root/.conan/shared-data,readonly'] if build.get('shared_cache') else []
        private_mounts=private_build_environment(directory,build,request) if buildsh else [
            '--mount','type=bind,src='+CACHE+',dst=/root/.conan/data',
            '--mount','type=bind,src='+str(directory/'settings.yml')+',dst=/root/.conan/settings.yml,readonly']
        if buildsh:shutil.copy2(directory/'settings.yml',directory/'conan-home/settings.yml')
        capture(['docker', 'run', '--detach', '--name', container, '--network='+('bridge' if buildsh else 'none'),
                 '--cpus=4', '--memory=10g', '--pids-limit=512',
                 '-e', 'CONAN_CPU_COUNT=4', '-e', 'CMAKE_BUILD_PARALLEL_LEVEL=4',
                 '--mount', 'type=bind,src=' + str(target) + ',dst=/root/workspace/apps',
                 *private_mounts,
                 *extra_mounts, IMAGE, 'sleep', 'infinity'])
        made_container = True
        state(state='BUILDING', compile_started_at=time.time(), copied_source=str(target))
        argv = [sys.executable, str(directory / 'build_uroad_eval.py'), '--source', str(target),
                '--expected-commit', COMMIT, '--artifact-id', 'artifact',
                '--artifacts-root', str(directory), '--container', container,
                '--execute','--eval-dump','on' if build.get('eval_dump',True) else 'off']
        argv+=['--baselock',build['baselock'],'--credential-launcher'] if buildsh else ['--offline-lock']
        if build.get('source_snapshot'):argv+=['--source-snapshot',str(target/'source-snapshot.json')]
        with (directory / 'coordinator.log').open('w') as log:
            result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=limit)
        assert result.returncode == 0, 'build helper failed; inspect artifact/build.log and coordinator.log'
        manifest = json.loads((directory / 'artifact/build-manifest.json').read_text())
        assert manifest['state'] == 'BUILT'
        output = Path(manifest['output'])
        assert sha(output) == manifest['sha256'] and output.stat().st_size == manifest['size_bytes']
        state(state='BUILT', finished_at=time.time(), elapsed_seconds=round(time.time()-started, 2),
              output=str(output), sha256=manifest['sha256'], size_bytes=manifest['size_bytes'],
              machine=manifest['machine'], manifest=str(directory / 'artifact/build-manifest.json'))
    except Exception as exc:
        log=directory/'artifact/build.log'
        text=''
        if log.is_file():
            with log.open('rb') as stream:
                stream.seek(max(0,log.stat().st_size-2*1024*1024));text=stream.read().decode('utf-8','replace')
        state(state='BUILD_FAILED', finished_at=time.time(), elapsed_seconds=round(time.time()-started, 2),
              error_type=type(exc).__name__, error=str(exc)[:1000],diagnostic=diagnose(text))
    finally:
        if made_container:
            stopped = subprocess.run(['docker', 'stop', '--time=10', container], capture_output=True, timeout=30)
            state(container_stopped=stopped.returncode == 0)
        # Authentication is transient and never an artifact. Remove it on both
        # success and failure, only after the process/container has stopped.
        auth=directory/'transient-auth'
        removed=False
        if not made_container or receipt.get('container_stopped'):
            removed=clear_private_auth(directory)
        state(transient_auth_removed=removed)
        state(cleanup_finished=True)


if __name__ == '__main__':
    main()
