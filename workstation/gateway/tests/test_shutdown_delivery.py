import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.core import Store
from bench_gateway.preparation import PreparationQueue
from bench_gateway import worker


class ShutdownDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cfg = {'project_root': str(self.root), 'state_root': str(self.root / 'state'),
                    'mode': 'simulate', 'live_enabled': False, 'notifications_enabled': True,
                    'cc_binary': 'fake-cc-connect', 'cc_project': 'isolated',
                    'notification_retry_seconds': 5, 'poll_seconds': .01}
        self.store = Store(self.cfg['state_root'])
        self.owner = 'ou_shutdown_delivery'
        self.store.allow(self.owner)
        self.session = 'feishu:isolated:' + self.owner

    def result(self, key, attachment=False):
        job, _ = self.store.submit(self.owner, self.session, key, {'action': 'replay'})
        result = {'task': '/isolated/' + key, 'workstation': 'isolated'}
        if attachment:
            report = self.store.user_root(self.owner) / (key + '.txt')
            report.write_text('isolated report', encoding='utf-8')
            result.update(report=str(report), local_report=str(report))
        self.store.finish(job, 'SUCCEEDED', result)
        return job

    def outbox(self, job):
        with self.store.connect() as connection:
            return dict(connection.execute('SELECT * FROM outbox WHERE job=?', (job['id'],)).fetchone())

    def preparation(self, pid):
        PreparationQueue(self.cfg, self.store)
        with self.store.connect() as connection:
            connection.execute('''INSERT INTO preparations(id,owner,session,fingerprint,state,payload,result,created,updated)
                                VALUES(?,?,?,?,?,?,?,?,?)''',
                               (pid, self.owner, self.session, 'isolated', 'NEEDS_INPUT', '{}',
                                json.dumps({'status': 'NEEDS_INPUT', 'issues': []}), time.time(), time.time()))

    def test_expired_budget_does_not_attempt_or_consume_pending_rows(self):
        job = self.result('expired')
        with patch.object(worker.time, 'monotonic', return_value=20), patch.object(worker.subprocess, 'run') as send:
            self.assertEqual(worker.deliver(self.cfg, self.store, deadline=20), 0)
            send.assert_not_called()
        saved = self.outbox(job)
        self.assertEqual(saved['sent'], 0)
        self.assertEqual(saved['attempts'], 0)

    def test_cli_body_and_file_share_remaining_budget(self):
        job = self.result('attachment', attachment=True)
        clock = [10.0]
        calls = []
        def send(argv, **kwargs):
            calls.append((list(argv), kwargs['timeout']))
            clock[0] += 3
            return SimpleNamespace(returncode=0)
        with patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(worker.subprocess, 'run', side_effect=send):
            self.assertEqual(worker.deliver(self.cfg, self.store, deadline=18), 1)
        self.assertEqual([item[1] for item in calls], [8, 5])
        self.assertIn('--stdin', calls[0][0])
        self.assertIn('--file', calls[1][0])
        self.assertEqual(self.outbox(job)['sent'], 1)

    def test_body_sent_at_deadline_keeps_attachment_for_next_start(self):
        job = self.result('body-only', attachment=True)
        clock = [0.0]
        calls = []
        def send(argv, **kwargs):
            calls.append(list(argv))
            clock[0] = 8
            return SimpleNamespace(returncode=0)
        with patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(worker.subprocess, 'run', side_effect=send):
            self.assertEqual(worker.deliver(self.cfg, self.store, deadline=8), 0)
        saved = self.outbox(job)
        self.assertEqual((saved['sent'], saved['text_sent'], saved['attempts']), (0, 1, 0))
        self.assertEqual(len(calls), 1)
        with patch.object(worker.time, 'monotonic', return_value=8), patch.object(worker.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as send:
            self.assertEqual(worker.deliver(self.cfg, self.store, deadline=16), 1)
            self.assertEqual(send.call_count, 1)
            self.assertIn('--file', send.call_args.args[0])
            self.assertNotIn('--stdin', send.call_args.args[0])
        self.assertEqual(self.outbox(job)['sent'], 1)

    def test_budget_stops_after_current_row_without_retrying_or_consuming_later_rows(self):
        first, second = self.result('first'), self.result('second')
        clock = [0.0]
        calls = []
        def sender(row):
            calls.append(row['job'])
            clock[0] = 8
            return True
        with patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertEqual(worker.deliver(self.cfg, self.store, sender, deadline=8), 1)
        self.assertEqual(calls, [first['id']])
        self.assertEqual(self.outbox(first)['sent'], 1)
        self.assertEqual((self.outbox(second)['sent'], self.outbox(second)['attempts']), (0, 0))

    def test_budgeted_cli_timeout_preserves_result_and_uses_normal_backoff(self):
        job = self.result('timeout')
        clock = [0.0]
        def send(argv, **kwargs):
            self.assertEqual(kwargs['timeout'], 8)
            clock[0] = 8
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        with patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(worker.subprocess, 'run', side_effect=send):
            self.assertEqual(worker.deliver(self.cfg, self.store, deadline=8), 0)
        saved = self.outbox(job)
        self.assertEqual((saved['sent'], saved['attempts']), (0, 1))
        self.assertGreater(saved['next_attempt_at'], time.time())
        self.assertEqual(self.store.job(self.owner, job['id'])['state'], 'SUCCEEDED')
        with patch.object(worker.subprocess, 'run') as send:
            self.assertEqual(worker.deliver(self.cfg, self.store, deadline=time.monotonic() + 8), 0)
            send.assert_not_called()

    def test_preparation_delivery_also_stops_before_later_rows(self):
        self.preparation('prep-first')
        self.preparation('prep-second')
        clock = [0.0]
        sent = []
        def sender(row):
            sent.append(row)
            clock[0] = 8
            return True
        with patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertEqual(worker.deliver(self.cfg, self.store, sender, deadline=8), 1)
        with self.store.connect() as connection:
            rows = [dict(row) for row in connection.execute('SELECT id,sent,attempts FROM preparations ORDER BY created,id')]
        self.assertEqual(len(sent), 1)
        self.assertEqual((rows[0]['sent'], rows[0]['attempts']), (1, 1))
        self.assertEqual((rows[1]['sent'], rows[1]['attempts']), (0, 0))

    def test_preparation_summary_exhaustion_does_not_start_send_or_increment_attempt(self):
        self.preparation('prep-summary-budget')
        clock = [0.0]
        def summary(*args, **kwargs):
            clock[0] = 8
            return 'prepared summary'
        with patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(PreparationQueue, 'completion_text', side_effect=summary), patch.object(worker.subprocess, 'run') as send:
            self.assertEqual(worker.deliver(self.cfg, self.store, deadline=8), 0)
            send.assert_not_called()
        with self.store.connect() as connection:
            row = connection.execute('SELECT sent,attempts FROM preparations WHERE id=?', ('prep-summary-budget',)).fetchone()
        self.assertEqual(tuple(row), (0, 0))

    def test_evaluation_publishes_start_before_dispatch_and_updated_queue_after_finish(self):
        events = []
        job = {'id': 'isolated-eval', 'payload': {'action': 'evaluate', 'source_task': '/isolated/result'}}
        store = SimpleNamespace(claim=lambda lane: job,
                                finish=lambda *args: events.append('finish'))
        queue = SimpleNamespace(publish=lambda: events.append('publish'))
        executor = SimpleNamespace(execute=lambda value: events.append('execute') or {'task': '/isolated/result'})
        with patch('bench_gateway.queue_status.QueueStatus', return_value=queue):
            self.assertEqual(worker.postprocess_tick(self.cfg, store, executor), job['id'])
        self.assertEqual(events, ['publish', 'execute', 'finish', 'publish'])

    def test_unbudgeted_sender_interface_remains_compatible(self):
        job = self.result('ordinary')
        seen = []
        self.assertEqual(worker.deliver(self.cfg, self.store, lambda row: seen.append(row) or True), 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.outbox(job)['sent'], 1)

    def test_managed_drain_keeps_notifier_running_during_active_work_and_flushes_final_result(self):
        preparing, evaluating = threading.Event(), threading.Event()
        release_preparation, release_evaluation = threading.Event(), threading.Event()
        preparation_delivered = threading.Event()
        finished = threading.Event()
        drain = self.root / 'runtime/service/drain.json'
        jobs, delivered, flushes, errors = {}, [], [], []
        checks = {'preparation': 0, 'evaluation': 0}
        test = self

        class FakePreparation:
            def __init__(self, cfg, store):
                pass
            def recover(self):
                pass
            def deliver(self, sender, deadline=None):
                return 0
            def tick(self):
                checks['preparation'] += 1
                preparing.set()
                if not release_preparation.wait(5):
                    raise RuntimeError('test did not release preparation')
                jobs['preparation'] = test.result('prepared-at-drain')

        def evaluate(cfg, store):
            checks['evaluation'] += 1
            evaluating.set()
            if not release_evaluation.wait(5):
                raise RuntimeError('test did not release evaluation')
            jobs['evaluation'] = test.result('evaluated-at-drain')

        def hardware_tick(cfg, store):
            if not preparing.wait(3) or not evaluating.wait(3):
                raise RuntimeError('active workers failed to start')
            drain.parent.mkdir(parents=True, exist_ok=True)
            drain.write_text('{}', encoding='utf-8')

        real_deliver = worker.deliver
        def sender(row):
            delivered.append(row['job'])
            if '/isolated/prepared-at-drain' in row['text']:
                preparation_delivered.set()
            return True
        def injected_deliver(cfg, store):
            # Keep the historic two-argument run() injection interface working.
            if threading.current_thread().name == 'isolated-drain-worker':
                flushes.append(cfg.get('_notification_deadline'))
            return real_deliver(cfg, store, sender)
        def run():
            try:
                worker.run(self.cfg, self.store)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        with patch.dict(os.environ, {'BENCH_GATEWAY_MANAGED': '1'}), patch('bench_gateway.preparation.PreparationQueue', FakePreparation), patch.object(worker, 'postprocess_tick', side_effect=evaluate), patch.object(worker, 'tick', side_effect=hardware_tick), patch.object(worker, 'deliver', side_effect=injected_deliver):
            thread = threading.Thread(target=run, name='isolated-drain-worker')
            thread.start()
            try:
                self.assertTrue(preparing.wait(3))
                self.assertTrue(evaluating.wait(3))
                until = time.monotonic() + 3
                while not drain.exists() and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(drain.exists())
                release_preparation.set()
                self.assertTrue(preparation_delivered.wait(3), 'notifier stopped while evaluation was still draining')
                self.assertFalse(finished.is_set())
                release_evaluation.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
            finally:
                release_preparation.set()
                release_evaluation.set()
                thread.join(7)
        self.assertEqual(errors, [])
        self.assertEqual(checks, {'preparation': 1, 'evaluation': 1})
        self.assertEqual(len(flushes), 1)
        self.assertIsInstance(flushes[0], (int, float))
        self.assertCountEqual(delivered, [jobs['preparation']['id'], jobs['evaluation']['id']])
        self.assertEqual(self.outbox(jobs['preparation'])['sent'], 1)
        self.assertEqual(self.outbox(jobs['evaluation'])['sent'], 1)


if __name__ == '__main__':
    unittest.main()
