"""Download directly into the owner's workstation directory; no CC file transport."""
import base64
import re
import json
from urllib.parse import urlsplit
from .core import GatewayError, name_check


REMOTE_HTTP = r'''
def authenticated_request(url):
    parsed=check(url);authorization=r.get('authorization')
    if not authorization:return url
    if (r.get('authorization_host')!=parsed.hostname or not isinstance(authorization,str)
            or not re.fullmatch(r'Basic [A-Za-z0-9+/]+=*',authorization)):
        raise ValueError('INVALID_AUTH_CONFIGURATION')
    request=urllib.request.Request(url)
    request.add_unredirected_header('Authorization',authorization)
    return request

class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,url):
        destination=check(url)
        redirected=super().redirect_request(req,fp,code,msg,headers,url)
        if redirected is None:return None
        # Approved download hosts do not share authentication credentials.
        for mapping in (redirected.headers,redirected.unredirected_hdrs):
            for key in list(mapping):
                if key.lower() in ('authorization','proxy-authorization'):del mapping[key]
        source=urllib.parse.urlsplit(req.full_url)
        authorization=req.get_header('Authorization')
        if (authorization and source.hostname==destination.hostname==r.get('authorization_host')
                and source.scheme==destination.scheme=='https'
                and source.port in (None,443) and destination.port in (None,443)):
            redirected.add_unredirected_header('Authorization',authorization)
        return redirected
'''


