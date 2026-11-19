"""Standalone feature only. Synthetic users, private test DB, no CI or hardware."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error

from bench_gateway.pcm_dual_build import (FEATURE, Drafts, branch_link, components,
                                         digest, render_card, run_validation, validate_configuration, release_parameters, COMPONENT_TEMPLATE)

URL = 'https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/feature/new'
RELEASE = {'BASE_LINE': '00B1', 'SOFTWARE_BRANCH': '0111', 'BUILDVARIANT': 'release'}
TEXT = 'conan_shared/8.4.0@livc/release-schu\nuroad_m100/3.7.0@livc/stable\ndag_compiler/2.0.0@livc/stable\nsensor/1.2.0@livc/stable'


def discovery(request):
    result = {'state': 'RESOLVED_FOR_REVIEW', 'source': {'repository': 'https://gitlab.chehejia.com/livc-acore/chassis/uroad.git', 'branch': 'feature/new', 'commit': 'a'*40},
            'resolved_dependencies': {'sensor': 'sensor/1.2.0@livc/stable'},
            'baseline': {'number': 101, 'parameters': {'APP_VERSION': 'conan_shared/8.4.0@livc/release-schu'}},
            'component_overrides': {ref.split('/')[0]: ref for ref in request.get('component_overrides', [])},
            'integration_package_names': ['uroad_m100', 'sensor', 'extra_component']}
    result['discovery_sha256'] = digest(result)
    return result


def verify(ref):
    return {'ref': ref, 'state': 'VERIFIED', 'recipe_revision': 'c'*31, 'recipe_sha256': 'd'*64}


def report():
    return validate_configuration({'release_parameters': RELEASE, 'branch_url': URL, 'component_versions': TEXT}, discovery, verify)


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from nodes(child)
    elif isinstance(value, list):
        for child in value: yield from nodes(child)


class InputTests(unittest.TestCase):
    def test_validating_card_never_marks_disabled_fields_required(self):
        draft = {'id': 'd', 'nonce': 'a'*32, 'revision': 2, 'state': 'VALIDATING',
                 'branch_url': URL, 'component_versions': TEXT, 'release_parameters': RELEASE}
        rendered = render_card(draft, callback_value={'feature': FEATURE, 'action': 'validate_build_config',
            'draft_id': 'd', 'nonce': 'a'*32, 'revision': 2})
        inputs = [n for n in nodes(rendered) if n.get('tag') in ('input', 'select_static')]
        self.assertEqual(len(inputs), 4)
        self.assertTrue(all(n.get('disabled') is True for n in inputs))
        self.assertTrue(all(n.get('required') is False for n in inputs))
        draft['state'] = 'DRAFT'
        editable = render_card(draft, callback_value={'feature': FEATURE, 'action': 'validate_build_config',
            'draft_id': 'd', 'nonce': 'a'*32, 'revision': 2})
        self.assertTrue(all(n['required'] for n in nodes(editable) if n.get('tag') in ('input', 'select_static')))

    def test_three_required_components_cannot_be_placeholders_or_silently_inherited(self):
        for text in ('', COMPONENT_TEMPLATE, TEXT.replace('dag_compiler/2.0.0@livc/stable\n', '')):
            with self.subTest(text=text), self.assertRaises(ValueError): components(text, require_required=True)
        self.assertIn('uroad_m100', components(TEXT, require_required=True))
        with self.assertRaises(ValueError): components('unknown/<版本>@livc/stable')

    def test_release_identity_is_explicit_and_normalized_without_historical_defaults(self):
        self.assertEqual(release_parameters({'BASE_LINE': 'sw00b1', 'SOFTWARE_BRANCH': 'ID0111', 'BUILDVARIANT': 'release'}), RELEASE)
        self.assertEqual(release_parameters({'BASE_LINE': '00B1', 'BUILDVARIANT': 'release'}), RELEASE)
        for value in (None, {}, dict(RELEASE, BASE_LINE=''), dict(RELEASE, SOFTWARE_BRANCH='0112'),
                      dict(RELEASE, BASE_LINE='XXXX'), dict(RELEASE, SOFTWARE_BRANCH='12'),
                      dict(RELEASE, BUILDVARIANT='arbitrary')):
            with self.subTest(value=value), self.assertRaises(ValueError): release_parameters(value)
        def no_call(*args): raise AssertionError('Missing release identity must be rejected before network')
        result = validate_configuration({'branch_url': URL, 'component_versions': TEXT}, no_call, no_call)
        self.assertEqual(result['state'], 'NEEDS_INPUT')

    def test_one_component_per_line_and_no_silent_duplicate_or_range(self):
        self.assertEqual(len(components(TEXT)), 4)
        for text in (TEXT+'\nsensor/1.3@livc/stable', 'sensor/[>1.0]@livc/stable',
                     'sensor=1.0', TEXT+'\nuroad_m100/1.0@livc/stable', 'x'*1001):
            with self.subTest(text=text[:40]), self.assertRaises(ValueError): components(text)

    def test_branch_is_explicit_not_repository_home_or_credential_url(self):
        self.assertEqual(branch_link(URL), URL)
        for value in ('https://gitlab.chehejia.com/team/repo',
                      URL.replace('https://', 'https://user:secret@'),
                      URL.replace('gitlab.chehejia.com', 'evil.invalid'), URL+'?token=secret'):
            with self.subTest(value=value), self.assertRaises(ValueError): branch_link(value)

    def test_validation_is_explicit_two_apps_and_classifies_appended_components(self):
        text = TEXT+'\nextra_component/9.0@livc/stable\nutility/2.0@livc/stable'
        result = validate_configuration({'release_parameters': RELEASE, 'branch_url': URL, 'component_versions': text}, discovery, verify)
        self.assertEqual(result['delivery'], 'app_pair')
        self.assertEqual(result['app_count'], 2)
        self.assertEqual(result['requested_app_modes'], ['OFF', 'ON'])
        self.assertIn('extra_component/9.0@livc/stable', result['common_app_refs'])
        self.assertIn('utility/2.0@livc/stable', result['common_locked_refs'])
        self.assertFalse(result['jenkins_submitted'])
        self.assertEqual(result['bench_tasks'], 0)
        self.assertEqual(result['compile_runtime_compatibility'], 'NOT_VALIDATED')

    def test_missing_required_versions_stops_before_discovery(self):
        def no_call(*args): raise AssertionError('Must not fetch on missing required component')
        result = validate_configuration({'release_parameters': RELEASE, 'branch_url': URL, 'component_versions': ''}, no_call, no_call)
        self.assertEqual(result['state'], 'NEEDS_INPUT')
        self.assertIn('conan_shared', result['message'])

    def test_m100_input_is_preserved_as_recipe_basis_not_a_common_reused_binary(self):
        result = report()
        self.assertEqual(result['state'], 'VALIDATED')
        self.assertEqual(result['m100_recipe_ref'], 'uroad_m100/3.7.0@livc/stable')
        self.assertEqual(result['m100_source_commit'], 'a'*40)
        self.assertFalse(any(v.startswith('uroad_m100/') for v in result['common_app_refs']+result['common_locked_refs']))
        self.assertEqual(result['effective_component_versions'], TEXT)

    def test_bad_input_never_invokes_readers(self):
        def no_call(*args): raise AssertionError('Unexpected reader')
        result = validate_configuration({'branch_url': 'bad', 'component_versions': TEXT}, no_call, no_call)
        self.assertEqual(result['state'], 'NEEDS_INPUT')

    def test_overrides_are_sent_for_source_and_baseline_compatibility_validation(self):
        calls = []
        def resolver(request):
            calls.append(request)
            return {'state': 'DEPENDENCY_OVERRIDE_CONFLICT'}
        result = validate_configuration({'release_parameters': RELEASE, 'branch_url': URL, 'component_versions': TEXT}, resolver, verify)
        self.assertEqual(calls[0]['component_overrides'], TEXT.splitlines())
        self.assertEqual(result['state'], 'NEEDS_INPUT')

    def test_unverified_remote_recipe_blocks_result(self):
        for field, value in [('state', 'UNKNOWN'), ('ref', 'wrong'), ('recipe_revision', ''), ('recipe_sha256', '')]:
            def invalid(ref): return dict(verify(ref), **{field: value})
            with self.subTest(field=field):
                self.assertEqual(validate_configuration({'release_parameters': RELEASE, 'branch_url': URL, 'component_versions': TEXT}, discovery, invalid)['state'], 'NEEDS_INPUT')

    def test_cached_wrong_branch_or_ignored_override_cannot_validate(self):
        def wrong(request):
            value = discovery(request);value['source']['branch'] = 'other'
            return value
        self.assertEqual(validate_configuration({'release_parameters': RELEASE, 'branch_url': URL, 'component_versions': TEXT}, wrong, verify)['state'], 'NEEDS_INPUT')


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Drafts(Path(self.temp.name)/'feature.sqlite3')
        self.draft = self.store.open('owner-a', 'session-a')

    def tearDown(self): self.temp.cleanup()

    def begin(self, draft=None, text=TEXT):
        d = draft or self.draft
        return self.store.begin_validation(d['owner'], d['session'], d['id'], d['nonce'], d['revision'], URL, text, release=RELEASE)

    def complete(self, ticket):
        self.assertTrue(self.store.finish_validation(ticket['owner'], ticket['session'], ticket['id'],
                ticket['revision'], ticket['attempt'], report()))

    def test_first_use_is_empty_and_open_preserves_existing_draft(self):
        self.assertEqual(self.draft['branch_url'], '')
        self.assertEqual(self.draft['component_versions'], '')
        ticket, started = self.begin()
        self.assertTrue(started)
        self.assertEqual(self.store.open('owner-a', 'session-a')['component_versions'], TEXT)
        self.assertEqual(self.store.open('owner-a', 'session-a')['id'], ticket['id'])
        self.assertEqual(ticket['release_parameters'], RELEASE)
        self.assertEqual(self.store.open('owner-a', 'next-session')['release_parameters'], {})

    def test_release_change_gets_new_revision_and_cannot_be_overwritten_by_old_result(self):
        first, _ = self.begin();self.complete(first)
        current = self.store.open('owner-a', 'session-a')
        updated = dict(RELEASE, BASE_LINE='00B2')
        second, started = self.store.begin_validation('owner-a', 'session-a', current['id'], current['nonce'],
                current['revision'], URL, TEXT, release=updated)
        self.assertTrue(started);self.assertEqual(second['release_parameters'], updated)
        with self.assertRaises(ValueError):
            self.store.finish_validation('owner-a', 'session-a', second['id'], second['revision'], second['attempt'], report())

    def test_old_database_upgrade_preserves_material_and_adds_no_release_default(self):
        import sqlite3
        path = Path(self.temp.name)/'old.sqlite3'
        from contextlib import closing
        with closing(sqlite3.connect(path)) as c:
            c.execute('''CREATE TABLE pcm_dual_drafts(id TEXT PRIMARY KEY,owner TEXT,session TEXT,revision INTEGER,
                         nonce TEXT,state TEXT,branch_url TEXT,component_versions TEXT,attempt TEXT,
                         last_revision INTEGER,input_sha TEXT,report TEXT,baseline_build INTEGER)''')
            c.execute('INSERT INTO pcm_dual_drafts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      ('old', 'owner-a', 'session-a', 7, 'nonce', 'DRAFT', URL, TEXT, None, None, None, None, 101))
            c.commit()
        d = Drafts(path).open('owner-a', 'session-a')
        self.assertEqual((d['id'], d['revision'], d['component_versions'], d['baseline_build']), ('old', 7, TEXT, 101))
        self.assertEqual(d['release_parameters'], {})

    def test_only_confirmed_defaults_are_reused_by_same_user(self):
        ticket, _ = self.begin();self.complete(ticket)
        self.assertEqual(self.store.open('owner-a', 'other-session')['component_versions'], '')
        self.assertTrue(self.store.confirm_configuration('owner-a', 'session-a', ticket['id'], ticket['nonce'], ticket['revision'], digest(report())))
        self.assertFalse(self.store.confirm_configuration('owner-a', 'session-a', ticket['id'], ticket['nonce'], ticket['revision'], digest(report())))
        current = self.store.open('owner-a', 'session-a')
        self.assertEqual(current['component_versions'], report()['effective_component_versions'])
        self.assertEqual(current['branch_url'], '')
        self.assertEqual(self.store.open('owner-b', 'session-a')['component_versions'], '')

    def test_validation_and_duplicate_event_do_not_confirm_or_queue_build(self):
        ticket, started = self.begin()
        again, started_again = self.begin()
        self.assertTrue(started);self.assertFalse(started_again)
        self.assertEqual(again['attempt'], ticket['attempt'])
        self.complete(ticket)
        again, started_again = self.begin()
        self.assertFalse(started_again)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pcm_dual_defaults').fetchone()[0], 0)
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'pcm_dual_defaults', 'pcm_dual_drafts'})

    def test_cross_user_session_nonce_or_revision_cannot_edit_or_confirm(self):
        d = self.draft
        for owner, session, nonce, revision in [('b', d['session'], d['nonce'], 1),
                (d['owner'], 'other', d['nonce'], 1), (d['owner'], d['session'], 'bad', 1),
                (d['owner'], d['session'], d['nonce'], 5)]:
            with self.subTest(owner=owner, session=session, revision=revision), self.assertRaises(ValueError):
                self.store.begin_validation(owner, session, d['id'], nonce, revision, URL, TEXT, release=RELEASE)
        with self.assertRaises(ValueError):
            self.store.confirm_configuration(d['owner'], d['session'], d['id'], d['nonce'], 1, digest({}))

    def test_stale_validation_result_cannot_replace_newer_revision(self):
        first, _ = self.begin()
        self.assertTrue(self.store.finish_validation(first['owner'], first['session'], first['id'], first['revision'], first['attempt'], {'state': 'NEEDS_INPUT', 'message': '缺材料'}))
        current = self.store.open(first['owner'], first['session'])
        second, _ = self.begin(current, TEXT+'\nextra_component/2.0@livc/stable')
        self.assertFalse(self.store.finish_validation(first['owner'], first['session'], first['id'], first['revision'], first['attempt'], report()))
        self.assertEqual(self.store.open(first['owner'], first['session'])['attempt'], second['attempt'])

    def test_worker_failure_is_visible_and_preserves_inputs_without_saving_defaults(self):
        ticket, _ = self.begin()
        def failed(*args): raise RuntimeError('fixture secret must not be shown')
        result, accepted = run_validation(self.store, ticket, failed, verify)
        self.assertTrue(accepted);self.assertEqual(result['state'], 'NEEDS_INPUT')
        self.assertNotIn('fixture secret', json.dumps(result))
        self.assertEqual(self.store.open('owner-a', 'session-a')['component_versions'], TEXT)
        self.assertEqual(self.store.open('owner-a', 'new-session')['component_versions'], '')

    def test_single_app_result_cannot_be_saved_as_dual_app_validation(self):
        ticket, _ = self.begin();result = report();result['app_count'] = 1
        with self.assertRaises(ValueError):
            self.store.finish_validation(ticket['owner'], ticket['session'], ticket['id'], ticket['revision'], ticket['attempt'], result)

    def test_remote_failures_show_safe_actionable_reason_without_credentials(self):
        errors = [(TimeoutError('secret'), '查询超时'),
                  (urllib.error.HTTPError('https://ci.ep.chehejia.com/private?token=secret', 403, 'secret', {}, None), 'Jenkins读取认证'),
                  (urllib.error.HTTPError('https://gitlab.chehejia.com/private', 404, 'secret', {}, None), 'GitLab中未找到'),
                  (urllib.error.URLError('secret'), '暂时不可达')]
        for error, expected in errors:
            with self.subTest(expected=expected):
                ticket, _ = self.begin(self.store.open('owner-a', 'session-a'))
                def failed(*args): raise error
                result, accepted = run_validation(self.store, ticket, failed, verify)
                self.assertTrue(accepted)
                self.assertIn(expected, result['message'])
                self.assertNotIn('secret', json.dumps(result))


class CardTests(unittest.TestCase):
    def test_empty_component_field_displays_editable_template_without_fixed_versions(self):
        d = self.draft();d['component_versions'] = ''
        field = next(n for n in nodes(render_card(d)) if n.get('name') == 'pcm_dual_component_versions')
        self.assertEqual(field['default_value'], COMPONENT_TEMPLATE)
        self.assertEqual(len(field['default_value'].splitlines()), 3)
        with self.assertRaises(ValueError): components(field['default_value'])

    def draft(self):
        return {'id': 'synthetic', 'revision': 1, 'nonce': 'nonce', 'state': 'DRAFT',
                'branch_url': '', 'component_versions': TEXT, 'report': None}

    def test_independent_card_with_explicit_release_identity(self):
        card = render_card(self.draft());all_nodes = list(nodes(card))
        self.assertEqual(card['header']['title']['content'], 'PCM 双版本构建')
        inputs = {n['name']: n for n in all_nodes if n.get('tag') == 'input'}
        self.assertEqual(set(inputs), {'pcm_dual_branch_url', 'pcm_dual_component_versions', 'pcm_dual_release_version'})
        self.assertEqual(inputs['pcm_dual_component_versions']['input_type'], 'multiline_text')
        self.assertEqual(inputs['pcm_dual_component_versions']['default_value'], TEXT)
        text = json.dumps(card, ensure_ascii=False)
        self.assertNotIn('数据集', text);self.assertNotIn('回放方式', text)
        self.assertIn('普通版 OFF', text);self.assertIn('评测版 ON', text)
        self.assertTrue(next(n for n in all_nodes if n.get('tag') == 'button')['disabled'])

    def test_host_binding_only_enables_validate_action_never_build_or_bench(self):
        d = self.draft();value = {'feature': FEATURE, 'action': 'validate_build_config',
            'draft_id': d['id'], 'revision': d['revision'], 'nonce': d['nonce']}
        card = render_card(d, callback_value=value)
        buttons = [n for n in nodes(card) if n.get('tag') == 'button']
        self.assertEqual(len(buttons), 1);self.assertFalse(buttons[0]['disabled'])
        self.assertEqual(buttons[0]['form_action_type'], 'submit')
        self.assertNotIn('behaviors', buttons[0])
        self.assertEqual(buttons[0]['name'], 'bench_confirm_nonce_1')
        with self.assertRaises(ValueError): render_card(d, callback_value=dict(value, revision=99))

    def test_validation_feedback_is_in_same_card(self):
        d = self.draft();d.update(state='VALIDATING')
        self.assertIn('正在校验', json.dumps(render_card(d), ensure_ascii=False))
        d.update(state='NEEDS_INPUT', report={'message': '组件重复，请修改。'})
        self.assertIn('组件重复', json.dumps(render_card(d), ensure_ascii=False))
