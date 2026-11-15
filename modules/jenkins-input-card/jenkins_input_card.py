"""Standalone, offline Feishu Jenkins input form. No network or build adapter.

The host supplies Identity from authenticated server context, never form fields.
Only explicit configuration confirmation updates per-tenant, per-user history.
"""
from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import unquote, urlsplit
import uuid


REQUIRED = {
    'conan_shared': ('livc', 'release-schu'),
    'uroad_m100': ('livc', 'stable'),
    'dag_compiler': ('livc', 'stable'),
}
EMPTY_TEMPLATE = '\n'.join(name + '/@' + user + '/' + channel
                           for name, (user, channel) in REQUIRED.items())
PROJECT = 'livc-acore/chassis/uroad'
BRANCH_PREFIX = '/' + PROJECT + '/-/tree/'
REF = re.compile(r'([A-Za-z0-9_][A-Za-z0-9_.-]*)/([A-Za-z0-9_][A-Za-z0-9_.+\-]*)@'
                 r'([A-Za-z0-9_][A-Za-z0-9_.-]*)/([A-Za-z0-9_][A-Za-z0-9_.-]*)\Z')
ACTION = re.compile(r'(check|save)__([a-f0-9]{32})__(\d+)\Z')
MAX_INPUT = 1000  # Feishu Card 2.0 input component limit.
SESSION_SECONDS = 24 * 60 * 60


class CardError(ValueError):
    """Expected input/session error, safe to return without a traceback."""


@dataclass(frozen=True)
class Identity:
    tenant: str
    user: str

    def __post_init__(self):
        for value in (self.tenant, self.user):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value):
                raise CardError('需要由宿主提供有效的租户及本人身份')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def branch_from_url(value):
    if re.search(r'\s', value):
        raise ValueError('分支链接不能包含空格或换行')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname != 'gitlab.chehejia.com'
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment or parsed.query not in ('', 'ref_type=heads')
            or not parsed.path.startswith(BRANCH_PREFIX)):
        raise ValueError('请填写 uroad 的 GitLab 分支链接，不使用仓库首页、凭据或其他参数')
    branch = unquote(parsed.path[len(BRANCH_PREFIX):])
    if (not branch or branch.startswith(('-', '/', '.')) or branch.endswith(('/', '.', '.lock'))
            or any(x in branch for x in ('..', '//', '@{'))
            or any(p.startswith('.') or p.endswith('.lock') for p in branch.split('/'))
            or re.search(r'[\s~^:?*\[\\\x00-\x1f\x7f]', branch)):
        raise ValueError('分支链接中的分支名无效')
    return branch


def validate(branch_link, app_version):
    """Local syntax only: existence, access and source compatibility are NOT checked."""
    errors, references = [], []
    branch = None
    try:
        branch = branch_from_url(branch_link)
    except ValueError as error:
        errors.append({'field': 'branch_link', 'message': str(error)})
    seen, present = set(), set()
    for line_number, raw in enumerate(app_version.splitlines(), 1):
        value = raw.strip()
        if not value:
            continue
        named = re.match(r'([A-Za-z0-9_][A-Za-z0-9_.-]*)/', value)
        if named:
            present.add(named[1])
        match = REF.fullmatch(value)
        if not match:
            errors.append({'field': 'app_version', 'message': f'第 {line_number} 行需填写完整精确引用：组件/版本@用户/通道'})
            continue
        name, version, user, channel = match.groups()
        if version in ('.', '..'):
            errors.append({'field': 'app_version', 'message': f'第 {line_number} 行版本无效'})
            continue
        if name in seen:
            errors.append({'field': 'app_version', 'message': f'第 {line_number} 行组件重复：{name}'})
        seen.add(name)
        if name in REQUIRED and (user, channel) != REQUIRED[name]:
            errors.append({'field': 'app_version', 'message': f'{name} 应使用 @{REQUIRED[name][0]}/{REQUIRED[name][1]}'})
        references.append(value)
    for name in REQUIRED:
        if name not in present:
            errors.append({'field': 'app_version', 'message': f'请补齐 {name} 的完整版本引用'})
    return {'errors': errors, 'branch': branch, 'references': references,
            'normalized': '\n'.join(references), 'remote_validation': 'NOT_CHECKED'}


def _text(value):
    if not isinstance(value, str) or len(value) > MAX_INPUT:
        raise CardError('每个输入框限 1000 字符，请缩短后重试；未覆盖已确认配置')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise CardError('输入包含不支持的控制字符')
    return value


