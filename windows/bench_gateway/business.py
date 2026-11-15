"""Deterministic business requirements and owner-scoped task summaries.

This module describes a request. It does not validate files, infer a successful
hardware run, contact a service, or change a stored draft or job.
"""
from copy import deepcopy


SO_TARGETS = {
    'camera_so': 'lib/libxcamera.so',
    'lidar_so': 'lib/libxlidar.so',
    'm100_so': 'lib/liburoad_m100.so',
}
LABELS = {
    'package': '软件包', 'camera_so': '相机 SO', 'lidar_so': '雷达 SO',
    'm100_so': 'M100 SO', 'dataset': '数据集', 'scene_scope': '场景范围',
    'goal': '评测能力', 'lpai_target': 'LPAI 交付', 'yaml_request': 'YAML',
}


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _issue(field, code, message, next_action):
    return {'field': field, 'code': code, 'message': message, 'next_action': next_action}


def missing_inputs(draft):
    """Return source requirements only; a recorded source is not a checked file."""
    draft = draft or {}
    issues = []
    for field in ('package', 'camera_so', 'lidar_so', 'm100_so', 'dataset', 'scene_scope'):
        if _text(draft.get(field)):
            continue
        if field == 'm100_so':
            message = 'M100 尚待准备；选择自动编译后，检查材料时会核对已验收配方。' if draft.get('m100_mode')=='build_if_missing' else '缺少 M100 成品 SO。'
            action = '可先检查材料以准备 M100；版本没有对应配方或编译失败时，请提供匹配的 liburoad_m100.so。'
            code = 'M100_BUILD_REQUIRED' if draft.get('m100_mode') == 'build_if_missing' else 'M100_SO_REQUIRED'
        elif field == 'scene_scope':
            message, action, code = '尚未明确本次场景范围。', '请说明“全部”或具体场景编号，例如 1,3,5-8。', 'SCENE_SCOPE_REQUIRED'
        elif field == 'dataset':
            message, action, code = '尚未提供数据集。', '可选择“共享18场景”；其他数据先接入工作站数据源，再提供名称与范围。', 'DATASET_REQUIRED'
        else:
            message = '尚未提供' + LABELS[field] + '来源。'
            action = '请提供本人已登记名称、工作站绝对路径或可下载的 HTTPS 文件链接。'
            code = field.upper() + '_REQUIRED'
        issues.append(_issue(field, code, message, action))
    return issues


def capability_issues(draft, evaluator=None):
    """State unsupported requested delivery explicitly, without downgrading it."""
    draft = draft or {}
    issues = []
    argv = evaluator.get('argv') if isinstance(evaluator, dict) else None
    valid_evaluator = (isinstance(argv, list) and bool(argv)
                       and all(isinstance(arg, str) and bool(arg.strip()) for arg in argv))
    valid_evaluator = valid_evaluator or (isinstance(evaluator,dict)
        and evaluator.get('kind')=='workstation-offline-v1' and evaluator.get('schema_version')==1)
    if draft.get('goal') == 'replay_and_evaluate' and not valid_evaluator:
        issues.append(_issue('goal', 'EVALUATOR_NOT_READY',
            '本次要求回放后评测，但尚未绑定有效的评测入口。',
            '请管理员接入评测能力，或明确改为“仅回放、不评测”后重新检查材料；不会静默取消评测。'))
    if draft.get('lpai_target') in ('team', 'personal'):
        target = '团队' if draft['lpai_target'] == 'team' else '本人'
        issues.append(_issue('lpai_target', 'LPAI_NOT_READY',
            '本次要求上传 LPAI ' + target + '空间，但统一上传身份、端点与目标权限尚未接入。',
            '请管理员接入上传能力，或明确“取消 LPAI 上传，仅交付工作站目录”后重新检查材料；不会静默降级交付。'))
    return issues


def _baseline_data(baseline):
    return (baseline or {}).get('data') or {}


def _preparation_id(preparation, baseline):
    return (preparation or {}).get('id') or _baseline_data(baseline).get('preparation_id')


def _descriptor(asset, role, source='', frozen=False):
    asset = asset if isinstance(asset, dict) else {}
    data = asset.get('data') or {}
    item = {'role': role, 'label': LABELS.get(role, role), 'source': source,
            'state': 'FROZEN' if frozen and asset else 'RESOLVED' if asset else 'RECORDED' if source else 'MISSING'}
    for field in ('name', 'id', 'version'):
        if field in asset:
            item[field] = deepcopy(asset[field])
    if data.get('sha256'):
        item['sha256'] = data['sha256']
    if role in SO_TARGETS:
        item['target'] = SO_TARGETS[role]
    return item


