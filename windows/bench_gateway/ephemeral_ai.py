"""PC-side Codex requests: prompt/results use pipes; no business files or state."""
import json,os,subprocess,queue,threading,time
from pathlib import Path
from .core import GatewayError
from .client_errors import ParserFailure,classify

def run(cfg,kind,prompt,timeout):
    from .parser import command
    from .service import validate_intent
    schema=Path(cfg['project_root'])/'config'/('intent-schema.json' if kind=='intent' else 'yaml-schema.json')
    if kind not in ('intent','yaml') or not schema.is_file():raise GatewayError('解析器静态结构配置缺失。')
    # CLI ephemeral mode and explicit disabled integrations are inherited from
    # the reviewed parser command. stdout contains the result, never a file.
    env={k:v for k,v in os.environ.items() if not k.startswith(('FEISHU_','LARK_','CC_','BENCH_FEISHU_'))}
    env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONUTF8']='1'
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    deadline=time.monotonic()+max(1,min(90,timeout))
    for attempt in range(2):
        # Resolve again per launch; never cache an app's versioned directory.
        argv=command(cfg,schema,Path('unused'),Path(cfg['project_root']))
        i=argv.index('--output-last-message');del argv[i:i+2]
        remaining=deadline-time.monotonic()
        if remaining<=0:raise ParserFailure('CODEX_TIMEOUT')
        try:
            p=subprocess.run(argv,input=prompt,text=True,encoding='utf-8',capture_output=True,timeout=remaining,env=env,**options)
            break
        except FileNotFoundError:
            # The executable vanished before any process or model request began.
            if attempt:raise ParserFailure('PARSER_FAILED') from None
        except subprocess.TimeoutExpired:raise ParserFailure('CODEX_TIMEOUT') from None
    if p.returncode:raise ParserFailure(classify((p.stderr or '')+'\n'+(p.stdout or '')))
    final=None
    for line in p.stdout.splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        item=event.get('item') or {}
        if event.get('type')=='item.completed':
            if item.get('type') not in ('agent_message','reasoning','error'):raise GatewayError('解析器出现非预期工具事件。')
            if item.get('type')=='agent_message':final=item.get('text')
    if not final or len(final)>65536:raise ParserFailure(classify(p.stdout))
    value=json.loads(final)
    return validate_intent(value) if kind=='intent' else value

def packets(stream,timeout=75):
    """Bound a silent SSH pipe as well as TCP failures; memory only."""
    inbox=queue.Queue(maxsize=4);closed=threading.Event()
    def read():
        try:
            while not closed.is_set():
                line=stream.readline(1024*1024+1)
                if not line or len(line)>1024*1024:break
                while not closed.is_set():
                    try:inbox.put(line,timeout=.2);break
                    except queue.Full:pass
        finally:
            if not closed.is_set():
                try:inbox.put(None,timeout=1)
                except queue.Full:pass
    threading.Thread(target=read,daemon=True).start()
    try:
        while True:
            try:line=inbox.get(timeout=timeout)
            except queue.Empty:raise TimeoutError('broker heartbeat missing') from None
            if line is None:return
            value=json.loads(line)
            if not isinstance(value,dict):raise ValueError('invalid broker packet')
            yield value
    finally:closed.set()

def broker_command(cfg,command):
    from .transfer import SSH
    connection_cfg=dict(cfg)
    if cfg.get('desktop_ssh_config'):connection_cfg['ssh_config']=cfg['desktop_ssh_config']
    argv=SSH(connection_cfg).argv();extra=[]
    if cfg.get('desktop_ssh_identity_file'):extra+=['-i',str(Path(cfg['desktop_ssh_identity_file']).resolve(strict=True))]
    if cfg.get('desktop_ssh_known_hosts'):extra+=['-o','UserKnownHostsFile='+str(Path(cfg['desktop_ssh_known_hosts']).resolve(strict=True))]
    return argv[:-1]+extra+[argv[-1],command]


def client_status(cfg,state,**values):
    # Connection metadata only: never persist prompts, results or raw stderr on PC.
    if not cfg.get('project_root'):return
    from .config import save
    try:save(Path(cfg['project_root'])/'runtime/service/parser-connection.json',
             {'state':state,'pid':os.getpid(),'updated':time.time(),**values})
    except OSError:pass


def serve(cfg):
    from desktop_menu_transport import start as start_menu_transport
    start_menu_transport(cfg)
    from .transfer import SSH
    from .config import load
    import sys
    last_error={}
    while True:
        process=None;ssh_errors=[]
        client_status(cfg,'CONNECTING',**last_error)
        try:
            remote=cfg['remote_project_root']
            cmd='cd '+__import__('shlex').quote(remote)+' && '+__import__('shlex').quote(cfg['remote_python'])+' -B -m bench_gateway.ai_broker '+__import__('shlex').quote(remote)
            options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            process=subprocess.Popen(broker_command(cfg,cmd),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',**options)
            def errors(stream=process.stderr):
                for line in iter(lambda:stream.readline(8193),''):
                    for phrase,code in [('Permission denied','AUTH_DENIED'),('Host key verification failed','HOST_KEY'),
                        ('Could not resolve hostname','HOST_ALIAS'),('Connection timed out','CONNECT_TIMEOUT'),
                        ('Connection refused','CONNECT_REFUSED'),('Address already in use','BROKER_BUSY'),
                        ('not accessible','IDENTITY_PATH'),('ModuleNotFoundError','REMOTE_MODULE'),('Traceback','REMOTE_START')]:
                        if phrase in line:ssh_errors[:]=[code]
            threading.Thread(target=errors,daemon=True).start()
            last_status=0
            for packet in packets(process.stdout):
                if packet.get('type') in ('ready','heartbeat','request') and time.time()-last_status>=5:
                    last_error={};client_status(cfg,'CONNECTED',ssh_pid=process.pid);last_status=time.time()
                if packet.get('type')!='request':continue
                answer={'id':packet['id'],'claim':packet['claim']}
                try:
                    # Return errors/results before the workstation expires its request.
                    budget=float(packet['remaining'])-5
                    if budget<=0:raise ParserFailure('CODEX_TIMEOUT')
                    answer['result']=run(cfg,packet['kind'],packet['prompt'],budget)
                except ParserFailure as error:answer['error']=error.code
                except Exception:answer['error']='PARSER_FAILED'
                process.stdin.write(json.dumps(answer,ensure_ascii=False)+'\n');process.stdin.flush()
            raise ConnectionError('broker pipe closed')
        except Exception as exc:
            last_error={'error_type':type(exc).__name__,'ssh_error_category':ssh_errors[-1] if ssh_errors else 'UNCLASSIFIED'}
            client_status(cfg,'DISCONNECTED',**last_error)
        finally:
            if process:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                for stream in (process.stdin,process.stdout,process.stderr):
                    if stream:stream.close()
        time.sleep(3)

if __name__=='__main__':
    import sys
    from .config import load
    serve(load(sys.argv[1]))