class InputCards:
    """Local persistence and deterministic card reducer; no Feishu/Jenkins client.

    The SQLite path is supplied by the host and must be outside this repository.
    Card JSON contains session IDs, not credentials or authoritative owner fields.
    """

    def __init__(self, database, clock=time.time):
        self.database = Path(database).resolve()
        repository = Path(__file__).resolve().parents[2]
        if self.database == repository or repository in self.database.parents:
            raise CardError('个人配置库必须放在仓库之外，不写入 Git')
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS configurations (
                    tenant TEXT NOT NULL, owner TEXT NOT NULL, revision INTEGER NOT NULL,
                    branch_link TEXT NOT NULL, app_version TEXT NOT NULL, confirmed_at REAL NOT NULL,
                    PRIMARY KEY (tenant, owner));
                CREATE TABLE IF NOT EXISTS input_cards (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, owner TEXT NOT NULL,
                    revision INTEGER NOT NULL, data TEXT NOT NULL, expires REAL NOT NULL,
                    last_request TEXT, last_response TEXT);
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.database), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _history(db, identity):
        row = db.execute('SELECT * FROM configurations WHERE tenant=? AND owner=?',
                         (identity.tenant, identity.user)).fetchone()
        return dict(row) if row else None

    def last_confirmed(self, identity):
        with self._connect() as db:
            return self._history(db, identity)

    def export_confirmed(self, identity, session_id):
        """Export this owner's current confirmed input, not a build authorization."""
        with self._connect() as db:
            row = db.execute('SELECT * FROM input_cards WHERE id=? AND tenant=? AND owner=?',
                             (session_id, identity.tenant, identity.user)).fetchone()
            if not row or row['expires'] < self.clock():
                raise CardError('无法导出此卡片，请重新打开本人卡片并确认')
            data = json.loads(row['data'])
            current = self._history(db, identity)
            if (data['phase'] != 'CONFIRMED' or not current
                    or current['revision'] != data['history_revision']):
                raise CardError('只能使用本人当前已确认的配置；旧卡或草稿不能生成发包计划')
            fields = {'branch_link': data['branch_link'], 'app_version': data['app_version']}
            return {'schema': 'pcm-confirmed-input/v1', **fields,
                    'configuration_sha256': digest(fields),
                    'configuration_revision': current['revision'],
                    'owner': {'tenant': identity.tenant, 'user': identity.user}}

    def open(self, identity, branch_link=''):
        """New card: always use explicit current branch, never silently reuse an old one."""
        _text(branch_link)
        with self._connect() as db:
            history = self._history(db, identity)
            data = {'branch_link': branch_link, 'app_version': history['app_version'] if history else EMPTY_TEMPLATE,
                    'prefill': 'PERSONAL_CONFIRMED' if history else 'EMPTY_TEMPLATE',
                    'previous_branch_link': history['branch_link'] if history else '',
                    'history_revision': history['revision'] if history else 0,
                    'phase': 'EDITING', 'errors': [], 'checked_digest': None,
                    'remote_validation': 'NOT_CHECKED'}
            session_id = uuid.uuid4().hex
            db.execute('INSERT INTO input_cards VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)',
                       (session_id, identity.tenant, identity.user, 0, json.dumps(data),
                        self.clock() + SESSION_SECONDS))
        return self._view(session_id, 0, data)

    def submit(self, identity, action_name, form_value):
        """Host passes authenticated identity plus action.name/action.form_value.

        This confirms CONFIGURATION ONLY. It cannot authorize or dispatch builds.
        Confirm is bound to the latest checked fields and history revision.
        """
        match = ACTION.fullmatch(action_name) if isinstance(action_name, str) else None
        if not match or not isinstance(form_value, dict):
            raise CardError('不支持的卡片操作')
        verb, session_id, expected_revision = match.groups()
        fields = {key: _text(form_value.get(key)) for key in ('branch_link', 'app_version')}
        if set(form_value) != set(fields):
            raise CardError('表单包含未定义字段；本人身份不能通过输入框指定')
        request_digest = digest({'action': action_name, 'fields': fields})
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM input_cards WHERE id=? AND tenant=? AND owner=?',
                             (session_id, identity.tenant, identity.user)).fetchone()
            if not row:
                raise CardError('无法访问此卡片，请使用本人卡片')
            if row['expires'] < self.clock():
                raise CardError('卡片已过期，请重新打开；本人上次确认配置仍保留')
            if row['last_request'] == request_digest:
                return json.loads(row['last_response'])
            if row['revision'] != int(expected_revision):
                raise CardError('卡片已更新，请使用最新卡片，避免覆盖较新的修改')
            data = json.loads(row['data'])
            if data['phase'] == 'CONFIRMED':
                raise CardError('此卡片已确认，请打开新卡片修改配置')
            checked_before = data.get('checked_digest')
            checked_fields = {'branch_link': fields['branch_link'].strip(),
                              'app_version': fields['app_version']}
            data.update(checked_fields)
            result = validate(**checked_fields)
            data['errors'] = result['errors']
            data['phase'] = 'EDITING'
            data['checked_digest'] = None
            current = self._history(db, identity)
            current_revision = current['revision'] if current else 0
            if not result['errors']:
                fields_digest = digest(checked_fields)
                if verb == 'check':
                    data.update(phase='CHECKED', checked_digest=fields_digest,
                                history_revision=current_revision)
                elif checked_before != fields_digest:
                    data['errors'] = [{'field': 'form', 'message': '输入内容已变化，请先重新检查，再确认保存'}]
                elif data['history_revision'] != current_revision:
                    data['errors'] = [{'field': 'form', 'message': '本人配置已在另一张卡片更新，请重新检查后再确认保存'}]
                else:
                    new_revision = current_revision + 1
                    db.execute('INSERT INTO configurations VALUES (?, ?, ?, ?, ?, ?) '
                               'ON CONFLICT(tenant, owner) DO UPDATE SET '
                               'revision=excluded.revision, branch_link=excluded.branch_link, '
                               'app_version=excluded.app_version, confirmed_at=excluded.confirmed_at',
                               (identity.tenant, identity.user, new_revision, data['branch_link'],
                                result['normalized'], self.clock()))
                    data.update(phase='CONFIRMED', app_version=result['normalized'],
                                history_revision=new_revision)
            revision = row['revision'] + 1
            view = self._view(session_id, revision, data)
            db.execute('UPDATE input_cards SET revision=?, data=?, last_request=?, last_response=? WHERE id=?',
                       (revision, json.dumps(data), request_digest, json.dumps(view), session_id))
        return view

    @staticmethod
    def _view(session_id, revision, data):
        return {'session_id': session_id, 'revision': revision, 'phase': data['phase'],
                'errors': data['errors'], 'prefill': data['prefill'],
                'branch_changed': bool(data['previous_branch_link'] and data['branch_link']
                                       and data['branch_link'] != data['previous_branch_link']),
                'remote_validation': 'NOT_CHECKED', 'build_dispatch': 'NOT_IMPLEMENTED',
                'card': render_card(session_id, revision, data)}


