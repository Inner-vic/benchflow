import importlib.util
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('publisher', Path(__file__).resolve().parents[1] / 'examples/ci-20261106/internal_pcm_pair/publish_recipes_ci.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublicationBoundaries(unittest.TestCase):
    def test_missing_service_credential_is_rejected(self):
        env = {'GITLAB_CI': 'true', 'CI_PROJECT_PATH': publisher.PROJECT,
               'CI_COMMIT_BRANCH': publisher.BRANCH,
               'ARTIFACTORY_CONAN_USER': 'example-rt-svc'}
        with self.assertRaisesRegex(ValueError, 'SERVICE_ACCOUNT'):
            publisher.check_environment(env)

    def test_local_invocation_stops_before_network_or_subprocess(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory(prefix='pcm-ci-stop-') as directory:
            try:
                os.chdir(directory)
                with patch.dict(os.environ, {}, clear=True), \
                     patch.object(publisher.subprocess, 'check_output') as git, \
                     patch.object(publisher.subprocess, 'run') as command, \
                     patch.object(publisher.urllib.request, 'build_opener') as network, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(publisher.main(), 2)
                git.assert_not_called()
                command.assert_not_called()
                network.assert_not_called()
                receipt = json.loads(Path('pcm-publication-receipt.json').read_text())
                self.assertEqual(receipt['status'], 'STOPPED')
                self.assertEqual(receipt['published'], [])
            finally:
                os.chdir(original)

    def test_redirect_does_not_forward_authentication(self):
        with self.assertRaisesRegex(ValueError, 'credentials not forwarded'):
            publisher.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_personal_account_and_fork_are_rejected(self):
        env = {'GITLAB_CI': 'true', 'CI_PROJECT_PATH': publisher.PROJECT,
               'CI_COMMIT_BRANCH': publisher.BRANCH, 'ARTIFACTORY_CONAN_USER': 'developer',
               'ARTIFACTORY_CONAN_PASS': 'TEST_ONLY_NOT_A_REAL_CREDENTIAL'}
        with self.assertRaisesRegex(ValueError, 'SERVICE_ACCOUNT'):
            publisher.check_environment(env)
        env['ARTIFACTORY_CONAN_USER'] = 'example-rt-svc'
        env['CI_PROJECT_PATH'] = 'someone/uroad-fork'
        with self.assertRaisesRegex(ValueError, 'personal fork'):
            publisher.check_environment(env)

    def test_existing_different_revision_stops_without_read_or_upload(self):
        listing = {'children': [{'folder': True, 'uri': '/different-revision'}]}
        called = []
        with self.assertRaisesRegex(ValueError, 'refusing overwrite'):
            publisher.existing_action(listing, 'expected', 'a' * 64, lambda value: called.append(value))
        self.assertEqual(called, [])

    def test_identical_existing_recipe_is_skipped(self):
        recipe = b'reviewed recipe bytes'
        digest = publisher.hashlib.sha256(recipe).hexdigest()
        listing = {'children': [{'folder': True, 'uri': '/expected'}]}
        self.assertEqual(publisher.existing_action(listing, 'expected', digest, lambda value: recipe),
                         'READ_BACK_AND_SKIP')
        with self.assertRaisesRegex(ValueError, 'refusing overwrite'):
            publisher.existing_action(listing, 'expected', digest, lambda value: b'changed bytes')

    def test_new_recipe_path_does_not_read_an_existing_recipe(self):
        def unexpected(_):
            raise AssertionError('No existing recipe may be read at this stage')
        self.assertEqual(publisher.existing_action(None, 'expected', 'a' * 64, unexpected), 'CREATE_NEW')

    def test_bundle_is_bound_to_all_reviewed_recipes(self):
        folder = Path(__file__).resolve().parents[1] / 'examples/ci-20261106/internal_pcm_pair/recipes'
        manifest = publisher.reviewed_inputs(folder)
        self.assertEqual(set(manifest['components']), {'OFF', 'ON', 'xlidar'})
        self.assertEqual(manifest['source_commit'], publisher.SOURCE)


if __name__ == '__main__':
    unittest.main()
