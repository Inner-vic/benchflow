"""Entire software pipeline with fake remote platforms. Never real CI or hardware."""
import copy
import base64
import hmac
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
import urllib.error
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import patch

from bench_gateway import pcm_dual_build as feature, pcm_dual_entry as entry
from bench_gateway.pcm_build_plan import make_review, checked, render_review, PROJECT, GITLAB, JOB
from bench_gateway.pcm_build_engine import Engine, API, ARTIFACT
from bench_gateway.pcm_build_jobs import Jobs, BuildFailure, Uncertain
from bench_gateway.pcm_dual_worker import Worker
from bench_gateway.core import GatewayError
from test_pcm_dual_entry import Client

SKILL = Path(os.environ.get('PCM_TEST_SKILL_ROOT', Path(__file__).resolve().parents[3]/'skills/pcm-dual-package'))
sys.path.insert(0, str(SKILL/'scripts'))
from prepare_pcm_components import recipe
from source_build_discovery import common_inputs

SOURCE = '''class App:
    name = "uroad_m100"
    version = tools.semver("3.7.0")
    branch = "business"
    def source(self):
        self.clone("https://gitlab.chehejia.com/livc-acore/chassis/uroad.git")
    def build(self):
        self._cmake.configure()
'''
TEXT = 'conan_shared/8.4.0@livc/release-schu\nuroad_m100/3.7.0@livc/stable\ndag_compiler/2.0@livc/stable'
URL = 'https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/feature/new'
SESSION = 'feishu:oc_private:ou_tester'
NAMING = {'BASE_LINE': '00B1', 'BUILDVARIANT': 'release'}


def resolve(request):
    d = {'state': 'RESOLVED_FOR_REVIEW', 'source': {'repository': 'https://gitlab.chehejia.com/'+PROJECT+'.git',
        'branch': 'feature/new', 'commit': 'a'*40}, 'resolved_dependencies': {},
        'component_overrides': {r.split('/')[0]: r for r in request['component_overrides']},
        'integration_package_names': ['uroad_m100', 'dag_compiler'],
        'integration_package_list_sha256': 'b'*64,
        'source_files': {'conanfile.py': hashlib.sha256(SOURCE.encode()).hexdigest()},
        'baseline': {'number': 101, 'product_variant': 'ULTRA', 'manifest': 'CCU_LIVC_AVM-release-SW0001_ULTRA_ID0111.20260101-101',
            'lock_sha256': 'c'*64, 'parameters': {'CONTROLLER': 'CCU_M100_AVMLIVC', 'BASE_LINE': '0001',
                'SOFTWARE_BRANCH': '0111', 'BUILDVARIANT': 'release', 'APP_VERSION': 'conan_shared/8.4.0@livc/release-schu'}},
        'base_app_mode': 'OFF', 'base_app_mode_evidence': {'mode': 'OFF'},
        'baseline_artifact_checks': {'verified': True, 'liware_present': False, 'limesh_present': False}}
    d['discovery_sha256'] = feature.digest(d)
    return d


def verify(reader, ref, revision=None):
    if '/' not in ref or '@' not in ref:
        raise AssertionError('Expected a complete Conan reference, not a component name')
    if hasattr(reader, 'review'):
        for mode, item in reader.review['manifest']['components'].items():
            if ref == item['ref']:
                return {'state': 'VERIFIED', 'ref': ref, 'recipe_revision': 'f'*32, 'recipe_sha256': item['recipe_sha256']}
    return {'state': 'VERIFIED', 'ref': ref, 'recipe_revision': 'd'*32, 'recipe_sha256': 'e'*64}


def review_for(ticket):
    report = feature.validate_configuration(ticket, resolve, lambda ref: verify(None, ref))
    template = (SKILL/'examples/ci-20261106/internal_pcm_pair/publish_recipes_ci.py').read_text()
    review = make_review(ticket, report, source_recipe=SOURCE, prepare_recipe=recipe,
        publisher_template=template, template_commit='9'*40, common_inputs=common_inputs)
    report['build_review'] = review
    return report


