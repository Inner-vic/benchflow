"""Live-route isolation tests using synthetic cards and an in-memory API substitute."""
import base64
import copy
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway import pcm_dual_entry as entry
from bench_gateway import pcm_dual_build as feature
from bench_gateway.core import GatewayError


class Client:
    def __init__(self): self.calls = [];self.cards = 0;self.messages = 0
    def _json(self, path, method='POST', body=None, timeout=None):
        self.calls.append((path, method, json.loads(body) if body else None))
        if path == '/cardkit/v1/cards':
            self.cards += 1
            return {'data': {'card_id': 'synthetic-card'+('' if self.cards == 1 else '-'+str(self.cards))}}
        if path.startswith('/im/v1/messages?'):
            self.messages += 1
            return {'data': {'message_id': 'om_synthetic'+('' if self.messages == 1 else '-'+str(self.messages)), 'chat_id': 'oc_private'}}
        return {'data': {}}


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        (self.root/'runtime').mkdir()
        (self.root/'runtime/credentials.json').write_text(json.dumps({'app_id': 'cli_test', 'app_secret': 'synthetic-secret'}))
        (self.root/'runtime/pcm-dual-validation.json').write_text(json.dumps({'enabled': True, 'jenkins_enabled': False}))
        self.cfg = {'project_root': str(self.root), 'feishu_app_id': 'cli_test', 'card_callbacks_enabled': True}
        self.store = feature.Drafts(self.root/'separate.sqlite3');self.client = Client();self.runs = 0
        def validator(store, ticket):
            self.runs += 1
            store.finish_validation(ticket['owner'], ticket['session'], ticket['id'], ticket['revision'], ticket['attempt'],
                                    {'state': 'NEEDS_INPUT', 'message': '请选择匹配基线。',
                                     'discovery': {'candidate_baselines': [{'number': 101, 'product_variant': 'ULTRA'}]}})
        self.controller = entry.Entry(self.cfg, store=self.store, client=self.client, validator=validator)
        self.session = 'feishu:oc_private:ou_tester'

    def tearDown(self): self.temp.cleanup()

    def event(self):
        d = self.store.open('ou_tester', self.session)
        return {'schema': 1, 'event_id': 'evt-synthetic', 'app_id': 'cli_test', 'operator_id': 'ou_tester',
                'chat_id': 'oc_private', 'message_id': 'om_synthetic', 'action': 'confirm_materials',
                'value': {'nonce': d['nonce'], 'revision': d['revision']},
                'form_value': {'pcm_dual_branch_url': 'https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/feature/example',
                               'pcm_dual_component_versions': '', 'pcm_dual_release_version': 'SW00B1',
                               'pcm_dual_build_variant': 'release'}}

    def packet(self, data, secret=b'synthetic-secret'):
        raw = json.dumps(data).encode();signature = hmac.new(secret, raw, hashlib.sha256).hexdigest()
        return '@@BENCH_CARD@@'+base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+signature

    def test_open_reuses_one_private_card_and_never_opens_legacy_queue_tables(self):
        self.assertEqual(self.controller.open(self.session), 'NO_REPLY')
        self.assertEqual(self.controller.open(self.session), 'NO_REPLY')
        sends = [x for x in self.client.calls if x[0].startswith('/im/v1/messages?')]
        self.assertEqual(len(sends), 1)
        with self.store.connect() as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('jobs', names);self.assertNotIn('task_requests', names)

    def test_explicit_reopen_after_cooldown_surfaces_same_draft_at_bottom(self):
        with patch.object(entry.time, 'time', return_value=100): self.controller.open(self.session)
        before = self.store.open('ou_tester', self.session);old_event = self.event()
        with patch.object(entry.time, 'time', return_value=111): self.controller.open(self.session)
        self.assertEqual(self.client.messages, 2)
        self.assertEqual(self.store.open('ou_tester', self.session), before)
        with self.assertRaisesRegex(GatewayError, '失效'):
            self.controller.handle(self.session, old_event)
        self.assertEqual(self.runs, 0)

    def test_open_during_validation_does_not_replace_active_message(self):
        with patch.object(entry.time, 'time', return_value=100): self.controller.open(self.session)
        d = self.store.open('ou_tester', self.session)
        self.store.begin_validation('ou_tester', self.session, d['id'], d['nonce'], d['revision'],
            'https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/test', '', release={'BASE_LINE': '00B1', 'SOFTWARE_BRANCH': '0111', 'BUILDVARIANT': 'release'})
        with patch.object(entry.time, 'time', return_value=200): self.controller.open(self.session)
        self.assertEqual(self.client.messages, 1)

    def test_command_normalization_is_exact_not_free_text_routing(self):
        for text in ['PCM双版本构建', 'pcm 双版本构建', 'ＰＣＭ　双版本构建', ' PCM  双版本构建\n']:
            self.assertTrue(entry.is_command(text))
        for text in ['取消PCM双版本构建', '关于PCM双版本构建的问题', '/pcm_dual_build extra']:
            self.assertFalse(entry.is_command(text))

    def test_real_transport_shape_routes_to_validation_then_updates_original_card(self):
        self.controller.open(self.session)
        with patch.object(entry, 'Entry', return_value=self.controller):
            reply = entry.maybe_handle(SimpleNamespace(cfg=self.cfg), self.session, self.packet(self.event()))
        self.assertEqual(reply, 'NO_REPLY');self.assertEqual(self.runs, 1)
        draft = self.store.open('ou_tester', self.session)
        self.assertEqual(draft['state'], 'NEEDS_INPUT')
        card = self.controller.card(draft)
        self.assertIn('pcm_dual_baseline', json.dumps(card))
        self.assertTrue(all('/cardkit/v1/cards/synthetic-card' in path for path, method, _ in self.client.calls if method == 'PUT'))

    def test_duplicate_event_never_revalidates_or_creates_card(self):
        self.controller.open(self.session);data = self.event()
        self.controller.handle(self.session, data);before = len(self.client.calls)
        self.controller.handle(self.session, data)
        self.assertEqual(self.runs, 1);self.assertEqual(len(self.client.calls), before)

    def test_wrong_original_card_chat_owner_or_nonce_is_rejected(self):
        self.controller.open(self.session)
        for key, value in [('message_id', 'om_wrong'), ('chat_id', 'oc_wrong'), ('operator_id', 'ou_other')]:
            data = self.event();data[key] = value
            with self.subTest(key=key), self.assertRaises(GatewayError): self.controller.handle(self.session, data)
        data = self.event();data['value']['nonce'] = 'bad'
        with self.assertRaises(GatewayError): self.controller.handle(self.session, data)
        self.assertEqual(self.runs, 0)

    def test_legacy_messages_and_callbacks_fall_through_unchanged(self):
        with patch.object(entry, 'Entry') as construct:
            self.assertIsNone(entry.maybe_handle(SimpleNamespace(cfg=self.cfg), self.session, '查询本人资产'))
            data = self.event();data['form_value'] = {'app_source': 'old-app', 'dataset': '/old/data'}
            self.assertIsNone(entry.maybe_handle(SimpleNamespace(cfg=self.cfg), self.session, self.packet(data)))
            construct.assert_not_called()

    def test_invalid_signature_and_disabled_feature_fail_closed(self):
        self.controller.open(self.session)
        with self.assertRaises(GatewayError):
            entry.maybe_handle(SimpleNamespace(cfg=self.cfg), self.session, self.packet(self.event(), b'wrong'))
        (self.root/'runtime/pcm-dual-validation.json').write_text(json.dumps({'enabled': False, 'jenkins_enabled': False}))
        with self.assertRaises(GatewayError):
            entry.maybe_handle(SimpleNamespace(cfg=self.cfg), self.session, self.packet(self.event()))
        self.assertEqual(self.runs, 0)

    def test_notification_failure_does_not_repeat_validation(self):
        self.controller.open(self.session);data = self.event()
        with patch.object(self.controller, 'update', side_effect=RuntimeError('synthetic delivery failure')):
            with self.assertRaises(GatewayError): self.controller.handle(self.session, data)
        self.assertEqual(self.runs, 0)
        self.assertEqual(self.store.open('ou_tester', self.session)['state'], 'NEEDS_INPUT')
        self.controller.handle(self.session, data)
        self.assertEqual(self.runs, 0)

    def test_selected_baseline_is_checked_against_original_candidates(self):
        self.controller.open(self.session);self.controller.handle(self.session, self.event())
        data = self.event();data['event_id'] = 'evt-second';data['form_value']['pcm_dual_baseline'] = '101'
        self.controller.handle(self.session, data)
        self.assertEqual(self.runs, 2)
        self.assertEqual(self.store.open('ou_tester', self.session)['baseline_build'], 101)

    def test_completed_validation_survives_delivery_failure_and_only_delivery_retries(self):
        self.controller.open(self.session);data = self.event()
        real_update = self.controller.update
        count = 0
        def fail_final(draft, mid):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('synthetic final notification failure')
            real_update(draft, mid)
        with patch.object(self.controller, 'update', side_effect=fail_final):
            with self.assertRaises(GatewayError): self.controller.handle(self.session, data)
        self.assertEqual(self.runs, 1)
        saved = self.store.open('ou_tester', self.session)
        self.assertIn('candidate_baselines', saved['report']['discovery'])
        self.controller.handle(self.session, data)
        self.assertEqual(self.runs, 1)
        self.assertEqual(self.store.open('ou_tester', self.session)['report'], saved['report'])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT state FROM pcm_dual_events').fetchone()[0], 'DONE')

    def test_invalid_baseline_keeps_specific_feedback_and_prior_result(self):
        self.controller.open(self.session);self.controller.handle(self.session, self.event())
        data = self.event();data['event_id'] = 'invalid-choice'
        data['form_value']['pcm_dual_baseline'] = '999'
        before = self.store.open('ou_tester', self.session)
        with self.assertRaisesRegex(GatewayError, '基线选择已失效'):
            self.controller.handle(self.session, data)
        self.assertEqual(self.store.open('ou_tester', self.session), before)
        self.assertEqual(self.runs, 1)

    def test_old_form_without_release_fields_does_not_fall_back_to_baseline(self):
        self.controller.open(self.session);data = self.event()
        for key in ('pcm_dual_release_version', 'pcm_dual_build_variant'):
            data['form_value'].pop(key)
        with self.assertRaisesRegex(GatewayError, '不会沿用历史'):
            self.controller.handle(self.session, data)
        self.assertEqual(self.runs, 0)
        self.assertEqual(self.store.open('ou_tester', self.session)['state'], 'DRAFT')

    def test_legacy_id_override_cannot_change_avm_default(self):
        self.controller.open(self.session);data = self.event()
        data['form_value']['pcm_dual_software_id'] = '0112'
        with self.assertRaisesRegex(GatewayError, '固定为0111'):
            self.controller.handle(self.session, data)
        self.assertEqual(self.runs, 0)


if __name__ == '__main__': unittest.main()