REMOTE_FETCH = r'''
import hashlib,json,os,pathlib,re,sys,time,urllib.request,urllib.parse,urllib.error,uuid,base64,signal
r=json.loads(base64.b64decode(sys.argv[1]));tmp=None;alarm=False;previous_handler=None
''' + REMOTE_HTTP + r'''
def fail(code):
    print(json.dumps({'state':'FAILED','code':code}));sys.exit(0)
def check(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or p.hostname not in r['hosts'] or p.username or p.password or p.port not in (None,443):
        raise ValueError('URL_NOT_ALLOWED')
    return p
def hash_file(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    return h.hexdigest()
def check_response(headers,head=None):
    mime=str(headers.get('Content-Type','')).split(';',1)[0].strip().lower()
    if mime in ('text/html','application/xhtml+xml','application/json','text/json') or mime.endswith('+json'):
        raise ValueError('RESPONSE_NOT_ARTIFACT')
    if head is None:return
    if not head:raise ValueError('INCOMPLETE')
    text=(head[3:] if head.startswith(b'\xef\xbb\xbf') else head).lstrip()
    if text.startswith((b'<',b'{',b'[')):raise ValueError('RESPONSE_NOT_ARTIFACT')
    if r['kind']=='package' and not head.startswith(b'\x1f\x8b\x08'):
        raise ValueError('UNEXPECTED_FORMAT')
    if r['kind']=='dependency' and re.search(r'\.so(?:\.[0-9]+)*$',str(r.get('target',''))):
        if not head.startswith(b'\x7fELF'):raise ValueError('UNEXPECTED_FORMAT')
try:
    parsed=check(r['url']);filename=pathlib.PurePosixPath(urllib.parse.unquote(parsed.path)).name
    if not filename or len(filename)>200 or any(c in filename for c in '\\/:*?"<>|\x00\r\n'):raise ValueError('INVALID_FILENAME')
    root=pathlib.Path(r['root'])
    if not root.is_absolute() or any(p.is_symlink() for p in (root,*root.parents)):raise ValueError('INVALID_ROOT')
    root.mkdir(parents=True,exist_ok=True);root=root.resolve(strict=True)
    kind=r['kind']
    if kind not in ('package','dependency'):raise ValueError('INVALID_KIND')
    inbox=root/'inbox'
    if inbox.is_symlink():raise ValueError('INVALID_ROOT')
    inbox.mkdir(exist_ok=True)
    cache=None;source_key=None
    if r.get('reuse_versioned_package'):
        if kind!='package' or parsed.query or parsed.fragment or not re.fullmatch(r'CCU_LIVC_AVM-[A-Za-z0-9_.-]+-[0-9]+\.app\.tar\.gz',filename):
            raise ValueError('INVALID_CACHE_POLICY')
        source_key=hashlib.sha256(r['url'].encode()).hexdigest()
        cache_dir=root/'download-sources'
        if cache_dir.is_symlink():raise ValueError('INVALID_ROOT')
        cache_dir.mkdir(mode=0o700,exist_ok=True)
        cache=cache_dir/(source_key+'.json')
        if cache.is_symlink():raise ValueError('INVALID_ROOT')
        if cache.exists():
            try:
                v=json.loads(cache.read_text());d=v['data'];digest=d['sha256']
                if (v.get('schema')!=1 or v.get('source_key')!=source_key or d.get('filename')!=filename
                        or not re.fullmatch('[0-9a-f]{64}',digest) or type(d.get('size')) is not int
                        or not 0<d['size']<=r['maximum']):raise ValueError()
                folder=root/'package'/digest;final=folder/'asset.bin'
                if d.get('remote_path')!=str(final):raise ValueError()
                if any(p.is_symlink() for p in (root/'package',folder,final)):raise ValueError()
            except Exception:raise ValueError('DOWNLOAD_CACHE_INVALID')
            if final.exists():
                if not final.is_file() or final.stat().st_size!=d['size'] or hash_file(final)!=digest:
                    raise ValueError('EXISTING_CONTENT_CHANGED')
                with final.open('rb') as f:check_response({},f.read(512))
                print(json.dumps({'state':'PASS','data':d,'acquisition':'REUSED'}));sys.exit(0)
    tmp=inbox/('.download-'+uuid.uuid4().hex)
    deadline=time.monotonic()+r['timeout'];total=0;h=hashlib.sha256()
    if hasattr(signal,'setitimer'):
        def expired(*args):raise ValueError('DOWNLOAD_TIMEOUT')
        previous_handler=signal.signal(signal.SIGALRM,expired)
        signal.setitimer(signal.ITIMER_REAL,r['timeout']);alarm=True
    with urllib.request.build_opener(Redirect()).open(authenticated_request(r['url']),timeout=15) as src,tmp.open('xb') as dst:
        length=int(src.headers.get('Content-Length','0'))
        if length>r['maximum']:raise ValueError('TOO_LARGE')
        check_response(src.headers)
        block=src.read(512)
        if len(block)>r['maximum']:raise ValueError('TOO_LARGE')
        check_response(src.headers,block)
        while True:
            if time.monotonic()>deadline:raise ValueError('DOWNLOAD_TIMEOUT')
            if not block:break
            total+=len(block)
            if total>r['maximum']:raise ValueError('TOO_LARGE')
            h.update(block);dst.write(block)
            block=src.read(1048576)
    if not total or (length and total!=length):raise ValueError('INCOMPLETE')
    folder=root/kind/h.hexdigest()
    for p in (root/kind,folder):
        if p.is_symlink():raise ValueError('INVALID_ROOT')
    folder.mkdir(parents=True,exist_ok=True);final=folder/'asset.bin'
    if final.is_symlink():raise ValueError('INVALID_ROOT')
    if final.exists():
        if hash_file(final)!=h.hexdigest():raise ValueError('EXISTING_CONTENT_CHANGED')
    else:
        os.chmod(tmp,0o600);os.replace(tmp,final)
    data={'remote_path':str(final),'sha256':h.hexdigest(),'size':total,'filename':filename}
    if cache is not None:
        data['download_source_sha256']=source_key
        cache_tmp=cache.with_name('.source-'+uuid.uuid4().hex)
        try:
            with cache_tmp.open('x') as f:
                os.chmod(cache_tmp,0o600)
                json.dump({'schema':1,'source_key':source_key,'data':data},f)
            os.replace(cache_tmp,cache)
        finally:
            if cache_tmp.exists():cache_tmp.unlink()
    print(json.dumps({'state':'PASS','data':data,'acquisition':'DOWNLOADED'}))
except urllib.error.HTTPError as exc:fail('AUTH_REQUIRED' if exc.code in (401,403) else 'HTTP_ERROR')
except ValueError as exc:
    code=str(exc)
    fail(code if code in ('URL_NOT_ALLOWED','INVALID_FILENAME','INVALID_ROOT','INVALID_KIND','INVALID_AUTH_CONFIGURATION','TOO_LARGE','DOWNLOAD_TIMEOUT','INCOMPLETE','EXISTING_CONTENT_CHANGED','RESPONSE_NOT_ARTIFACT','UNEXPECTED_FORMAT','INVALID_CACHE_POLICY','DOWNLOAD_CACHE_INVALID') else 'DOWNLOAD_FAILED')
except Exception:fail('DOWNLOAD_FAILED')
finally:
    if alarm:
        signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous_handler)
    if tmp is not None and tmp.exists():tmp.unlink()
'''


