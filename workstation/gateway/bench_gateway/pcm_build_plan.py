"""Frozen dual-APP review and private recipe publication payload; no side effects."""
import ast
import copy
import hashlib
import html
import json
import re
import secrets
from urllib.parse import quote

from .pcm_dual_build import components, digest, release_parameters

PROJECT = 'livc-acore/chassis/uroad'
GITLAB = 'https://gitlab.chehejia.com/api/v4/'
JOB = 'https://ci.ep.chehejia.com/job/TRANSIT/job/XCU/job/SCM/job/CCU-ACORE-RELEASE-BUILD/'
MODES = ('OFF', 'ON')


def checked(review):
    from .pcm_release import is_release, checked as checked_release
    if is_release(review):
        return checked_release(review)
    if (review.get('schema') != 'pcm-bot-release/v1'
            or digest({k: v for k, v in review.items() if k != 'review_sha256'}) != review.get('review_sha256')
            or set(review.get('parameters', {})) != set(MODES)
            or review.get('compile_location') != 'jenkins'
            or review.get('bench_actions') != 0):
        raise ValueError('构建参数快照已变化，请重新校验。')
    if (review.get('job_url') != JOB or review.get('source', {}).get('repository') != 'https://gitlab.chehejia.com/'+PROJECT+'.git'
            or not re.fullmatch(r'codex/pcm-bot-b[a-f0-9]{12}r[1-9][0-9]*', review.get('private_branch', ''))):
        raise ValueError('构建范围不属于本次独立PCM发布。')
    for mode in MODES:
        params = review['parameters'][mode];component = review['manifest']['components'][mode]
        if (params.get('CONTROLLER') != 'CCU_M100_AVMLIVC' or params.get('SOFTWARE_BRANCH') != '0111'
                or params.get('BUILD_REPLAY') is not True or params.get('UPLOAD_ALL_PACKAGE') is not False
                or any(params.get(k) for k in ('LIWARE_URL', 'LIMESH_URL', 'PORTAL_TRIGGER_URL', 'PORTAL_RESOURCE_KEY'))
                or component.get('cmake') != {'WITH_EVAL_DUMP': mode}
                or [v for v in params.get('APP_VERSION', '').splitlines() if v.startswith('uroad_m100/')] != [component['ref']]
                or any(v.startswith('uroad_m100/') for v in params.get('LOCKED_LIBS', '').splitlines())):
            raise ValueError('最终参数不符合AVM双APP发布边界。')
        text = review['payload']['internal_pcm_pair/recipes/'+mode+'/conanfile.py']
        if hashlib.sha256(text.encode()).hexdigest() != component['recipe_sha256']:
            raise ValueError('变体配方与已确认摘要不同。')
    return review


def publication_script(template, branch, commit, release_id, manifest):
    tree = ast.parse(template)
    values = {'PROJECT': PROJECT, 'BRANCH': branch, 'SOURCE': commit,
              'XLIDAR_SOURCE': None, 'PAIR': release_id,
              'EXPECTED': {mode: (item['ref'], item['recipe_sha256'])
                           for mode, item in manifest['components'].items()}}
    lines = template.splitlines(keepends=True)
    spans = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in values:
                spans.append((node.lineno-1, node.end_lineno, name+' = '+repr(values.pop(name))+'\n'))
    if values:
        raise ValueError('受控发布脚本模板不完整。')
    for start, end, value in reversed(spans): lines[start:end] = [value]
    text = ''.join(lines).replace('THREE_REVIEWED_RECIPES_VERIFIED', 'REVIEWED_RECIPES_VERIFIED')
    ast.parse(text)
    return text


