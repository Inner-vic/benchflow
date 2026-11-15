import base64
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from bench_gateway import download_auth as auth
from bench_gateway.core import GatewayError, Store


class DownloadAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {'project_root': str(self.root), 'state_root': str(self.root / 'state'),
                    'download_hosts': ['repo.example.com', 'other.example.com']}
        self.user = 'ou_download_owner'
        self.other = 'ou_other_owner'
        store = Store(self.cfg['state_root'])
        store.allow(self.user)
        store.allow(self.other)

    def path(self, user=None, host='repo.example.com'):
        return auth._scope(self.cfg, user or self.user, host)[1]

    def save(self, username='dummy-user', password='dummy-password'):
        auth.save_basic(self.cfg, self.user, 'repo.example.com', username, password)

    def test_missing_credentials_is_anonymous(self):
        self.assertIsNone(auth.load_authorization(self.cfg, self.user, 'repo.example.com'))
        self.assertEqual(auth.credential_status(self.cfg, self.user, 'repo.example.com'),
                         {'configured': False, 'needs_update': False, 'host': 'repo.example.com', 'scheme': 'basic'})
        self.assertFalse((self.root / 'runtime').exists())

    def test_real_local_roundtrip_and_status_discloses_no_account(self):
        self.save()
        expected = 'Basic ' + base64.b64encode(b'dummy-user:dummy-password').decode()
        self.assertEqual(auth.load_authorization(self.cfg, self.user, 'repo.example.com'), expected)
        status = auth.credential_status(self.cfg, self.user, 'repo.example.com')
        self.assertTrue(status['configured'])
        self.assertNotIn('dummy', json.dumps(status))
        self.assertNotIn('Basic ', json.dumps(status))
        raw = self.path().read_bytes()
        self.assertNotIn(b'dummy-user', raw)
        self.assertNotIn(b'dummy-password', raw)
        if os.name == 'nt':
            self.assertNotIn(expected.encode(), raw)
            self.assertNotIn(base64.b64encode(expected.encode()), raw)
            self.assertEqual(json.loads(raw)['protection'], 'windows-dpapi-current-user')
        else:
            self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.path().parent.stat().st_mode), 0o700)

    def test_no_cross_owner_or_host_fallback(self):
        self.save()
        self.assertIsNone(auth.load_authorization(self.cfg, self.other, 'repo.example.com'))
        self.assertIsNone(auth.load_authorization(self.cfg, self.user, 'other.example.com'))

    def test_copied_blob_cannot_change_owner_or_host(self):
        self.save()
        for user, host in [(self.other, 'repo.example.com'), (self.user, 'other.example.com')]:
            target = self.path(user, host)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.path(), target)
            with self.assertRaises(GatewayError):
                auth.load_authorization(self.cfg, user, host)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI binding test')
    def test_dpapi_entropy_prevents_metadata_rebinding(self):
        self.save()
        target = self.path(self.other)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(self.path().read_bytes())
        data['owner_key'] = auth.owner_key(self.other)
        target.write_text(json.dumps(data), encoding='ascii')
        with self.assertRaises(GatewayError):
            auth.load_authorization(self.cfg, self.other, 'repo.example.com')

    def test_atomic_update_replaces_only_same_scope(self):
        self.save()
        original = self.path().read_bytes()
        self.save(password='new-dummy-password')
        self.assertNotEqual(original, self.path().read_bytes())
        self.assertEqual(auth.load_authorization(self.cfg, self.user, 'repo.example.com'),
                         'Basic ' + base64.b64encode(b'dummy-user:new-dummy-password').decode())
        self.assertEqual(list(self.path().parent.glob('.saving-*')), [])

    def test_unknown_or_disabled_owner_rejected(self):
        for user in ['ou_not_registered', '../../escape']:
            with self.assertRaises(GatewayError):
                auth.save_basic(self.cfg, user, 'repo.example.com', 'dummy', 'dummy')
        with Store(self.cfg['state_root']).connect() as conn:
            conn.execute('UPDATE users SET enabled=0 WHERE id=?', (self.user,))
        with self.assertRaises(GatewayError):
            auth.load_authorization(self.cfg, self.user, 'repo.example.com')

    def test_unapproved_or_non_host_inputs_rejected(self):
        for host in ['unknown.example.com', 'https://repo.example.com', 'repo.example.com:443',
                     'repo.example.com/path', 'user@repo.example.com', 'Repo.example.com',
                     'repo.example.com\n', '..', '-repo.example.com']:
            with self.subTest(host=host):
                with self.assertRaises(GatewayError):
                    auth.save_basic(self.cfg, self.user, host, 'dummy', 'dummy')

    def test_invalid_credentials_do_not_create_directory_or_echo_input(self):
        for username, password in [('', 'dummy'), ('name:bad', 'dummy'), ('dummy', ''),
                                   ('dummy', 'secret\r\nheader'), (' dummy', 'dummy'),
                                   ('dummy', 'x' * 4097)]:
            with self.assertRaises(GatewayError) as caught:
                self.save(username, password)
            self.assertNotIn(password, str(caught.exception)) if password else None
        self.assertFalse((self.root / 'runtime').exists())

    def test_acl_failure_leaves_no_blob_or_plaintext(self):
        real = auth._restrict_access
        def fail_file(path, directory=False):
            if not directory:
                raise OSError('dummy-sensitive-system-error')
            return real(path, directory=directory)
        with mock.patch.object(auth, '_restrict_access', side_effect=fail_file):
            with self.assertRaises(GatewayError) as caught:
                self.save()
        self.assertNotIn('dummy', str(caught.exception))
        self.assertFalse(self.path().exists())
        self.assertEqual(list(self.path().parent.glob('.saving-*')), [])

    def test_failed_update_preserves_prior_protected_file(self):
        self.save()
        old = self.path().read_bytes()
        with mock.patch.object(auth.os, 'replace', side_effect=OSError('dummy-secret')):
            with self.assertRaises(GatewayError):
                self.save(password='replacement-secret')
        self.assertEqual(self.path().read_bytes(), old)
        self.assertEqual(list(self.path().parent.glob('.saving-*')), [])

    def test_corrupt_blob_has_generic_error_and_can_be_overwritten(self):
        self.save()
        self.path().write_text('{"dummy-password":"sensitive"}', encoding='ascii')
        with self.assertRaises(GatewayError) as caught:
            auth.load_authorization(self.cfg, self.user, 'repo.example.com')
        self.assertNotIn('sensitive', str(caught.exception))
        self.assertEqual(auth.credential_status(self.cfg, self.user, 'repo.example.com')['needs_update'], True)
        self.save()
        self.assertTrue(auth.credential_status(self.cfg, self.user, 'repo.example.com')['configured'])

    def test_path_symlink_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        runtime = self.root / 'runtime'
        try:
            runtime.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation not permitted')
        with self.assertRaises(GatewayError):
            self.save()
        self.assertEqual(list(outside.iterdir()), [])

    def test_owner_path_symlink_or_junction_rejected_before_read(self):
        self.save()
        with mock.patch.object(Path, 'is_junction', create=True, return_value=True):
            with self.assertRaises(GatewayError):
                auth.load_authorization(self.cfg, self.user, 'repo.example.com')

    def test_hardlinked_credential_file_rejected(self):
        self.save()
        try:
            os.link(self.path(), self.root / 'hardlink')
        except OSError:
            self.skipTest('hardlink creation not permitted')
        with self.assertRaises(GatewayError):
            auth.load_authorization(self.cfg, self.user, 'repo.example.com')
        with self.assertRaises(GatewayError):
            self.save(password='replacement')

    @unittest.skipIf(os.name == 'nt', 'POSIX modes')
    def test_permissive_posix_file_rejected(self):
        self.save()
        self.path().chmod(0o644)
        with self.assertRaises(GatewayError):
            auth.load_authorization(self.cfg, self.user, 'repo.example.com')


if __name__ == '__main__':
    unittest.main()
