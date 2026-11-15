"""Official asset SDK authentication + direct workstation transfer, no PC data relay."""
import base64,contextlib,io,json,logging,shlex,subprocess,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def remote_code():
    worker=(ROOT/'bench_gateway/cloud_dataset_worker.py').read_text(encoding='utf-8')
    return worker+'''
if __name__=='__main__':
 import sys,logging
 logging.disable(logging.CRITICAL)
 try:
  r=json.load(sys.stdin);auth=r.pop('_auth');import boto3
  from botocore.config import Config
  client=boto3.client('s3',region_name=auth['region_name'],endpoint_url=auth['endpoint_url'],
   aws_access_key_id=auth['aws_access_key_id'],aws_secret_access_key=auth['aws_secret_access_key'],aws_session_token=auth['aws_session_token'],
   config=Config(connect_timeout=10,read_timeout=60,retries={'max_attempts':2,'mode':'standard'},max_pool_connections=16,s3={'addressing_style':auth['addressing_style'],'signature_version':'s3v4'}))
  if r['action']=='discover':
   source=r['source'];loc=r['location'];base=loc['prefix']+source['relative_root'].rstrip('/')+'/'
   _,folders=listing(client,loc['bucket'],base,'/',2000)
   names=[]
   for p in folders:
    name=p[len(base):].rstrip('/')
    if SCENE.fullmatch(name):names.append(name)
    elif re.fullmatch(r'20\\d{2}-\\d{2}-\\d{2}',name):
     _,sub=listing(client,loc['bucket'],p,'/',2000);names.extend(x[len(p):].rstrip('/') for x in sub if SCENE.fullmatch(x[len(p):].rstrip('/')))
   if not names or len(names)!=len(set(names)):raise ValueError('source scene list missing or ambiguous')
   result={'scene_names':sorted(names),'scene_count':len(names)}
  elif r['action']=='plan':result=discover(client,r['location'],r['source'],r['indices'])
  elif r['action']=='prepare':
   result=download(client,r['location'],r['source'],r['plan'],r['owner_root'],max_bytes=r['max_bytes'],workers=r.get('workers',8),shared_root=r.get('shared_root'),
    progress=lambda p:print(json.dumps({'kind':'progress','value':p}),flush=True))
  else:raise ValueError('unsupported import operation')
  print(json.dumps({'kind':'result','ok':True,'data':result}),flush=True)
 except Exception as e:
  message=str(e)[:350] if isinstance(e,ValueError) else 'Cloud source access/transfer failed; inspect this import without replaying hardware'
  print(json.dumps({'kind':'result','ok':False,'error_type':type(e).__name__,'message':message}),flush=True)
  sys.exit(2)
'''

def main():
    logging.disable(logging.CRITICAL)
    request=json.loads(sys.stdin.read(128*1024**2));cfg=request.pop('config')
    from bench_gateway.cloud_dataset_paths import parse_root
    from bench_gateway.transfer import SSH
    volumes=cfg.get('cloud_dataset_volumes') or {}
    source=parse_root(request.pop('source_path'),volumes)
    source['requested_path']=source['evaluation_path']
    policy=volumes[source['volume']]
    if not isinstance(policy,dict) or not policy.get('uid'):raise ValueError('cloud input volume not configured')
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        import asset
        from asset.cli.cmd import _init_config
        from asset.util import parse_s3_path
        volume=asset.resource('volumes/'+policy['uid'],config=_init_config());info=volume.get()
        if info['metadata']['name']!=source['volume']:raise ValueError('volume identity changed')
        spec=info['spec'];spec=json.loads(spec) if isinstance(spec,str) else spec
        loc=parse_s3_path(spec['dir']['path'])
        auths=volume.get_s3_sts(use='download',storage='')
        matching=[a for a in auths.values() if a.get('endpoint_url','').rstrip('/')==loc['endpoint'].rstrip('/')]
        if len(matching)!=1:raise ValueError('ambiguous source credential endpoint')
        auth=matching[0]
    public={'bucket':loc['bucket'],'prefix':loc['path']}
    payload={**request,'source':source,'location':public,'_auth':auth,'_program':remote_code()}
    command='python3 -c '+shlex.quote("import sys,json,io; r=json.load(sys.stdin); code=r.pop('_program'); sys.stdin=io.StringIO(json.dumps(r)); exec(compile(code,'cloud_dataset_worker.py','exec'))")
    process=subprocess.Popen(SSH(cfg).argv()+[command],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        text=True,encoding='utf-8')
    # Neither command arguments nor persisted plans contain the ephemeral STS.
    process.stdin.write(json.dumps(payload));process.stdin.close();payload.pop('_auth',None);auth=None
    result_seen=False
    for line in process.stdout:
        value=json.loads(line)
        if value.get('kind') not in ('progress','result'):raise ValueError('unexpected input transfer message')
        if value['kind']=='result':result_seen=True
        print(json.dumps(value),flush=True)
    rc=process.wait()
    if not result_seen:print(json.dumps({'kind':'result','ok':False,'message':'工作站连接中断，数据准备结果待核验；未派发台架。'}),flush=True)
    return rc

if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        print(json.dumps({'kind':'result','ok':False,'message':'开发空间数据读取或认证未完成；未派发台架。'}),flush=True);sys.exit(2)
