"""Independent PCM dual-APP input, per-user defaults and validation state.

No Jenkins submission, publication, bench dispatch or main gateway DB migration.
Callback authentication and message identity checks belong to the host service.
"""
import hashlib
import html
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import secrets
import sqlite3
import uuid
import urllib.error
from urllib.parse import unquote, urlsplit

FEATURE = 'pcm_dual_app'
REF = re.compile(r'([A-Za-z0-9_-]+)/[A-Za-z0-9_.+-]+@[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
COMPONENT_TEMPLATE = '\n'.join([
    'conan_shared/<版本>@livc/release-schu',
    'uroad_m100/<版本>@livc/stable',
    'dag_compiler/<版本>@livc/stable'])
TEMPLATE_ROWS = frozenset(COMPONENT_TEMPLATE.splitlines())
REQUIRED_COMPONENTS = ('conan_shared', 'uroad_m100', 'dag_compiler')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def components(text, *, require_required=False):
    if not isinstance(text, str) or len(text) > 1000:
        raise ValueError('组件配置超过卡片的1000字符限制，请精简；不会截断保存。')
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line in TEMPLATE_ROWS:
            raise ValueError('请填写必选组件的精确版本：'+line.split('/')[0]+'。')
        match = REF.fullmatch(line)
        if not match:
            raise ValueError('第'+str(number)+'行请填写精确组件引用：名称/版本@用户/通道。')
        name = match[1]
        if name in result:
            raise ValueError('组件重复：'+name+'，请每个组件只保留一行。')
        result[name] = line
    if require_required:
        missing = [name for name in REQUIRED_COMPONENTS if name not in result]
        if missing:
            raise ValueError('请补齐必选组件：'+'、'.join(missing)+'。')
    return result


def branch_link(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError('源码分支地址无效。')
    parsed = urlsplit(value.strip())
    if (parsed.scheme != 'https' or parsed.hostname != 'gitlab.chehejia.com'
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.fragment or parsed.query not in ('', 'ref_type=heads')
            or '/-/tree/' not in parsed.path):
        raise ValueError('请填写公司GitLab的完整分支链接。')
    project, branch = unquote(parsed.path).strip('/').split('/-/tree/', 1)
    if (len(project.split('/')) < 2 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ('.', '..') for p in project.split('/'))
            or not branch or branch.startswith(('-', '.', '/')) or branch.endswith(('.', '/', '.lock'))
            or any(p in branch for p in ('..', '//', '@{')) or re.search(r'[\s~^:?*\[\\\x00-\x1f]', branch)):
        raise ValueError('源码分支地址无效，请重新复制GitLab分支链接。')
    return value.strip()


def release_parameters(value):
    """Explicit release identity; never inherit naming from a historical baseline."""
    keys = {'BASE_LINE', 'SOFTWARE_BRANCH', 'BUILDVARIANT'}
    if (not isinstance(value, dict) or not {'BASE_LINE', 'BUILDVARIANT'} <= set(value) <= keys
            or any(not isinstance(value.get(k), str) for k in ('BASE_LINE', 'BUILDVARIANT'))):
        raise ValueError('请填写本次软件版本和构建类型；不会沿用历史软件版本。')
    version = value['BASE_LINE'].strip().upper().removeprefix('SW')
    software_id = value.get('SOFTWARE_BRANCH') or '0111'
    if not isinstance(software_id, str):
        raise ValueError('AVM构建的软件ID固定为0111，请重新打开构建卡。')
    software_id = software_id.strip().upper().removeprefix('ID')
    variant = value['BUILDVARIANT'].strip()
    if not re.fullmatch(r'[0-9A-F]{4}', version):
        raise ValueError('软件版本须为4位十六进制字符，可带SW前缀。')
    if software_id != '0111':
        raise ValueError('AVM构建的软件ID固定为0111，请重新打开构建卡。')
    if variant not in ('release', 'debug'):
        raise ValueError('请选择有效的构建类型：release或debug。')
    return {'BASE_LINE': version, 'SOFTWARE_BRANCH': software_id, 'BUILDVARIANT': variant}


def validate_configuration(value, discover, verify_component):
    """Injected discovery/recipe readers must be read-only. Never start a build."""
    try:
        url = branch_link(value['branch_url'])
        selected = components(value['component_versions'], require_required=True)
        release = release_parameters(value.get('release_parameters'))
    except (ValueError, KeyError) as error:
        return {'state': 'NEEDS_INPUT', 'message': str(error), 'feature': FEATURE}
    request = {'repository': url, 'component_overrides': list(selected.values())}
    if value.get('baseline_build') is not None:
        request['baseline_build'] = value['baseline_build']
    source = discover(request)
    if source.get('state') != 'RESOLVED_FOR_REVIEW':
        messages = {
            'DEPENDENCY_OVERRIDE_CONFLICT': '组件版本与本次源码声明不匹配，请修改组件版本或选择配套分支。',
            'NEEDS_BASELINE_SELECTION': '构建基线存在歧义，需要明确选择；输入已保留。',
            'BASELINE_SEARCH_INCOMPLETE': '有界搜索尚未确定基线，需要明确选择；输入已保留。',
            'NO_VERIFIED_BASELINE': '未找到匹配的成功基线，请核对源码和组件版本。',
            'DEPENDENCY_RESOLUTION_REQUIRED': '部分依赖声明无法自动锁定，需要核对精确版本。',
            'REPOSITORY_COMPATIBILITY_CHECK_REQUIRED': '该仓库尚未适配此构建流程，请核对仓库或联系维护者。'}
        return {'state': 'NEEDS_INPUT', 'message': messages.get(source.get('state'), '源码或基线尚未对齐，请核对构建配置。'),
                'feature': FEATURE, 'discovery': source}
    project, expected_branch = unquote(urlsplit(url).path).strip('/').split('/-/tree/', 1)
    expected_repo = 'https://gitlab.chehejia.com/' + project.removesuffix('.git') + '.git'
    identity = source.get('source', {})
    if (identity.get('repository') != expected_repo or identity.get('branch') != expected_branch
            or not re.fullmatch(r'[a-f0-9]{40}', identity.get('commit', ''))
            or source.get('component_overrides') != selected
            or digest({k: v for k, v in source.items() if k != 'discovery_sha256'}) != source.get('discovery_sha256')):
        return {'state': 'NEEDS_INPUT', 'feature': FEATURE,
                'message': '源码或组件修改的解析证据不一致，请重新校验。'}
    proof = source.get('resolved_dependencies', {})
    effective = dict(proof)
    shared = [line.strip() for line in source['baseline']['parameters']['APP_VERSION'].splitlines()
              if line.strip().startswith('conan_shared/')]
    if len(shared) != 1:
        raise ValueError('基线缺少唯一conan_shared引用。')
    effective['conan_shared'] = shared[0].split('#')[0]
    effective.update(selected)
    receipts = {}
    for name, ref in effective.items():
        receipt = verify_component(ref)
        if (receipt.get('ref') != ref or receipt.get('state') != 'VERIFIED'
                or not re.fullmatch(r'[a-f0-9]{1,64}', receipt.get('recipe_revision', ''))
                or not re.fullmatch(r'[a-f0-9]{64}', receipt.get('recipe_sha256', ''))):
            reason = '该版本存在多个配方修订，需要明确来源：' if receipt.get('state') == 'NEEDS_REVISION_SELECTION' else '组件来源未核验：'
            return {'state': 'NEEDS_INPUT', 'message': reason+name,
                    'feature': FEATURE, 'discovery': source}
        receipts[name] = receipt
    roots = set(source['integration_package_names'])
    m100_ref = effective.pop('uroad_m100')
    app_refs, locked_refs = [effective.pop('conan_shared')], []
    for name, ref in effective.items():
        (app_refs if name in roots else locked_refs).append(ref)
    # Keep the editable field limited to user-selected components. Discovered
    # transitive dependencies stay in the report instead of expanding the form.
    text = '\n'.join(selected.values())
    components(text, require_required=True)
    return {'state': 'VALIDATED', 'feature': FEATURE, 'delivery': 'app_pair',
            'compile_modes': ['OFF', 'ON'], 'requested_app_modes': ['OFF', 'ON'], 'app_count': 2,
            'source': source['source'], 'baseline': source['baseline'],
            'release_parameters': release,
            'effective_component_versions': text, 'common_app_refs': app_refs,
            'm100_recipe_ref': m100_ref, 'm100_recipe_evidence': receipts['uroad_m100'],
            'm100_source_commit': source['source']['commit'],
            'common_locked_refs': locked_refs, 'recipe_evidence': receipts,
            'discovery_sha256': source['discovery_sha256'],
            'discovery': source,
            'message': '构建配置校验完成，待确认最终参数；尚未编译或发包。',
            'compile_runtime_compatibility': 'NOT_VALIDATED',
            'jenkins_submitted': False, 'bench_tasks': 0}


class Drafts:
    """Use a separate Linux business-state file; no imports from the bench queue."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('配置状态文件不能是链接。')
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        with self.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS pcm_dual_defaults(
              owner TEXT PRIMARY KEY,component_versions TEXT NOT NULL,confirmation TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS pcm_dual_drafts(
              id TEXT PRIMARY KEY,owner TEXT NOT NULL,session TEXT NOT NULL,revision INTEGER NOT NULL,
              nonce TEXT NOT NULL,state TEXT NOT NULL,branch_url TEXT NOT NULL,component_versions TEXT NOT NULL,
              attempt TEXT,last_revision INTEGER,input_sha TEXT,report TEXT)''')
            if 'baseline_build' not in {r['name'] for r in c.execute('PRAGMA table_info(pcm_dual_drafts)')}:
                c.execute('ALTER TABLE pcm_dual_drafts ADD COLUMN baseline_build INTEGER')
            if 'release_parameters' not in {r['name'] for r in c.execute('PRAGMA table_info(pcm_dual_drafts)')}:
                c.execute("ALTER TABLE pcm_dual_drafts ADD COLUMN release_parameters TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def unpack(row):
        value = dict(row)
        value['report'] = json.loads(value['report']) if value['report'] else None
        value['release_parameters'] = json.loads(value.get('release_parameters') or '{}')
        return value

    def open(self, owner, session):
        if not isinstance(owner, str) or not owner or not isinstance(session, str) or not session:
            raise ValueError('缺少已认证的用户或会话。')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT * FROM pcm_dual_drafts WHERE owner=? AND session=? AND state!='CONFIRMED' ORDER BY rowid DESC LIMIT 1", (owner, session)).fetchone()
            if row:
                return self.unpack(row)
            previous = c.execute('SELECT component_versions FROM pcm_dual_defaults WHERE owner=?', (owner,)).fetchone()
            text = previous['component_versions'] if previous else ''
            key = uuid.uuid4().hex
            c.execute('INSERT INTO pcm_dual_drafts(id,owner,session,revision,nonce,state,branch_url,component_versions,attempt,last_revision,input_sha,report) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (key, owner, session, 1, secrets.token_hex(16), 'DRAFT', '', text, None, None, None, None))
            return self.unpack(c.execute('SELECT * FROM pcm_dual_drafts WHERE id=?', (key,)).fetchone())

    @staticmethod
    def owned(c, owner, session, key, nonce):
        row = c.execute('SELECT * FROM pcm_dual_drafts WHERE id=? AND owner=? AND session=?', (key, owner, session)).fetchone()
        if not row or not secrets.compare_digest(row['nonce'], nonce):
            raise ValueError('卡片不属于当前用户/会话，或已失效。')
        return row

    def begin_validation(self, owner, session, key, nonce, revision, branch_url, component_versions, baseline_build=None, release=None):
        if type(revision) is not int or any(not isinstance(x, str) or len(x)>1000 for x in (branch_url, component_versions)):
            raise ValueError('输入无效或超过卡片字符限制；原配置保留。')
        if baseline_build is not None and (type(baseline_build) is not int or baseline_build < 1):
            raise ValueError('构建基线编号无效。')
        release = release_parameters(release)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self.owned(c, owner, session, key, nonce)
            if row['branch_url'] and row['branch_url'] != branch_url:
                baseline_build = None
            fingerprint = digest({'branch_url': branch_url, 'component_versions': component_versions,
                                  'baseline_build': baseline_build, 'release_parameters': release})
            if row['state'] != 'CONFIRMED' and row['last_revision'] == revision and row['input_sha'] == fingerprint:
                return self.unpack(row), False
            if row['revision'] != revision or row['state'] in ('CONFIRMED', 'VALIDATING'):
                raise ValueError('卡片已更新或正在校验，请使用当前卡片。')
            c.execute("UPDATE pcm_dual_drafts SET revision=revision+1,state='VALIDATING',branch_url=?,component_versions=?,attempt=?,last_revision=?,input_sha=?,report=NULL,baseline_build=?,release_parameters=? WHERE id=?",
                      (branch_url, component_versions, uuid.uuid4().hex, revision, fingerprint, baseline_build, json.dumps(release), key))
            return self.unpack(c.execute('SELECT * FROM pcm_dual_drafts WHERE id=?', (key,)).fetchone()), True

    def finish_validation(self, owner, session, key, revision, attempt, report):
        if report.get('state') not in ('VALIDATED', 'NEEDS_INPUT'):
            raise ValueError('无效的校验结果。')
        if report['state'] == 'VALIDATED':
            single = (report.get('delivery') == 'app' and report.get('app_count') == 1
                      and report.get('compile_modes') == ['OFF']
                      and report.get('build_review', {}).get('schema') == 'pcm-bot-release/v2')
            pair = (report.get('delivery') == 'app_pair' and report.get('app_count') == 2
                    and report.get('compile_modes') == ['OFF', 'ON'])
            if (report.get('feature') != FEATURE or not (single or pair)
                    or report.get('jenkins_submitted') is not False or report.get('bench_tasks') != 0):
                raise ValueError('结果不属于本次软件构建校验，不能保存。')
            if single:
                from .pcm_release import checked
                checked(report['build_review'])
            components(report['effective_component_versions'], require_required=True)
            release_parameters(report.get('release_parameters'))
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM pcm_dual_drafts WHERE id=? AND owner=? AND session=?', (key, owner, session)).fetchone()
            if not row or row['revision'] != revision or row['attempt'] != attempt or row['state'] != 'VALIDATING':
                return False
            if report['state'] == 'VALIDATED' and report['release_parameters'] != json.loads(row['release_parameters']):
                raise ValueError('本次发包编号与冻结输入不一致，不能保存校验结果。')
            c.execute('UPDATE pcm_dual_drafts SET state=?,report=? WHERE id=?',
                      (report['state'], json.dumps(report, ensure_ascii=False), key))
            if report['state'] == 'VALIDATED':
                c.execute('UPDATE pcm_dual_drafts SET component_versions=? WHERE id=?',
                          (report['effective_component_versions'], key))
            return True

    def confirm_configuration(self, owner, session, key, nonce, revision, report_sha):
        """Only call from authenticated final confirmation. Saves defaults, starts nothing."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self.owned(c, owner, session, key, nonce)
            report = json.loads(row['report']) if row['report'] else {}
            if row['revision'] != revision or digest(report) != report_sha or row['state'] not in ('VALIDATED', 'CONFIRMED'):
                raise ValueError('校验结果已变化，请重新核对并确认。')
            if row['state'] == 'CONFIRMED':
                return False
            if release_parameters(report.get('release_parameters')) != json.loads(row['release_parameters']):
                raise ValueError('本次发包编号尚未核对，请重新校验。')
            text = report['effective_component_versions'];components(text, require_required=True)
            c.execute('INSERT INTO pcm_dual_defaults VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET component_versions=excluded.component_versions,confirmation=excluded.confirmation', (owner, text, key))
            c.execute("UPDATE pcm_dual_drafts SET state='CONFIRMED' WHERE id=?", (key,))
            return True


def live_drafts(state_root):
    root = Path(state_root).resolve()
    if os.name == 'nt' or Path('/mnt/data/bench-eval') not in root.parents:
        raise ValueError('PCM构建配置只能存于Linux工作站业务目录。')
    return Drafts(root/'pcm-dual-build.sqlite3')


def run_validation(store, ticket, discover, verify_component, prepare_review=None, *, validator=None):
    """One read-only validation unit; stale results cannot replace newer input."""
    try:
        report = (validator or validate_configuration)(ticket, discover, verify_component)
        if report.get('state') == 'VALIDATED' and prepare_review is not None:
            report['build_review'] = prepare_review(ticket, report)
    except Exception as error:
        message = '配置校验暂未完成，请稍后重新校验；已保留输入。'
        if isinstance(error, TimeoutError):
            message = '远端配置查询超时，已停止本轮校验；输入已保留，请稍后重新校验。'
        elif isinstance(error, urllib.error.HTTPError):
            host = urlsplit(error.url).hostname
            system = {'gitlab.chehejia.com': 'GitLab', 'ci.ep.chehejia.com': 'Jenkins',
                      'artifactory.ep.chehejia.com': 'JFrog'}.get(host, '远端服务')
            if error.code in (401, 403):
                message = system+'读取认证失效或权限不足，请管理员检查认证配置；输入已保留。'
            elif error.code == 404:
                message = system+'中未找到所需分支、构建记录或组件，请核对输入；已保留配置。'
            else:
                message = system+'读取失败（HTTP '+str(error.code)+'），请稍后重新校验；输入已保留。'
        elif isinstance(error, urllib.error.URLError):
            message = '远端服务暂时不可达，请稍后重新校验；输入已保留。'
        report = {'state': 'NEEDS_INPUT', 'feature': FEATURE,
                  'message': message,
                  'error_type': type(error).__name__}
    accepted = store.finish_validation(ticket['owner'], ticket['session'], ticket['id'],
                                      ticket['revision'], ticket['attempt'], report)
    return report, accepted


def render_card(draft, *, callback_value=None):
    """Native card. Disabled when no host-authenticated callback binding is supplied."""
    branch, text = draft.get('branch_url', ''), draft.get('component_versions', '')
    if any(not isinstance(v, str) or len(v)>1000 for v in (branch, text)):
        raise ValueError('卡片输入超出显示上限，不会截断。')
    enabled = callback_value is not None and draft.get('state') in ('DRAFT', 'NEEDS_INPUT', 'VALIDATED')
    if callback_value is not None:
        expected = {'feature': FEATURE, 'action': 'validate_build_config', 'draft_id': draft['id'],
                    'revision': draft['revision'], 'nonce': draft['nonce']}
        if callback_value != expected:
            raise ValueError('校验按钮绑定与当前草稿不一致。')
    button = {'tag': 'button', 'name': 'validate_build_config',
              'text': {'tag': 'plain_text', 'content': '校验构建配置'}, 'type': 'primary_filled',
              'form_action_type': 'submit', 'width': 'fill', 'disabled': not enabled}
    if enabled:
        # Native form submit omits action.value. The existing connector decodes
        # this name; the dedicated route checks feature nonce + original message
        # before the legacy material-confirmation handler can see the event.
        button.update(form_action_type='submit', name='bench_confirm_'+draft['nonce']+'_'+str(draft['revision']), width='fill')
    else:
        button['disabled_tips'] = {'tag': 'plain_text', 'content': '正在校验' if draft.get('state') == 'VALIDATING' else '本次配置已确认' if draft.get('state') == 'CONFIRMED' else '功能预览，尚未接入生产回调'}
    fields = [
        {'tag': 'input', 'name': 'pcm_dual_branch_url', 'label': {'tag': 'plain_text', 'content': '源码分支地址'},
         'label_position': 'top', 'input_type': 'text', 'width': 'fill', 'required': draft.get('state') != 'VALIDATING',
         'disabled': draft.get('state') == 'VALIDATING',
         'placeholder': {'tag': 'plain_text', 'content': '请输入 GitLab 分支链接'}, 'default_value': branch}]
    release = draft.get('release_parameters') or {}
    for name, key, label, hint in [
            ('pcm_dual_release_version', 'BASE_LINE', '软件版本', '本次版本，4位十六进制字符；可带SW前缀')]:
        fields.append({'tag': 'input', 'name': name, 'label': {'tag': 'plain_text', 'content': label},
            'label_position': 'top', 'input_type': 'text', 'width': 'fill', 'required': draft.get('state') != 'VALIDATING',
            'max_length': 6, 'disabled': draft.get('state') == 'VALIDATING',
            'placeholder': {'tag': 'plain_text', 'content': hint}, 'default_value': release.get(key, '')})
    fields.append({'tag': 'markdown', 'content': '**构建类型**', 'text_size': 'notation'})
    fields.append({'tag': 'select_static', 'name': 'pcm_dual_build_variant', 'width': 'fill', 'required': draft.get('state') != 'VALIDATING',
        'placeholder': {'tag': 'plain_text', 'content': '构建类型'},
        'initial_option': release.get('BUILDVARIANT', 'release'),
        'options': [{'text': {'tag': 'plain_text', 'content': v}, 'value': v} for v in ('release', 'debug')],
        'disabled': draft.get('state') == 'VALIDATING'})
    fields += [
        {'tag': 'input', 'name': 'pcm_dual_component_versions', 'label': {'tag': 'plain_text', 'content': '组件版本配置'},
         'label_position': 'top', 'input_type': 'multiline_text', 'rows': 3, 'width': 'fill', 'required': draft.get('state') != 'VALIDATING',
         'disabled': draft.get('state') == 'VALIDATING',
         'placeholder': {'tag': 'plain_text', 'content': '请填写三个必选组件的精确版本；其他组件可追加'}, 'default_value': text or COMPONENT_TEMPLATE},
        {'tag': 'markdown', 'content': '以上三项必填，其他组件可在末尾追加；每行一个完整引用。', 'text_size': 'notation'},
        {'tag': 'markdown', 'content': '**构建产物：**普通版 OFF · 评测版 ON\n校验后展示最终参数，确认构建后才开始。'}]
    report = draft.get('report') or {}
    if draft.get('state') == 'VALIDATED':
        naming = report.get('release_parameters')
        if naming:
            product = report.get('baseline', {}).get('product_variant')
            summary = '**本次输出：**SW'+naming['BASE_LINE']+' · ID'+naming['SOFTWARE_BRANCH']+' · '+naming['BUILDVARIANT']
            if product: summary += ' · '+str(product)
            fields.append({'tag': 'markdown', 'content': html.escape(summary), 'text_size': 'notation'})
    candidates = report.get('discovery', {}).get('candidate_baselines', [])
    if report.get('state') == 'NEEDS_INPUT' and candidates:
        options = [{'text': {'tag': 'plain_text', 'content': '#'+str(b['number'])+' · '+str(b.get('product_variant') or '待核对')},
                    'value': str(b['number'])} for b in candidates[:40]]
        choice = {'tag': 'select_static', 'name': 'pcm_dual_baseline', 'width': 'fill', 'required': draft.get('state') != 'VALIDATING',
                  'placeholder': {'tag': 'plain_text', 'content': '请选择匹配的构建基线'}, 'options': options}
        current = str(draft.get('baseline_build') or '')
        if current in {o['value'] for o in options}: choice['initial_option'] = current
        fields += [{'tag': 'markdown', 'content': '**构建基线（需确认）**', 'text_size': 'notation'}, choice]
    if draft.get('state') == 'VALIDATING':
        fields.append({'tag': 'markdown', 'content': '正在校验源码、基线与组件引用。', 'text_size': 'notation'})
    elif draft.get('report'):
        message = draft['report'].get('message', '')
        if not isinstance(message, str) or len(message) > 300:
            raise ValueError('校验摘要无效。')
        fields.append({'tag': 'markdown', 'content': html.escape(message), 'text_size': 'notation'})
    fields.append(button)
    return {'schema': '2.0', 'config': {'update_multi': True, 'enable_forward': False},
            'header': {'template': 'blue', 'title': {'tag': 'plain_text', 'content': 'PCM 双版本构建'},
                       'subtitle': {'tag': 'plain_text', 'content': '基于同一源码，生成普通版与评测版 APP'}},
            'body': {'padding': '12px', 'elements': [{'tag': 'form', 'name': FEATURE,
                     'vertical_spacing': '8px', 'elements': fields}]}}
