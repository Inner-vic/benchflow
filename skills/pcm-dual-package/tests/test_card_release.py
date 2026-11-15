"""Synthetic card-to-plan and OFF/ON delivery journeys; no service calls."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_existing_job as fixtures
import plan_from_card as bridge


LINK = 'https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/dev-314'
REFS = ('conan_shared/1.0.38-avm@livc/release-schu\n'
        'uroad_m100/3.1.4@livc/stable\n'
        'dag_compiler/0.0.19@livc/stable\n'
        'anti_pinch/0.0.10@livc/stable')


def action(view, prefix):
    return next(e['name'] for f in view['card']['body']['elements'] if f['tag'] == 'form'
                for e in f['elements'] if e['tag'] == 'button' and e['name'].startswith(prefix + '__'))


def context(published=True):
    value = fixtures.context(published)
    value['input_component'] = {'ref': 'uroad_m100/3.1.4@livc/stable',
                                'source_commit': value['source']['commit']}
    return value


def result(state, mode):
    number = state['builds'][mode]['build_url'].rstrip('/').rsplit('/', 1)[1]
    return {'build_url': state['builds'][mode]['build_url'], 'result': 'SUCCESS',
            'app_url': fixtures.job.ARTIFACT_ROOT + 'Release/app/app-' + number + '.app.tar.gz',
            'lock_url': fixtures.job.ARTIFACT_ROOT + 'Release/app-' + number + '.lock',
            'app_sha256': ('a' if mode == 'OFF' else 'b') * 64,
            'lock_sha256': ('c' if mode == 'OFF' else 'd') * 64}


class CardToRelease(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pcm-card-release-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cards = bridge.card.InputCards(self.root / 'preferences.sqlite3')
        self.actor = bridge.card.Identity('synthetic-tenant', 'synthetic-user')
        self.fields = {'branch_link': LINK, 'app_version': REFS}

    def confirm(self):
        initial = self.cards.open(self.actor)
        checked = self.cards.submit(self.actor, action(initial, 'check'), self.fields)
        return self.cards.submit(self.actor, action(checked, 'save'), self.fields)

    def plan(self, ctx=None):
        view = self.confirm()
        return bridge.plan_from_card(self.cards, self.actor, view['session_id'], ctx or context())

    def test_owner_confirmation_and_source_evidence_create_two_original_parameter_sets(self):
        state = self.plan()
        off, on = [state['builds'][mode]['parameters'] for mode in ('OFF', 'ON')]
        self.assertEqual(['APP_VERSION'], [k for k in off if off[k] != on[k]])
        for mode in ('OFF', 'ON'):
            params = state['builds'][mode]['parameters']
            self.assertIn('conan_shared/1.0.38-avm@livc/release-schu', params['APP_VERSION'])
            self.assertIn('anti_pinch/0.0.10@livc/stable', params['APP_VERSION'])
            self.assertIn(state['builds'][mode]['component']['ref'], params['APP_VERSION'])
            self.assertNotIn('uroad_m100/3.1.4@livc/stable', params['APP_VERSION'])
        self.assertEqual('READY_FOR_REVIEW', state['status'])
        self.assertEqual(self.actor.user, state['input_confirmation']['owner']['user'])

    def test_draft_other_owner_and_superseded_card_cannot_start_a_plan(self):
        draft = self.cards.open(self.actor)
        with self.assertRaises(ValueError):
            bridge.plan_from_card(self.cards, self.actor, draft['session_id'], context())
        confirmed = self.confirm()
        other = bridge.card.Identity('synthetic-tenant', 'another-user')
        with self.assertRaises(ValueError):
            bridge.plan_from_card(self.cards, other, confirmed['session_id'], context())
        self.confirm()
        with self.assertRaises(ValueError):
            bridge.plan_from_card(self.cards, self.actor, confirmed['session_id'], context())

    def test_changed_export_or_unverified_source_component_is_rejected(self):
        view = self.confirm()
        snapshot = self.cards.export_confirmed(self.actor, view['session_id'])
        changed = copy.deepcopy(snapshot)
        changed['app_version'] += '\nextra/1@livc/stable'
        with self.assertRaises(ValueError):
            bridge.plan_from_confirmed(changed, context())
        for key in ('branch', 'commit'):
            ctx = context(); ctx['source'][key] = 'another' if key == 'branch' else '9' * 40
            with self.assertRaises(ValueError):
                bridge.plan_from_confirmed(snapshot, ctx)
        ctx = context(); del ctx['input_component']
        with self.assertRaises(ValueError):
            bridge.plan_from_confirmed(snapshot, ctx)

    def test_user_versions_override_baseline_app_defaults_but_locked_overlap_stops(self):
        ctx = context(); ctx['common_app_refs'].append('dag_compiler/0.0.1@livc/stable')
        state = self.plan(ctx)
        self.assertNotIn('dag_compiler/0.0.1@livc/stable', state['builds']['OFF']['parameters']['APP_VERSION'])
        view = self.confirm()
        ctx = context(); ctx['common_locked_refs'].append('dag_compiler/0.0.1@livc/stable')
        with self.assertRaises(ValueError):
            bridge.plan_from_card(self.cards, self.actor, view['session_id'], ctx)

    def test_unpublished_components_remain_blocked_and_confirmation_is_bound_to_review_hash(self):
        state = self.plan(context(False))
        self.assertEqual('COMPONENT_RECIPES_REQUIRED', state['status'])
        with self.assertRaises(ValueError):
            fixtures.job.submit_intent(state, 'OFF', state['plan_sha256'])
        state = self.plan()
        state['input_confirmation']['configuration_revision'] += 1
        with self.assertRaises(ValueError):
            fixtures.job.submit_intent(state, 'OFF', state['plan_sha256'])

    def test_preparation_and_delivery_export_do_not_call_network_or_processes(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('process forbidden')):
            state = self.plan()
            bridge.write_plan(state, self.root / 'new-pair')
            self.assertIsNone(fixtures.job.delivery_manifest(state)['packages']['ON']['artifacts'])
        with self.assertRaises(FileExistsError):
            bridge.write_plan(state, self.root / 'new-pair')

    def test_cli_from_unrelated_cwd_writes_both_plans_and_labelled_index_without_building(self):
        view = self.confirm()
        snapshot = self.cards.export_confirmed(self.actor, view['session_id'])
        source = self.root / 'confirmed.json'; ctx = self.root / 'context.json'
        source.write_bytes(fixtures.job.canonical(snapshot)); ctx.write_bytes(fixtures.job.canonical(context()))
        output = self.root / 'cli-pair'
        run = subprocess.run([sys.executable, '-B', str(Path(bridge.__file__)),
            '--confirmed-input', str(source), '--context', str(ctx), '--output-dir', str(output)],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(0, json.loads(run.stdout)['builds_submitted'])
        self.assertEqual({'pair.json', 'OFF.parameters.json', 'ON.parameters.json',
                          'package-index.json', 'package-index.md'}, {p.name for p in output.iterdir()})


class PackageIdentity(unittest.TestCase):
    def pair(self):
        state = fixtures.job.make_plan(context())
        fixtures.start_pair(state)
        for mode in ('OFF', 'ON'):
            fixtures.job.record_result(state, mode, result(state, mode))
        return state

    def test_each_mode_maps_to_distinct_real_recorded_build_and_original_filename(self):
        state = self.pair(); manifest = fixtures.job.delivery_manifest(state)
        self.assertEqual('普通版 OFF', manifest['packages']['OFF']['label'])
        self.assertEqual('评测版 ON', manifest['packages']['ON']['label'])
        self.assertEqual(101, manifest['packages']['OFF']['build_number'])
        self.assertEqual(102, manifest['packages']['ON']['build_number'])
        self.assertTrue(manifest['packages']['OFF']['artifacts']['app_url'].endswith('/app-101.app.tar.gz'))
        self.assertEqual('OFF', manifest['packages']['OFF']['download_subdirectory'])
        text = fixtures.job.summary(state)
        for value in ('普通版 OFF', '评测版 ON', '#101', '#102', 'APP SHA-256'):
            self.assertIn(value, text)

    def test_swapped_result_same_build_or_wrong_variant_cannot_be_labelled_as_valid(self):
        for change in ('result', 'build', 'mode'):
            state = self.pair()
            if change == 'result': state['builds']['ON']['result'] = state['builds']['OFF']['result']
            if change == 'build': state['builds']['ON']['build_url'] = state['builds']['OFF']['build_url']
            if change == 'mode': state['builds']['ON']['component']['cmake']['WITH_EVAL_DUMP'] = 'OFF'
            with self.subTest(change=change), self.assertRaises(ValueError):
                fixtures.job.delivery_manifest(state)

    def test_partial_result_has_no_invented_on_artifact_and_comparison_failure_is_preserved(self):
        state = fixtures.job.make_plan(context()); fixtures.start_pair(state)
        fixtures.job.record_result(state, 'OFF', result(state, 'OFF'))
        fixtures.job.record_result(state, 'ON', {'build_url': state['builds']['ON']['build_url'], 'result': 'FAILURE'})
        manifest = fixtures.job.delivery_manifest(state)
        self.assertEqual('PARTIAL_OR_FAILED', manifest['status'])
        self.assertIsNone(manifest['packages']['ON']['artifacts'])
        state = self.pair(); state.update(status='COMPARISON_FAILED', comparison_error='synthetic extra difference')
        manifest = fixtures.job.delivery_manifest(state)
        self.assertEqual('COMPARISON_FAILED', manifest['status'])
        self.assertFalse(manifest['runtime_verified'])

    def test_delivery_export_cli_does_not_mutate_state_or_overwrite_an_existing_index(self):
        with tempfile.TemporaryDirectory(prefix='pcm-delivery-cli-') as folder:
            root = Path(folder); state = self.pair()
            state.update(status='COMPARISON_FAILED', comparison_error='synthetic extra difference')
            path = root / 'pair.json'; path.write_bytes(fixtures.job.canonical(state))
            before = path.read_bytes(); output = root / 'delivery'
            command = [sys.executable, '-B', str(Path(fixtures.job.__file__)), 'export-delivery',
                       '--state', str(path), '--output-dir', str(output)]
            first = subprocess.run(command, cwd=root, capture_output=True, timeout=30)
            self.assertEqual(0, first.returncode, first.stderr)
            index = (output / 'package-index.json').read_bytes()
            self.assertEqual('COMPARISON_FAILED', json.loads(index)['status'])
            second = subprocess.run(command, cwd=root, capture_output=True, timeout=30)
            self.assertNotEqual(0, second.returncode)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(index, (output / 'package-index.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