def make_review(ticket, report, *, source_recipe, prepare_recipe, publisher_template,
                template_commit, common_inputs, ci_file_exists=True):
    """Jenkins compiles each mode once; no invented local compilation receipts."""
    selected = components(ticket['component_versions'], require_required=True)
    naming = release_parameters(ticket['release_parameters'])
    discovery = report['discovery']
    source = report['source']
    if (source['repository'] != 'https://gitlab.chehejia.com/'+PROJECT+'.git'
            or not re.fullmatch('[a-f0-9]{40}', source['commit'])
            or not re.fullmatch('[a-f0-9]{40}', template_commit)
            or hashlib.sha256(source_recipe.encode()).hexdigest() != discovery['source_files']['conanfile.py']):
        raise ValueError('源码或CI模板来源未对齐。')
    common = common_inputs(discovery, naming)
    if (sorted(common['common_app_refs']) != sorted(report['common_app_refs'])
            or sorted(common['common_locked_refs']) != sorted(report['common_locked_refs'])):
        raise ValueError('集成组件分类与校验结果不一致。')
    release_id = 'b'+ticket['id'][:12]+'r'+str(ticket['revision'])
    branch = 'codex/pcm-bot-'+release_id
    base_version = selected['uroad_m100'].split('/', 1)[1].split('@')[0]
    if len(base_version) > 64:
        raise ValueError('M100基准版本过长，无法生成独立变体。')
    manifest = {'schema': 'pcm-bot-components/v1', 'pair_id': release_id,
                'source_commit': source['commit'], 'xlidar_commit': None, 'components': {}}
    payload, parameters = {}, {}
    adjustments = discovery.get('dependency_adjustments', {})
    if set(adjustments) - {'xlidar'}:
        raise ValueError('未支持的源码依赖替换，请核对配套源码。')
    lidar_ref = None
    if adjustments:
        proof = adjustments['xlidar'];lidar_ref = proof['requested']
        if (proof.get('baseline') != report['baseline']['number']
                or proof.get('lock_sha256') != report['baseline']['lock_sha256']
                or proof.get('locked_ref', '').split('#')[0] != lidar_ref
                or discovery.get('resolved_dependencies', {}).get('xlidar') != lidar_ref
                or selected.get('xlidar') != lidar_ref):
            raise ValueError('雷达替换缺少本次成功基线精确锁定证据。')
    for mode in MODES:
        version = base_version+'-pcm-'+mode.lower()+'.'+release_id
        ref = 'uroad_m100/'+version+'@livc/pcm-'+release_id
        text = prepare_recipe(source_recipe, 'uroad_m100', version, source['commit'], mode, lidar_ref=lidar_ref)
        raw_sha = hashlib.sha256(text.encode()).hexdigest()
        manifest['components'][mode] = {'ref': ref, 'recipe_sha256': raw_sha,
            'source_commit': source['commit'], 'cmake': {'WITH_EVAL_DUMP': mode}}
        payload['internal_pcm_pair/recipes/'+mode+'/conanfile.py'] = text
        params = copy.deepcopy(common['common_parameters'])
        params['APP_VERSION'] = '\n'.join([*report['common_app_refs'], ref])+'\n'
        params['LOCKED_LIBS'] = '\n'.join(report['common_locked_refs'])+'\n'
        parameters[mode] = params
    payload['internal_pcm_pair/recipes/components.json'] = json.dumps(manifest, ensure_ascii=False, indent=2)+'\n'
    payload['internal_pcm_pair/publish_recipes_ci.py'] = publication_script(
        publisher_template, branch, source['commit'], release_id, manifest)
    payload['.gitlab-ci.yml'] = f'''# Isolated Bot publication branch; never merge into a business branch.
include:
  - project: "liconan/conan_ci_template"
    ref: "{template_commit}"
    file: ".gitlab-ci-template.yml"
workflow:
  rules:
    - if: '$CI_PROJECT_PATH == "{PROJECT}" && $CI_COMMIT_BRANCH == "{branch}" && $CI_COMMIT_TAG == null && ($CI_PIPELINE_SOURCE == "api" || $CI_PIPELINE_SOURCE == "web")'
    - when: never
publish-pcm-recipes:
  extends: .conan_upload:base
  stage: deploy
  resource_group: pcm-bot-publication
  interruptible: false
  timeout: 10 minutes
  variables:
    GIT_DEPTH: "20"
    GIT_SUBMODULE_STRATEGY: none
    PYTHONDONTWRITEBYTECODE: "1"
  before_script:
    - set +x
    - !reference [.download_scripts, script]
  script:
    - python3 internal_pcm_pair/publish_recipes_ci.py
  after_script: []
  cache: []
  artifacts:
    when: always
    paths: [pcm-publication-receipt.json]
    expire_in: 30 days
  rules:
    - if: '$CI_PIPELINE_SOURCE == "api" || $CI_PIPELINE_SOURCE == "web"'
'''
    value = {'schema': 'pcm-bot-release/v1', 'release_id': release_id,
        'owner': ticket['owner'], 'session': ticket['session'], 'draft_id': ticket['id'],
        'revision': ticket['revision'], 'nonce': secrets.token_hex(16), 'source': source,
        'private_branch': branch, 'ci_file_exists': ci_file_exists,
        'compile_location': 'jenkins', 'parameters': parameters, 'manifest': manifest,
        'payload': payload, 'basis_components': selected,
        'recipe_evidence': report['recipe_evidence'], 'baseline': report['baseline'],
        'dependency_adjustments': adjustments,
        'integration_package_list_sha256': discovery['integration_package_list_sha256'],
        'template_commit': template_commit, 'job_url': JOB, 'bench_actions': 0}
    value['review_sha256'] = digest(value)
    return value