def _assets(preparation, baseline):
    data = _baseline_data(baseline)
    frozen = data.get('assets') or {}
    prepared = ((preparation or {}).get('result') or {}).get('assets') or {}
    result = {'package': frozen.get('package') or prepared.get('package')}
    for role, target in SO_TARGETS.items():
        matches = [asset for asset in frozen.get('dependencies', [])
                   if (asset.get('data') or {}).get('target') == target]
        result[role] = matches[0] if len(matches) == 1 else prepared.get(role)
    return result


def make_contract(draft, preparation=None, baseline=None, evaluate_after=None, evaluator=None):
    """Freeze a displayable description using the supplied authoritative records."""
    draft = deepcopy(draft or {})
    result = (preparation or {}).get('result') or {}
    data = _baseline_data(baseline)
    goal = draft.get('goal', 'replay')
    if evaluate_after is not None:
        goal = 'replay_and_evaluate' if evaluate_after else 'replay'
    draft['goal'] = goal
    lpai = draft.get('lpai_target', 'none')
    frozen = bool(baseline and data.get('readiness') == 'READY_FOR_GATE')
    if baseline and data.get('kind') != 'candidate-v1' and data.get('gate_verified'):
        frozen = True
    assets = _assets(preparation, baseline)
    materials = [_descriptor(assets.get(role), role, _text(draft.get(role)), frozen)
                 for role in ('package', 'camera_so', 'lidar_so', 'm100_so')]
    dataset_asset = (data.get('assets') or {}).get('dataset') or result.get('dataset') or {}
    dataset = _descriptor(dataset_asset, 'dataset', _text(draft.get('dataset')), frozen)
    dataset_data = dataset_asset.get('data') or {}
    dataset['scope'] = 'shared' if dataset_asset.get('owner') == 'shared' else 'personal'
    for field in ('remote_path', 'manifest', 'scene_count'):
        if field in dataset_data:
            dataset[field] = deepcopy(dataset_data[field])
    if dataset_asset.get('origin'):
        dataset['origin'] = dataset_asset['origin']
    scenes = deepcopy(data.get('scenes') if 'scenes' in data else result.get('scenes', []))
    if not isinstance(scenes, list):
        scenes = []
    yaml = result.get('yaml_preparation') or {}
    outputs = []
    for output in yaml.get('outputs', []):
        outputs.append({key: deepcopy(output[key]) for key in
                        ('target', 'path', 'sha256', 'before_sha256', 'diff_path', 'diff_sha256') if key in output})
    # A supplied authoritative preparation may already resolve references that
    # are absent from a draft (for example when rendering a saved queue item).
    requirements = dict(draft)
    for role, asset in assets.items():
        if asset and not _text(requirements.get(role)):
            requirements[role] = asset.get('id') or asset.get('name') or '已核验材料'
    if dataset_asset and not _text(requirements.get('dataset')):
        requirements['dataset'] = dataset_asset.get('id') or dataset_asset.get('name') or '已核验数据源'
    if scenes and not _text(requirements.get('scene_scope')):
        requirements['scene_scope'] = ','.join(str(scene) for scene in scenes)
    contract = {
        'schema_version': 1,
        'preparation_id': _preparation_id(preparation, baseline),
        'baseline_id': (baseline or {}).get('id'),
        'baseline_name': (baseline or {}).get('name'),
        'input_digest': (data.get('binding') or {}).get('input_digest'),
        'input_state': 'FROZEN' if frozen else 'RECORDED',
        'goal': goal,
        'delivery': {'workstation_directory': True, 'original_txt': goal == 'replay_and_evaluate',
                     'lpai_target': lpai, 'lpai_upload': lpai in ('team', 'personal')},
        'materials': materials,
        'dataset': dataset,
        'scene_scope': _text(draft.get('scene_scope')),
        'scenes': scenes,
        'precheck': {'required': True, 'scene': scenes[0] if scenes else None,
                     'status': 'NOT_VERIFIED', 'full_replay_requires_this_job_pass': True},
        'yaml': {'request': _text(draft.get('yaml_request')), 'state': yaml.get('status', 'NOT_REQUESTED' if not draft.get('yaml_request') else 'PENDING'),
                 'outputs': outputs, 'changes': deepcopy(yaml.get('changes', []))},
        'execution_policy': deepcopy(data.get('execution_policy') or {}),
        'software_name': _text(draft.get('software_name')),
        'start_point': 'saved_assembly' if draft.get('assembly_ref') else 'ccu_app',
        'assembly_ref': _text(draft.get('assembly_ref')),
        'build_reference': _text(draft.get('build_reference')),
        'issues': missing_inputs(requirements) + capability_issues(draft, data.get('evaluator') or evaluator),
        'm100_build': deepcopy(result.get('m100_build') or {}),
        'package_assembly': deepcopy(result.get('package_assembly') or {}),
    }
    return contract


