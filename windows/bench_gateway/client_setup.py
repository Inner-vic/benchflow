"""Windows-only onboarding. Persist program configuration, never business data.

    python tools/windows_client_setup.py [doctor|wizard|ssh|login|install|start|stop|verify|package]
"""
import argparse,hashlib,json,os,re,shlex,shutil,subprocess,sys,time,zipfile
from pathlib import Path
from .config import load,save,ssh_binary
from .core import GatewayError

REMOTE_ROOT='/mnt/data/bench-eval/service/gateway'
REMOTE_PYTHON='/home/lixiang/miniconda3/envs/tvg/bin/python'
AUTH_HELP='https://learn.chatgpt.com/docs/auth'

def config(root):
    path=Path(root)/'config/gateway.json'
    if not path.exists():return {'project_root':str(Path(root).resolve()),'deployment_role':'ai-client','workstation':'ubuntu-workstation','remote_project_root':REMOTE_ROOT,'remote_python':REMOTE_PYTHON,'codex_binary':'codex'}
    value=load(root)
    if value.get('deployment_role')!='ai-client':raise GatewayError('该目录是工作站业务服务，不能作为Windows解析客户端修改。')
    return value

def local_run(args,timeout=20,**kwargs):
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' and not kwargs.pop('visible',False) else {}
    return subprocess.run(args,timeout=timeout,text=True,encoding='utf-8',errors='replace',**options,**kwargs)

def login_status(cfg):
    from .parser import resolve_binary
    try:
        p=local_run([resolve_binary(cfg),'login','status'],capture_output=True)
        return 'CACHED_LOGIN_PRESENT' if p.returncode==0 else 'LOGIN_REQUIRED'
    except (OSError,subprocess.TimeoutExpired):return 'CLI_UNAVAILABLE'

REMOTE_INSPECT=r'''import pathlib,sys,json,sqlite3,time
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root))
from bench_gateway.config import load
cfg=load(root)
s=json.loads((root/'runtime/service/status.json').read_text())
c=sqlite3.connect('file:'+str(pathlib.Path(cfg['state_root'])/'ai-requests.sqlite3')+'?mode=ro',uri=True)
row=c.execute('SELECT heartbeat,busy_until FROM client WHERE id=1').fetchone();c.close()
print(json.dumps({'service_ready':s.get('phase')=='RUNNING' and 0<=time.time()-s.get('heartbeat',0)<10 and all(s.get('children',{}).get(x,{}).get('ready') for x in ('bridge','worker')),
 'parser_connected':bool(row and (0<=time.time()-row[0]<12 or row[1]>time.time())),
 'parser_busy':bool(row and row[1]>time.time()),'parser_guard_installed':'def broker_guard(' in (root/'bench_gateway/ai_broker.py').read_text(),
 'maintenance':json.loads((root/'runtime/maintenance.json').read_text()).get('enabled'),
 'confirmation_required':cfg.get('require_material_confirmation') is True,'model':cfg.get('model'),
 'credentials_location':'workstation','cloud_auto_start':cfg.get('cloud_browser_autostart') is True}))
'''

def remote_info(cfg):
    from .transfer import SSH
    try:
        return json.loads(SSH(cfg).python(REMOTE_INSPECT,(cfg['remote_project_root'],),timeout=25))
    except Exception as e:
        # SSH stderr may contain network details. Never echo arbitrary output.
        raise GatewayError('工作站SSH检查未通过。检查公司网络/VPN、公钥登记和主机指纹；不要关闭StrictHostKeyChecking。') from None

def doctor(cfg):
    result={'python':sys.version_info>=(3,11),'codex':login_status(cfg),'workstation':None}
    try:result['workstation']=remote_info(cfg)
    except GatewayError:result['workstation']={'service_ready':False,'ssh':'FAILED','action':'先选择“配置SSH”；新公钥需工作站管理员登记。主机指纹变化须核对，不能跳过。'}
    result['guidance']=[
        '缺Codex CLI：安装公司认可的Codex桌面应用或按 https://developers.openai.com/codex/cli 安装官方CLI，然后重开向导。',
        'Codex登录状态是本机缓存检查；选择“真实解析验收”才能验证当前网络和模型权限。',
        '飞书、JFrog、LPAI凭据继续由Linux工作站使用，换Windows电脑不用重新填写或复制。',
        '云端评测实例自动启动尚未接通；实例停止时由管理员启动，心跳过期不能当作登录失效。',
        '运行依赖当前Windows用户已登录、电脑联网且不休眠；不自动解锁Windows或修改电源策略。']
    return result

