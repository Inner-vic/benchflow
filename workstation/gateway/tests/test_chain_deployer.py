"""Isolated chain deployment checks; no service, Feishu, SSH, or hardware runs."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bench_gateway.config import load, save
from bench_gateway.core import Store
from bench_gateway.preparation import PreparationQueue
from tools import deploy_chain_update as deploy


class ChainDeployerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'build'
        self.target = self.root / '台架飞书统一入口'
        self.source_file = self.source / 'bench_gateway/worker.py'
        self.source_file.parent.mkdir(parents=True)
        self.source_file.write_text('# validated new worker\n', encoding='utf-8')
        self.destination = self.target / 'bench_gateway/worker.py'
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text('# deployed old worker\n', encoding='utf-8')
        self.old_worker = self.destination.read_bytes()
        self.cfg = {'mode': 'simulate', 'live_enabled': False,
                    'workstation_evaluation_enabled': True,
                    'default_evaluator': {'descriptor_sha256': 'existing-evaluator'},
                    'unchanged_property': 'preserve exact bytes'}
        save(self.target / 'config/gateway.json', self.cfg)
        self.original_config = (self.target / 'config/gateway.json').read_bytes()
        cfg = load(self.target)
        PreparationQueue(cfg, Store(cfg['state_root']))
        for name in deploy.RESEARCH_FILES:
            save(self.source / 'research' / name, {'state': 'PASS'})
        self.write_validation()
        self.source_patch = patch.object(deploy, 'SOURCE', self.source)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.code_sources = [self.source_file]
        self.sources = patch.object(deploy, 'deployment_sources', side_effect=lambda: list(self.code_sources)).start()
        self.addCleanup(patch.stopall)
        self.argv = patch('sys.argv', ['deploy_chain_update.py', '--deployment-root', str(self.target)])
        self.argv.start()
        self.start = patch.object(deploy, 'start', return_value={'active': True}).start()
        self.stop = patch.object(deploy, 'stop', return_value={'active': False}).start()
        self.status = patch.object(deploy, 'status', return_value={'instance': 'old-instance'}).start()
        self.ready = patch.object(deploy, 'wait_ready', return_value={
            'active': True, 'phase': 'RUNNING',
            'children': {'bridge': {'ready': True}, 'worker': {'ready': True}}}).start()

    def write_validation(self, **overrides):
        evidence = {'state': 'PASS', 'code_hashes': {
            'bench_gateway/worker.py': deploy.digest(self.source_file)}}
        evidence.update(overrides)
        save(self.source / 'research/chain-validation.json', evidence)

    def assert_config_unchanged(self):
        self.assertEqual((self.target / 'config/gateway.json').read_bytes(), self.original_config)

    def test_inventory_keeps_assembly_tools_and_adds_chain_tools(self):
        self.assertEqual(set(deploy.RESEARCH_FILES), {
            'chain-validation.json', 'chain-full-suite.log',
            'chain-audit.json', 'chain-workstation-validation.json'})
        self.assertTrue({'verify_assembly_workstation.py', 'verify_assembly_parser.py',
                         'verify_business_entry.py', 'deploy_evaluator_update.py',
                         'deploy_assembly_update.py', 'deploy_chain_update.py',
                         'audit_gateway_chain.py', 'manage_download_hosts.py',
                         'verify_chain_workstation.py'}.issubset(deploy.TOOLS))

    def test_success_backs_up_sqlite_and_exact_config_before_copy(self):
        with patch.object(deploy, 'assert_drained', wraps=deploy.assert_drained) as drained:
            deploy.main()
        self.assertEqual(drained.call_count, 2)
        self.assertEqual(self.destination.read_bytes(), self.source_file.read_bytes())
        self.assert_config_unchanged()
        self.stop.assert_called_once()
        self.start.assert_called_once()
        self.ready.assert_called_once()
        record = json.loads((self.target / 'research/chain-deployment.json').read_text(encoding='utf-8'))
        self.assertEqual(record['state'], 'PASS')
        self.assertFalse(record['config_modified'])
        self.assertFalse(record['live_enabled'])
        self.assertTrue(record['workstation_evaluation_enabled'])
        self.assertEqual(record['default_evaluator_sha256'], 'existing-evaluator')
        backup = Path(record['backup'])
        self.assertTrue(backup.name.startswith('before-chain-'))
        self.assertEqual((backup / 'config/gateway.json').read_bytes(), self.original_config)
        self.assertEqual((backup / 'bench_gateway/worker.py').read_bytes(), self.old_worker)
        with closing(sqlite3.connect(backup / 'gateway-state.sqlite3')) as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_changed_validation_hash_refuses_before_stopping(self):
        self.source_file.write_text('# changed after validation\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'Source changed since validation'):
            deploy.main()
        self.stop.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), self.old_worker)
        self.assert_config_unchanged()

    def test_uncovered_deployment_file_refuses_before_stopping(self):
        extra = self.source / 'README.md'
        extra.write_text('not validated', encoding='utf-8')
        self.code_sources.append(extra)
        with self.assertRaisesRegex(RuntimeError, 'not covered by validation'):
            deploy.main()
        self.stop.assert_not_called()

    def test_execution_switches_must_remain_disabled_hardware_enabled_evaluation(self):
        for override in ({'mode': 'live'}, {'live_enabled': True},
                         {'workstation_evaluation_enabled': False}):
            with self.subTest(override=override):
                save(self.target / 'config/gateway.json', {**self.cfg, **override})
                with self.assertRaisesRegex(RuntimeError, 'Keep hardware replay disabled'):
                    deploy.main()
        self.stop.assert_not_called()

    def test_failed_second_drain_restarts_unchanged_old_service(self):
        with patch.object(deploy, 'assert_drained', side_effect=[None, RuntimeError('new preparation')]):
            with self.assertRaisesRegex(RuntimeError, 'new preparation'):
                deploy.main()
        self.start.assert_called_once()
        self.assertEqual(self.destination.read_bytes(), self.old_worker)
        self.assert_config_unchanged()

    def test_backup_failure_restarts_unchanged_old_service(self):
        with patch.object(deploy.shutil, 'copy2', side_effect=OSError('backup unavailable')):
            with self.assertRaisesRegex(OSError, 'backup unavailable'):
                deploy.main()
        self.start.assert_called_once()
        self.assertEqual(self.destination.read_bytes(), self.old_worker)
        self.assert_config_unchanged()

    def test_readiness_failure_stops_new_service_then_rolls_back_files(self):
        self.ready.side_effect = RuntimeError('worker not ready')
        with self.assertRaisesRegex(RuntimeError, 'worker not ready'):
            deploy.main()
        self.assertEqual(self.stop.call_count, 2)
        self.assertEqual(self.start.call_count, 2)
        self.assertEqual(self.destination.read_bytes(), self.old_worker)
        self.assertFalse((self.target / 'research/chain-validation.json').exists())
        self.assertFalse((self.target / 'research/chain-deployment.json').exists())
        self.assert_config_unchanged()


if __name__ == '__main__':
    unittest.main()
