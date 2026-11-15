import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway import candidate_remote as remote


class FailureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run = self.root / 'data/runs/job-123-smoke'
        self.adapter = self.root / 'runner/adapters/job-123-smoke'
        self.worker = self.run / 'logs/worker-cli.log'
        self.probe = self.run / 'probe.log'
        self.report = self.adapter / 'evidence/job-123-smoke/batch-result.json'
        self.worker.parent.mkdir(parents=True)
        self.report.parent.mkdir(parents=True)

    def diagnose(self):
        return remote.bounded_failure_diagnostics(self.run, self.adapter)

    def write(self, path, text):
        path.write_text(text, encoding='utf-8')

    def test_positive_failures_have_specific_categories_and_next_actions(self):
        cases = (
            ('error while loading shared libraries: libxcamera.so: cannot open shared object file: No such file or directory', 'MISSING_LIBRARY'),
            ('symbol lookup error: /app/livc/lib/liburoad_m100.so: undefined symbol: CreateEngine', 'ABI_SYMBOL_MISMATCH'),
            ("version `GLIBCXX_3.4.30' not found (required by libxlidar.so)", 'ABI_SYMBOL_MISMATCH'),
            ('grpc UNAVAILABLE failed to connect to all addresses: connection refused 127.0.0.1:50053', 'ENDPOINT_UNAVAILABLE'),
            ("FileNotFoundError: [Errno 2] No such file or directory: '/data/scenes/frames/0001.jpeg'", 'DATA_MISSING'),
            ('subprocess.TimeoutExpired: recorder timed out after 600 seconds', 'TIMEOUT'),
        )
        for message, expected in cases:
            with self.subTest(category=expected, message=message):
                self.write(self.worker, message)
                result = self.diagnose()
                self.assertEqual(result['category'], expected)
                self.assertEqual(result['diagnostic_scope'], 'bounded-local-logs')
                self.assertTrue(result['next_action'])
                self.assertEqual(result['evidence'][0]['path'], str(self.worker))
                self.assertIn(message, result['evidence'][0]['excerpt'])

    def test_timeout_does_not_claim_abi_incompatibility(self):
        self.write(self.probe, 'Deadline exceeded waiting for result')
        result = self.diagnose()
        self.assertEqual(result['category'], 'TIMEOUT')
        self.assertIn('尚不能据此判断', result['summary'])
        self.assertIn('不自动重试', result['next_action'])

    def test_success_and_configuration_are_not_failure_evidence(self):
        self.write(self.worker, 'timeout=600\nCompleted successfully, all files available\n')
        self.write(self.report, json.dumps({'state': 'PASS', 'message': 'previous failure was connection refused',
                                          'scene': {'status': 'PASS', 'message': 'timed out was a test label'}}))
        result = self.diagnose()
        self.assertEqual(result['category'], 'UNKNOWN')
        self.assertEqual(result['evidence'], [])

    def test_report_extracts_explicit_error_and_failed_entries_only(self):
        self.write(self.report, json.dumps({'state': 'FAILED', 'scenes': [
            {'state': 'FAILED', 'error': 'undefined symbol: CameraInit'},
            {'state': 'PASS', 'message': 'No such file or directory: manifest.json'},
        ]}))
        result = self.diagnose()
        self.assertEqual(result['category'], 'ABI_SYMBOL_MISMATCH')
        self.assertEqual(len(result['evidence']), 1)
        self.assertEqual(result['evidence'][0]['path'], str(self.report))

    def test_unknown_error_is_preserved_without_fabricated_root_cause(self):
        self.write(self.worker, 'ERROR processing aborted: unusual model shape')
        result = self.diagnose()
        self.assertEqual(result['category'], 'UNKNOWN')
        self.assertIn('unusual model shape', result['evidence'][0]['excerpt'])
        self.assertNotIn('ABI', result['summary'])

    def test_missing_or_unreadable_logs_remain_unknown(self):
        result = self.diagnose()
        self.assertEqual(result['category'], 'UNKNOWN')
        self.assertEqual(result['logs'], [])
        self.assertIn('原因尚未确定', result['summary'])

    def test_evidence_redacts_credentials_url_queries_and_control_sequences(self):
        text = ('\x1b[31mERROR timed out url=https://alice:password-value@example.test/file?signature=signature-value#fragment-value '
                'access_token="token-value" password=pass-value appSecret="secret-value" '
                'Cookie: session=cookie-value; other=other-cookie-value\x1b[0m')
        self.write(self.worker, text)
        result = self.diagnose()
        excerpt = result['evidence'][0]['excerpt']
        for secret in ('alice', 'password-value', 'signature-value', 'fragment-value', 'token-value',
                       'pass-value', 'secret-value', 'cookie-value', 'other-cookie-value', '\x1b'):
            self.assertNotIn(secret, excerpt)
        self.assertIn('https://example.test/file?', excerpt)
        self.assertIn('[REDACTED]', excerpt)

    def test_json_authorization_and_escaped_secret_are_redacted(self):
        self.write(self.worker, 'ERROR timed out {"Authorization":"Basic credential-value", "secret":"escaped\\\"secret-value"} --token cli-token-value')
        result = self.diagnose()
        excerpt = result['evidence'][0]['excerpt']
        for secret in ('credential-value', 'secret-value', 'cli-token-value'):
            self.assertNotIn(secret, excerpt)

    def test_long_secret_and_unterminated_quoted_value_are_redacted_before_excerpt(self):
        self.write(self.worker, 'ERROR timed out token="' + 'very-sensitive ' * 500 + 'unfinished-value')
        result = self.diagnose()
        excerpt = result['evidence'][0]['excerpt']
        self.assertNotIn('very-sensitive', excerpt)
        self.assertNotIn('unfinished-value', excerpt)
        self.assertIn('[REDACTED]', excerpt)

    def test_incomplete_first_line_is_never_classified_or_quoted(self):
        self.worker.write_bytes(b'token=' + b'private-data ' * 6000 + b'ERROR undefined symbol: tail-of-secret')
        result = self.diagnose()
        self.assertEqual(result['category'], 'UNKNOWN')
        self.assertEqual(result['evidence'], [])

    def test_only_tail_is_read_and_no_recursive_scan(self):
        self.worker.write_bytes(b'undefined symbol: historical-error\n' + b'normal completed line\n' * 5000)
        nested = self.run / 'large-data/arbitrary.log'
        nested.parent.mkdir()
        self.write(nested, 'undefined symbol: must-not-be-read')
        real_read = os.read
        reads = []
        def bounded_read(descriptor, size):
            reads.append(size)
            return real_read(descriptor, size)
        with patch.object(remote.os, 'read', side_effect=bounded_read):
            result = self.diagnose()
        self.assertEqual(result['category'], 'UNKNOWN')
        self.assertEqual(reads, [64 * 1024])
        self.assertEqual(result['logs'], [str(self.worker)])

    def test_file_and_evidence_limits(self):
        self.write(self.worker, '\n'.join('ERROR undefined symbol: x%d %s' % (i, 'x' * 1000) for i in range(50)))
        self.write(self.probe, 'ERROR timed out')
        self.write(self.report, json.dumps({'error': 'connection refused'}))
        result = self.diagnose()
        self.assertLessEqual(len(result['logs']), 4)
        self.assertEqual(len(result['evidence']), 3)
        self.assertTrue(all(len(item['excerpt']) <= 400 for item in result['evidence']))

    def test_partial_or_large_report_is_not_misread_as_error_json(self):
        self.write(self.report, '{"state":"PASS", "padding":"' + 'a' * (70 * 1024) + '", "message":"undefined symbol: label"}')
        result = self.diagnose()
        self.assertEqual(result['category'], 'UNKNOWN')
        self.assertEqual(result['evidence'], [])

    def test_invalid_root_and_other_run_are_rejected_before_reading(self):
        self.write(self.worker, 'undefined symbol: secret')
        cases = ((Path('runs/job-123-smoke'), self.adapter),
                 (self.run, self.adapter.parent / 'another-run'),
                 (self.root / 'other/job-123-smoke', self.adapter),
                 (self.run / '..' / 'job-123-smoke', self.adapter))
        with patch.object(remote.os, 'read', side_effect=AssertionError('must not read')):
            for run, adapter in cases:
                with self.subTest(run=run):
                    result = remote.bounded_failure_diagnostics(run, adapter)
                    self.assertEqual(result['category'], 'UNKNOWN')
                    self.assertEqual(result['logs'], [])

    def test_symlink_log_is_not_read(self):
        outside = self.root / 'outside.log'
        self.write(outside, 'undefined symbol: outside-secret')
        try:
            self.worker.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links unavailable')
        result = self.diagnose()
        self.assertEqual(result['category'], 'UNKNOWN')
        self.assertEqual(result['logs'], [])
        self.assertNotIn('outside-secret', json.dumps(result))

    def test_symlink_parent_is_not_read(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.write(outside / 'batch-result.json', '{"error":"undefined symbol: outside-secret"}')
        link = self.adapter / 'evidence' / self.run.name
        link.rmdir()
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links unavailable')
        result = self.diagnose()
        self.assertEqual(result['category'], 'UNKNOWN')
        self.assertEqual(result['logs'], [])

    def test_changed_file_is_not_used_as_evidence(self):
        self.write(self.worker, 'undefined symbol: original')
        real_read = os.read
        def changed_read(descriptor, size):
            result = real_read(descriptor, size)
            self.worker.write_bytes(b'changed during read')
            return result
        with patch.object(remote.os, 'read', side_effect=changed_read):
            result = self.diagnose()
        self.assertEqual(result['category'], 'UNKNOWN')
        self.assertEqual(result['logs'], [])

    def test_diagnosis_is_read_only_and_keeps_recovery_lock_gate(self):
        self.write(self.worker, 'undefined symbol: CameraInit')
        before = hashlib.sha256(self.worker.read_bytes()).hexdigest()
        restore = self.run / 'restore-verification.json'
        self.write(restore, '{"restore_ok":true}')
        m = SimpleNamespace(ROOT=self.run.parent.parent, BASE=self.adapter.parent.parent)
        (m.BASE / 'locks').mkdir()
        req = {'operation': 'diagnose', 'run_id': self.run.name}
        with patch.object(remote, 'binding_receipt', return_value={'candidate': {}}), \
             patch.object(remote, 'load_binding', side_effect=AssertionError('diagnosis must not reload software')):
            result = remote.execute(req, m, None, None, None, None, None)
            self.assertEqual(result['state'], 'DIAGNOSED')
            self.assertEqual(result['category'], 'ABI_SYMBOL_MISMATCH')
            self.assertTrue(result['safe_to_continue'])
            (m.BASE / 'locks/bench.lock').write_text('occupied', encoding='utf-8')
            self.assertFalse(remote.execute(req, m, None, None, None, None, None)['safe_to_continue'])
            (m.BASE / 'locks/bench.lock').unlink()
            self.write(restore, '{"restore_ok":false}')
            self.assertFalse(remote.execute(req, m, None, None, None, None, None)['safe_to_continue'])
        self.assertEqual(hashlib.sha256(self.worker.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