def persist(cfg):
    allowed=('deployment_role','workstation','remote_project_root','remote_python','codex_binary','model','ssh_config')
    value={k:cfg[k] for k in allowed if k in cfg and cfg[k] is not None}
    save(Path(cfg['project_root'])/'config/gateway.json',value)

def validate_profile(value):
    if set(value)-{'schema_version','host','user','port','host_keys'}:raise GatewayError('工作站连接档案包含未支持的字段。')
    if value.get('schema_version')!=1 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',value.get('host','')) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*',value.get('user','')):raise GatewayError('工作站连接档案无效。')
    if type(value.get('port')) is not int or not 1<=value['port']<=65535:raise GatewayError('SSH端口无效。')
    keys=value.get('host_keys');pattern=r'(?:ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]+'
    if not isinstance(keys,list) or not keys or any(not isinstance(k,str) or not re.fullmatch(pattern,k) for k in keys):raise GatewayError('需要经过核验的工作站公钥；不自动信任ssh-keyscan结果。')
    return value

def setup_ssh(cfg):
    root=Path(cfg['project_root']);profile=root/'config/workstation-public.json'
    if not profile.is_file():raise GatewayError('缺少工作站公开连接档案。请在原电脑选择“导出迁移包”，或让管理员填写经核对的host/user/port/host_keys。')
    value=validate_profile(json.loads(profile.read_text(encoding='utf-8')))
    key=Path.home()/'.ssh/bench_gateway_ed25519'
    key.parent.mkdir(parents=True,exist_ok=True)
    exe=Path(ssh_binary()).with_name('ssh-keygen.exe' if os.name=='nt' else 'ssh-keygen')
    if not key.exists():
        p=local_run([str(exe),'-t','ed25519','-f',str(key),'-N','','-C','bench-gateway-client'],capture_output=True)
        if p.returncode:raise GatewayError('SSH密钥创建失败；请检查当前用户.ssh目录权限。')
        if os.name=='nt':
            who=local_run(['whoami.exe'],capture_output=True).stdout.strip()
            p=local_run(['icacls.exe',str(key),'/inheritance:r','/grant:r',who+':(R,W)'],capture_output=True)
            if p.returncode:raise GatewayError('新私钥权限收紧失败；请管理员检查，未尝试连接。')
        else:key.chmod(0o600)
    pub=key.with_suffix('.pub')
    if not pub.is_file():raise GatewayError('专用私钥已存在但缺少公钥，请管理员检查；未覆盖原密钥。')
    known=root/'config/workstation-known-hosts'
    host=value['host'] if value['port']==22 else '['+value['host']+']:'+str(value['port'])
    text=''.join(host+' '+k+'\n' for k in value['host_keys'])
    if known.exists() and known.read_text()!=text:raise GatewayError('工作站公钥发生变化，请核对后手动更新；未覆盖旧信任记录。')
    known.write_text(text,encoding='utf-8')
    path=root/'config/workstation-ssh.conf'
    path.write_text('Host ubuntu-workstation\n  HostName '+value['host']+'\n  User '+value['user']+'\n  Port '+str(value['port'])+'\n  IdentityFile "'+key.as_posix()+'"\n  UserKnownHostsFile "'+known.as_posix()+'"\n  IdentitiesOnly yes\n  StrictHostKeyChecking yes\n  UpdateHostKeys no\n',encoding='utf-8')
    cfg['ssh_config']=str(path.resolve());persist(cfg)
    return {'state':'CONFIGURED','public_key':pub.read_text().strip(),'action':'请工作站管理员将此公钥登记到现有Linux账号的authorized_keys（只追加，不覆盖）。私钥不离开本机。登记后运行诊断。'}

