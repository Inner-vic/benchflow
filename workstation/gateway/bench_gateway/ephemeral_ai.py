"""PC-side Codex requests: prompt/results use pipes; no business files or state."""
import json,os,subprocess
from pathlib import Path
from .core import GatewayError

def run(cfg,kind,prompt,timeout):
    from .parser import command
    from .service import validate_intent
    schema=Path(cfg['project_root'])/'config'/('intent-schema.json' if kind=='intent' else 'yaml-schema.json')
    if kind not in ('intent','yaml') or not schema.is_file():raise GatewayError('解析器静态结构配置缺失。')
    argv=command(cfg,schema,Path('unused'),Path(cfg['project_root']))
    i=argv.index('--output-last-message');del argv[i:i+2]
    # CLI ephemeral mode and explicit disabled integrations are inherited from
    # the reviewed parser command. stdout contains the result, never a file.
    env={k:v for k,v in os.environ.items() if not k.startswith(('FEISHU_','LARK_','CC_','BENCH_FEISHU_'))}
    env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONUTF8']='1'
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    p=subprocess.run(argv,input=prompt,text=True,encoding='utf-8',capture_output=True,timeout=max(1,min(90,timeout)),env=env,**options)
    if p.returncode:raise GatewayError('Codex解析失败。')
    final=None
    for line in p.stdout.splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        item=event.get('item') or {}
        if event.get('type')=='item.completed':
            if item.get('type') not in ('agent_message','reasoning','error'):raise GatewayError('解析器出现非预期工具事件。')
            if item.get('type')=='agent_message':final=item.get('text')
    if not final or len(final)>65536:raise GatewayError('解析器未返回有效结果。')
    value=json.loads(final)
    return validate_intent(value) if kind=='intent' else value

def serve(cfg):
    from .transfer import SSH
    from .config import load
    import sys,time
    while True:
        process=None
        try:
            remote=cfg['remote_project_root']
            cmd='cd '+__import__('shlex').quote(remote)+' && '+__import__('shlex').quote(cfg['remote_python'])+' -B -m bench_gateway.ai_broker '+__import__('shlex').quote(remote)
            options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            process=subprocess.Popen(SSH(cfg).argv()+[cmd],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',**options)
            for line in process.stdout:
                packet=json.loads(line)
                if packet.get('type')!='request':continue
                answer={'id':packet['id'],'claim':packet['claim']}
                try:answer['result']=run(cfg,packet['kind'],packet['prompt'],packet['remaining']-1)
                except Exception:answer['error']='PARSER_FAILED'
                process.stdin.write(json.dumps(answer,ensure_ascii=False)+'\n');process.stdin.flush()
        except Exception:pass
        finally:
            if process and process.poll() is None:process.terminate()
        time.sleep(3)

if __name__=='__main__':
    import sys
    from .config import load
    serve(load(sys.argv[1]))
