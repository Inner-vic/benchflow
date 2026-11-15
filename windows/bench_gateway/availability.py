"""Read-only workstation/bench readiness probe, before any transaction is dispatched."""
import json
import time
from .transfer import SSH
from .link_diagnostics import REMOTE_CODE,format_network

REMOTE_PROBE = REMOTE_CODE+r'''
import json,pathlib,subprocess,sys,uuid
base=pathlib.Path('/home/lixiang/bench-eval')
result={'probe_kind':'ssh-readonly-v1','workstation_ssh':'PASS','bench_ssh':'NOT_CHECKED','runner_state':'UNKNOWN'}
try:
    sys.path.insert(0,str(base/'current/automation'))
    from ssh_transport import command,environment
    token=uuid.uuid4().hex
    script='import json,socket,platform\nprint(json.dumps({"challenge":'+repr(token)+',"hostname":socket.gethostname(),"system":platform.system()}))\n'
    argv=command('ssh',['-T','-F',str(base/'config/ssh_config'),'-o','StrictHostKeyChecking=yes',
        '-o','ConnectTimeout=5','-o','ConnectionAttempts=1','-o','ControlMaster=no','-o','ControlPath=none'], 'keyboard-interactive-empty')
    response=subprocess.run(argv+['bench-avm','python3 -'],input=script,text=True,capture_output=True,timeout=12,
        env=environment('keyboard-interactive-empty'))
    result['bench_ssh_exit_code']=response.returncode
    if response.returncode:
        error=response.stderr.lower()
        if response.returncode!=255:reason='CHECK_FAILED'
        elif 'remote host identification has changed' in error:reason='HOST_KEY_CHANGED'
        elif 'host key verification failed' in error:reason='HOST_KEY_FAILED'
        elif 'permission denied' in error or 'authentication failed' in error:reason='AUTH_FAILED'
        elif any(x in error for x in ('timed out','connection refused','no route to host','network is unreachable','could not resolve hostname')):reason='UNREACHABLE'
        else:reason='SSH_FAILED'
        result['bench_ssh']=reason
    else:
        receipt=json.loads(response.stdout)
        if receipt.get('challenge')!=token or not receipt.get('hostname') or not receipt.get('system'):raise ValueError('invalid bench receipt')
        result.update(bench_ssh='PASS',bench_host=str(receipt['hostname'])[:128],bench_system=str(receipt['system'])[:32])
except subprocess.TimeoutExpired:result['bench_ssh']='TIMEOUT'
except Exception:result['bench_ssh']='CHECK_FAILED'
try:result['network']=inspect_network(base,result['bench_ssh'] in ('PASS','HOST_KEY_CHANGED','AUTH_FAILED'))
except Exception:result['network']={'state':'UNKNOWN','identity_verified':False}
# A busy or unavailable Runner never suppresses the independent SSH connectivity check.
try:
    q=subprocess.run([str(base/'bin/bench-eval'),'queue'],capture_output=True,text=True,timeout=12)
    if q.returncode:raise ValueError('queue unavailable')
    queue=json.loads(q.stdout)
    if (not isinstance(queue,dict) or type(queue.get('schema_version')) is not int or queue['schema_version']!=1
            or not {'active','queued','waiting'}.issubset(queue)
            or queue['active'] is not None and not isinstance(queue['active'],dict)
            or any(not isinstance(queue[key],list) or any(not isinstance(item,dict) for item in queue[key]) for key in ('queued','waiting'))):
        raise ValueError('invalid queue contract')
    lock=base/'locks/bench.lock'
    busy=queue['active'] is not None or bool(queue['queued'] or queue['waiting']) or lock.exists() or lock.is_symlink()
    result['runner_state']='INVALID_LOCK' if (queue['active'] or {}).get('state')=='INVALID_LOCK' else 'BUSY' if busy else 'IDLE'
except Exception:result['runner_state']='UNKNOWN'
if result['bench_ssh'] in ('TIMEOUT','UNREACHABLE'):result['state']='BENCH_UNREACHABLE'
elif result['bench_ssh']!='PASS':result['state']='BENCH_SSH_FAILED'
elif result['runner_state'] in ('UNKNOWN','INVALID_LOCK'):result['state']='STATUS_UNAVAILABLE'
elif result['runner_state']=='BUSY':result['state']='RESOURCE_BUSY'
else:result['state']='READY'
print(json.dumps(result))
'''

STATES={'READY','RESOURCE_BUSY','BENCH_UNREACHABLE','WORKSTATION_UNAVAILABLE','STATUS_UNAVAILABLE','BENCH_SSH_FAILED'}


