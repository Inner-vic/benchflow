"""Only owner metadata recovery; adapters are mocks and no messages are sent."""
import contextlib
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_assemblies as fixtures
from bench_gateway.assemblies import list_assemblies, record_success
from bench_gateway.assembly_recovery import recover_assembly
from bench_gateway.core import GatewayError


class AssemblyRecoveryTests(unittest.TestCase):
    example = fixtures.AssemblyCatalogTests.example

    def setUp(self):
        fixtures.AssemblyCatalogTests.setUp(self)
        self.cfg.update(mode='simulate', live_enabled=False, notifications_enabled=False)
        self.result['smoke']['task'] = '/workstation/tasks/' + self.job['id'] + '-smoke'
        self.result['assembly_archive_warning'] = 'archive previously interrupted'
        self.archive = copy.deepcopy(self.result['assembly_archive'])
        self.store.finish(self.job, 'SUCCEEDED', self.result)
        self.job = self.store.job(self.user, self.job['id'])
        self.executor = SimpleNamespace(verify_assembly=Mock(side_effect=lambda job, receipt: receipt),
            save_assembly=Mock(side_effect=lambda job, result, **kwargs: {**result, 'assembly_archive': copy.deepcopy(self.archive)}),
            execute=Mock(side_effect=AssertionError('recovery cannot execute')),
            replay_pipeline=Mock(side_effect=AssertionError('recovery cannot replay')))

    def recover(self, **kwargs):
        return recover_assembly(self.cfg, self.store, kwargs.get('user', self.user),
                                kwargs.get('job_id', self.job['id']), self.executor)

    def update(self, result=None, state=None, payload=None):
        current = self.store.job(self.user, self.job['id'])
        with self.store.connect() as connection:
            connection.execute('UPDATE jobs SET state=?,result=?,payload=? WHERE id=?',
                (state or current['state'], json.dumps(result if result is not None else current['result']),
                 json.dumps(payload if payload is not None else current['payload']), self.job['id']))

    def transport_state(self):
        with self.store.connect() as connection:
            return {name: [tuple(row) for row in connection.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]
                    for name in ('outbox', 'queue_outbox')}

    def test_saved_receipt_only_verifies_then_registers_without_hardware_or_notifications(self):
        before = self.transport_state()
        outcome = self.recover()
        current = self.store.job(self.user, self.job['id'])
        self.assertEqual('ASSEMBLY_RECOVERED', outcome['state'])
        self.assertEqual('SUCCEEDED', current['state'])
        self.assertEqual('PASS', current['result']['state'])
        self.assertEqual(self.result['task'], current['result']['task'])
        self.assertEqual(self.archive, current['result']['assembly_archive'])
        self.assertEqual('SAVED', current['result']['assembly']['state'])
        self.assertNotIn('assembly_archive_warning', current['result'])
        self.executor.verify_assembly.assert_called_once()
        self.executor.save_assembly.assert_not_called()
        self.executor.execute.assert_not_called()
        self.executor.replay_pipeline.assert_not_called()
        self.assertEqual(before, self.transport_state())
        self.assertEqual(1, len(self.store.jobs(self.user)))

    def test_missing_receipt_only_calls_archive_with_stage_disabled(self):
        result = {key: value for key, value in self.result.items() if key != 'assembly_archive'}
        self.update(result=result)
        before = self.transport_state()
        outcome = self.recover()
        self.assertEqual('ASSEMBLY_RECOVERED', outcome['state'])
        self.executor.verify_assembly.assert_not_called()
        self.executor.save_assembly.assert_called_once()
        self.assertIs(False, self.executor.save_assembly.call_args.kwargs['record_stage'])
        self.assertEqual(before, self.transport_state())

    def test_existing_catalog_registration_is_idempotent_without_ssh(self):
        record_success(self.cfg, self.store, self.job, self.result)
        first = self.recover()
        current = self.store.job(self.user, self.job['id'])
        before, updated = self.transport_state(), current['updated']
        second = self.recover()
        self.assertIs(True, first['idempotent'])
        self.assertIs(True, second['idempotent'])
        self.assertEqual(first['assembly'], second['assembly'])
        self.assertEqual(updated, self.store.job(self.user, self.job['id'])['updated'])
        self.assertEqual(1, len(list_assemblies(self.cfg, self.store, self.user)[0]['sources']))
        self.executor.verify_assembly.assert_not_called()
        self.executor.save_assembly.assert_not_called()
        self.assertEqual(before, self.transport_state())

    def test_failed_existing_archive_verification_never_falls_back_to_save_or_replay(self):
        self.executor.verify_assembly.side_effect = GatewayError('private token must not be echoed')
        before = self.transport_state()
        outcome = self.recover()
        current = self.store.job(self.user, self.job['id'])
        self.assertEqual('ASSEMBLY_PENDING', outcome['state'])
        self.assertEqual('ARCHIVE_VERIFY_FAILED', outcome['error_code'])
        self.assertEqual('SUCCEEDED', current['state'])
        self.assertEqual('PASS', current['result']['state'])
        self.assertIn('assembly_archive_warning', current['result'])
        self.assertNotIn('private token', str(outcome) + str(current['result']))
        self.assertEqual([], list_assemblies(self.cfg, self.store, self.user))
        self.executor.save_assembly.assert_not_called()
        self.assertEqual(before, self.transport_state())

    def test_changed_verified_receipt_is_not_registered(self):
        self.executor.verify_assembly.side_effect = lambda job, receipt: {**receipt, 'input_digest': '0' * 64}
        self.assertEqual('ASSEMBLY_PENDING', self.recover()['state'])
        self.assertEqual([], list_assemblies(self.cfg, self.store, self.user))

    def test_late_failure_cannot_downgrade_another_successful_recovery(self):
        before = self.transport_state()
        successful = SimpleNamespace(verify_assembly=Mock(side_effect=lambda job, receipt: receipt))
        def finish_other_recovery_then_fail(job, receipt):
            other = recover_assembly(self.cfg, self.store, self.user, self.job['id'], successful)
            self.assertEqual('ASSEMBLY_RECOVERED', other['state'])
            # Model a concurrently arriving evaluation result as well.
            current = self.store.job(self.user, self.job['id'])
            result = {**current['result'], 'evaluation_state': 'PARTIAL',
                      'report': 'concurrent.txt', 'local_report': 'concurrent.txt',
                      'warnings': ['VISION_GT_MISSING']}
            self.update(state='PARTIAL_SUCCESS', result=result)
            raise GatewayError('this delayed verification failed')
        self.executor.verify_assembly.side_effect = finish_other_recovery_then_fail
        outcome = self.recover()
        current = self.store.job(self.user, self.job['id'])
        self.assertEqual('ASSEMBLY_RECOVERED', outcome['state'])
        self.assertIs(True, outcome['idempotent'])
        self.assertEqual('PARTIAL_SUCCESS', current['state'])
        self.assertEqual('SAVED', current['result']['assembly']['state'])
        self.assertEqual('concurrent.txt', current['result']['report'])
        self.assertEqual(['VISION_GT_MISSING'], current['result']['warnings'])
        self.assertNotIn('assembly_archive_warning', current['result'])
        self.assertEqual(1, len(list_assemblies(self.cfg, self.store, self.user)[0]['sources']))
        self.assertEqual(before, self.transport_state())
        self.executor.save_assembly.assert_not_called()

    def test_failure_cannot_use_fake_saved_display_metadata_as_success(self):
        result = {**self.result, 'assembly': {'id': 'asm-' + 'f' * 24, 'state': 'SAVED', 'name': 'fake display'}}
        self.update(result=result)
        self.executor.verify_assembly.side_effect = GatewayError('no real artifact verified')
        outcome = self.recover()
        self.assertEqual('ASSEMBLY_PENDING', outcome['state'])
        self.assertIs(False, outcome['idempotent'])
        self.assertEqual([], list_assemblies(self.cfg, self.store, self.user))

    def test_another_source_registration_cannot_promote_failed_recovery(self):
        other_job, other_result = self.example(name='another source')
        record_success(self.cfg, self.store, other_job, other_result)
        self.executor.verify_assembly.side_effect = GatewayError('this source still failed')
        outcome = self.recover()
        self.assertEqual('ASSEMBLY_PENDING', outcome['state'])
        sources = list_assemblies(self.cfg, self.store, self.user)[0]['sources']
        self.assertEqual([other_job['id']], [item['source_job'] for item in sources])

    def test_concurrent_catalog_row_still_must_match_source_frozen_identity(self):
        def insert_mismatched_source_then_fail(job, receipt):
            record_success(self.cfg, self.store, self.job, self.result)
            with self.store.connect() as connection:
                row = connection.execute('SELECT data FROM assembly_sources WHERE owner=? AND source_job=?',
                                         (self.user, self.job['id'])).fetchone()
                source = json.loads(row['data'])
                source['archive']['input_digest'] = '0' * 64
                connection.execute('UPDATE assembly_sources SET data=? WHERE owner=? AND source_job=?',
                                   (json.dumps(source), self.user, self.job['id']))
            raise GatewayError('verification failed')
        self.executor.verify_assembly.side_effect = insert_mismatched_source_then_fail
        with self.assertRaises(GatewayError):
            self.recover()
        self.assertNotIn('assembly', self.store.job(self.user, self.job['id'])['result'])

    def test_missing_receipt_archive_failure_preserves_result_and_original_state(self):
        result = {key: value for key, value in self.result.items() if key != 'assembly_archive'}
        self.update(result=result, state='PARTIAL_SUCCESS')
        self.executor.save_assembly.side_effect = ConnectionError('SSH unavailable')
        outcome = self.recover()
        self.assertEqual('ASSEMBLY_PENDING', outcome['state'])
        current = self.store.job(self.user, self.job['id'])
        self.assertEqual('PARTIAL_SUCCESS', current['state'])
        self.assertEqual(result['task'], current['result']['task'])
        self.assertEqual('PASS', current['result']['state'])

    def test_partial_or_in_progress_evaluation_result_and_state_are_preserved(self):
        for state in ('PARTIAL_SUCCESS', 'POSTPROCESSING'):
            with self.subTest(state=state):
                result = {**self.result, 'evaluation_job': self.job['id'] + '-eval',
                          'evaluation_state': 'RUNNING', 'report': 'original.txt',
                          'local_report': 'original.txt', 'warnings': ['VISION_GT_MISSING'],
                          'evaluation': {'metrics': {'count': 12}}, 'error': 'evaluation partial'}
                self.update(result=result, state=state)
                outcome = self.recover()
                current = self.store.job(self.user, self.job['id'])
                self.assertEqual(state, outcome['job_state'])
                self.assertEqual(state, current['state'])
                for key in ('evaluation_job', 'evaluation_state', 'report', 'local_report', 'warnings', 'evaluation', 'error'):
                    self.assertEqual(result[key], current['result'][key])

    def test_only_owner_can_recover_the_job(self):
        with self.assertRaises(GatewayError):
            self.recover(user=self.other)
        self.executor.verify_assembly.assert_not_called()
        self.executor.save_assembly.assert_not_called()

    def test_display_success_or_missing_full_smoke_receipts_are_not_enough(self):
        cases = []
        for field, value in (('state', 'RUNNING'), ('run_id', 'other-run'), ('input_digest', '0' * 64),
                             ('task', '/workstation/tasks/other'), ('simulated', True), ('smoke', {})):
            cases.append({**self.result, field: value})
        for field, value in (('state', 'FAILED'), ('run_id', 'old-smoke'), ('input_digest', '0' * 64), ('task', None), ('simulated', True)):
            cases.append({**self.result, 'smoke': {**self.result['smoke'], field: value}})
        for result in cases:
            with self.subTest(result=result):
                self.update(result=result)
                with self.assertRaises(GatewayError):
                    self.recover()
        self.executor.verify_assembly.assert_not_called()

    def test_unknown_running_queued_or_simulated_jobs_cannot_be_promoted(self):
        for state in ('NEEDS_REVIEW', 'RUNNING', 'QUEUED', 'SIMULATED', 'FAILED'):
            with self.subTest(state=state):
                self.update(state=state)
                with self.assertRaises(GatewayError):
                    self.recover()
        self.executor.verify_assembly.assert_not_called()

    def test_retained_evaluation_legacy_and_foreign_software_are_rejected(self):
        original = copy.deepcopy(self.job['payload'])
        cases = []
        for field, value in (('retained_result', True), ('action', 'evaluate'), ('registration_only', True)):
            cases.append({**copy.deepcopy(original), field: value})
        legacy = copy.deepcopy(original)
        legacy['baseline']['data']['kind'] = 'legacy'
        cases.append(legacy)
        foreign = copy.deepcopy(original)
        foreign['assets']['dependencies'][0]['owner'] = self.other
        cases.append(foreign)
        for payload in cases:
            with self.subTest(payload=payload):
                self.update(payload=payload)
                with self.assertRaises(GatewayError):
                    self.recover()
        self.executor.verify_assembly.assert_not_called()

    def racing_connection(self, callback):
        original = self.store.connect
        state = {'injected': False}
        class Proxy:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def execute(self, sql, parameters=()):
                if sql.lstrip().startswith('UPDATE jobs SET result=?,updated=?') and not state['injected']:
                    state['injected'] = True
                    with original() as concurrent:
                        row = concurrent.execute('SELECT * FROM jobs WHERE id=?', (self_job,)).fetchone()
                        callback(concurrent, row)
                return self.connection.execute(sql, parameters)
        self_job = self.job['id']
        @contextlib.contextmanager
        def connect():
            with original() as connection:
                yield Proxy(connection)
        return patch.object(self.store, 'connect', connect), state

    def test_cas_retry_keeps_concurrent_parent_evaluation_and_does_not_touch_outboxes(self):
        self.update(state='POSTPROCESSING')
        before = self.transport_state()
        def complete_evaluation(connection, row):
            result = json.loads(row['result'])
            result.update(evaluation_state='PARTIAL', report='arrived.txt', local_report='arrived.txt',
                          evaluation={'warnings': ['VISION_GT_MISSING']}, warnings=['VISION_GT_MISSING'])
            connection.execute("UPDATE jobs SET state='PARTIAL_SUCCESS',result=?,updated=updated+1 WHERE id=?",
                               (json.dumps(result), self.job['id']))
        racing, state = self.racing_connection(complete_evaluation)
        with racing:
            outcome = self.recover()
        current = self.store.job(self.user, self.job['id'])
        self.assertIs(True, state['injected'])
        self.assertEqual('PARTIAL_SUCCESS', outcome['job_state'])
        self.assertEqual('arrived.txt', current['result']['report'])
        self.assertEqual('PARTIAL', current['result']['evaluation_state'])
        self.assertEqual(['VISION_GT_MISSING'], current['result']['warnings'])
        self.assertEqual('SAVED', current['result']['assembly']['state'])
        self.assertEqual(before, self.transport_state())

    def test_source_identity_change_during_cas_does_not_overwrite_current_results(self):
        def change_source(connection, row):
            result = json.loads(row['result'])
            result['task'] = '/workstation/tasks/different-run'
            connection.execute('UPDATE jobs SET result=?,updated=updated+1 WHERE id=?',
                               (json.dumps(result), self.job['id']))
        racing, state = self.racing_connection(change_source)
        with racing:
            with self.assertRaises(GatewayError):
                self.recover()
        current = self.store.job(self.user, self.job['id'])
        self.assertIs(True, state['injected'])
        self.assertEqual('/workstation/tasks/different-run', current['result']['task'])
        self.assertNotIn('assembly', current['result'])

    def test_callback_mutation_of_private_argument_cannot_change_stored_result(self):
        result = {key: value for key, value in self.result.items() if key != 'assembly_archive'}
        self.update(result=result)
        def save(job, result, **kwargs):
            job['payload']['action'] = 'evaluate'
            result['assembly_archive'] = copy.deepcopy(self.archive)
            return result
        self.executor.save_assembly.side_effect = save
        self.assertEqual('ASSEMBLY_RECOVERED', self.recover()['state'])
        self.assertEqual('replay', self.store.job(self.user, self.job['id'])['payload']['action'])


if __name__ == '__main__':
    unittest.main()
