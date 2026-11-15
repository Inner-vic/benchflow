"""Administrator-maintained release recipes, never populated from chat messages."""
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from .core import GatewayError


def identity(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False).encode()).hexdigest()


def _sha(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('invalid recipe hash')


def _path(value):
    p=PurePosixPath(value)
    if not p.is_absolute() or '..' in p.parts or str(p)!=value or '\\' in value:raise ValueError('invalid recipe path')


def load(cfg=None):
    root=Path((cfg or {}).get('project_root') or Path(__file__).resolve().parents[1])
    path=root/'config/release-recipes.json'
    try:
        if path.is_symlink() or path.stat().st_size>256*1024:raise ValueError('invalid recipe catalog')
        catalog=json.loads(path.read_text(encoding='utf-8'))
        if catalog['schema_version']!=1 or not isinstance(catalog['recipes'],list):raise ValueError('invalid recipe schema')
        seen=set()
        for recipe in catalog['recipes']:
            if recipe['id'] in seen or not re.fullmatch('[a-z0-9][a-z0-9-]{0,95}',recipe['id']):raise ValueError('invalid recipe id')
            seen.add(recipe['id'])
            if recipe['profile']!='schu' or recipe['build_mode']!='offline-eval-v1':raise ValueError('unsupported build mode')
            for value in recipe['package_sha256']+list(recipe['sensors'].values())+[recipe['m100']['sha256']]:_sha(value)
            if set(recipe['sensors'])!={'camera_so','lidar_so'}:raise ValueError('invalid sensor roles')
            build=recipe['build']
            for key in ('source','cache'):_path(build[key])
            if not re.fullmatch('[a-f0-9]{40}',build['commit']):raise ValueError('invalid source revision')
            for key in ('source_diff_sha256','source_lock_sha256'):_sha(build[key])
            if not re.fullmatch('sha256:[a-f0-9]{64}',build['image']):raise ValueError('invalid image identity')
            if build['settings_file']!='config/m100-conan-settings.yml':raise ValueError('invalid Conan schema path')
            _sha(build['settings_sha256'])
            settings=root/build['settings_file']
            if settings.is_symlink() or settings.stat().st_size>128*1024 or hashlib.sha256(settings.read_bytes()).hexdigest()!=build['settings_sha256']:
                raise ValueError('Conan schema differs')
            assembly=recipe.get('assembly')
            if assembly:
                if assembly['archive_prefix']!='livc/':raise ValueError('unsupported archive prefix')
                _sha(assembly['source_sha256'])
                if assembly.get('output_sha256'):_sha(assembly['output_sha256'])
                if set(assembly['yaml_patches'])!={'etc/uroad/config/common_param.yaml','etc/uroad/config/postprocess_param.yaml'}:raise ValueError('unsupported package patches')
                for target,patch in assembly['yaml_patches'].items():
                    _sha(patch['before_sha256']);_sha(patch['after_sha256'])
                    if not patch['old'] or not patch['new']:raise ValueError('empty package patch')
                if assembly['dependency']['target']!='lib/libvehicle_status.so':raise ValueError('unsupported dependency patch')
                _path(assembly['dependency']['source']);_sha(assembly['dependency']['sha256'])
        return copy.deepcopy(catalog['recipes'])
    except (OSError,ValueError,TypeError,KeyError):
        raise GatewayError('版本配方配置无效，请管理员核对 release-recipes.json；不会猜测源码或依赖版本。') from None


def select(cfg,package_sha256,*,raw=False):
    matches=[r for r in load(cfg) if package_sha256 in r['package_sha256'] or
             (raw and (r.get('assembly') or {}).get('source_sha256')==package_sha256)]
    if len(matches)>1:raise GatewayError('同一软件包匹配多个版本配方，请管理员明确唯一配置。')
    return matches[0] if matches else None


def sensor_match(recipe,user,dependencies):
    return all((dependencies or {}).get(role,{}).get('owner')==user and
               (dependencies or {}).get(role,{}).get('data',{}).get('sha256')==value
               for role,value in recipe['sensors'].items())