def install(cfg):
    if os.name!='nt':raise GatewayError('后台安装仅支持Windows。')
    if login_status(cfg)!='CACHED_LOGIN_PRESENT':raise GatewayError('请先完成Codex登录，再安装后台任务。')
    remote=remote_info(cfg)
    if not remote['service_ready'] or not remote['confirmation_required']:raise GatewayError('现有工作站服务尚未就绪，请管理员检查；不新建工作站服务。')
    cfg['model']=remote.get('model');persist(cfg)
    from .windows_host import task_name,manifest_path
    pythonw=Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():raise GatewayError('缺少pythonw.exe，请使用完整的Windows Python 3.11或更新版本。')
    script=Path(cfg['project_root'])/'tools/register_windows_host.ps1'
    powershell=shutil.which('pwsh.exe') or shutil.which('powershell.exe')
    p=local_run([powershell,'-NoProfile','-NonInteractive','-File',str(script),'-ProjectRoot',cfg['project_root'],'-PythonwPath',str(pythonw)],capture_output=True,timeout=30)
    if p.returncode:raise GatewayError('计划任务注册未通过；检查任务计划程序权限或组织脚本策略。未尝试绕过策略。')
    result=json.loads(p.stdout.lstrip('\ufeff'))
    if result.get('task_name')!=task_name(cfg):raise GatewayError('计划任务身份不一致，未启动。')
    save(manifest_path(cfg),{'schema_version':1,'enabled':True,'project_root':str(Path(cfg['project_root']).resolve()),'task_name':task_name(cfg),'registered_at':time.time()})
    return {'state':'INSTALLED','task':task_name(cfg),'started':False,'action':'新电脑验收完毕后，先停止旧电脑解析客户端，再在新电脑选择启动。不会停止Linux服务。'}

def start(cfg):
    from .windows_host import task_name
    from .ai_client_admin import start as native_start
    info=remote_info(cfg)
    # An already connected parser is preserved; do not guess its laptop owner.
    p=local_run(['schtasks.exe','/Query','/TN',task_name(cfg),'/XML'],capture_output=True)
    if p.returncode:raise GatewayError('请先安装后台任务。')
    if info['parser_connected']:
        raise GatewayError('工作站已有在线解析客户端；保持现状，不启动第二台。迁移请先在旧电脑选择“仅停止本机解析器”，等待心跳离线再启动新电脑。')
    if not info.get('parser_guard_installed'):raise GatewayError('工作站尚未安装解析客户端互斥保护，请管理员先升级；不并行启动。')
    return native_start(cfg)

def verify(cfg):
    info=remote_info(cfg)
    if info['parser_busy']:raise GatewayError('解析器正在处理请求，请完成后再验收。')
    if not info['parser_connected']:raise GatewayError('解析客户端未连接，请先启动并运行诊断。')
    from .transfer import SSH
    code="""import sys,json
sys.path.insert(0,sys.argv[1])
from bench_gateway.config import load
from bench_gateway.ai_broker import request
from bench_gateway.yaml_plan import INSTRUCTIONS,validate_plan
cfg=load(sys.argv[1]);text='将 common_param.yaml 的 debug.open_debug 设置为 true'
value=request(cfg,'yaml',INSTRUCTIONS+'\\n本次请求：'+json.dumps(text,ensure_ascii=False),50)
value=validate_plan(value,text)
assert value['status']=='READY' and len(value['operations'])==1
print(json.dumps({'state':'PASS','actual_roundtrip':True,'yaml_applied':False,'bench_submitted':False}))
"""
    return json.loads(SSH(cfg).python(code,(cfg['remote_project_root'],),timeout=60))

