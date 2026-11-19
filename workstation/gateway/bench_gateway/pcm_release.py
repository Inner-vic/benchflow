"""Ordinary APP release specification; read-only preparation, no CI publication."""
import copy
import hashlib
import html
import re
import secrets

from . import pcm_dual_build as legacy
from .m100_auto_inputs import recipe_source, official_eval_mode

SCHEMA = 'pcm-bot-release/v2'
REPOSITORY = 'https://gitlab.chehejia.com/livc-acore/chassis/uroad.git'
JOB = 'https://ci.ep.chehejia.com/job/TRANSIT/job/XCU/job/SCM/job/CCU-ACORE-RELEASE-BUILD/'
ARTIFACT = 'https://artifactory.ep.chehejia.com/artifactory/'
API = 'https://gitlab.chehejia.com/api/v4/projects/livc-acore%2Fchassis%2Furoad'


def is_release(review):
    return review.get('schema') == SCHEMA


def modes(review):
    return ('APP',) if is_release(review) else ('OFF', 'ON')


def binding(reader, proof, source, cmake):
    """Use exact published bytes and the existing immutable-source parser."""
    ref = proof['ref']
    match = re.fullmatch(r'uroad_m100/([A-Za-z0-9_.+-]+)@([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)', ref)
    if (not match or proof.get('repository') not in ('liconan-dev-local', 'liconan-release-local')
            or not re.fullmatch('[a-f0-9]{1,64}', proof.get('recipe_revision', ''))):
        raise ValueError('M100配方来源不完整，请提供本次分支对应的已发布组件版本。')
    path = '/'.join((proof['repository'], match[2], 'uroad_m100', match[1], match[3],
                     proof['recipe_revision'], 'export', 'conanfile.py'))
    raw = reader.get(ARTIFACT+path, 1024*1024)
    if hashlib.sha256(raw).hexdigest() != proof.get('recipe_sha256'):
        raise ValueError('M100配方摘要已变化，请重新校验。')
    try:
        commit = recipe_source(raw)
        enabled, origin = official_eval_mode(raw, cmake)
    except (ValueError, SyntaxError, UnicodeError, TypeError):
        raise ValueError('M100配方未能证明固定源码提交及编译模式，请先通过组件CI准备明确版本。') from None
    if source.get('repository') != REPOSITORY or commit != source.get('commit'):
        raise ValueError('所填M100版本对应的源码与本次分支提交不一致，请提供匹配的组件版本。')
    if enabled:
        raise ValueError('所填M100为评测配置，请选择与本次源码对应的普通版OFF组件。')
    return {'ref': ref, 'recipe_revision': proof['recipe_revision'],
            'recipe_sha256': proof['recipe_sha256'], 'source_commit': commit,
            'cmake_sha256': hashlib.sha256(cmake).hexdigest(),
            'compile_mode': 'OFF', 'mode_origin': origin}


def checked(review):
    """Validate the frozen single-build contract without rewriting old reviews."""
    if (not is_release(review)
            or legacy.digest({k: v for k, v in review.items() if k != 'review_sha256'}) != review.get('review_sha256')
            or set(review.get('parameters', {})) != {'APP'}
            or review.get('job_url') != JOB or review.get('source', {}).get('repository') != REPOSITORY
            or not re.fullmatch('[a-f0-9]{40}', review.get('source', {}).get('commit', ''))
            or review.get('compile_location') != 'jenkins' or review.get('bench_actions') != 0
            or review.get('publication_required') is not False or review.get('payload') or review.get('private_branch')):
        raise ValueError('发包参数快照无效，请重新校验。')
    params = review['parameters']['APP'];proof = review.get('m100_binding', {})
    legacy.release_parameters({k: params.get(k) for k in ('BASE_LINE', 'SOFTWARE_BRANCH', 'BUILDVARIANT')})
    apps = legacy.components(params.get('APP_VERSION', ''), require_required=True)
    libs = legacy.components(params.get('LOCKED_LIBS', ''))
    if (set(apps) & set(libs) or apps['uroad_m100'] != proof.get('ref')
            or proof.get('source_commit') != review['source']['commit'] or proof.get('compile_mode') != 'OFF'
            or params.get('CONTROLLER') != 'CCU_M100_AVMLIVC'
            or params.get('BUILD_REPLAY') is not True or params.get('UPLOAD_ALL_PACKAGE') is not False
            or params.get('SILENT_BUILD') is not False or params.get('RUN_INTE_TEST') is not False
            or any(params.get(k) for k in ('LIWARE_URL', 'LIMESH_URL', 'PORTAL_TRIGGER_URL', 'PORTAL_RESOURCE_KEY', 'NOTIFY_CHAT_ID', 'NOTIFY_USER'))):
        raise ValueError('发包组件或构建范围与确认记录不一致。')
    evidence = review.get('recipe_evidence', {})
    if set(evidence) != set(apps) | set(libs):
        raise ValueError('组件核验记录不完整。')
    for name, item in evidence.items():
        if (item.get('ref') != (apps | libs)[name] or item.get('state') != 'VERIFIED'
                or not re.fullmatch('[a-f0-9]{1,64}', item.get('recipe_revision', ''))
                or not re.fullmatch('[a-f0-9]{64}', item.get('recipe_sha256', ''))):
            raise ValueError('组件来源或修订未固定。')
    m100 = evidence['uroad_m100']
    if (any(proof.get(k) != m100.get(k) for k in ('ref', 'recipe_revision', 'recipe_sha256'))
            or not re.fullmatch('[a-f0-9]{64}', proof.get('cmake_sha256', ''))
            or review.get('manifest', {}).get('components', {}).get('APP', {}).get('ref') != proof['ref']):
        raise ValueError('M100源码证明与配方记录不一致。')
    return review


