"""Deterministic LIVC filename expansion against an administrator-pinned source."""
import re
from urllib.parse import urlsplit
from .core import GatewayError

NAME=re.compile(r'CCU_LIVC_AVM-[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.app\.tar\.gz\Z')


def is_filename(value):
    return isinstance(value,str) and '..' not in value and bool(NAME.fullmatch(value.strip()))


def normalize(cfg,value):
    if not isinstance(value,str):return value
    value=value.strip()
    if not value.startswith('CCU_'):return value
    if not is_filename(value):raise GatewayError('请提供完整的 CCU_LIVC_AVM-… .app.tar.gz 文件名，或使用完整下载链接。')
    base=cfg.get('livc_app_base_url','');parsed=urlsplit(base)
    if (parsed.scheme!='https' or parsed.hostname not in set(cfg.get('download_hosts',[])) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not parsed.path.endswith('/app/')):
        raise GatewayError('尚未配置可信的 LIVC 发布目录，请提供完整下载链接。')
    return base+value