def render_card(session_id, revision, data):
    """Feishu Card JSON 2.0; one editable form, no prefill or build buttons."""
    plain = lambda text: {'tag': 'plain_text', 'content': text}
    confirmed = data['phase'] == 'CONFIRMED'
    source = ('已带出本人上次确认的组件配置，可直接修改或追加。' if data['prefill'] == 'PERSONAL_CONFIRMED'
              else '首次使用，请填写三项版本；其他组件可在末尾追加，每行一条。')
    if confirmed:
        source = '本次组件配置已确认，下次本人使用时将自动带出。'
    if data['previous_branch_link'] and data['branch_link'] != data['previous_branch_link']:
        source += ' 本次分支需重新核验，历史版本仅供编辑参考。'
    elements = [{'tag': 'markdown', 'content': source}]
    if data['errors']:
        elements.append({'tag': 'div', 'text': plain('\n'.join(e['message'] for e in data['errors']))})
    if data['phase'] == 'CHECKED':
        elements.append({'tag': 'markdown', 'content': '**格式检查通过**。版本存在性、权限、源码依赖和 OFF/ON 引用仍待核验。'})
    if confirmed:
        elements.append({'tag': 'markdown', 'content': '**配置已保存**，下次本人打开卡片时自动带出。本次未触发发包。'})
    form = [
        {'tag': 'input', 'name': 'branch_link', 'label': plain('uroad 分支链接'),
         'placeholder': plain('粘贴本次 GitLab 分支链接'), 'input_type': 'text',
         'default_value': data['branch_link'], 'required': True, 'max_length': MAX_INPUT,
         'width': 'fill', 'disabled': confirmed},
        {'tag': 'input', 'name': 'app_version', 'label': plain('APP_VERSION（可修改和追加）'),
         'placeholder': plain('每行：组件/精确版本@用户/通道'), 'input_type': 'multiline_text',
         'default_value': data['app_version'], 'required': True, 'max_length': MAX_INPUT,
         'rows': 7, 'width': 'fill', 'disabled': confirmed},
    ]
    if data['phase'] == 'CHECKED':
        form.append({'tag': 'button', 'name': f'save__{session_id}__{revision}',
                     'text': plain('确认并保存配置'), 'type': 'primary_filled', 'width': 'fill',
                     'form_action_type': 'submit'})
    form.append({'tag': 'button', 'name': f'check__{session_id}__{revision}',
                 'text': plain('检查参数'), 'type': 'default' if data['phase'] == 'CHECKED' else 'primary_filled',
                 'width': 'fill', 'form_action_type': 'submit', 'disabled': confirmed})
    elements.append({'tag': 'form', 'name': 'jenkins_inputs', 'elements': form})
    elements.append({'tag': 'markdown', 'content': '本次仅保存配置，不触发 Jenkins。请勿填写密码或 Token。'})
    return {'schema': '2.0', 'config': {'update_multi': True, 'width_mode': 'default', 'enable_forward': False},
            'header': {'title': plain('Jenkins 发包配置'), 'template': 'green' if confirmed else 'blue'},
            'body': {'elements': elements}}