def _asset_label(item):
    name = item.get('name') or item.get('source') or '待提供'
    if item.get('version') is not None:
        name += ' · v' + str(item['version'])
    if item.get('sha256'):
        name += ' · SHA256 ' + item['sha256'][:12]
    return name


def render_contract(contract):
    """Small immutable-input summary reusable in preparation and queue replies."""
    lines = ['本次输入：' + ('已冻结版本' if contract.get('input_state') == 'FROZEN' else '已记录来源，尚待核验')]
    if contract.get('software_name'):
        lines.append('软件名称：' + contract['software_name'])
    if contract.get('assembly_ref'):
        lines.append('起点：本人已验证装配 '+contract['assembly_ref']+'；跳过下载，配置按本次要求重新核验')
    if contract.get('build_reference'):
        lines.append('构建来源：' + contract['build_reference'])
    for item in contract.get('materials', []):
        suffix = ' → ' + item['target'] if item.get('target') else ''
        lines.append(item.get('label', item['role']) + '：' + _asset_label(item) + suffix)
    if contract.get('package_assembly'):
        a=contract['package_assembly']
        lines.append('APP装配：'+str(a.get('recipe',''))+'；原始包保留，已补齐配套依赖和配置。')
        lines.append('装配修改：'+'、'.join(a.get('changed_members',[])))
    if contract.get('m100_build',{}).get('state') in ('BUILT','REUSED'):
        lines.append('M100 来源：'+('复用本人已核验编译产物' if contract['m100_build']['state']=='REUSED' else '工作站固定版本编译')+'；本次仍需台架小场景预检。')
        if contract['m100_build'].get('absent_optional_outputs'):
            lines.append('此构建不含附加输出 input_traj / sus_preview_timeline；依赖这些输出的任务需使用对应版本成品。')
    dataset = contract.get('dataset') or {}
    lines.append('数据集：' + _asset_label(dataset) + ('（共享只读，原位引用）' if dataset.get('scope') == 'shared' else ''))
    scenes = contract.get('scenes') or []
    scope = ','.join(str(scene) for scene in scenes) if scenes else contract.get('scene_scope') or '待明确'
    lines.append('正式场景：' + scope)
    first = (contract.get('precheck') or {}).get('scene')
    lines.append('本次小场景预检：' + ('场景 ' + str(first) if first is not None else '待范围核验后确定') + '；通过后才全量回放')
    lines.append('目标：' + ('回放后评测' if contract.get('goal') == 'replay_and_evaluate' else '仅数据回放'))
    delivery = contract.get('delivery') or {}
    expected = '工作站结果目录' + ('、原始 TXT' if delivery.get('original_txt') else '')
    if delivery.get('lpai_target') in ('team', 'personal'):
        expected += '、LPAI ' + ('团队' if delivery['lpai_target'] == 'team' else '本人') + '空间（上传能力待接入）'
    lines.append('要求交付：' + expected)
    yaml = contract.get('yaml') or {}
    lines.append('用户 YAML：' + (yaml.get('request') or '保留原配置作为输入，未要求额外修改；回放固定配置见下'))
    for output in yaml.get('outputs', [])[:4]:
        lines.append('YAML 候选 ' + output.get('target', '') + '：' + output.get('path', '')
                     + ('；差异：' + output['diff_path'] if output.get('diff_path') else ''))
    policy = contract.get('execution_policy') or {}
    if policy:
        # Keep the policy intelligible without interpreting arbitrary adapter keys.
        summary = policy.get('summary')
        if isinstance(summary, str) and summary.strip():
            lines.append('回放固定配置：' + summary.strip())
        else:
            lines.append('回放固定配置：已随候选绑定；按审核协议应用，用户变更冲突时停止')
        forced = policy.get('fixed_configuration') or policy.get('forced_config') or policy.get('forced_fields') or policy.get('configuration_overrides')
        if isinstance(forced, dict):
            lines.append('固定字段：' + '；'.join(str(key) + '=' + str(value) for key, value in forced.items()))
        elif isinstance(forced, list):
            lines.append('固定字段：' + '；'.join(str(value) for value in forced))
        normalization=policy.get('protocol_normalization')
        if normalization:
            fields=normalization.get('fields') if isinstance(normalization,dict) else None
            hashes=[normalization.get(key) for key in ('before_sha256','after_sha256')] if isinstance(normalization,dict) else []
            if (isinstance(normalization,dict)
                    and type(normalization.get('schema_version')) is int and normalization['schema_version']==1
                    and normalization.get('rule_id')=='sw0058-build2737-m1004-gt-off-v1'
                    and normalization.get('source')=='reviewed-protocol-default'
                    and normalization.get('target')=='etc/uroad/config/common_param.yaml'
                    and isinstance(fields,dict) and set(fields)=={'eval_dump.gt_trajectory.enable'}
                    and fields['eval_dump.gt_trajectory.enable'] is False
                    and len(hashes)==2 and all(isinstance(h,str) and len(h)==64 and all(c in '0123456789abcdef' for c in h) for h in hashes)):
                lines.append('协议配置补齐：本次任务副本新增 eval_dump.gt_trajectory.enable=false；原包不变，属于固定回放配置。')
                lines.append('配置哈希：'+hashes[0][:12]+' → '+hashes[1][:12])
            else:
                lines.append('协议配置补齐记录待核对；不能据此认定已完成。')
        outputs = policy.get('required_output_types') or policy.get('output_types')
        if isinstance(outputs, list) and outputs:
            lines.append('验收产物：' + '、'.join(str(value) for value in outputs))
    else:
        lines.append('回放固定配置：待适配器绑定确认；未要求额外 YAML 修改不等于台架执行原样配置')
    return '\n'.join(lines)


