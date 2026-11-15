"""Offline user journeys and isolation checks; no platform credentials or network."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('jenkins_input_card', Path(__file__).parents[1] / 'jenkins_input_card.py')
card = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = card
spec.loader.exec_module(card)

BRANCH = 'https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/dev-315-eval'
VERSIONS = ('conan_shared/1.0.38-avm@livc/release-schu\n'
            'uroad_m100/3.1.7-debugTest3@livc/stable\n'
            'dag_compiler/0.0.19-debugTest1@livc/stable')
EXTRA = '\nanti_pinch/0.0.10@livc/stable'


def fields(branch=BRANCH, refs=VERSIONS):
    return {'branch_link': branch, 'app_version': refs}


def inputs(view):
    return {item['name']: item['default_value']
            for element in view['card']['body']['elements'] if element['tag'] == 'form'
            for item in element['elements'] if item['tag'] == 'input'}


def button(view, verb):
    return next(item['name']
                for element in view['card']['body']['elements'] if element['tag'] == 'form'
                for item in element['elements'] if item['tag'] == 'button' and item['name'].startswith(verb + '__'))


class UserJourneys(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='jenkins-input-test-')
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'preferences.sqlite3'
        self.now = [1000.0]
        self.cards = card.InputCards(self.database, clock=lambda: self.now[0])
        self.alice = card.Identity('tenant-one', 'alice')
        self.bob = card.Identity('tenant-one', 'bob')

    def checked(self, data=None, identity=None, view=None):
        identity = identity or self.alice
        view = view or self.cards.open(identity)
        return self.cards.submit(identity, button(view, 'check'), data or fields())

    def confirmed(self, data=None, identity=None):
        data, identity = data or fields(), identity or self.alice
        view = self.checked(data, identity)
        return self.cards.submit(identity, button(view, 'save'), data)

    def test_first_open_has_empty_version_template_without_prefill_button(self):
        view = self.cards.open(self.alice)
        self.assertEqual(card.EMPTY_TEMPLATE, inputs(view)['app_version'])
        self.assertEqual('', inputs(view)['branch_link'])
        self.assertEqual('EMPTY_TEMPLATE', view['prefill'])
        form = next(e for e in view['card']['body']['elements'] if e['tag'] == 'form')
        self.assertEqual(['检查参数'], [e['text']['content'] for e in form['elements'] if e['tag'] == 'button'])
        self.assertIsNone(self.cards.last_confirmed(self.alice))

    def test_unfilled_template_cannot_be_confirmed(self):
        view = self.checked(fields(refs=card.EMPTY_TEMPLATE))
        self.assertEqual('EDITING', view['phase'])
        self.assertTrue(view['errors'])
        self.assertIsNone(self.cards.last_confirmed(self.alice))

    def test_edit_append_confirm_reopen_and_restart_preserve_personal_versions(self):
        data = fields(refs=VERSIONS + EXTRA)
        done = self.confirmed(data)
        self.assertEqual('CONFIRMED', done['phase'])
        restarted = card.InputCards(self.database)
        reopened = restarted.open(self.alice)
        self.assertEqual(data['app_version'], inputs(reopened)['app_version'])
        self.assertEqual('PERSONAL_CONFIRMED', reopened['prefill'])
        self.assertEqual('', inputs(reopened)['branch_link'])
        self.assertEqual(card.EMPTY_TEMPLATE, inputs(restarted.open(self.bob))['app_version'])

    def test_draft_checks_and_abandoned_edits_never_replace_history(self):
        self.confirmed()
        original = self.cards.last_confirmed(self.alice)
        self.checked(fields(refs=VERSIONS + EXTRA))
        self.checked(fields(refs='bad'))
        self.assertEqual(original, self.cards.last_confirmed(self.alice))

    def test_deleted_optional_line_is_not_silently_reintroduced(self):
        self.confirmed(fields(refs=VERSIONS + EXTRA))
        self.confirmed(fields())
        self.assertEqual(VERSIONS, inputs(self.cards.open(self.alice))['app_version'])

    def test_same_user_in_another_tenant_has_no_shared_history(self):
        self.confirmed()
        elsewhere = card.Identity('tenant-two', 'alice')
        self.assertEqual('EMPTY_TEMPLATE', self.cards.open(elsewhere)['prefill'])

    def test_forwarded_card_cannot_be_read_or_changed_by_another_user(self):
        view = self.cards.open(self.alice)
        with self.assertRaises(card.CardError):
            self.cards.submit(self.bob, button(view, 'check'), fields())
        self.assertIsNone(self.cards.last_confirmed(self.bob))

    def test_branch_change_keeps_editor_text_but_invalidates_review(self):
        self.confirmed()
        view = self.checked()
        edited = fields(branch=BRANCH.replace('dev-315-eval', 'feature/new-eval'))
        response = self.cards.submit(self.alice, button(view, 'save'), edited)
        self.assertEqual('EDITING', response['phase'])
        self.assertTrue(response['branch_changed'])
        self.assertEqual(edited, inputs(response))
        self.assertEqual(BRANCH, self.cards.last_confirmed(self.alice)['branch_link'])
        rechecked = self.checked(edited, view=response)
        self.assertEqual('CHECKED', rechecked['phase'])

    def test_edit_after_check_requires_a_new_check_before_save(self):
        view = self.checked()
        edited = fields(refs=VERSIONS + EXTRA)
        response = self.cards.submit(self.alice, button(view, 'save'), edited)
        self.assertEqual('EDITING', response['phase'])
        self.assertEqual(edited, inputs(response))
        self.assertIsNone(self.cards.last_confirmed(self.alice))

    def test_duplicate_delivery_of_check_and_save_is_idempotent(self):
        initial = self.cards.open(self.alice)
        action = button(initial, 'check')
        checked = self.cards.submit(self.alice, action, fields())
        self.assertEqual(checked, self.cards.submit(self.alice, action, fields()))
        save = button(checked, 'save')
        done = self.cards.submit(self.alice, save, fields())
        self.assertEqual(done, self.cards.submit(self.alice, save, fields()))
        self.assertEqual(1, self.cards.last_confirmed(self.alice)['revision'])

    def test_out_of_order_old_card_does_not_overwrite_new_editor(self):
        initial = self.cards.open(self.alice)
        action = button(initial, 'check')
        self.cards.submit(self.alice, action, fields())
        with self.assertRaises(card.CardError):
            self.cards.submit(self.alice, action, fields(refs=VERSIONS + EXTRA))

    def test_two_cards_cannot_silently_overwrite_a_newer_confirmation(self):
        first, second = self.checked(), self.checked(fields(refs=VERSIONS + EXTRA))
        self.cards.submit(self.alice, button(second, 'save'), fields(refs=VERSIONS + EXTRA))
        response = self.cards.submit(self.alice, button(first, 'save'), fields())
        self.assertEqual('EDITING', response['phase'])
        self.assertEqual(VERSIONS + EXTRA, self.cards.last_confirmed(self.alice)['app_version'])

    def test_expired_session_preserves_last_confirmed_configuration(self):
        self.confirmed()
        view = self.cards.open(self.alice)
        self.now[0] += card.SESSION_SECONDS + 1
        with self.assertRaises(card.CardError):
            self.cards.submit(self.alice, button(view, 'check'), fields())
        self.assertEqual(VERSIONS, self.cards.last_confirmed(self.alice)['app_version'])

    def test_owner_field_is_not_an_authoritative_identity_input(self):
        view = self.cards.open(self.alice)
        with self.assertRaises(card.CardError):
            self.cards.submit(self.alice, button(view, 'check'), {**fields(), 'owner': 'bob'})

    def test_oversized_input_is_rejected_without_truncation_or_persistence(self):
        view = self.cards.open(self.alice)
        with self.assertRaises(card.CardError):
            self.cards.submit(self.alice, button(view, 'check'), fields(refs='x' * 1001))
        self.assertIsNone(self.cards.last_confirmed(self.alice))

    def test_no_network_or_subprocess_and_remote_checks_remain_pending(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('process forbidden')):
            done = self.confirmed()
        self.assertEqual('NOT_CHECKED', done['remote_validation'])
        self.assertEqual('NOT_IMPLEMENTED', done['build_dispatch'])

    def test_store_inside_repository_is_rejected(self):
        with self.assertRaises(card.CardError):
            card.InputCards(Path(__file__).parents[1] / 'preferences.sqlite3')


class InputValidation(unittest.TestCase):
    def test_valid_refs_and_crlf_and_blank_lines_preserve_order(self):
        result = card.validate(BRANCH, '\r\n' + (VERSIONS + EXTRA).replace('\n', '\r\n') + '\r\n')
        self.assertEqual([], result['errors'])
        self.assertEqual(VERSIONS + EXTRA, result['normalized'])

    def test_duplicate_missing_range_placeholder_and_wrong_channel(self):
        cases = [VERSIONS + '\nuroad_m100/9@livc/stable',
                 VERSIONS.replace('dag_compiler/', 'another/'),
                 VERSIONS.replace('1.0.38-avm', '*'),
                 VERSIONS.replace('1.0.38-avm', '<填写版本>'),
                 VERSIONS.replace('1.0.38-avm', '[>1.0]'),
                 VERSIONS.replace('release-schu', 'release_schu'),
                 VERSIONS.replace('3.1.7-debugTest3', '3.1.7#revision')]
        for refs in cases:
            with self.subTest(refs=refs):
                self.assertTrue(card.validate(BRANCH, refs)['errors'])

    def test_wrong_repo_home_credentials_query_and_invalid_branch_are_rejected(self):
        for url in (BRANCH.replace('/uroad/', '/other/'), BRANCH.split('/-/')[0],
                    BRANCH.replace('https://', 'https://name:secret@'),
                    BRANCH + '?access_token=fictional', BRANCH + '/../next',
                    BRANCH.replace('/dev-315-eval', '/bad%20branch'), BRANCH + '#anchor'):
            with self.subTest(url=url):
                self.assertTrue(card.validate(url, VERSIONS)['errors'])

    def test_slash_branch_and_standard_gitlab_ref_query_are_accepted(self):
        result = card.validate(BRANCH.replace('dev-315-eval', 'feature/test') + '?ref_type=heads', VERSIONS)
        self.assertEqual([], result['errors'])
        self.assertEqual('feature/test', result['branch'])

    def test_feishu_form_structure_uses_documented_form_submit_contract(self):
        data = {'branch_link': '', 'app_version': card.EMPTY_TEMPLATE, 'previous_branch_link': '',
                'prefill': 'EMPTY_TEMPLATE', 'phase': 'EDITING', 'errors': []}
        view = card.render_card('a' * 32, 0, data)
        self.assertEqual('2.0', view['schema'])
        names = []
        for element in view['body']['elements']:
            if element['tag'] == 'form':
                for item in element['elements']:
                    names.append(item['name'])
                    if item['tag'] == 'button':
                        self.assertEqual('submit', item['form_action_type'])
                        self.assertNotIn('behaviors', item)
                    if item['tag'] == 'input':
                        self.assertLessEqual(item['max_length'], 1000)
        self.assertEqual(len(set(names)), len(names))
        self.assertLess(len(json.dumps(view).encode()), 30 * 1024)


if __name__ == '__main__':
    unittest.main()