def make_review(ticket, report, reader, common_inputs):
    discovery = report['discovery']
    common = common_inputs(discovery, ticket['release_parameters'])
    cmake = reader.get(API+'/repository/files/CMakeLists.txt/raw?ref='+ticket_commit(report), 1024*1024)
    if hashlib.sha256(cmake).hexdigest() != discovery['source_files'].get('CMakeLists.txt'):
        raise ValueError('本次源码CMake摘要不一致，请重新校验。')
    bound = binding(reader, report['recipe_evidence']['uroad_m100'], report['source'], cmake)
    params = copy.deepcopy(common['common_parameters'])
    params['APP_VERSION'] = '\n'.join([*report['common_app_refs'], bound['ref']])+'\n'
    params['LOCKED_LIBS'] = '\n'.join(report['common_locked_refs'])+'\n'
    review = {'schema': SCHEMA, 'release_id': 'app-'+ticket['id']+'r'+str(ticket['revision']),
              'owner': ticket['owner'], 'session': ticket['session'], 'draft_id': ticket['id'],
              'revision': ticket['revision'], 'nonce': secrets.token_hex(16),
              'source': copy.deepcopy(report['source']), 'parameters': {'APP': params},
              'compile_location': 'jenkins', 'publication_required': False, 'bench_actions': 0,
              'recipe_evidence': copy.deepcopy(report['recipe_evidence']), 'm100_binding': bound,
              'baseline': copy.deepcopy(report['baseline']), 'job_url': JOB,
              'manifest': {'components': {'APP': {'ref': bound['ref'], 'cmake': {'WITH_EVAL_DUMP': 'OFF'}}}}}
    review['review_sha256'] = legacy.digest(review)
    return checked(review)


def ticket_commit(report):
    commit = report['source']['commit']
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('源码提交尚未固定。')
    return commit


def validate(value, discover, verify_component, reader, common_inputs):
    report = legacy.validate_configuration(value, discover, verify_component)
    if report.get('state') != 'VALIDATED':
        return report
    try:
        report['build_review'] = make_review(value, report, reader, common_inputs)
    except ValueError as error:
        return {'state': 'NEEDS_INPUT', 'feature': legacy.FEATURE,
                'message': str(error), 'discovery': report['discovery']}
    report.update(delivery='app', app_count=1, compile_modes=['OFF'], requested_app_modes=['OFF'])
    return report


def render_input(draft, callback_value):
    card = legacy.render_card(draft, callback_value=callback_value)
    card['header']['title']['content'] = 'Jenkins 发包'
    card['header']['subtitle']['content'] = '配置源码与组件版本，构建普通 APP'
    fields = card['body']['elements'][0]['elements']
    for field in fields:
        if field.get('name') == 'pcm_dual_component_versions' and not draft.get('component_versions'):
            field['default_value'] = legacy.COMPONENT_TEMPLATE.replace('/<版本>@', '/@')
        if field.get('content', '').startswith('**构建产物：**'):
            field['content'] = '**交付：**普通 APP · Lockfile · 构建链接\n校验配置后核对参数并确认发包。'
    return card


def render_review(draft, build_enabled):
    review = checked(draft['report']['build_review']);params = review['parameters']['APP']
    md = lambda value: {'tag': 'markdown', 'content': value}
    def button(action, title, primary=False):
        return {'tag': 'button', 'text': {'tag': 'plain_text', 'content': title},
                'type': 'primary_filled' if primary else 'default', 'disabled': primary and not build_enabled,
                'behaviors': [{'type': 'callback', 'value': {'action': 'bench:pcm_'+action,
                    'task_id': draft['id'], 'nonce': review['nonce'], 'revision': draft['revision']}}]}
    details = [md('**'+key+'**\n'+html.escape(str(value).lower() if type(value) is bool else str(value) or '空'))
               for key, value in params.items()]
    return {'schema': '2.0', 'config': {'update_multi': True, 'enable_forward': False},
            'header': {'template': 'blue', 'title': {'tag': 'plain_text', 'content': 'Jenkins 发包'}},
            'body': {'elements': [md('**请确认发包参数**\nSW'+params['BASE_LINE']+' · ID0111 · '+params['BUILDVARIANT']),
                md('**源码分支** '+html.escape(review['source']['branch'])+'\n**Commit** `'+review['source']['commit']+'`'),
                md('**M100** '+html.escape(review['m100_binding']['ref'])+'\n源码提交已对应，普通版 OFF。'),
                md('确认后向原 Jenkins 提交一次构建，交付普通 APP、Lockfile 和构建链接。正式卡片沿用原 Job 通知配置；台架与 M100 评测准备另行处理。'),
                {'tag': 'collapsible_panel', 'expanded': False,
                 'header': {'title': {'tag': 'plain_text', 'content': '完整 Jenkins 参数'}}, 'elements': details},
                {'tag': 'column_set', 'columns': [
                    {'tag': 'column', 'width': 'weighted', 'weight': 1, 'elements': [button('confirm_build', '确认发包', True)]},
                    {'tag': 'column', 'width': 'weighted', 'weight': 1, 'elements': [button('edit_build', '返回修改')]}]}]}}
