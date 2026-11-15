"""Independent software configuration and real-user build confirmation route.

Uses the existing trusted transport and HMAC decoder, a separate feature DB,
and the existing CardKit client. It does not restart the shared scheduler.
"""
import hashlib
import json
from pathlib import Path
import sys
import time
import unicodedata
import uuid

from .core import GatewayError
from . import pcm_dual_build as feature

COMMANDS = {'PCM 双版本构建', 'PCM双版本构建', '/pcm_dual_build'}


def is_command(text):
    value = ''.join(unicodedata.normalize('NFKC', text).split()).casefold()
    return value in {'pcm双版本构建', '/pcm_dual_build'}


def enabled(cfg):
    path = Path(cfg['project_root'])/'runtime/pcm-dual-validation.json'
    if not path.is_file() or path.stat().st_size > 4096:
        return False
    value = json.loads(path.read_text())
    return value.get('enabled') is True and type(value.get('jenkins_enabled')) is bool


def build_enabled(cfg):
    path = Path(cfg['project_root'])/'runtime/pcm-dual-validation.json'
    return path.is_file() and json.loads(path.read_text()).get('jenkins_enabled') is True


class Entry:
    def __init__(self, cfg, *, store=None, client=None, validator=None, source_check=None):
        self.cfg = cfg
        self.store = store or feature.live_drafts(cfg['state_root'])
        if client is None:
            from .task_cards import _client
            client = _client(cfg)
        self.client = client
        self.validator = validator or self.read_only_validator
        self.source_check = source_check or self.check_source
        from .pcm_build_jobs import Jobs
        self.jobs = Jobs(self.store)
        with self.store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS pcm_dual_surfaces(
              owner TEXT PRIMARY KEY,session TEXT NOT NULL,draft_id TEXT NOT NULL,
              message_id TEXT NOT NULL,chat_id TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS pcm_dual_events(
              event_id TEXT PRIMARY KEY,owner TEXT NOT NULL,session TEXT NOT NULL,
              message_id TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS pcm_dual_opens(
              owner TEXT PRIMARY KEY,send_key TEXT NOT NULL,created REAL NOT NULL,state TEXT NOT NULL)''')

    def card(self, draft):
        if draft['state'] == 'VALIDATED' and (draft.get('report') or {}).get('build_review'):
            from .pcm_build_plan import render_review
            return render_review(draft, build_enabled=build_enabled(self.cfg))
        return feature.render_card(draft, callback_value={'feature': feature.FEATURE,
            'action': 'validate_build_config', 'draft_id': draft['id'],
            'revision': draft['revision'], 'nonce': draft['nonce']})

    def update(self, draft, mid):
        from .card_entities import update
        update(self.store, self.client, mid, self.card(draft), lambda: 12)

    def open(self, session):
        from .service import identity
        from .card_entities import send_owner
        owner = identity(session)
        now = time.time()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            visit = c.execute('SELECT * FROM pcm_dual_opens WHERE owner=?', (owner,)).fetchone()
            active = c.execute("SELECT d.state FROM pcm_dual_surfaces s JOIN pcm_dual_drafts d ON s.draft_id=d.id WHERE s.owner=?", (owner,)).fetchone()
            processing = c.execute("SELECT 1 FROM pcm_dual_events WHERE owner=? AND state='PROCESSING' LIMIT 1", (owner,)).fetchone()
            # An uncertain send reuses its UUID even after the short cooldown.
            # A running validation continues updating the original message.
            reuse = visit and (visit['state'] == 'PENDING' or now-visit['created'] < 10 or processing or (active and active['state'] == 'VALIDATING'))
            if reuse:
                key = visit['send_key']
            else:
                key = hashlib.sha256((self.cfg['feishu_app_id']+':pcm-dual:'+owner+':'+uuid.uuid4().hex).encode()).hexdigest()[:32]
                c.execute("INSERT INTO pcm_dual_opens VALUES(?,?,?,'PENDING') ON CONFLICT(owner) DO UPDATE SET send_key=excluded.send_key,created=excluded.created,state=excluded.state", (owner, key, now))
        loading = {'schema': '2.0', 'config': {'update_multi': True, 'enable_forward': False},
            'header': {'template': 'blue', 'title': {'tag': 'plain_text', 'content': 'PCM 双版本构建'}},
            'body': {'elements': [{'tag': 'markdown', 'content': '正在打开构建配置。'}]}}
        mid, chat = send_owner(self.store, self.client, owner, loading, key, lambda: 12)
        native = session.split(':')[0]+':'+chat+':'+owner
        draft = self.store.open(owner, native)
        with self.store.connect() as c:
            c.execute('INSERT INTO pcm_dual_surfaces VALUES(?,?,?,?,?) ON CONFLICT(owner) DO UPDATE SET session=excluded.session,draft_id=excluded.draft_id,message_id=excluded.message_id,chat_id=excluded.chat_id',
                      (owner, native, draft['id'], mid, chat))
        self.update(draft, mid)
        with self.store.connect() as c:
            c.execute("UPDATE pcm_dual_opens SET state='DONE' WHERE owner=? AND send_key=?", (owner, key))
        return 'NO_REPLY'

    def read_only_validator(self, store, ticket):
        scripts = Path(self.cfg['project_root'])/'extensions/pcm-dual-validation/scripts'
        if not scripts.is_dir():
            raise GatewayError('PCM校验组件未部署完整，输入已保留。')
        original = list(sys.path)
        try:
            sys.path.insert(0, str(scripts))
            from source_build_discovery import LiveReader, discover, verify_recipe
            reader = LiveReader(self.cfg['project_root'], ticket['owner'])
            result = {}
            def resolve(request):
                value = discover(request, reader);result['value'] = value
                return value
            def verify(ref):
                lock = result.get('value', {}).get('baseline', {}).get('lock_refs', {})
                value = lock.get(ref.split('/')[0], '')
                revision = value.split('#', 1)[1] if '#' in value and value.split('#')[0] == ref else None
                return verify_recipe(reader, ref, revision)
            prepare = None
            if build_enabled(self.cfg):
                from .pcm_build_plan import prepare_live_review
                prepare = lambda current, report: prepare_live_review(self.cfg, current, report, reader, scripts=scripts)
            return feature.run_validation(store, ticket, resolve, verify, prepare)
        finally:
            sys.path[:] = original

    def handle(self, session, data):
        if data.get('action') in ('pcm_confirm_build', 'pcm_edit_build'):
            return self.review_action(session, data)
        from .service import identity
        owner = identity(session)
        value, raw_form = data.get('value') or {}, data.get('form_value') or {}
        if data.get('operator_id') != owner or data.get('chat_id') != session.split(':')[1]:
            raise GatewayError('PCM操作人或私聊身份不一致。')
        names = {'pcm_dual_branch_url': 'branch_url', 'pcm_dual_component_versions': 'component_versions',
                 'pcm_dual_baseline': 'baseline_build', 'pcm_dual_release_version': 'release_version',
                 'pcm_dual_software_id': 'software_id', 'pcm_dual_build_variant': 'build_variant'}
        if data.get('action') != 'confirm_materials' or set(raw_form) - set(names):
            raise GatewayError('PCM校验操作不匹配，未提交任何任务。')
        form = {names[k]: v for k, v in raw_form.items()}
        if not all(isinstance(form.get(k), str) for k in ('branch_url', 'component_versions')):
            raise GatewayError('请在当前PCM卡片填写源码和组件配置。')
        try:
            release = feature.release_parameters({'BASE_LINE': form.get('release_version'),
                'SOFTWARE_BRANCH': form.get('software_id'), 'BUILDVARIANT': form.get('build_variant')})
        except ValueError as error:
            raise GatewayError(str(error)) from None
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            opening = c.execute('SELECT state FROM pcm_dual_opens WHERE owner=?', (owner,)).fetchone()
            if opening and opening['state'] == 'PENDING':
                raise GatewayError('构建卡正在打开，请在展示完成后使用最新卡片校验。')
            row = c.execute('SELECT d.* FROM pcm_dual_surfaces s JOIN pcm_dual_drafts d ON d.id=s.draft_id WHERE s.owner=? AND s.session=? AND s.message_id=? AND s.chat_id=?',
                            (owner, session, data['message_id'], data['chat_id'])).fetchone()
            if not row or row['nonce'] != value.get('nonce'):
                raise GatewayError('PCM卡片已失效或不属于本人私聊，请发送“PCM 双版本构建”重新打开。')
            draft = feature.Drafts.unpack(row)
            old = c.execute('SELECT * FROM pcm_dual_events WHERE event_id=?', (data['event_id'],)).fetchone()
            if old:
                if (old['owner'], old['session'], old['message_id']) != (owner, session, data['message_id']):
                    raise GatewayError('PCM事件身份不一致。')
                retry_delivery = old['state'] == 'DELIVERY_PENDING'
                if not retry_delivery:
                    return 'NO_REPLY'
            else:
                retry_delivery = False
                c.execute('INSERT INTO pcm_dual_events VALUES(?,?,?,?,?,?)',
                          (data['event_id'], owner, session, data['message_id'], 'PROCESSING', time.time()))
        if retry_delivery:
            # Delivery retries must never redo discovery or replace its result.
            self.update(self.store.open(owner, session), data['message_id'])
            with self.store.connect() as c:
                c.execute("UPDATE pcm_dual_events SET state='DONE' WHERE event_id=?", (data['event_id'],))
            return 'NO_REPLY'
        ticket = None
        try:
            baseline = form.get('baseline_build')
            if baseline not in (None, ''):
                allowed = {str(b['number']) for b in (draft.get('report') or {}).get('discovery', {}).get('candidate_baselines', [])}
                if str(baseline) not in allowed or not str(baseline).isdigit():
                    raise GatewayError('基线选择已失效，请重新校验。')
                baseline = int(baseline)
            else:
                baseline = draft.get('baseline_build')
            ticket, started = self.store.begin_validation(owner, session, draft['id'], draft['nonce'],
                    value.get('revision'), form['branch_url'], form['component_versions'], baseline, release)
        except (ValueError, GatewayError) as error:
            with self.store.connect() as c:
                c.execute("UPDATE pcm_dual_events SET state='REJECTED' WHERE event_id=?", (data['event_id'],))
            raise GatewayError(str(error)) from None
        try:
            if started:
                # A failed progress notification is a delivery failure, not a
                # failed validation. Do not run discovery invisibly afterwards.
                try:
                    self.update(ticket, data['message_id'])
                except Exception:
                    self.store.finish_validation(owner, session, ticket['id'], ticket['revision'], ticket['attempt'],
                        {'state': 'NEEDS_INPUT', 'message': '进度通知发送失败，尚未开始校验；输入已保存，请重新校验。'})
                    raise
                try:
                    self.validator(self.store, ticket)
                except Exception:
                    self.store.finish_validation(owner, session, ticket['id'], ticket['revision'], ticket['attempt'],
                        {'state': 'NEEDS_INPUT', 'message': '配置校验暂未完成，输入已保留；请稍后重试。'})
            current = self.store.open(owner, session)
            self.update(current, data['message_id'])
        except Exception:
            with self.store.connect() as c:
                c.execute("UPDATE pcm_dual_events SET state='DELIVERY_PENDING' WHERE event_id=?", (data['event_id'],))
            raise GatewayError('PCM校验或卡片更新未完成，输入已保留；请发送“PCM 双版本构建”重新打开。') from None
        with self.store.connect() as c:
            c.execute("UPDATE pcm_dual_events SET state='DONE' WHERE event_id=?", (data['event_id'],))
        return 'NO_REPLY'

    def check_source(self, owner, review):
        from .pcm_build_remote import Remote
        from .pcm_build_plan import GITLAB, PROJECT
        from urllib.parse import quote
        head = Remote(self.cfg, owner).json(GITLAB+'projects/'+quote(PROJECT, safe='')+
            '/repository/branches/'+quote(review['source']['branch'], safe=''))
        return head.get('commit', {}).get('id') == review['source']['commit']

    def review_action(self, session, data):
        from .service import identity
        from .pcm_build_plan import checked
        from .pcm_build_cards import render_parent
        owner = identity(session);value = data.get('value') or {}
        if data.get('operator_id') != owner or data.get('chat_id') != session.split(':')[1] or data.get('form_value'):
            raise GatewayError('构建确认必须来自本人私聊的参数卡。')
        with self.store.connect() as c:
            row = c.execute('SELECT * FROM pcm_dual_drafts WHERE id=? AND owner=? AND session=?',
                            (value.get('task_id'), owner, session)).fetchone()
            if not row: raise GatewayError('找不到本人的构建配置。')
            draft = feature.Drafts.unpack(row)
        try:
            review = checked((draft.get('report') or {}).get('build_review') or {})
            if value.get('nonce') != review['nonce'] or value.get('revision') != draft['revision']:
                raise ValueError('最终参数卡已更新，请重新核对。')
            existing = self.jobs.for_draft(owner, session, draft['id'])
            if data['action'] == 'pcm_confirm_build':
                if not build_enabled(self.cfg): raise ValueError('自动构建暂未开启，本次未提交。')
                if existing is None:
                    try: source_matches = self.source_check(owner, review)
                    except Exception:
                        raise ValueError('源码状态读取未完成，请稍后再次确认；本次尚未入队。') from None
                    if not source_matches:
                        raise ValueError('源码分支已更新，请返回修改并重新校验；未提交构建。')
                job, created = self.jobs.confirm(owner, session, data, review['review_sha256'])
                # Queued state is committed before any notification. A failed
                # notification never recreates this job or its external submits.
                from .card_entities import update
                try: update(self.store, self.client, job['message_id'], render_parent(job), lambda: 10)
                except Exception: pass
                return 'NO_REPLY'
            if existing: raise ValueError('该配置已提交构建，不能修改本次冻结输入。')
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                surface = c.execute('SELECT * FROM pcm_dual_surfaces WHERE owner=? AND session=?', (owner, session)).fetchone()
                current = c.execute('SELECT * FROM pcm_dual_drafts WHERE id=?', (draft['id'],)).fetchone()
                if (not surface or surface['message_id'] != data['message_id'] or surface['draft_id'] != draft['id']
                        or current['state'] != 'VALIDATED' or current['revision'] != value['revision']):
                    raise ValueError('参数卡已变化，未修改配置。')
                c.execute("UPDATE pcm_dual_drafts SET revision=revision+1,state='DRAFT',report=NULL,attempt=NULL,last_revision=NULL,input_sha=NULL WHERE id=?", (draft['id'],))
            self.update(self.store.open(owner, session), data['message_id'])
            return 'NO_REPLY'
        except ValueError as error:
            raise GatewayError(str(error)) from None


def maybe_handle(service, session, text):
    """Exact routing before legacy materials, parser and any bench queue calls."""
    from .card_ui import PREFIX, decode
    command = is_command(text)
    data = None
    if not command:
        if not text.startswith(PREFIX):
            return None
        data = decode(service.cfg, session, text)
        form = data.get('form_value') or {}
        if not any(k.startswith('pcm_dual_') for k in form) and data.get('action') not in ('pcm_confirm_build', 'pcm_edit_build'):
            return None
    if not enabled(service.cfg):
        raise GatewayError('PCM双版本构建校验暂未开启，未提交任何任务。')
    entry = Entry(service.cfg)
    return entry.open(session) if command else entry.handle(session, data)
