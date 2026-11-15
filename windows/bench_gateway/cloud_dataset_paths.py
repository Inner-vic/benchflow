"""Pure parsing for mounted cloud dataset roots; no remote or production actions."""
from datetime import datetime
from pathlib import PurePosixPath
import re

VOLUME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
SCENE = re.compile(r'(20\d{2}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})_([A-Za-z0-9]{1,64})\Z')


def parse_root(raw, allowed_volumes):
    """Return a volume and relative root, preserving the user's requested path.

    The caller must separately verify permissions, mount identity and existence.
    """
    if not isinstance(raw,str) or raw != raw.strip() or any(x in raw for x in ('\\','\x00','\r','\n')):
        raise ValueError('invalid cloud dataset path')
    if not raw.startswith('/') or '//' in raw or raw.endswith('/') or any(p in ('.','..') for p in raw.split('/')):
        raise ValueError('absolute canonical cloud dataset path required')
    p=PurePosixPath(raw);parts=p.parts
    if parts[:3]==('/','lpai','volumes'): offset=3
    elif parts[:3]==('/','mnt','volumes'): offset=3
    else: raise ValueError('unsupported cloud mount')
    if len(parts)<offset+2:raise ValueError('dataset root below volume required')
    volume=parts[offset]
    if not VOLUME.fullmatch(volume) or volume not in set(allowed_volumes):raise ValueError('volume not authorized')
    relative='/'.join(parts[offset+1:])
    return {'requested_path':raw,'volume':volume,'relative_root':relative,
            'evaluation_path':'/mnt/volumes/'+volume+'/'+relative}


def parse_scene(name):
    if not isinstance(name,str):return None
    m=SCENE.fullmatch(name)
    if not m:return None
    try:stamp=datetime.strptime(m[1]+' '+m[2],'%Y-%m-%d %H-%M-%S')
    except ValueError:return None
    return {'scene':name,'recorded_time':stamp.isoformat(timespec='seconds'),'vin':m[3]}


def select_scenes(names):
    """List canonical scene names; caller decides how to present unknown entries."""
    parsed=[];unknown=[]
    for n in names:
        value=parse_scene(n)
        if value:parsed.append(value)
        else:unknown.append(n)
    if len({x['scene'] for x in parsed})!=len(parsed):raise ValueError('duplicate scene')
    return sorted(parsed,key=lambda x:x['scene']),unknown


def is_cloud_path(raw):
    return isinstance(raw,str) and raw.startswith(('/lpai/volumes/','/mnt/volumes/'))