class DependencyBindingTests(unittest.TestCase):
    def test_verified_replay_lidar_is_bound_into_both_private_recipes(self):
        ticket = {'id': 'a'*32, 'owner': 'ou_tester', 'session': SESSION, 'revision': 2,
                  'component_versions': TEXT+'\nxlidar/9.0@livc/stable', 'release_parameters': NAMING}
        report = feature.validate_configuration(ticket | {'branch_url': URL}, resolve, lambda ref: verify(None, ref))
        source = SOURCE.replace('    def source(self):', '    def requirements(self):\n        self.requires("xlidar/1.0@livc/stable")\n    def source(self):')
        d = report['discovery'];d['source_files']['conanfile.py'] = hashlib.sha256(source.encode()).hexdigest()
        report['baseline']['lock_sha256'] = 'c'*64
        d['resolved_dependencies']['xlidar'] = 'xlidar/9.0@livc/stable'
        proof = {'declared': 'xlidar/1.0@livc/stable', 'requested': 'xlidar/9.0@livc/stable',
                 'baseline': 101, 'lock_sha256': 'c'*64, 'locked_ref': 'xlidar/9.0@livc/stable#'+'b'*32}
        d['dependency_adjustments'] = {'xlidar': proof}
        d['discovery_sha256'] = feature.digest({k:v for k,v in d.items() if k != 'discovery_sha256'})
        common = common_inputs(d, NAMING)
        report['common_app_refs'] = common['common_app_refs'];report['common_locked_refs'] = common['common_locked_refs']
        template = (SKILL/'examples/ci-20261106/internal_pcm_pair/publish_recipes_ci.py').read_text()
        review = make_review(ticket, report, source_recipe=source, prepare_recipe=recipe,
            publisher_template=template, template_commit='9'*40, common_inputs=common_inputs)
        for mode in ('ON', 'OFF'):
            generated = review['payload']['internal_pcm_pair/recipes/'+mode+'/conanfile.py']
            self.assertIn("self.requires('xlidar/9.0@livc/stable')", generated)
            self.assertNotIn('xlidar/1.0@livc/stable', generated)
        proof['lock_sha256'] = 'f'*64
        with self.assertRaises(ValueError):
            make_review(ticket, report, source_recipe=source, prepare_recipe=recipe,
                publisher_template=template, template_commit='9'*40, common_inputs=lambda *a: common)