def fetch(registry,user,kind,name,url,target=None,timeout=None,progress=None):
    registry.store.require_user(user);name_check(name)
    if kind not in ('package','dependency'):raise GatewayError('大数据集不通过链接下载器或 CC Connect 传输，请接入工作站数据源')
    hosts=registry.cfg.get('download_hosts',[])
    try:
        parsed=urlsplit(url)
        allowed=parsed.scheme=='https' and parsed.hostname in hosts and not parsed.username and not parsed.password and parsed.port in (None,443)
    except ValueError:raise GatewayError('下载链接格式无效，请提供完整 HTTPS 文件链接') from None
    if not allowed:
        raise GatewayError('链接域名尚未获准或不是 HTTPS 下载链接，请管理员配置下载域名；也可提供本人工作站路径或已登记名称')
    if kind=='dependency' and not target:raise GatewayError('依赖需要 APP 内相对目标路径')
    seconds=min(registry.cfg.get('material_download_timeout_seconds',300) if timeout is None else timeout,300)
    if seconds<=0:raise GatewayError('下载时间预算已用完，请重新检查材料')
    request={'root':registry.store.remote_root(user,registry.cfg['remote_users_root']),'kind':kind,'url':url,'hosts':hosts,
             'maximum':min(registry.cfg.get('max_download_bytes',8*1024**3),8*1024**3),'timeout':seconds,'target':target}
    base=registry.cfg.get('livc_app_base_url','')
    filename=parsed.path.rsplit('/',1)[-1]
    request['reuse_versioned_package']=bool(kind=='package' and base and url==base+filename
        and not parsed.query and not parsed.fragment
        and re.fullmatch(r'CCU_LIVC_AVM-[A-Za-z0-9_.-]+-[0-9]+\.app\.tar\.gz',filename))
    from .download_auth import load_authorization
    authorization=load_authorization(registry.cfg,user,parsed.hostname)
    if authorization:
        request.update(authorization=authorization,authorization_host=parsed.hostname)
    encoded=base64.b64encode(json.dumps(request).encode()).decode()
    # SSH.python sends code on stdin. Keep signed links out of remote argv and
    # transport exceptions; base64 is serialization, not secret protection.
    code='import sys\nsys.argv=["fetch",'+repr(encoded)+']\n'+REMOTE_FETCH
    try:result=json.loads(registry.ssh.python(code,timeout=seconds+30))
    except Exception:raise GatewayError('工作站下载未完成，请检查 SSH 和来源访问；不会自动重新下载') from None
    if result.get('state')!='PASS':
        reasons={'AUTH_REQUIRED':'JFrog 登录认证未通过或账号权限不足；请管理员更新工作站下载认证，或通过已授权下载工具放到本人工作站目录再提供路径。不会连续重试登录。',
                 'DOWNLOAD_CACHE_INVALID':'本人软件包下载记录异常，请管理员核对；未重新下载或覆盖文件',
                 'EXISTING_CONTENT_CHANGED':'本人已下载软件包内容发生变化，请管理员核对；未覆盖文件',
                 'TOO_LARGE':'文件超过下载上限','DOWNLOAD_TIMEOUT':'下载超过时限，请先在工作站下载后提供路径',
                 'URL_NOT_ALLOWED':'重定向目标不在允许的下载域名内','INCOMPLETE':'下载内容不完整',
                 'RESPONSE_NOT_ARTIFACT':'来源返回登录页或 HTML/JSON 页面，文件未登记；需要认证时请通过已授权工具下载到本人工作站目录，再提供路径',
                 'UNEXPECTED_FORMAT':'下载内容不是所需 gzip 软件包或 ELF SO，文件未登记；请提供直接文件链接，需要认证时先通过已授权工具下载到本人工作站目录'}
        raise GatewayError(reasons.get(result.get('code'),'工作站下载失败，文件未登记；请检查下载地址或改用工作站路径'))
    if progress:progress('REUSED' if result.get('acquisition')=='REUSED' else 'DOWNLOADED')
    data=result['data']
    if target:data['target']=target
    return registry.record(user,kind,name,data)