def prepare_live_review(cfg, ticket, report, reader, *, scripts):
    import sys
    import urllib.error
    from pathlib import Path
    original = list(sys.path)
    try:
        sys.path.insert(0, str(scripts))
        from prepare_pcm_components import recipe
        from source_build_discovery import common_inputs
        source = report['source'];api = GITLAB+'projects/'+quote(PROJECT, safe='')
        source_recipe = reader.get(api+'/repository/files/conanfile.py/raw?ref='+source['commit'], 1024*1024).decode()
        template = json.loads(reader.get(GITLAB+'projects/'+quote('liconan/conan_ci_template', safe='')+
            '/repository/branches/'+quote('develop/livc-vside', safe=''), 1024*1024))
        try:
            reader.get(api+'/repository/files/.gitlab-ci.yml/raw?ref='+source['commit'], 1024*1024)
            exists = True
        except urllib.error.HTTPError as error:
            if error.code != 404: raise
            exists = False
        publisher = Path(scripts, 'publish_recipes_ci.py').read_text()
        return make_review(ticket, report, source_recipe=source_recipe, prepare_recipe=recipe,
            publisher_template=publisher, template_commit=template['commit']['id'],
            common_inputs=common_inputs, ci_file_exists=exists)
    finally:
        sys.path[:] = original


def render_review(draft, *, build_enabled):
    review = checked(draft['report']['build_review'])
    def md(value): return {'tag': 'markdown', 'content': value}
    def callback(action, title, primary=False):
        return {'tag': 'button', 'text': {'tag': 'plain_text', 'content': title},
            'type': 'primary_filled' if primary else 'default',
            'disabled': primary and not build_enabled,
            'behaviors': [{'type': 'callback', 'value': {'action': 'bench:pcm_'+action,
                'task_id': draft['id'], 'nonce': review['nonce'], 'revision': draft['revision']}}]}
    def column(elements):
        return {'tag': 'column', 'width': 'weighted', 'weight': 1, 'elements': elements}
    p = review['parameters']['OFF'];source = review['source']
    details = []
    for name, proof in review.get('dependency_adjustments', {}).items():
        details.append(md('**隔离配方依赖替换 · '+name+'**\n'+html.escape(proof['declared'])+' → '+html.escape(proof['requested'])+'\n依据成功基线 #'+str(proof['baseline'])+' 的精确锁文件；编译兼容性仍待本次构建验证。'))
    for mode in MODES:
        details += [md('**'+mode+' · Jenkins参数**')]
        for key, value in review['parameters'][mode].items():
            shown = '空' if value == '' else str(value).lower() if type(value) is bool else str(value)
            details.append(md('**'+key+'**\n'+html.escape(shown)))
    return {'schema': '2.0', 'config': {'update_multi': True, 'enable_forward': False},
        'header': {'template': 'blue', 'title': {'tag': 'plain_text', 'content': 'PCM 双版本构建'}},
        'body': {'padding': '12px', 'elements': [
            {'tag': 'column_set', 'background_style': 'blue-50', 'flex_mode': 'none', 'columns': [
                {'tag': 'column', 'width': 'weighted', 'weight': 1, 'padding': '8px', 'elements': [
                    md('**请确认最终构建参数**'), md('SW'+p['BASE_LINE']+' · ID0111 · '+p['BUILDVARIANT']+' · 普通版 OFF / 评测版 ON')]}]},
            {'tag': 'column_set', 'flex_mode': 'none', 'columns': [column([
                md('**源码分支**\n'+html.escape(source['branch'])+'\n**Commit** `'+source['commit']+'`'),
                md('**产品基线** #'+str(review['baseline']['number'])+' · '+html.escape(str(review['baseline'].get('product_variant') or '待核对'))),
                md('以所填M100版本为基准生成独立ON/OFF配方，不覆盖stable。确认后由Jenkins编译发包，保留原群通知，并在本私聊交付两张结果卡；不操作台架。')])]},
            {'tag': 'collapsible_panel', 'expanded': False,
             'header': {'title': {'tag': 'plain_text', 'content': '完整参数与ON/OFF组件引用'}}, 'elements': details},
            {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
                column([callback('confirm_build', '确认构建两版 APP', True)]),
                column([callback('edit_build', '返回修改')])]}]}}
