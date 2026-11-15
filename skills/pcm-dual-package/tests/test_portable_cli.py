"""Black-box CLI checks run in a clean cwd, without live service access."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_existing_job as fixtures


MODULE = Path(__file__).resolve().parents[1]


class PortableCliTests(unittest.TestCase):
    def command(self, cwd, script, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        return subprocess.run([sys.executable, '-B', str(MODULE / 'scripts' / script),
                               *map(str, args)], cwd=cwd, env=env, capture_output=True,
                              text=True, encoding='utf-8', timeout=30)

    def test_branch_example_parses_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory(prefix='pcm-cli-') as directory:
            output = Path(directory) / 'resolved.json'
            result = self.command(directory, 'resolve_pcm_input.py', '--request',
                                  MODULE / 'examples/input.json', '--output', output)
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(parsed['status'], 'READY_TO_PIN_COMMIT')
            self.assertEqual(parsed['branch'], 'dev-314')
            self.assertNotIn('commit', parsed)  # Parsing alone did not contact GitLab.

    def test_unpublished_example_creates_plan_but_cannot_record_submission(self):
        with tempfile.TemporaryDirectory(prefix='pcm-cli-') as directory:
            work = Path(directory)
            state = work / 'pair.json'
            result = self.command(work, 'pcm_existing_job.py', 'plan', '--context',
                                  MODULE / 'examples/context.synthetic.json', '--state', state)
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(state.read_text(encoding='utf-8'))
            self.assertEqual(plan['status'], 'COMPONENT_RECIPES_REQUIRED')
            self.assertTrue((work / 'OFF.parameters.json').is_file())
            self.assertTrue((work / 'ON.parameters.json').is_file())
            before = state.read_bytes()
            blocked = self.command(work, 'pcm_existing_job.py', 'submit-intent', '--state',
                                   state, '--mode', 'OFF', '--reviewed-sha', plan['plan_sha256'])
            self.assertNotEqual(blocked.returncode, 0)
            self.assertEqual(state.read_bytes(), before)

    def test_other_project_requires_compatibility_check(self):
        with tempfile.TemporaryDirectory(prefix='pcm-cli-') as directory:
            request = Path(directory) / 'request.json'
            request.write_text(json.dumps({'value':
                'https://gitlab.chehejia.com/another/project/-/tree/dev-314'}), encoding='utf-8')
            result = self.command(directory, 'resolve_pcm_input.py', '--request', request)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'],
                             'REPOSITORY_COMPATIBILITY_CHECK_REQUIRED')

    def test_compile_time_difference_is_reported_and_cannot_pass_strict_acceptance(self):
        with tempfile.TemporaryDirectory(prefix='pcm-cli-') as directory:
            work = Path(directory)
            state = fixtures.job.make_plan(fixtures.context())
            fixtures.start_pair(state)
            locks = fixtures.locks()
            files = {}
            for mode, number in [('OFF', 101), ('ON', 102)]:
                lock = work / ('app-' + str(number) + '.lock')
                lock.write_bytes(fixtures.job.canonical(locks[mode]))
                app = work / ('app-' + str(number) + '.app.tar.gz')
                fixtures.archive(app, mode, lock.name, lock.read_bytes(),
                    {'livc/lib/libexiv2.so': b'compile-time:' +
                     (b'01:46:19' if mode == 'OFF' else b'01:37:10')})
                fixtures.job.record_result(state, mode, {
                    'build_url': fixtures.job.JOB_URL + str(number) + '/', 'result': 'SUCCESS',
                    'app_url': fixtures.job.ARTIFACT_ROOT + 'Release/app/' + app.name,
                    'lock_url': fixtures.job.ARTIFACT_ROOT + 'Release/' + lock.name,
                    'app_sha256': hashlib.sha256(app.read_bytes()).hexdigest(),
                    'lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest()})
                files[mode] = (app, lock)
            path = work / 'pair.json'
            path.write_bytes(fixtures.job.canonical(state))
            result = self.command(work, 'pcm_existing_job.py', 'compare', '--state', path,
                '--off-app', files['OFF'][0], '--on-app', files['ON'][0],
                '--off-lock', files['OFF'][1], '--on-lock', files['ON'][1])
            self.assertNotEqual(result.returncode, 0)
            actual = fixtures.job.load(path)
            self.assertEqual(actual['status'], 'COMPARISON_FAILED')
            self.assertEqual(actual['lock_comparison']['status'], 'PASS')
            self.assertIn('livc/lib/libexiv2.so', actual['comparison_error'])


if __name__ == '__main__':
    unittest.main()
