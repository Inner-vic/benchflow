"""Single ordinary APP, source binding, trusted confirmations and old-job recovery.

All platforms and users are synthetic; no builds, publication or bench actions.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from bench_gateway import pcm_release as release
from bench_gateway import pcm_dual_build as feature, pcm_dual_entry as entry
from bench_gateway.pcm_build_plan import checked
from bench_gateway.pcm_build_engine import JOB
from bench_gateway.pcm_dual_worker import Worker
from bench_gateway.core import GatewayError
from test_pcm_dual_entry import Client
from test_pcm_build_pipeline import resolve as old_resolve, FakeRemote, TEXT, URL, SESSION, NAMING, review_for
from source_build_discovery import common_inputs

CMAKE = b'option(WITH_EVAL_DUMP "Enable offline eval dump" OFF)\n'
RECIPE = '''class App:
    name = "uroad_m100"
    branch = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    def source(self):
        self.run("git init")
        self.run("git fetch --depth=1 https://gitlab.chehejia.com/livc-acore/chassis/uroad.git " + self.branch)
        self.run("git checkout FETCH_HEAD")
    def build(self):
        self._cmake.definitions["WITH_EVAL_DUMP"] = False
        self._cmake.configure()
'''.encode()


def resolve(request):
    result = old_resolve(request)
    result['source_files']['CMakeLists.txt'] = hashlib.sha256(CMAKE).hexdigest()
    result['discovery_sha256'] = feature.digest({k: v for k, v in result.items() if k != 'discovery_sha256'})
    return result


def proof(ref, raw=RECIPE):
    return {'state': 'VERIFIED', 'ref': ref, 'recipe_revision': 'f'*32 if ref.startswith('uroad_m100/') else 'd'*32,
            'recipe_sha256': hashlib.sha256(raw).hexdigest() if ref.startswith('uroad_m100/') else 'e'*64,
            'repository': 'liconan-dev-local'}


class Reader:
    def __init__(self, recipe=RECIPE, cmake=CMAKE):
        self.recipe, self.cmake = recipe, cmake
    def get(self, url, limit=1024*1024):
        if '/repository/files/CMakeLists.txt/raw?ref=' in url:
            return self.cmake
        if url.endswith('/export/conanfile.py'):
            return self.recipe
        raise AssertionError(url)


class ReleaseRemote(FakeRemote):
    def __init__(self, review):
        super().__init__(review)
        self.ambiguous = False;self.moved = False
    def submit_jenkins(self, params):
        self.submits.append('APP')
        self.dispatches['APP'] = {'queue_id': 101, 'queue_url': 'https://ci.ep.chehejia.com/queue/item/101/'}
        self.states.setdefault('APP', 'success')
        if self.timeout_submit:
            self.timeout_submit = False
            raise TimeoutError('synthetic lost response')
        return self.dispatches['APP']
    def build(self, mode):
        return {'number': 201, 'queueId': 101, 'url': JOB+'201/', 'building': self.states['APP'] == 'running',
                'result': 'FAILURE' if self.states['APP'] == 'failure' else 'SUCCESS',
                'actions': [{'parameters': [{'name': k, 'value': v} for k, v in self.review['parameters']['APP'].items()]}]}
    def lock(self, mode):
        rows = {}
        params = self.review['parameters']['APP']
        for i, ref in enumerate((params['APP_VERSION']+'\n'+params['LOCKED_LIBS']).splitlines()):
            if not ref or ref.startswith('conan_shared/'):
                continue
            rows[str(i)] = {'ref': ref+'#'+self.review['recipe_evidence'][ref.split('/')[0]]['recipe_revision'],
                            'package_id': 'synthetic-package', 'prev': 'synthetic-prev'}
        return json.dumps({'graph_lock': {'nodes': rows}}).encode()
    def post(self, url, body):
        raise AssertionError('Ordinary APP must never publish components or start GitLab CI')
    def json(self, url):
        path = unquote(urlsplit(url).path)
        if url.startswith(JOB) and 'property' in url:
            return {'property': [{'parameterDefinitions': [{'name': k} for k in self.review['parameters']['APP']]}]}
        if '/repository/branches/' in path:
            return {'commit': {'id': 'b'*40 if self.moved else self.review['source']['commit']}}
        if '/queue/item/' in path:
            return {} if self.states['APP'] == 'queued' else {'executable': {'url': JOB+'201/'}}
        return super().json(url)
    def get(self, url, limit=8*1024*1024):
        if '/repository/files/CMakeLists.txt/raw?ref=' in url:
            return CMAKE
        if url.endswith('/export/conanfile.py'):
            return RECIPE
        return super().get(url, limit)


def verify_remote(reader, ref, revision=None):
    if reader.ambiguous:
        return {'state': 'NEEDS_REVISION_SELECTION', 'ref': ref}
    return proof(ref)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        (self.root/'runtime').mkdir()
        (self.root/'runtime/pcm-dual-validation.json').write_text('{"enabled":true,"jenkins_enabled":true}')
        self.cfg = {'project_root': str(self.root), 'state_root': str(self.root/'state'), 'feishu_app_id': 'cli_test'}
        self.store = feature.Drafts(self.root/'isolated.sqlite3');self.client = Client()
        self.entry = entry.Entry(self.cfg, store=self.store, client=self.client, source_check=lambda *args: True)
        self.entry.open(SESSION)
        d = self.store.open('ou_tester', SESSION)
        self.ticket, _ = self.store.begin_validation('ou_tester', SESSION, d['id'], d['nonce'], d['revision'],
                                                    URL, TEXT, release=NAMING)
        self.report = release.validate(self.ticket, resolve, proof, Reader(), common_inputs)
        self.assertEqual(self.report['state'], 'VALIDATED', self.report.get('message'))
        self.store.finish_validation('ou_tester', SESSION, self.ticket['id'], self.ticket['revision'],
                                     self.ticket['attempt'], self.report)
        self.review = self.report['build_review'];self.jobs = self.entry.jobs
        self.remote = ReleaseRemote(self.review)
    def tearDown(self):
        self.temp.cleanup()
    def event(self):
        return {'operator_id': 'ou_tester', 'chat_id': 'oc_private', 'message_id': 'om_synthetic',
                'event_id': 'synthetic-confirm', 'action': 'pcm_confirm_build', 'form_value': {},
                'value': {'task_id': self.ticket['id'], 'nonce': self.review['nonce'], 'revision': self.ticket['revision']}}
    def confirm(self):
        self.entry.review_action(SESSION, self.event())
        return self.jobs.for_draft('ou_tester', SESSION, self.ticket['id'])
    def worker(self):
        return Worker(self.cfg, store=self.store, client=self.client, remote_factory=lambda owner: self.remote,
                      verify_recipe=verify_remote, output_root=self.root/'outputs')
    def finish(self, worker=None):
        worker = worker or self.worker()
        for _ in range(6):
            worker.once()

    def test_plan_has_original_m100_and_only_one_parameter_set(self):
        self.assertEqual(set(self.review['parameters']), {'APP'})
        self.assertIn('uroad_m100/3.7.0@livc/stable', self.review['parameters']['APP']['APP_VERSION'])
        self.assertNotIn('payload', self.review);self.assertNotIn('private_branch', self.review)
        self.assertEqual(self.report['app_count'], 1)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pcm_build_jobs').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM pcm_dual_defaults').fetchone()[0], 0)

    def test_card_uses_jenkins_name_and_explicit_final_confirmation(self):
        card = self.entry.card(self.store.open('ou_tester', SESSION))
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn('Jenkins 发包', text);self.assertIn('确认发包', text)
        self.assertNotIn('两版', text);self.assertNotIn('确认执行', text)
        for key in self.review['parameters']['APP']:
            self.assertIn(key, text)
        draft = dict(self.ticket, state='DRAFT', report=None)
        text = json.dumps(self.entry.card(draft), ensure_ascii=False)
        self.assertIn('Jenkins 发包', text);self.assertNotIn('评测版 ON', text)
        draft['component_versions'] = ''
        text = json.dumps(self.entry.card(draft), ensure_ascii=False)
        self.assertIn('conan_shared/@livc/release-schu', text)
        self.assertNotIn('<版本>', text)

    def test_entry_read_only_validator_uses_current_flow_without_ci_publication(self):
        import source_build_discovery as discovery_module
        scripts = self.root/'extensions/pcm-dual-validation/scripts';scripts.mkdir(parents=True)
        draft = self.store.open('ou_tester', SESSION)
        ticket, _ = self.store.begin_validation('ou_tester', SESSION, draft['id'], draft['nonce'],
            draft['revision'], URL, TEXT, release=NAMING)
        with patch.object(discovery_module, 'LiveReader', return_value=Reader()), \
             patch.object(discovery_module, 'discover', side_effect=lambda request, reader: resolve(request)), \
             patch.object(discovery_module, 'verify_recipe', side_effect=lambda reader, ref: proof(ref)):
            report, accepted = self.entry.read_only_validator(self.store, ticket)
        self.assertTrue(accepted);self.assertEqual(report['app_count'], 1)
        self.assertEqual(report['build_review']['schema'], release.SCHEMA)
        self.assertEqual(self.remote.submits, [])

    def test_one_build_one_original_card_and_resume_does_not_rebuild(self):
        job = self.confirm();self.confirm();self.finish()
        done = self.jobs.get(job['id'])
        self.assertEqual(done['state'], 'SUCCEEDED', done['error'])
        self.assertEqual(self.remote.submits, ['APP'])
        self.assertEqual(set(done['modes']), {'APP'})
        self.assertEqual(self.remote.posts, [])
        self.assertEqual(self.client.messages, 1)
        self.assertEqual(done['modes']['APP']['m100']['recipe_compile_mode'], 'OFF')
        self.finish(self.worker());self.assertEqual(self.remote.submits, ['APP'])
        with self.store.connect() as c:
            names = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('jobs', names);self.assertNotIn('task_requests', names)

    def test_source_mismatch_and_moving_recipe_branch_require_matching_version(self):
        for body in (RECIPE.replace(b'a'*40, b'b'*40), RECIPE.replace(b'a'*40, b'dev-example')):
            result = release.validate(self.ticket, resolve, lambda ref: proof(ref, body), Reader(body), common_inputs)
            self.assertEqual(result['state'], 'NEEDS_INPUT');self.assertNotIn('build_review', result)

    def test_eval_component_unknown_macro_or_changed_cmake_are_rejected(self):
        for body in (RECIPE.replace(b'= False', b'= True'), RECIPE.replace(b'= False', b'= self.options.eval_dump')):
            result = release.validate(self.ticket, resolve, lambda ref: proof(ref, body), Reader(body), common_inputs)
            self.assertEqual(result['state'], 'NEEDS_INPUT')
        result = release.validate(self.ticket, resolve, proof, Reader(cmake=CMAKE+b'# changed'), common_inputs)
        self.assertEqual(result['state'], 'NEEDS_INPUT')

    def test_frozen_parameters_cannot_omit_m100_or_change_release_scope(self):
        for field, value in [('APP_VERSION', 'conan_shared/8.4.0@livc/release-schu\ndag_compiler/2.0@livc/stable'),
                             ('LOCKED_LIBS', 'uroad_m100/3.7.0@livc/stable'),
                             ('SOFTWARE_BRANCH', '0112'), ('LIWARE_URL', 'unexpected')]:
            review = copy.deepcopy(self.review);review['parameters']['APP'][field] = value
            review['review_sha256'] = feature.digest({k: v for k, v in review.items() if k != 'review_sha256'})
            with self.subTest(field=field), self.assertRaises(ValueError): checked(review)

    def test_moved_branch_and_multiple_recipe_revisions_block_before_submit(self):
        job = self.confirm();self.remote.moved = True;self.finish()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'FAILED')
        self.assertEqual(self.remote.submits, [])
    def test_ambiguous_recipe_blocks_before_submit(self):
        job = self.confirm();self.remote.ambiguous = True;self.finish()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'FAILED')
        self.assertEqual(self.remote.submits, [])

    def test_source_change_after_preflight_still_blocks_first_submit(self):
        job = self.confirm();worker = self.worker();worker.once()
        self.remote.moved = True;self.finish(worker)
        self.assertEqual(self.jobs.get(job['id'])['state'], 'FAILED')
        self.assertEqual(self.remote.submits, [])

    def test_actual_m100_revision_mismatch_is_not_delivered_as_success(self):
        job = self.confirm();original = self.remote.lock
        self.remote.lock = lambda mode: original(mode).replace(('f'*32).encode(), ('a'*32).encode())
        self.finish()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'NEEDS_REVIEW')
        self.assertEqual(self.remote.submits, ['APP'])

    def test_confirming_wrong_user_card_revision_or_moved_source_creates_nothing(self):
        for field, value in [('operator_id', 'ou_other'), ('message_id', 'om_wrong'), ('chat_id', 'oc_wrong')]:
            event = self.event();event[field] = value
            with self.subTest(field=field), self.assertRaises((GatewayError, ValueError)):
                self.entry.review_action(SESSION, event)
        with patch.object(self.entry, 'source_check', return_value=False), self.assertRaises(GatewayError): self.confirm()
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pcm_build_jobs').fetchone()[0], 0)

    def test_lost_response_reconciles_original_build(self):
        job = self.confirm();self.remote.timeout_submit = True;self.finish()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'SUCCEEDED')
        self.assertEqual(self.remote.submits, ['APP'])

    def test_unknown_submit_intent_is_never_reposted(self):
        job = self.confirm();worker = self.worker();worker.once()
        params = self.review['parameters']['APP']
        intent = {'mode': 'APP', 'parameters_sha': hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()}
        with self.store.connect() as c:
            c.execute("INSERT INTO pcm_build_steps VALUES(?,?,'INTENT',?,NULL,0)",
                      (job['id'], 'JENKINS_APP', json.dumps(intent)))
        self.finish(worker)
        self.assertEqual(self.jobs.get(job['id'])['state'], 'NEEDS_REVIEW');self.assertEqual(self.remote.submits, [])

    def test_queued_running_failed_or_delayed_artifacts_do_not_resubmit(self):
        job = self.confirm();self.remote.states['APP'] = 'queued';self.finish()
        self.assertEqual(self.remote.submits, ['APP'])
        self.remote.states['APP'] = 'running';self.finish()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'RUNNING')
        self.remote.states['APP'] = 'success';self.remote.artifacts_missing = True;self.finish()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'RUNNING')
        self.remote.artifacts_missing = False;self.finish()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'SUCCEEDED');self.assertEqual(self.remote.submits, ['APP'])

    def test_build_failure_stops_without_retry(self):
        job = self.confirm();self.remote.states['APP'] = 'failure';self.finish()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'FAILED');self.assertEqual(self.remote.submits, ['APP'])

    def test_notification_failure_retries_only_original_card_delivery(self):
        job = self.confirm()
        with patch.object(self.client, '_json', side_effect=TimeoutError('synthetic delivery failure')): self.finish()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'SUCCEEDED')
        self.finish();self.assertEqual(self.client.messages, 1);self.assertEqual(self.remote.submits, ['APP'])

    def test_preferences_save_only_on_confirmation_and_remain_personal(self):
        self.confirm()
        own = self.store.open('ou_tester', SESSION)
        other = self.store.open('ou_other', 'feishu:oc_other:ou_other')
        self.assertEqual(own['component_versions'], TEXT)
        self.assertEqual(own['branch_url'], '');self.assertEqual(own['release_parameters'], {})
        self.assertEqual(other['component_versions'], '')

    def test_commands_are_exact_and_old_command_is_a_current_flow_alias(self):
        for text in ('Jenkins 发包', 'jenkins发包', '/jenkins_build', 'PCM双版本构建'):
            self.assertTrue(entry.is_command(text))
        self.assertFalse(entry.is_command('请问Jenkins发包如何使用'))

    def test_unconfirmed_old_review_cannot_create_new_pair_job(self):
        old = review_for(self.ticket)
        with self.store.connect() as c:
            c.execute('UPDATE pcm_dual_drafts SET report=? WHERE id=?', (json.dumps(old), self.ticket['id']))
        event = self.event();event['value']['nonce'] = old['build_review']['nonce']
        with self.assertRaisesRegex(GatewayError, '旧版'): self.entry.review_action(SESSION, event)
        self.entry.open(SESSION)
        draft = self.store.open('ou_tester', SESSION)
        self.assertEqual(draft['state'], 'DRAFT');self.assertEqual(draft['component_versions'], TEXT)
        self.assertIsNone(draft['report'])


if __name__ == '__main__':
    unittest.main()
