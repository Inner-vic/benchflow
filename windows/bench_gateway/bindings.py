"""Bind immutable, owner-scoped prepared inputs to an operator-reviewed adapter.

A candidate is ready for this job's small-scenario gate. It is never evidence
that a previous gate or a real hardware run has passed.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re

from .core import GatewayError, name_check, remote_within
from .intake import Intake


KIND = 'candidate-v1'
READINESS = 'READY_FOR_GATE'
SO_FIELDS = ('camera_so', 'lidar_so', 'm100_so')
IMPLEMENTATION_NAMES = ('candidate_files', 'candidate_adapter', 'candidate_remote', 'batch')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _identity(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _prepared_identity(preparation, assets):
    return _identity({'assets':assets,'scenes':preparation['result']['scenes'],
                      'yaml_preparation':preparation['result'].get('yaml_preparation') or {}})


def _sha(value):
    return isinstance(value, str) and bool(re.fullmatch('[a-f0-9]{64}', value))


def _implementation_hashes(value):
    if (not isinstance(value, dict) or set(value) != set(IMPLEMENTATION_NAMES)
            or any(not _sha(value[name]) for name in IMPLEMENTATION_NAMES)):
        raise GatewayError('候选绑定缺少完整实现身份，请重新检查材料并绑定，旧记录不能直接执行。')
    return dict(value)


def implementation_snapshot(root):
    """Hash only the fixed, installed administrator sources; never load code."""
    try:
        root = Path(root)
        paths = {name: root / 'bench_gateway' / (name + '.py')
                 for name in IMPLEMENTATION_NAMES if name != 'batch'}
        paths['batch'] = root / 'vendor/run_stream_batch.py'
        paths['provenance'] = root / 'vendor/provenance.json'
        raw = {}
        for name, path in paths.items():
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('missing or unsupported administrator source')
            raw[name] = path.read_bytes()
        provenance = json.loads(raw.pop('provenance').decode('utf-8-sig'))
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()}
        expected = provenance['run_stream_batch.py']['sha256']
        if not _sha(expected) or hashes['batch'] != expected:
            raise ValueError('batch provenance differs')
        return _implementation_hashes(hashes)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError('本机候选实现或回放入口校验失败，请管理员核对部署后重新检查材料，暂不能受理执行。') from None


def current_preparation(cfg, store, user, preparation_id=None, allow_other_task=False):
    from .preparation import PreparationQueue, fingerprint
    store.require_user(user)
    queue = PreparationQueue(cfg, store)
    latest = queue.latest(user)
    if preparation_id:
        with store.connect() as connection:
            row = connection.execute('SELECT * FROM preparations WHERE owner=? AND id=?',
                                     (user, preparation_id)).fetchone()
        if not row:
            raise GatewayError('只能绑定本人存在的材料检查记录。')
        preparation = store.unpack(row)
    else:
        preparation = latest
    from .tasks import Tasks
    tasks=Tasks(store)
    request=tasks.get(user,preparation['request_id']) if preparation and preparation.get('request_id') else None
    if request:
        focus=tasks.current(user)
        if not allow_other_task and (not focus or focus['id']!=request['id']):
            raise GatewayError('此材料检查属于本人另一个任务，请先切换到对应任务。')
        is_latest=request['prep_id']==preparation['id']
    else:is_latest=bool(preparation and latest and preparation['id']==latest['id'])
    if not preparation or not is_latest:
        raise GatewayError('请先检查当前最新材料；旧材料检查不能作为本次绑定。')
    if preparation['state'] != 'STATIC_PASSED' or (preparation.get('result') or {}).get('status') != 'STATIC_PASSED':
        raise GatewayError('最新材料检查尚未静态通过，请补齐材料并重新检查。')
    draft = request['data'] if request else Intake(store).get(user)
    if (not draft or fingerprint(draft) != preparation['fingerprint']
            or fingerprint(preparation['payload']) != preparation['fingerprint']):
        raise GatewayError('本次草稿已经变化，请重新检查材料后再绑定适配器。')
    return preparation


def prepared_assets(preparation, user):
    result = preparation['result']
    materials = result.get('assets') or {}
    try:
        package = materials['package']
        dependencies = [materials[field] for field in SO_FIELDS]
        dataset = result['dataset']
        for asset in [package, *dependencies]:
            if asset['owner'] != user:
                raise GatewayError('不能绑定其他用户的软件包或依赖。')
        if package['kind'] != 'package' or any(asset['kind'] != 'dependency' for asset in dependencies):
            raise GatewayError('材料检查中的软件包或依赖类别无效。')
        if (dataset['kind'] != 'dataset' or not (dataset['owner'] == user or
                (dataset['owner'] == 'shared' and dataset.get('origin') == 'dataset_catalog'))):
            raise GatewayError('只能绑定本人数据源或已授权的共享数据源。')
        for asset in [package, dataset, *dependencies]:
            if (not isinstance(asset['id'], str) or not asset['id'] or not isinstance(asset['name'], str)
                    or type(asset['version']) is not int or asset['version'] < 1
                    or not isinstance(asset['data'], dict) or not _sha(asset['data'].get('sha256'))):
                raise GatewayError('材料检查缺少完整的资产版本或文件哈希，请重新检查。')
        if len({asset['id'] for asset in dependencies}) != len(SO_FIELDS):
            raise GatewayError('相机、雷达和 M100 必须分别绑定对应的依赖文件。')
        scenes = result['scenes']
        count = dataset['data']['scene_count']
        if (type(count) is not int or count <= 0 or not isinstance(scenes, list) or not scenes
                or any(type(scene) is not int or not 1 <= scene <= count for scene in scenes)
                or scenes != sorted(set(scenes))):
            raise GatewayError('材料检查中的场景范围无效，请重新检查。')
        yaml = result.get('yaml_preparation') or {}
        if preparation['payload'].get('yaml_request') and (yaml.get('status') != 'PREPARED' or not yaml.get('outputs')):
            raise GatewayError('本次 YAML 要求尚未生成经核验的任务副本，不能忽略变更绑定。')
    except (KeyError, TypeError, AttributeError):
        raise GatewayError('材料检查结果不完整，请重新检查软件包、三项依赖和数据源。') from None
    # JSON cloning freezes values without sharing mutable dictionaries with callers.
    return json.loads(_canonical({'package': package, 'dataset': dataset, 'dependencies': dependencies}))


def asset_ids(assets):
    return {'package': assets['package']['id'], 'dataset': assets['dataset']['id'],
            'dependencies': sorted(asset['id'] for asset in assets['dependencies'])}


def _binding_receipt(cfg, store, user, receipt, template):
    if not isinstance(receipt, dict) or receipt.get('state') != READINESS:
        raise GatewayError('工作站未返回待预检绑定收据，不能将静态准备标记为台架验证通过。')
    if (not _sha(receipt.get('sha256')) or not _sha(receipt.get('input_digest'))
            or not isinstance(template.get('template_id'), str) or not template['template_id']
            or receipt.get('template_id') != template['template_id']):
        raise GatewayError('绑定收据哈希或适配器模板身份不匹配。')
    implementation = _implementation_hashes(receipt.get('implementation_hashes'))
    try:
        current_implementation = implementation_snapshot(cfg['project_root'])
    except KeyError:
        raise GatewayError('本机候选实现部署位置缺失，请管理员核对后重新检查材料。') from None
    if implementation != current_implementation:
        raise GatewayError('候选适配器实现已更新，请重新检查材料并绑定；本次不会进入台架队列。')
    try:
        path = remote_within(store.remote_root(user, cfg['remote_users_root']), receipt['path'])
    except (KeyError, TypeError):
        raise GatewayError('绑定收据没有有效的本人工作站路径。') from None
    return {'path': path, 'sha256': receipt['sha256'], 'input_digest': receipt['input_digest'],
            'template_id': receipt['template_id'], 'implementation_hashes': implementation}


def bind_preparation(cfg, store, user, template_name=None, name=None, preparation_id=None):
    """Operator/preparation entry; never accepts templates or shell commands from chat."""
    preparation = current_preparation(cfg, store, user, preparation_id,allow_other_task=True)
    assets = prepared_assets(preparation, user)
    selected = template_name or cfg.get('default_adapter_template')
    templates = cfg.get('adapter_templates') or {}
    if not selected or not isinstance(templates, dict) or not isinstance(templates.get(selected), dict):
        raise GatewayError('尚未配置可用的回放适配器模板，请管理员配置后绑定。')
    template = json.loads(_canonical(templates[selected]))
    if not isinstance(template.get('template_id'), str) or not template['template_id']:
        raise GatewayError('适配器模板缺少固定身份，请管理员核验配置。')
    baseline_name = name or 'prepared-' + preparation['id']
    name_check(baseline_name)
    from .executor import Executor
    receipt = Executor(cfg, store).bind_candidate(user, assets, preparation, template)
    binding = _binding_receipt(cfg, store, user, receipt, template)
    # The SSH operation can take time; never attach its result to a changed draft.
    current = current_preparation(cfg, store, user, preparation['id'],allow_other_task=True)
    if _canonical(current['result']) != _canonical(preparation['result']):
        raise GatewayError('材料检查结果在绑定过程中变化，请重新检查并绑定。')
    data = {'kind': KIND, 'readiness': READINESS, 'gate_verified': False,
            'preparation_id': preparation['id'], 'preparation_fingerprint': preparation['fingerprint'],
            'prepared_identity_sha256': _prepared_identity(preparation, assets),
            'asset_ids': asset_ids(assets), 'assets': assets, 'scenes': preparation['result']['scenes'],
            'binding': binding,
            'execution_policy': json.loads(_canonical(receipt.get('execution_policy') or {})),
            'template': {'name': selected, 'template_id': template['template_id'],
                         'source_run': template.get('source_run', ''), 'config_sha256': _identity(template)}}
    if cfg.get('default_evaluator'):
        from .workstation_evaluation import validate_descriptor
        validate_descriptor(cfg,cfg['default_evaluator'])
        data['evaluator']=json.loads(_canonical(cfg['default_evaluator']))
    with store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        rows = [store.unpack(row) for row in connection.execute('SELECT * FROM baselines WHERE owner=?', (user,))]
        for existing in rows:
            old = existing['data']
            if old.get('kind') == KIND and (old.get('binding') or {}).get('input_digest') == binding['input_digest']:
                # A fresh preparation owns a fresh immutable record even if its content
                # digest is identical. Reusing the old record would fail the latest-ID check.
                if old.get('preparation_id') != preparation['id']:
                    continue
                # A remote digest collision or inconsistent receipt must not replace a saved baseline.
                if _canonical(old) != _canonical(data):
                    raise GatewayError('相同绑定输入返回了不同的适配记录，请管理员核验收据。')
                return existing
        if any(existing['name'] == baseline_name for existing in rows):
            raise GatewayError('绑定名称已存在且对应不同输入；请使用新名称，旧绑定不会被覆盖。')
    # add_baseline enforces the unique owner/name key during concurrent calls.
    try:
        return store.add_baseline(user, baseline_name, data)
    except GatewayError:
        existing = store.baseline(user, baseline_name)
        if _canonical(existing['data']) == _canonical(data):
            return existing
        raise


def validate_candidate(cfg, store, user, baseline, allow_other_task=False):
    """Verify the local frozen input identity; dispatch rechecks remote hashes."""
    if baseline.get('owner') != user:
        raise GatewayError('不能使用其他用户的候选适配器。')
    data = baseline.get('data') or {}
    if (data.get('evaluator') or {}).get('kind')=='workstation-offline-v1':
        from .workstation_evaluation import validate_descriptor
        validate_descriptor(cfg,data['evaluator'])
    if data.get('kind') != KIND or data.get('readiness') != READINESS or data.get('gate_verified') is not False:
        raise GatewayError('候选适配器不是待本次小场景预检状态，不能提交。')
    preparation = current_preparation(cfg, store, user, data.get('preparation_id'),allow_other_task=allow_other_task)
    assets = prepared_assets(preparation, user)
    if (data.get('preparation_id') != preparation['id']
            or data.get('preparation_fingerprint') != preparation['fingerprint']
            or data.get('asset_ids') != asset_ids(assets)
            or _canonical(data.get('assets')) != _canonical(assets)
            or data.get('prepared_identity_sha256') != _prepared_identity(preparation, assets)
            or data.get('scenes') != preparation['result']['scenes']):
        raise GatewayError('候选适配器与当前材料、版本或场景不一致，请重新检查并绑定。')
    binding = data.get('binding') or {}
    template = data.get('template') or {}
    _binding_receipt(cfg, store, user, {**binding, 'state': data['readiness']}, template)
    configured = (cfg.get('adapter_templates') or {}).get(template.get('name'))
    if (not isinstance(configured, dict) or configured.get('template_id') != template.get('template_id')
            or _identity(configured) != template.get('config_sha256')):
        raise GatewayError('适配器模板已变化，请重新绑定当前材料。')
    return assets, list(data['scenes'])


def candidate_summary(cfg, store, user, baseline):
    data = baseline['data']
    summary = {'name': baseline['name'], 'kind': KIND, 'asset_ids': data.get('asset_ids', {}),
               'gate_verified': False, 'readiness': data.get('readiness', 'UNKNOWN'),
               'preparation_id': data.get('preparation_id'), 'scenes': data.get('scenes', [])}
    try:
        validate_candidate(cfg, store, user, baseline)
        summary['current'] = True
    except GatewayError:
        summary['current'] = False
        summary['readiness'] = 'RECHECK_REQUIRED'
    return summary


def match_selection(assets, scenes, intent, text):
    """Use saved assets; explicit selections may confirm but never replace them."""
    from .dataset_sources import reference
    for field in ('package', 'dataset'):
        requested = intent.get(field)
        if not requested:
            continue
        asset = assets[field]
        aliases = {asset['id'], asset['name'], reference(asset)}
        if field == 'dataset' and requested == '共享18场景':
            requested = '共享:18场景'
        if requested not in aliases:
            raise GatewayError('本条选择的软件包或数据集与已绑定材料不同，请更新材料并重新检查绑定。')
    requested = intent.get('dependencies') or []
    if requested:
        selected = []
        for name in requested:
            matches = [asset for asset in assets['dependencies'] if name in (asset['id'], asset['name'])]
            if len(matches) != 1:
                raise GatewayError('本条依赖选择与已绑定材料不同，请重新检查并绑定。')
            selected.append(matches[0]['id'])
        if sorted(selected) != sorted(asset['id'] for asset in assets['dependencies']):
            raise GatewayError('本条依赖组合与已绑定材料不同，请重新检查并绑定。')
    requested_scenes = intent.get('scenes') or []
    if requested_scenes and requested_scenes != scenes:
        raise GatewayError('本条场景范围与已绑定材料不同，请更新场景后重新检查绑定。')
    explicit_all = bool(re.search(r'(?:全部|所有)(?:的)?\s*场景|(?:回放|回灌|执行)\s*(?:全部|所有)|(?:全部|所有)\s*(?:回放|回灌)', text))
    if explicit_all and scenes != list(range(1, assets['dataset']['data']['scene_count'] + 1)):
        raise GatewayError('本条要求全部场景，但候选只绑定部分场景；请更新场景后重新检查绑定。')
