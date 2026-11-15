"""Treat forwarded build cards as authenticated source data, never instructions."""
import base64,hashlib,hmac,html,json,re
from urllib.parse import urlsplit,unquote
from .core import GatewayError

PREFIX='@@BENCH_SOURCE@@'

def decode(cfg,session,text,*,secret=None):
    from .card_ui import key
    from .service import identity
    try:
        packet,signature=text[len(PREFIX):].rsplit('.',1)
        raw=base64.urlsafe_b64decode(packet+'='*(-len(packet)%4))
        if len(raw)>24000 or not hmac.compare_digest(hmac.new(secret or key(cfg),raw,hashlib.sha256).hexdigest(),signature):raise ValueError()
        data=json.loads(raw)
        if data['schema']!=1 or data['kind']!='interactive' or data['app_id']!=cfg['feishu_app_id'] or data['operator_id']!=identity(session) or data['chat_id']!=session.split(':')[1]:raise ValueError()
        if not re.fullmatch(r'om_[A-Za-z0-9_-]{5,128}',data['message_id']):raise ValueError()
        if not isinstance(data['content'],str) or len(data['content'].encode())>16000:raise ValueError()
        return data
    except Exception:raise GatewayError('转发卡片的来源核验未通过，请直接复制软件包下载链接。') from None

def extract(text,*,require_app=False):
    """Return only one APP URL and one optional CI build URL; reject ambiguity."""
    links=[]
    for raw in re.findall(r'https://[^\s<>\[\]"\x00]+',html.unescape(text)):
        value=raw.rstrip('),;，；。')
        try:
            p=urlsplit(value)
            if p.scheme!='https' or not p.hostname or p.username or p.password:continue
            if value not in links:links.append(value)
        except ValueError:continue
    apps=[u for u in links if unquote(urlsplit(u).path).lower().endswith('.app.tar.gz')]
    if not apps:
        from .app_sources import is_filename
        names=re.findall(r'(?m)^\s*(?:LIVC\s*APP\s*[:：]\s*)?(CCU_LIVC_AVM-[A-Za-z0-9_.-]+\.app\.tar\.gz)\s*$',text)
        apps=list(dict.fromkeys(n for n in names if is_filename(n)))
    builds=[u for u in links if '/job/' in urlsplit(u).path and re.search(r'/\d+/?$',urlsplit(u).path) and u not in apps]
    if len(apps)>1:return {},'这条卡片含多个 LIVC APP，请只转发本次版本，或直接提供一个 APP 下载链接。'
    if len(builds)>1:return {},'这条卡片含多个构建来源，请明确本次构建链接。'
    if require_app and not apps:return {},'未能提取本次 LIVC APP 下载链接，未沿用旧软件提交。请复制 APP 下载链接，或重新单独转发完整构建卡片。'
    values={}
    if apps:values['package']=apps[0]
    if builds:values['build_reference']=builds[0]
    return values,'' if values else '未能读取完整材料链接，请直接复制 LIVC APP 下载链接和构建链接；已有材料保留。'

def apply(service,user,values):
    """Do not turn the same original source URL into a fresh package alias."""
    from .tasks import Tasks
    from .material_summary import describe
    task=Tasks(service.store).current(user);values=dict(values)
    if task and values.get('package')==describe(service.store,user,task=task).get('software_url'):
        values.pop('package',None)
    if values:change=service.update_inputs(user,values)
    else:change={'unchanged':True}
    # A forwarded release card is an explicit visit, not a background update.
    # Present current inputs at the bottom even within the ordinary idle window.
    service.surface_material_card=True
    return service.material_ack(user,values,change)