def probe(cfg):
    started=time.monotonic()
    try:
        raw=SSH(cfg).python(REMOTE_PROBE,timeout=40,fresh=True)
    except Exception:
        value={'state':'WORKSTATION_UNAVAILABLE','workstation_ssh':'FAILED','bench_ssh':'NOT_CHECKED','runner_state':'UNKNOWN'}
    else:
        try:
            value=json.loads(raw)
            if value.get('probe_kind')!='ssh-readonly-v1' or value.get('state') not in STATES:raise ValueError('invalid readiness response')
            if value.get('state') in ('READY','RESOURCE_BUSY') and (value.get('workstation_ssh')!='PASS' or value.get('bench_ssh')!='PASS'):raise ValueError('SSH receipt absent')
            if value.get('state')=='READY' and value.get('runner_state')!='IDLE':raise ValueError('idle Runner receipt absent')
            if value.get('state')=='RESOURCE_BUSY' and value.get('runner_state')!='BUSY':raise ValueError('busy Runner receipt absent')
        except Exception:
            value={'state':'STATUS_UNAVAILABLE','workstation_ssh':'PASS','bench_ssh':'NOT_VERIFIED','runner_state':'UNKNOWN'}
    return {**value,'checked_at':time.time(),'duration_seconds':round(time.monotonic()-started,2)}


def connection_text(resource):
    from datetime import datetime,timezone,timedelta
    when=resource.get('checked_at')
    if not when:return 'SSH 链路尚未检查。'
    age=max(0,time.time()-when)
    stamp=datetime.fromtimestamp(when,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S UTC+8')
    status={'PASS':'正常（SSH 登录及只读命令成功）','FAILED':'失败（网络、认证或远端命令检查未完成）',
            'UNREACHABLE':'不可达','TIMEOUT':'连接超时','AUTH_FAILED':'认证失败','HOST_KEY_FAILED':'SSH 设备指纹校验失败，需管理员核对设备身份',
            'HOST_KEY_CHANGED':'SSH 服务已响应，但设备指纹与历史记录不一致；需管理员核对设备身份，无需提供登录密码',
            'SSH_FAILED':'SSH 连接失败','CHECK_FAILED':'只读命令或返回核验失败','NOT_CHECKED':'未检查','NOT_VERIFIED':'未验证'}
    lines=[f'SSH 实测时间：{stamp}'+('（已过期，请重新查询）' if age>60 else ''),
        '个人电脑 → 工作站：'+status.get(resource.get('workstation_ssh'),'未验证'),
        '工作站 → 台架：'+status.get(resource.get('bench_ssh'),'未验证'),
        'Runner 占用：'+{'BUSY':'被任务或锁占用','IDLE':'未发现排队任务或锁','UNKNOWN':'未能确认',
                          'INVALID_LOCK':'锁记录损坏，无法核验真实占用；需管理员核对，入口不会自动释放'}.get(resource.get('runner_state'),'未能确认')]
    if resource.get('bench_host') and resource.get('bench_ssh')=='PASS':lines.append('台架返回主机名：'+resource['bench_host'])
    network=format_network(resource.get('network'))
    if network:lines.append(network)
    return '\n'.join(lines)


def description(state):
    return {
        'READY':'两段 SSH 链路已通过只读验证，派发前仍会复核输入并获取 Runner 锁。',
        'RESOURCE_BUSY':'台架被现有任务或 Runner 锁占用，释放并通过核验后自动继续。',
        'BENCH_UNREACHABLE':'工作站可达，但无法连接台架。请联系现场同事（联系人待指定）检查台架是否已物理连接到工作站，并检查供电和网络。仅凭连接失败无法确认具体原因；任务保留等待，恢复后自动继续。',
        'WORKSTATION_UNAVAILABLE':'个人电脑无法完成工作站 SSH 检查，请检查工作站网络或 SSH 认证。尚不能判断工作站与台架之间的物理连接；任务保留等待。',
        'BENCH_SSH_FAILED':'工作站可达，但台架 SSH 认证、主机指纹或只读返回核验未通过；请管理员检查，不据此认定物理断连。任务保留等待。',
        'STATUS_UNAVAILABLE':'台架状态检查未完成，需管理员检查 Runner 或连接配置；任务保留等待。',
        'NEEDS_REVIEW':'前序任务执行结果或恢复状态待核验，暂停自动派发；核验完成后再继续。',
        'RUNNING':'入口已有任务执行中。',
        'UNKNOWN':'台架状态待调度检查。',
    }.get(state,'台架状态待调度检查。')