def _matching_jobs(jobs, preparation, baseline):
    preparation_id = _preparation_id(preparation, baseline)
    baseline_id = (baseline or {}).get('id')
    baseline_name = (baseline or {}).get('name')
    owner = (baseline or {}).get('owner') or (preparation or {}).get('owner')
    matched = []
    for job in jobs or ():
        if owner and job.get('owner') and job['owner'] != owner:
            continue
        payload = job.get('payload') or {}
        selected = payload.get('baseline') or {}
        selected_data = selected.get('data') or {}
        contract = payload.get('business_contract') or payload.get('contract') or {}
        job_preparation = selected_data.get('preparation_id') or contract.get('preparation_id')
        if preparation_id:
            if job_preparation != preparation_id:
                continue
            if baseline_id and selected.get('id') and selected['id'] != baseline_id:
                continue
        elif baseline_id:
            if selected.get('id') != baseline_id:
                continue
        elif baseline_name:
            if selected.get('name') != baseline_name:
                continue
        else:
            continue
        matched.append(job)
    return sorted(matched, key=lambda job: (job.get('created') or 0, job.get('updated') or 0, job.get('id', '')), reverse=True)


def render_review(draft, preparation=None, baseline=None, jobs=(), stale=False, evaluator=None):
    """One business task sheet, with one deterministic next step."""
    contract = make_contract(draft, preparation, baseline,evaluator=evaluator)
    if stale:
        # The live draft is authoritative after an edit. Old frozen asset names
        # must not be presented as the inputs the user just changed to.
        contract = make_contract(draft,evaluator=evaluator)
        contract['preparation_id'] = _preparation_id(preparation, baseline)
    result = (preparation or {}).get('result') or {}
    binding = result.get('adapter_binding') or {}
    if not isinstance(binding, dict):
        binding = {}
    current_jobs = _matching_jobs(jobs, preparation, baseline) if not stale else []
    current_job = current_jobs[0] if current_jobs else None
    saved_contract = (current_job or {}).get('payload', {}).get('business_contract')
    if isinstance(saved_contract, dict) and saved_contract.get('schema_version') == 1:
        # An admitted job owns its business intent and effective configuration.
        # Live evaluator or candidate metadata must not rewrite that task sheet.
        contract = deepcopy(saved_contract)
        issues = list(contract.get('issues') or [])
    else:
        issues = list(contract['issues'])
        for issue in result.get('issues', []):
            if isinstance(issue, dict) and issue.get('message') and not any(issue.get('field') == old.get('field') and issue.get('code') == old.get('code') for old in issues):
                issues.append(issue)
    state = (preparation or {}).get('state')
    job_state = current_job.get('state') if current_job else None
    if stale:
        phase, next_step = '材料已变化，旧检查及绑定已过期', '发送“检查材料”，按当前材料重新核验并绑定。'
    elif current_job:
        phases = {'QUEUED': '就绪排队中', 'RUNNING': '任务执行中', 'POSTPROCESSING': '回放完成，等待或正在评测',
                  'SUCCEEDED': '任务完成', 'PARTIAL_SUCCESS': '回放已保留，后处理未完成', 'SIMULATED': '模拟验证完成，未执行台架',
                  'FAILED': '任务失败，已停止', 'NEEDS_REVIEW': '执行结果待核验', 'CANCELLED': '等待任务已取消'}
        phase = phases.get(job_state, '任务状态待核对')
        if job_state in ('QUEUED', 'RUNNING', 'POSTPROCESSING'):
            next_step = '查询任务 ' + current_job['id'] + '；状态变化会回复原私聊。'
        elif job_state == 'NEEDS_REVIEW':
            next_step = '请管理员核验本任务 Runner、恢复及产品收据后续接；不要重新提交回放。'
        elif job_state == 'PARTIAL_SUCCESS':
            next_step = '请管理员处理本任务未完成的后处理并续接交付；已完成的回放不要重新执行。'
        elif job_state == 'FAILED':
            next_step = '按失败原因补充或修正本次材料，再发送“检查材料”；已完成的回放结果应续接后处理。'
        elif job_state == 'SUCCEEDED':
            next_step = '使用已交付结果；后续直接提供新材料，系统自动归入下一次任务。'
        elif job_state == 'SIMULATED':
            next_step = '等待管理员完成真实执行验收；本次没有真实回放结果。'
        elif job_state == 'CANCELLED':
            next_step = '确认仍需同一输入时，引用已取消任务编号重新提交。'
        else:
            next_step = '请管理员核对本任务记录。'
    elif state in ('QUEUED', 'RUNNING'):
        phase = '等待材料检查' if state == 'QUEUED' else '工作站材料检查中'
        next_step = '发送“查看准备状态”；本次检查不占用台架。'
    elif state == 'NEEDS_INPUT':
        phase, next_step = '材料检查需要补充或修正', '按以下问题补充材料，再发送“检查材料”。'
    elif binding.get('state') == 'BINDING':
        phase, next_step = '静态检查通过，正在离线绑定候选', '发送“查看准备状态”；当前尚不能提交台架执行。'
    elif binding.get('state') == 'NEEDS_INPUT':
        phase, next_step = '静态检查通过，适配器绑定未完成', '按绑定原因修正材料或联系管理员，再发送“检查材料”。'
        issues.append(_issue('adapter', 'BINDING_NOT_READY', binding.get('message') or '本次适配器尚未绑定。', '重新检查本次材料并核验协议要求。'))
    elif issues:
        phase, next_step = '材料或所选交付能力未就绪', '补齐以下阻塞项；修改目标或交付要求后发送“检查材料”。'
    elif baseline and contract['input_state'] == 'FROZEN':
        phase, next_step = '候选已绑定，待提交本次小场景预检', '发送“执行回放”；先跑本任务小场景预检，通过才正式全量回放。'
    elif state == 'STATIC_PASSED':
        phase, next_step = '静态检查通过，待绑定适配器', '请管理员完成本次候选绑定；不能用静态检查代替小场景预检。'
    else:
        phase, next_step = '材料来源已记录，待检查', '发送“检查材料”，在工作站核验并生成本次候选。'
    lines = ['本次台架任务单', '阶段：' + phase]
    if not preparation and not baseline:
        lines.append('草稿仅记录来源（尚未下载/编译/入队）。')
    if contract.get('preparation_id'):
        lines.append('材料检查：' + contract['preparation_id'])
    if current_job:
        lines.append('任务：' + current_job['id'])
    lines.append(render_contract(contract))
    if baseline:
        lines.append('候选就绪只表示可提交，不表示本任务小场景预检或实机兼容已通过。')
    if issues:
        lines.append('阻塞项：')
        for issue in issues[:12]:
            label = LABELS.get(issue.get('field'), issue.get('field', '材料'))
            lines.append(label + '：' + issue.get('message', '') + ('；' + issue['next_action'] if issue.get('next_action') else ''))
    if current_job:
        job_result = current_job.get('result') or {}
        if job_result.get('stage'):
            lines.append('执行阶段：' + str(job_result['stage']))
        if job_result.get('error'):
            lines.append('处理说明：' + str(job_result['error']))
        if job_state != 'SIMULATED':
            if job_result.get('task'):
                lines.append('已保留工作站结果：' + str(job_result['task']))
            if job_result.get('report'):
                lines.append('原始 TXT：' + str(job_result['report']))
    lines.append('下一步：' + next_step)
    return '\n'.join(lines)