class FakeRemote:
    def __init__(self, review):
        self.review = review;self.posts = [];self.submits = [];self.private = False;self.ci = False
        self.pipeline_status = 'success';self.states = {};self.dispatches = {};self.timeout_submit = False
        self.artifacts_missing = False
    def post(self, url, body):
        self.posts.append((url, copy.deepcopy(body)))
        if url.endswith('/ci/lint'):
            return {'valid': True, 'jobs': [{'name': 'publish-pcm-recipes'}], 'merged_yaml': 'synthetic merged'}
        if url.endswith('/repository/commits'):
            self.private = True;return {'id': 'c'*40}
        if url.endswith('/pipeline'):
            self.ci = True;return {'id': 77, 'sha': 'c'*40, 'ref': self.review['private_branch']}
        raise AssertionError(url)
    def submit_jenkins(self, params):
        mode = 'ON' if '-pcm-on.' in params['APP_VERSION'] else 'OFF'
        self.submits.append(mode);qid = 101 if mode == 'OFF' else 102
        self.dispatches[mode] = {'queue_id': qid, 'queue_url': 'https://ci.ep.chehejia.com/queue/item/'+str(qid)+'/'}
        self.states.setdefault(mode, 'success')
        if self.timeout_submit:
            self.timeout_submit = False;raise TimeoutError('synthetic after-submit failure')
        return self.dispatches[mode]
    def build(self, mode):
        q = self.dispatches[mode]['queue_id'];n = q+100
        return {'number': n, 'queueId': q, 'url': JOB+str(n)+'/', 'building': self.states[mode] == 'running',
            'result': 'FAILURE' if self.states[mode] == 'failure' else 'SUCCESS',
            'actions': [{'parameters': [{'name': k, 'value': v} for k, v in self.review['parameters'][mode].items()]}]}
    def lock(self, mode):
        rows = {}
        refs = [r for key in ('APP_VERSION', 'LOCKED_LIBS') for r in self.review['parameters'][mode][key].splitlines() if r and not r.startswith('conan_shared/')]
        for i, ref in enumerate(refs):
            rows[str(i)] = {'ref': ref+'#'+('f'*32 if ref.startswith('uroad_m100/') else 'd'*32), 'package_id': 'package', 'prev': 'revision'}
        return json.dumps({'graph_lock': {'nodes': rows}}).encode()
    def names(self, mode):
        n = 201 if mode == 'OFF' else 202
        base = 'CCU_LIVC_AVM-release-SW00B1_ULTRA_ID0111.20261112-'+str(n)
        return {'app': ARTIFACT+'api/storage/xcu-acore-test-local/Acore/Civu/Release/app/'+base+'.app.tar.gz',
                'lock': ARTIFACT+'api/storage/xcu-acore-test-local/Acore/Civu/Release/'+base+'.lock'}
    def json(self, url):
        path = unquote(urlsplit(url).path);query = parse_qs(urlsplit(url).query)
        if url.startswith(JOB) and 'property[' in query.get('tree', [''])[0]:
            return {'property': [{'parameterDefinitions': [{'name': k} for k in self.review['parameters']['OFF']]+[
                {'name': 'IMPORTANT', 'defaultParameterValue': {'value': 'https://example.feishu.cn/docx/DEMO00000000000000000000002'}}]}]}
        if '/repository/branches/' in path:
            branch = path.split('/repository/branches/')[1]
            if branch == self.review['private_branch']:
                if not self.private: raise urllib.error.HTTPError(url, 404, 'missing', {}, None)
                return {'commit': {'id': 'c'*40}}
            return {'commit': {'id': self.review['source']['commit']}}
        if path.endswith('/repository/commits/'+'c'*40): return {'parent_ids': [self.review['source']['commit']]}
        if path.endswith('/diff'): return [{'new_path': p} for p in self.review['payload']]
        if path.endswith('/pipelines'):
            return [{'id': 77}] if self.ci else []
        if path.endswith('/pipelines/77'): return {'sha': 'c'*40, 'ref': self.review['private_branch'], 'status': self.pipeline_status}
        if path.endswith('/pipelines/77/jobs'): return [{'id': 88, 'name': 'publish-pcm-recipes', 'status': 'success'}]
        if path.endswith('/artifacts/pcm-publication-receipt.json'):
            return {'status': 'REVIEWED_RECIPES_VERIFIED', 'verified': [dict(item, mode=mode, recipe_revision='f'*32)
                for mode, item in self.review['manifest']['components'].items()]}
        if '/queue/item/' in path:
            qid = int(path.split('/queue/item/')[1].split('/')[0]);mode = 'OFF' if qid == 101 else 'ON'
            return {} if self.states[mode] == 'queued' else {'executable': {'url': self.build(mode)['url']}}
        if path == '/queue/api/json':
            return {'items': [dict(self.build(mode), id=d['queue_id'], task={'url': JOB}) for mode, d in self.dispatches.items() if self.states[mode] == 'queued']}
        if url.startswith(JOB) and path.endswith('/api/json'):
            tail = path[:-len('/api/json')].split('/')[-1]
            if tail in ('201', '202'): return self.build('OFF' if tail == '201' else 'ON')
            return {'builds': [self.build(mode) for mode in self.dispatches]}
        if '/api/search/artifact' in path:
            if self.artifacts_missing: return {'results': []}
            name = query['name'][0];mode = 'OFF' if '-201.' in name else 'ON';kind = 'app' if name.endswith('.tar.gz') else 'lock'
            return {'results': [{'uri': self.names(mode)[kind]}]}
        if '/api/storage/' in path:
            mode = 'OFF' if '-201.' in path else 'ON';raw = b'app' if path.endswith('.app.tar.gz') else self.lock(mode)
            return {'size': len(raw), 'checksums': {'sha256': hashlib.sha256(raw).hexdigest()}}
        raise AssertionError(url)
    def get(self, url, limit=8*1024*1024):
        path = unquote(urlsplit(url).path)
        if '/repository/files/' in path:
            name = path.split('/repository/files/')[1].split('/raw')[0]
            return self.review['payload'][name].encode()
        if url.endswith('.lock'):
            return self.lock('OFF' if '-201.' in url else 'ON')
        return json.dumps(self.json(url)).encode()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        (self.root/'runtime').mkdir()
        (self.root/'runtime/pcm-dual-validation.json').write_text(json.dumps({'enabled': True, 'jenkins_enabled': True}))
        (self.root/'runtime/credentials.json').write_text(json.dumps({'app_id': 'cli_test', 'app_secret': 'synthetic-secret'}))
        self.cfg = {'project_root': str(self.root), 'state_root': str(self.root/'state'), 'feishu_app_id': 'cli_test', 'card_callbacks_enabled': True}
        self.store = feature.Drafts(self.root/'feature.sqlite3');self.client = Client()
        self.entry = entry.Entry(self.cfg, store=self.store, client=self.client, source_check=lambda owner, review: True)
        self.entry.open(SESSION)
        d = self.store.open('ou_tester', SESSION)
        ticket, _ = self.store.begin_validation('ou_tester', SESSION, d['id'], d['nonce'], d['revision'], URL, TEXT, release=NAMING)
        report = review_for(ticket)
        self.store.finish_validation('ou_tester', SESSION, ticket['id'], ticket['revision'], ticket['attempt'], report)
        self.review = report['build_review'];self.jobs = self.entry.jobs
        self.remote = FakeRemote(self.review)
    def tearDown(self): self.temp.cleanup()
    def event(self):
        return {'operator_id': 'ou_tester', 'chat_id': 'oc_private', 'message_id': 'om_synthetic',
            'event_id': 'synthetic-confirm', 'action': 'pcm_confirm_build', 'form_value': {},
            'value': {'task_id': self.review['draft_id'], 'nonce': self.review['nonce'], 'revision': self.review['revision']}}
    def confirm(self):
        self.entry.review_action(SESSION, self.event())
        return self.jobs.for_draft('ou_tester', SESSION, self.review['draft_id'])
    def worker(self):
        return Worker(self.cfg, store=self.store, remote_factory=lambda owner: self.remote,
            client=self.client, verify_recipe=verify, output_root=self.root/'outputs')

    def test_validation_does_not_enqueue_or_change_confirmed_defaults(self):
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pcm_build_jobs').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM pcm_dual_defaults').fetchone()[0], 0)
        self.assertEqual(self.remote.posts, [])

    def test_review_shows_both_complete_parameters_and_callback_never_bench(self):
        draft = self.store.open('ou_tester', SESSION);card = render_review(draft, build_enabled=True)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn('确认构建两版 APP', text);self.assertIn('bench:pcm_confirm_build', text)
        for mode in ('OFF', 'ON'):
            for key in self.review['parameters'][mode]: self.assertIn(key, text)
        self.assertNotIn('确认执行', text)

    def test_confirmation_is_atomic_and_duplicate_events_never_enqueue_again(self):
        first = self.confirm();second = self.confirm()
        self.assertEqual(first['id'], second['id'])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pcm_build_jobs').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM pcm_dual_defaults').fetchone()[0], 1)
        self.assertEqual(self.remote.posts, [])

    def test_signed_connector_callback_without_form_fields_routes_to_build_confirmation(self):
        from types import SimpleNamespace
        data = dict(self.event(), schema=1, app_id='cli_test')
        raw = json.dumps(data).encode();mac = hmac.new(b'synthetic-secret', raw, hashlib.sha256).hexdigest()
        packet = '@@BENCH_CARD@@'+base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+mac
        with patch.object(entry, 'Entry', return_value=self.entry):
            self.assertEqual(entry.maybe_handle(SimpleNamespace(cfg=self.cfg), SESSION, packet), 'NO_REPLY')
        self.assertIsNotNone(self.jobs.for_draft('ou_tester', SESSION, self.review['draft_id']))

    def test_review_tampering_and_concurrent_reopen_cannot_enqueue(self):
        review = copy.deepcopy(self.review);review['parameters']['ON']['BASE_LINE'] = 'AAAA'
        with self.assertRaises(ValueError): checked(review)
        with self.store.connect() as c:
            c.execute("UPDATE pcm_dual_opens SET state='PENDING' WHERE owner='ou_tester'")
        with self.assertRaises(GatewayError): self.confirm()
        with self.store.connect() as c: self.assertEqual(c.execute('SELECT count(*) FROM pcm_build_jobs').fetchone()[0], 0)

    def test_wrong_owner_card_nonce_revision_or_source_cannot_confirm(self):
        for key, value in [('operator_id', 'ou_other'), ('chat_id', 'oc_other'), ('message_id', 'om_other')]:
            data = self.event();data[key] = value
            with self.subTest(key=key), self.assertRaises((GatewayError, ValueError)):
                self.entry.review_action(SESSION, data)
        data = self.event();data['value']['nonce'] = 'wrong'
        with self.assertRaises(GatewayError): self.entry.review_action(SESSION, data)
        with patch.object(self.entry, 'source_check', return_value=False), self.assertRaises(GatewayError): self.confirm()
        with self.store.connect() as c: self.assertEqual(c.execute('SELECT count(*) FROM pcm_build_jobs').fetchone()[0], 0)

    def test_return_to_edit_invalidates_review_but_keeps_inputs(self):
        data = self.event();data['action'] = 'pcm_edit_build'
        self.entry.review_action(SESSION, data)
        draft = self.store.open('ou_tester', SESSION)
        self.assertEqual(draft['component_versions'], TEXT);self.assertEqual(draft['state'], 'DRAFT')
        self.assertIsNone(draft['report'])
        with self.assertRaises(GatewayError): self.confirm()

    def test_end_to_end_two_builds_and_two_result_cards_with_resume(self):
        job = self.confirm();worker = self.worker()
        for _ in range(8): worker.once()
        done = self.jobs.get(job['id']);self.assertEqual(done['state'], 'SUCCEEDED')
        self.assertEqual(self.remote.submits, ['OFF', 'ON'])
        self.assertEqual(len([u for u, _ in self.remote.posts if u.endswith('/repository/commits')]), 1)
        self.assertEqual(len([u for u, _ in self.remote.posts if u.endswith('/pipeline')]), 1)
        self.assertEqual(set(done['modes']), {'OFF', 'ON'})
        # One parent card + exactly two result cards, across repeated notifications.
        self.assertEqual(self.client.messages, 3)
        restarted = self.worker();restarted.once()
        self.assertEqual(self.remote.submits, ['OFF', 'ON']);self.assertEqual(self.client.messages, 3)
        with self.store.connect() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('jobs', tables);self.assertNotIn('task_requests', tables)

    def test_queued_and_running_jenkins_is_polled_without_resubmission(self):
        job = self.confirm();self.remote.states['OFF'] = 'queued';worker = self.worker()
        for _ in range(5): worker.once()
        self.assertEqual(self.remote.submits, ['OFF'])
        self.remote.states['OFF'] = 'running';worker.once()
        self.assertEqual(self.jobs.get(job['id'])['modes']['OFF']['state'], 'RUNNING')
        self.remote.states['OFF'] = 'success'
        for _ in range(5): worker.once()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'SUCCEEDED')
        self.assertEqual(self.remote.submits, ['OFF', 'ON'])

    def test_lost_submit_response_recovers_original_identity(self):
        job = self.confirm();self.remote.timeout_submit = True;worker = self.worker()
        for _ in range(8): worker.once()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'SUCCEEDED')
        self.assertEqual(self.remote.submits, ['OFF', 'ON'])

    def test_unknown_intent_is_not_reposted(self):
        job = self.confirm();worker = self.worker()
        worker.once()
        with self.store.connect() as c:
            c.execute("INSERT INTO pcm_build_steps VALUES(?,?,'INTENT',?,NULL,0)",
                (job['id'], 'CI_COMMIT', json.dumps({'branch': self.review['private_branch'], 'review_sha': job['review_sha']})))
        worker.once()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'NEEDS_REVIEW')
        self.assertFalse(any(u.endswith('/repository/commits') for u, _ in self.remote.posts))

    def test_committed_branch_with_lost_receipt_is_recovered_not_recreated(self):
        job = self.confirm();worker = self.worker();worker.once()
        self.remote.private = True
        with self.store.connect() as c:
            c.execute("INSERT INTO pcm_build_steps VALUES(?,?,'INTENT',?,NULL,0)",
                (job['id'], 'CI_COMMIT', json.dumps({'branch': self.review['private_branch'], 'review_sha': job['review_sha']})))
        for _ in range(7): worker.once()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'SUCCEEDED')
        self.assertFalse(any(u.endswith('/repository/commits') for u, _ in self.remote.posts))
        self.assertEqual(self.remote.submits, ['OFF', 'ON'])

    def test_restart_after_off_checkpoint_never_rebuilds_off(self):
        job = self.confirm();worker = self.worker()
        worker.once();worker.once()
        self.assertIsNotNone(self.jobs.step(job['id'], 'OFF_DONE'))
        with self.store.connect() as c:
            c.execute("UPDATE pcm_build_jobs SET modes='{}' WHERE id=?", (job['id'],))
        restarted = self.worker()
        for _ in range(5): restarted.once()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'SUCCEEDED')
        self.assertEqual(self.remote.submits, ['OFF', 'ON'])

    def test_disable_and_maintenance_do_not_claim_new_jobs(self):
        job = self.confirm();worker = self.worker()
        (self.root/'runtime/maintenance.json').write_text('{"enabled":true}')
        worker.once();self.assertEqual(self.jobs.get(job['id'])['state'], 'QUEUED')
        self.assertEqual(self.remote.posts, [])
        (self.root/'runtime/maintenance.json').write_text('{"enabled":false}')
        (self.root/'runtime/pcm-dual-validation.json').write_text('{"enabled":true,"jenkins_enabled":false}')
        worker.once();self.assertEqual(self.jobs.get(job['id'])['state'], 'QUEUED')
        self.assertEqual(self.remote.posts, [])

    def test_software_fifo_keeps_first_confirmed_job_ahead_of_next_draft(self):
        first = self.confirm()
        with patch.object(entry.time, 'time', return_value=first['created']+20): self.entry.open(SESSION)
        d = self.store.open('ou_tester', SESSION)
        ticket, _ = self.store.begin_validation('ou_tester', SESSION, d['id'], d['nonce'], d['revision'], URL, TEXT, release=NAMING)
        report = review_for(ticket)
        self.store.finish_validation('ou_tester', SESSION, ticket['id'], ticket['revision'], ticket['attempt'], report)
        review = report['build_review']
        with self.store.connect() as c:
            surface = c.execute('SELECT message_id FROM pcm_dual_surfaces WHERE owner=?', ('ou_tester',)).fetchone()[0]
        event = dict(self.event(), event_id='second-confirm', message_id=surface,
            value={'task_id': review['draft_id'], 'nonce': review['nonce'], 'revision': review['revision']})
        self.entry.review_action(SESSION, event)
        second = self.jobs.for_draft('ou_tester', SESSION, review['draft_id'])
        self.assertEqual(self.jobs.next()['id'], first['id'])
        self.jobs.update(first['id'], state='FAILED', error='synthetic completion')
        self.assertEqual(self.jobs.next()['id'], second['id'])

    def test_first_build_failure_stops_second_mode_and_does_not_retry(self):
        job = self.confirm();self.remote.states['OFF'] = 'failure';worker = self.worker()
        for _ in range(8): worker.once()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'FAILED')
        self.assertEqual(self.remote.submits, ['OFF'])

    def test_artifact_index_delay_retries_reads_not_builds(self):
        job = self.confirm();self.remote.artifacts_missing = True;worker = self.worker()
        for _ in range(5): worker.once()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'RUNNING')
        self.assertEqual(self.remote.submits, ['OFF'])
        self.remote.artifacts_missing = False
        for _ in range(5): worker.once()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'SUCCEEDED')
        self.assertEqual(self.remote.submits, ['OFF', 'ON'])

    def test_notification_failure_only_retries_delivery(self):
        job = self.confirm();worker = self.worker()
        original = self.client._json
        def unavailable(*a, **kw): raise TimeoutError('synthetic messaging failure')
        with patch.object(self.client, '_json', side_effect=unavailable):
            for _ in range(8): worker.once()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'SUCCEEDED')
        self.assertEqual(self.remote.submits, ['OFF', 'ON'])
        worker.once();worker.once()
        self.assertEqual(self.client.messages, 3);self.assertEqual(self.remote.submits, ['OFF', 'ON'])

    def test_generated_publisher_accepts_only_two_reviewed_recipes(self):
        folder = self.root/'recipes';folder.mkdir()
        for path, text in self.review['payload'].items():
            if path.startswith('internal_pcm_pair/recipes/'):
                target = folder/Path(path).relative_to('internal_pcm_pair/recipes');target.parent.mkdir(parents=True, exist_ok=True);target.write_text(text)
        script = self.root/'publisher.py';script.write_text(self.review['payload']['internal_pcm_pair/publish_recipes_ci.py'])
        module = runpy.run_path(str(script), run_name='synthetic_import')
        manifest = module['reviewed_inputs'](folder)
        self.assertEqual(set(manifest['components']), {'OFF', 'ON'})
        environment = {'GITLAB_CI': 'true', 'CI_PROJECT_PATH': PROJECT,
            'CI_COMMIT_BRANCH': self.review['private_branch'], 'ARTIFACTORY_CONAN_USER': 'synthetic-rt-svc',
            'ARTIFACTORY_CONAN_PASS': 'synthetic-test-only'}
        self.assertEqual(module['check_environment'](environment), 'synthetic-rt-svc')
        for change in ({'CI_PROJECT_PATH': 'another/project'}, {'CI_COMMIT_BRANCH': 'main'}, {'ARTIFACTORY_CONAN_USER': 'personal'}):
            with self.subTest(change=change), self.assertRaises(ValueError): module['check_environment'](dict(environment, **change))
        for mode in ('OFF', 'ON'):
            text = (folder/mode/'conanfile.py').read_text()
            self.assertIn("['WITH_EVAL_DUMP'] = '"+mode+"'", text)
            self.assertIn(self.review['source']['commit'], text)


if __name__ == '__main__': unittest.main()
