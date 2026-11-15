"""Exercise generated recovery with a crashed service awaiting automatic restart."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from bench_gateway.candidate_adapter import render
from test_candidate_adapter import fixture


def functions(source, names, namespace):
    tree = ast.parse(source)
    tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(tree, '<generated recovery>', 'exec'), namespace)
    return namespace


class RegistryCleanupTests(unittest.TestCase):
    def driver(self, rows, fail=None):
        template, candidate = fixture()
        generated = render(template, candidate, 'registry-recovery-test', 'owner')
        calls = []
        def bench(command, **kwargs):
            calls.append(command)
            if command.endswith('listservice'):
                return rows
            if fail and command.endswith(fail):
                raise RuntimeError('service manager rejected unload')
            return ''
        namespace = functions(generated['driver'],
            {'registered_apps', 'stop_registered_apps'},
            {'bench': bench, 'SMCTL': 'smctl', 'ProbeError': RuntimeError})
        return generated, namespace, calls

    def test_inactive_but_registered_service_is_unloaded(self):
        generated, ns, calls = self.driver(
            'NAME STATE\ninfer_expert_pcm_replay inactive\ninfer_expert_fusion running\n')
        ns['stop_registered_apps']()
        self.assertEqual(calls, ['smctl listservice',
            'smctl unloadsdf infer_expert_pcm_replay', 'smctl unloadsdf infer_expert_fusion'])
        restore = functions(generated['driver'], {'restore_formal_app'},
                            {'stop_registered_apps': lambda: (_ for _ in ()).throw(RuntimeError('stop reached'))})
        with self.assertRaisesRegex(RuntimeError, 'stop reached'):
            restore['restore_formal_app']({}, False)

    def test_absent_or_similarly_named_service_not_unloaded(self):
        _, ns, calls = self.driver('NAME STATE\ninfer_expert_pcm_replay_other running\n')
        ns['stop_registered_apps']()
        self.assertEqual(calls, ['smctl listservice'])

    def test_unload_error_propagates_before_formal_service_restart(self):
        _, ns, calls = self.driver('NAME STATE\ninfer_expert_pcm_replay inactive\n',
                                   fail='unloadsdf infer_expert_pcm_replay')
        with self.assertRaisesRegex(RuntimeError, 'rejected unload'):
            ns['stop_registered_apps']()
        self.assertEqual(len(calls), 2)

    def test_invalid_registry_response_rejects_cleanup(self):
        for rows in ('', 'failed to contact service manager\n', 'NAME\n'):
            with self.subTest(rows=rows):
                _, ns, calls = self.driver(rows)
                with self.assertRaisesRegex(RuntimeError, 'malformed'):
                    ns['stop_registered_apps']()
                self.assertEqual(calls, ['smctl listservice'])

    def test_restore_verification_rejects_pending_restart_registration(self):
        generated, _, _ = self.driver('NAME STATE\n')
        for registered, succeeds in (({'infer_expert_pcm_replay'}, False), (set(), True)):
            with self.subTest(registered=registered):
                times = iter([0, 0, 151])
                services = {'replay': 'inactive', 'fusion': 'active', 'xrecorder': False}
                receipts = []
                case = SimpleNamespace(
                    formal_status=lambda: services, registered_apps=lambda: registered,
                    ProbeError=RuntimeError, bench=lambda *a, **k: '',
                    sha256_remote=lambda paths: {},
                    write_json=lambda *a: receipts.append(a))
                ns = functions(generated['adapter'], {'verify_restore'}, {
                    'Path': Path, 'identity': lambda *a: None,
                    'state': lambda *a: {'services': services, 'hashes': {}},
                    'case': case, 'time': SimpleNamespace(monotonic=lambda: next(times), sleep=lambda t: None)})
                if succeeds:
                    ns['verify_restore'](Path('/runs/example'), 'scene')
                    self.assertEqual(len(receipts), 1)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'did not recover'):
                        ns['verify_restore'](Path('/runs/example'), 'scene')
                    self.assertEqual(receipts, [])


if __name__ == '__main__':unittest.main()