REMOTE_PROBE = r'''
import base64,json,re,sys,urllib.request,urllib.parse,urllib.error
r=json.loads(base64.b64decode(sys.argv[1]))
def check(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or p.hostname not in r['hosts'] or p.username or p.password or p.port not in (None,443):
        raise ValueError('URL_NOT_ALLOWED')
    return p
''' + REMOTE_HTTP + r'''
try:
    req=authenticated_request(r['url'])
    if isinstance(req,str):req=urllib.request.Request(req)
    req.add_header('Range','bytes=0-511')
    with urllib.request.build_opener(Redirect()).open(req,timeout=15) as src:
        head=src.read(512);status=src.getcode()
        good=status in (200,206) and head.startswith(b'\x1f\x8b\x08')
        print(json.dumps({'state':'PASS' if good else 'UNEXPECTED_ARTIFACT',
                          'http_status':status,'sample_bytes_read':len(head),
                          'gzip_magic':head.startswith(b'\x1f\x8b\x08')}))
except urllib.error.HTTPError as exc:
    print(json.dumps({'state':'AUTH_REQUIRED' if exc.code in (401,403) else 'HTTP_ERROR','http_status':exc.code}))
except Exception:
    print(json.dumps({'state':'PROBE_FAILED'}))
'''


def probe_download(cfg,user,url):
    """Test real workstation authentication, read at most 512 body bytes, write no assets."""
    from .core import Store
    from .download_auth import load_authorization
    from .transfer import SSH
    Store(cfg['state_root']).require_user(user)
    try:
        parsed=urlsplit(url)
        if (parsed.scheme!='https' or parsed.hostname not in cfg.get('download_hosts',[])
                or parsed.username or parsed.password or parsed.port not in (None,443)):
            raise ValueError()
    except ValueError:raise GatewayError('请提供已批准域名的 HTTPS 软件包链接') from None
    request={'url':url,'hosts':cfg['download_hosts']}
    authorization=load_authorization(cfg,user,parsed.hostname)
    if authorization:request.update(authorization=authorization,authorization_host=parsed.hostname)
    encoded=base64.b64encode(json.dumps(request).encode()).decode()
    code='import sys\nsys.argv=["probe",'+repr(encoded)+']\n'+REMOTE_PROBE
    try:value=json.loads(SSH(cfg).python(code,timeout=30,fresh=True))
    except Exception:raise GatewayError('工作站下载认证检查未完成，请检查 SSH 或来源连接') from None
    state=value.get('state')
    if state not in ('PASS','UNEXPECTED_ARTIFACT','AUTH_REQUIRED','HTTP_ERROR','PROBE_FAILED'):
        raise GatewayError('下载认证检查未返回有效结果')
    result={'state':state,'credentials_configured':bool(authorization),'host':parsed.hostname,
            'download_location':'workstation','maximum_sample_bytes':512,'asset_writes':0}
    for key in ('http_status','sample_bytes_read'):
        if type(value.get(key)) is int:result[key]=value[key]
    if type(value.get('gzip_magic')) is bool:result['gzip_magic']=value['gzip_magic']
    return result