def package(cfg,destination):
    from .transfer import SSH
    root=Path(cfg['project_root']);exe=SSH(cfg).argv()[0]
    p=local_run([exe,*(['-F',cfg['ssh_config']] if cfg.get('ssh_config') else []),'-G',cfg['workstation']],capture_output=True)
    fields=dict(line.split(None,1) for line in p.stdout.splitlines() if ' ' in line)
    if p.returncode:raise GatewayError('无法读取SSH公开配置。')
    keys=SSH(cfg).python("import pathlib,json\nprint(json.dumps([' '.join(p.read_text().split()[:2]) for p in pathlib.Path('/etc/ssh').glob('ssh_host_*_key.pub') if p.is_file()]))")
    profile=validate_profile({'schema_version':1,'host':fields['hostname'],'port':int(fields['port']),'user':fields['user'],'host_keys':json.loads(keys)})
    files=[*root.glob('bench_gateway/*.py'),*(root/'config').glob('*-schema.json')]
    for name in ('tools/windows_client_setup.py','tools/windows_setup.ps1','tools/register_windows_host.ps1','tools/windows_service_host.py','windows-client-setup.cmd','START_HERE.md','docs/Windows电脑迁移交接.md','docs/Windows解析电脑部署与授权.md'):
        files.append(root/name)
    if any(not p.is_file() or p.is_symlink() for p in files):raise GatewayError('迁移程序文件缺失，请检查安装包。')
    destination=Path(destination).resolve()
    if destination.exists():raise GatewayError('迁移包已存在，请使用新文件名；不覆盖。')
    with zipfile.ZipFile(destination,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for p in files:z.write(p,p.relative_to(root).as_posix())
        z.writestr('config/workstation-public.json',json.dumps(profile,ensure_ascii=False,indent=2))
        z.writestr('config/windows-client.example.json',json.dumps({'deployment_role':'ai-client','workstation':'ubuntu-workstation','remote_project_root':cfg['remote_project_root'],'remote_python':cfg['remote_python'],'codex_binary':'codex','model':cfg.get('model')},indent=2))
    return {'state':'PACKAGED','path':str(destination),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'includes':'程序、静态配置及经当前SSH验证的工作站公钥；无登录凭据、私钥或业务数据'}

def login(cfg):
    from .parser import resolve_binary
    return {'state':'LOGIN_COMPLETED' if local_run([resolve_binary(cfg),'login'],visible=True,timeout=600).returncode==0 else 'LOGIN_NOT_COMPLETED','action':'若浏览器回调不通，可手动运行 codex login --device-auth（需组织允许）。无需退出原有登录。'}

def main(argv=None):
    parser=argparse.ArgumentParser(description='Windows解析电脑部署向导；Linux工作站及台架不变')
    parser.add_argument('action',nargs='?',default='wizard',choices=['wizard','doctor','ssh','login','install','start','stop','verify','package'])
    parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));parser.add_argument('--output')
    args=parser.parse_args(argv);cfg=config(args.root)
    def perform(action):
        if action=='doctor':return doctor(cfg)
        if action=='ssh':return setup_ssh(cfg)
        if action=='login':return login(cfg)
        if action=='install':return install(cfg)
        if action=='start':return start(cfg)
        if action=='stop':
            from .ai_client_admin import stop
            return stop(cfg)
        if action=='verify':return verify(cfg)
        if action=='package':return package(cfg,args.output or str(Path(cfg['project_root'])/'windows-parser-client.zip'))
    if args.action!='wizard':
        try:print(json.dumps(perform(args.action),ensure_ascii=False,indent=2));return 0
        except (GatewayError,OSError,ValueError,subprocess.TimeoutExpired) as e:
            print(str(e) if isinstance(e,GatewayError) else '操作未完成，请按部署文档核对；未输出凭据或原始日志。');return 1
    print('台架评测：Windows解析电脑部署向导\nLinux工作站、机器人、业务数据与台架保持原位。凭据不随迁移包复制。')
    if not (Path(cfg['project_root'])/'config/gateway.json').exists():
        sample=Path(cfg['project_root'])/'config/windows-client.example.json'
        if sample.exists():
            v=json.loads(sample.read_text());cfg.update({k:v[k] for k in ('remote_project_root','remote_python','model') if k in v})
        persist(cfg)
    actions={'1':'doctor','2':'ssh','3':'login','4':'install','5':'start','6':'verify','7':'stop','8':'package'}
    while True:
        print('\n1 环境与授权诊断  2 配置SSH  3 登录Codex\n4 安装后台托管  5 启动本机解析器  6 真实解析验收\n7 仅停止本机解析器  8 导出迁移包  0 退出\n首次部署：1→2→3→4；切换时先在旧电脑7，再在新电脑5→6。')
        value=input('请选择：').strip()
        if value=='0':return 0
        if value not in actions:continue
        if value=='7' and input('将停止本机解析器，工作站业务继续。输入 STOP 确认：')!='STOP':continue
        try:print(json.dumps(perform(actions[value]),ensure_ascii=False,indent=2))
        except (GatewayError,OSError,ValueError,subprocess.TimeoutExpired) as e:print(str(e) if isinstance(e,GatewayError) else '操作未完成，请按部署文档核对；不重复提交或输出凭据。')

if __name__=='__main__':raise SystemExit(main())
