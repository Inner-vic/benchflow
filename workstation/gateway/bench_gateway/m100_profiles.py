"""Package-specific M100 build profiles. No implicit default source or output hash."""
import hashlib,json,re
from pathlib import Path,PurePosixPath
from .core import GatewayError
from .recipes import select

def resolve(cfg,package_sha):
    root=Path(cfg['project_root']);path=root/'config/m100-build-profiles.json'
    profiles=[]
    if path.exists():
        try:
            if path.is_symlink() or path.stat().st_size>262144:raise ValueError()
            catalog=json.loads(path.read_text());assert catalog['schema_version']==1
            profiles=catalog['profiles'];assert isinstance(profiles,list)
            for profile in profiles:
                assert re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}',profile['id'])
                assert profile['profile']=='schu' and profile['build_mode']=='offline-eval-v1'
                assert profile['package_sha256'] and all(re.fullmatch('[a-f0-9]{64}',v) for v in profile['package_sha256'])
                b=profile['build']
                for key in ['source','cache']:
                    p=PurePosixPath(b[key]);assert p.is_absolute() and '..' not in p.parts and str(p)==b[key]
                assert re.fullmatch('[a-f0-9]{40}',b['commit'])
                for key in ['source_diff_sha256','source_lock_sha256','settings_sha256']:assert re.fullmatch('[a-f0-9]{64}',b[key])
                assert re.fullmatch('sha256:[a-f0-9]{64}',b['image'])
                assert b['settings_file']=='config/m100-conan-settings.yml'
                p=root/b['settings_file'];assert not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==b['settings_sha256']
            hits=[p for p in profiles if package_sha in p['package_sha256']]
            assert len(hits)<=1
            if hits:return hits[0]
        except (AssertionError,KeyError,TypeError,ValueError,OSError):
            raise GatewayError('本APP的M100编译配置无效，请管理员核对源码、提交和依赖锁；未使用旧版本配置。') from None
    # Legacy releases remain available only on an exact APP content match.
    return select(cfg,package_sha)
