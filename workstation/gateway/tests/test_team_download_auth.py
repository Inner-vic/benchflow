"""A dedicated team download credential never shares personal assets or secrets."""
import base64
import json
import unittest

import test_download_auth as fixtures
from bench_gateway import download_auth as auth
from bench_gateway.core import GatewayError,Store


class TeamDownloadAuthTests(unittest.TestCase):
    setUp=fixtures.DownloadAuthTests.setUp
    host='repo.example.com'

    def candidate(self,password='team-dummy-password'):
        return auth.save_team_candidate(self.cfg,self.user,self.host,'team-dummy',password)

    def activate(self,digest,**change):
        value={'state':'PASS','host':self.host,'credentials_configured':True,'asset_writes':0,'gzip_magic':True}
        auth.activate_team_candidate(self.cfg,self.user,self.host,digest,{**value,**change})

    def test_saved_candidate_does_not_change_either_users_effective_credential(self):
        auth.save_basic(self.cfg,self.user,self.host,'personal','personal-dummy')
        before=auth.load_authorization(self.cfg,self.user,self.host)
        self.candidate()
        self.assertEqual(auth.load_authorization(self.cfg,self.user,self.host),before)
        self.assertIsNone(auth.load_authorization(self.cfg,self.other,self.host))
        self.assertFalse(auth.team_credential_status(self.cfg,self.user,self.host)['configured'])

    def test_verified_activation_applies_to_enabled_users_and_preserves_personal_blob(self):
        auth.save_basic(self.cfg,self.user,self.host,'personal','personal-dummy')
        _,path=auth._scope(self.cfg,self.user,self.host);before=path.read_bytes()
        digest=self.candidate();self.activate(digest)
        expected='Basic '+base64.b64encode(b'team-dummy:team-dummy-password').decode()
        for user in (self.user,self.other):self.assertEqual(auth.load_authorization(self.cfg,user,self.host),expected)
        self.assertEqual(before,path.read_bytes())
        self.assertIsNone(auth.load_authorization(self.cfg,self.other,'other.example.com'))

    def test_candidate_can_only_be_used_by_the_local_validation_call(self):
        self.candidate()
        cfg={**self.cfg,'_download_auth_team_candidate_host':self.host}
        self.assertTrue(auth.load_authorization(cfg,self.user,self.host).startswith('Basic '))
        self.assertIsNone(auth.load_authorization(self.cfg,self.user,self.host))

    def test_failed_probe_and_changed_candidate_preserve_active_credentials(self):
        self.activate(self.candidate())
        original=auth.load_authorization(self.cfg,self.other,self.host)
        digest=self.candidate('replacement-dummy')
        for changes in ({'state':'AUTH_REQUIRED'},{'host':'other.example.com'},{'gzip_magic':False}):
            with self.assertRaises(GatewayError):self.activate(digest,**changes)
            self.assertEqual(original,auth.load_authorization(self.cfg,self.other,self.host))
        self.candidate('another-dummy')
        with self.assertRaises(GatewayError):self.activate(digest)
        self.assertEqual(original,auth.load_authorization(self.cfg,self.other,self.host))

    def test_disabled_or_unknown_user_cannot_borrow_team_identity(self):
        self.activate(self.candidate())
        s=Store(self.cfg['state_root'])
        with s.connect() as c:c.execute('UPDATE users SET enabled=0 WHERE id=?',(self.other,))
        for user in (self.other,'ou_unregistered'):
            with self.assertRaises(GatewayError):auth.load_authorization(self.cfg,user,self.host)

    def test_corrupt_team_credential_does_not_silently_use_personal_account(self):
        auth.save_basic(self.cfg,self.user,self.host,'personal','personal-dummy')
        self.activate(self.candidate())
        _,path=auth._team_scope(self.cfg,self.user,self.host)
        path.write_bytes(b'not-json')
        with self.assertRaises(GatewayError):auth.load_authorization(self.cfg,self.user,self.host)
        self.assertTrue(auth.team_credential_status(self.cfg,self.user,self.host)['needs_update'])

    def test_status_and_encrypted_envelope_never_disclose_account_or_password(self):
        self.activate(self.candidate())
        _,path=auth._team_scope(self.cfg,self.user,self.host)
        output=path.read_text()+json.dumps(auth.team_credential_status(self.cfg,self.user,self.host))
        for secret in ('team-dummy','team-dummy-password','Basic '):self.assertNotIn(secret,output)

    def test_activation_does_not_create_user_assets_jobs_or_drafts(self):
        store=Store(self.cfg['state_root'])
        def snapshot():
            with store.connect() as c:
                return {table:[tuple(r) for r in c.execute('SELECT * FROM '+table)] for table in ('users','jobs','assets','material_drafts')}
        before=snapshot();self.activate(self.candidate())
        self.assertEqual(before,snapshot())


if __name__=='__main__':unittest.main()
