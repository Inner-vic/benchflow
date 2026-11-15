"""Per-task dataset/GT association. Shared by signed sender and cloud receiver."""
import hashlib
import json
from pathlib import Path,PurePosixPath

POLICY='task-dataset-v1'

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()

def cloud_path(raw):
    if not isinstance(raw,str) or len(raw)>2000 or any(x in raw for x in ('\\','\0','\n','\r')):
        raise ValueError('invalid GT path')
    if not raw.startswith('/') or any(p in ('.','..','') for p in raw.split('/')[1:]):raise ValueError('canonical absolute GT path required')
    if raw.startswith('/lpai/volumes/'):raw='/mnt/volumes/'+raw[len('/lpai/volumes/'):]
    if not raw.startswith('/mnt/volumes/'):raise ValueError('mounted cloud dataset required')
    return raw

def source_spec(dataset_root,gt_root=None):
    # A dataset-style root normally contains scene/{original,dataset}; manual
    # workstation datasets may explicitly associate a different GT root.
    root=cloud_path(dataset_root)
    return {'schema':1,'dataset_root':root,'gt_root':cloud_path(gt_root) if gt_root else root}

def validate_binding(binding,profile):
    if not isinstance(binding,dict) or set(binding)!={'schema','dataset_root','gt_root','source_identity'} or binding['schema']!=1:
        raise ValueError('explicit dataset GT binding required')
    roots=[cloud_path(p) for p in profile.get('gt_mount_roots',[])]
    if not roots:raise ValueError('no authorized data mounts')
    for key in ('dataset_root','gt_root'):
        p=cloud_path(binding[key])
        if p!=binding[key] or not any(PurePosixPath(r) in PurePosixPath(p).parents for r in roots):
            raise ValueError('dataset/GT outside authorized mount')
    identity=binding['source_identity']
    if not isinstance(identity,str) or len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity):raise ValueError('dataset identity required')
    return binding

def binding_from_dataset(dataset,profile):
    if not isinstance(dataset,dict) or not dataset.get('remote_path') or not dataset.get('manifest') or not dataset.get('sha256'):
        raise ValueError('frozen dataset source required')
    spec=dataset.get('evaluation_source')
    if not isinstance(spec,dict) or set(spec)!={'schema','dataset_root','gt_root'} or spec['schema']!=1:
        raise ValueError('dataset has no explicit GT association')
    identity={k:dataset[k] for k in ('remote_path','manifest','sha256')}
    binding={**spec,'source_identity':hashlib.sha256(canonical(identity)).hexdigest()}
    return validate_binding(binding,profile)

def runtime_root(request,profile,legacy_root=None):
    if profile.get('gt_policy')!=POLICY:
        if 'gt_binding' in request:raise ValueError('legacy request cannot override GT')
        if legacy_root is None:raise ValueError('saved legacy GT unavailable')
        return Path(legacy_root).resolve(strict=True)
    binding=validate_binding(request.get('gt_binding'),profile)
    root=Path(binding['gt_root']).resolve(strict=True)
    allowed=[Path(r).resolve(strict=True) for r in profile['gt_mount_roots']]
    if not root.is_dir() or not any(r in root.parents for r in allowed):raise ValueError('resolved GT escapes authorized mount')
    return root
