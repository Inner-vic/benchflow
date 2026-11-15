"""Host/project-scoped, encrypted read credential for the reviewed M100 source repo."""
import base64
from pathlib import Path
from .core import GatewayError
from .download_auth import _save_basic,_load_authorization

HOST='gitlab.chehejia.com'
PROJECT='livc-acore/chassis/uroad'
REPOSITORY='https://'+HOST+'/'+PROJECT+'.git'
OWNER='team-m100-source'

def credential_path(cfg):
    return Path(cfg['project_root'])/'runtime/source-auth'/OWNER/'gitlab.json'

def save(cfg,token):
    if not isinstance(token,str) or not 16<=len(token)<=4096 or any(c in token for c in '\r\n\0'):
        raise GatewayError('GitLab只读Token格式无效')
    _save_basic(OWNER,credential_path(cfg),HOST,'oauth2',token)

def load(cfg):
    value=_load_authorization(OWNER,credential_path(cfg),HOST)
    if value is None:return None
    try:
        scheme,encoded=value.split(' ',1)
        user,token=base64.b64decode(encoded,validate=True).decode().split(':',1)
        if scheme!='Basic' or user!='oauth2' or not token:raise ValueError()
        return token
    except (ValueError,UnicodeError):raise GatewayError('GitLab源码认证不可用，请重新安全配置') from None
