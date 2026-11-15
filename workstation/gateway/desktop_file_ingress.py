"""Signed desktop file references; all resource bytes and intake state stay on WS."""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import json
from pathlib import Path
import re
import uuid

PREFIX='@@BENCH_FILE@@'
TRAILER='\n\n(Files saved locally, please read them: '
_batch=ContextVar('desktop_file_batch',default=None)


def decode(text,session,app_id,secret):
    if not text.startswith(PREFIX) or len(text)>16000:raise ValueError('附件引用格式不符合要求')
    try:
        raw,signature=text[len(PREFIX):].rsplit('.',1)
        raw=base64.b64decode(raw+'='*(-len(raw)%4),altchars=b'-_',validate=True)
        expected=hmac.new(secret.encode() if isinstance(secret,str) else secret,raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature):raise ValueError()
        value=json.loads(raw)
        keys={'schema','kind','app_id','operator_id','chat_id','message_id','file_key','file_name'}
        if set(value)!=keys or value['schema']!=1 or value['kind']!='file_reference':raise ValueError()
        parts=session.split(':')
        if len(parts)!=3 or parts[0] not in ('feishu','lark'):raise ValueError()
        if value['app_id']!=app_id or value['operator_id']!=parts[2] or value['chat_id']!=parts[1]:raise ValueError()
        if not re.fullmatch(r'ou_[A-Za-z0-9_-]{3,100}',parts[2]):raise ValueError()
        if not re.fullmatch(r'om_[A-Za-z0-9_-]{5,128}',value['message_id']):raise ValueError()
        if any(not isinstance(value[k],str) or not 1<=len(value[k])<=1024 or any(ord(c)<32 for c in value[k]) for k in keys-{'schema'}):raise ValueError()
        name=value['file_name']
        if name in ('.','..') or any(c in name for c in '/\\:,') or len(name.encode())>240:raise ValueError()
        return value
    except (KeyError,TypeError,ValueError):raise ValueError('附件签名、原消息或本人身份核验未通过') from None


def original_message(client,value):
    message=client.message(value['message_id'])
    body=json.loads(message.get('body',{}).get('content','{}'))
    if (message.get('message_id')!=value['message_id'] or message.get('msg_type')!='file'
        or message.get('sender',{}).get('id')!=value['operator_id']
        or message.get('sender',{}).get('sender_type')!='user' or message.get('chat_id')!=value['chat_id']
        or body.get('file_key')!=value['file_key'] or body.get('file_name')!=value['file_name']):
        raise ValueError('附件原消息、文件资源与发送人不匹配，未下载')


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def download(cfg,value,client):
    """One local WS download, serialised by message; unknown leftovers fail closed."""
    original_message(client,value)
    root=Path(cfg['project_root']).resolve()
    target=root/'.cc-connect/attachments'/value['message_id']/value['file_name']
    for path in (target,*target.parents):
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)():raise ValueError('附件目录含链接，未下载')
    metadata=root/'runtime/desktop-file-references';metadata.mkdir(parents=True,exist_ok=True)
    index=metadata/(value['message_id']+'.json');lock=metadata/(value['message_id']+'.lock')
    try:handle=lock.open('x',encoding='utf-8')
    except FileExistsError:raise ValueError('这条附件正在接收或上次接收未确认，请先核对原任务') from None
    try:
        handle.write(uuid.uuid4().hex);handle.close()
        identity={k:v for k,v in value.items() if k!='file_key'}
        identity['file_key_sha256']=hashlib.sha256(value['file_key'].encode()).hexdigest()
        if target.exists():
            if not index.exists():raise ValueError('工作站已有同名接收副本但缺少本次传输收据，未覆盖')
            previous=json.loads(index.read_text(encoding='utf-8'))
            if previous.get('reference')!=identity or previous.get('sha256')!=digest(target):raise ValueError('工作站接收副本与附件引用不一致，未覆盖')
        else:
            if index.exists():raise ValueError('附件收据存在但文件缺失，请管理员核对')
            receipt=client.download(value['message_id'],value['file_key'],target,maximum=int(cfg.get('attachment_max_bytes',128*1024*1024)))
            if not target.is_file() or digest(target)!=receipt['sha256']:raise ValueError('工作站下载校验失败')
            temp=index.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            temp.write_text(json.dumps({'reference':identity,'sha256':receipt['sha256']},ensure_ascii=False),encoding='utf-8')
            temp.chmod(0o600);temp.replace(index)
        return 'Please analyze the attached file(s).'+TRAILER+str(target)+')'
    finally:
        handle.close();lock.unlink()


def current_batch(store,user):
    value=_batch.get()
    if value is None:return None
    if value.user!=user or value.store.root!=store.root:raise ValueError('附件输入批次身份不匹配')
    return value


@contextmanager
def intake(cfg,session,text):
    if not text.startswith(PREFIX):
        yield text;return
    from bench_gateway.card_ui import key
    from bench_gateway.service import Service,identity
    from bench_gateway.tasks import Tasks
    from bench_gateway.incremental import InputBatch
    from bench_gateway.feishu_files import FeishuFiles
    value=decode(text,session,cfg['feishu_app_id'],key(cfg))
    service=Service(cfg);user=identity(session)
    if cfg.get('provision_from_feishu',True):service.store.provision(user)
    else:service.store.require_user(user)
    Tasks(service.store)
    batch=InputBatch(service.store,user,'files:'+value['file_name'].lower())
    token=_batch.set(batch);succeeded=False
    try:
        yield download(cfg,value,FeishuFiles(cfg));succeeded=True
    finally:
        _batch.reset(token)
        if not batch.finished:batch.finish(succeeded)
