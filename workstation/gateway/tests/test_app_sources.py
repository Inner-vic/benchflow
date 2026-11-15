import unittest
from bench_gateway.app_sources import normalize,is_filename
from bench_gateway.parser import quick
from bench_gateway.build_messages import extract
from bench_gateway.core import GatewayError

NAME='CCU_LIVC_AVM-release-SW0058_ULTRA_ID0111.20260918-2737.app.tar.gz'
BASE='https://artifactory.ep.chehejia.com/artifactory/xcu-acore-test-local/Acore/Civu/Release/app/'


class AppSourceTests(unittest.TestCase):
    def setUp(self):self.cfg={'livc_app_base_url':BASE,'download_hosts':['artifactory.ep.chehejia.com']}
    def test_known_complete_filename_expands_to_verified_2737_source(self):
        self.assertEqual(BASE+NAME,normalize(self.cfg,NAME));self.assertTrue(is_filename(NAME))
    def test_full_url_is_preserved_for_other_repositories(self):
        url='https://other.test/release/'+NAME
        self.assertEqual(url,normalize(self.cfg,url))
    def test_no_prefix_is_guessed_without_a_configured_source(self):
        with self.assertRaisesRegex(GatewayError,'尚未配置'):normalize({},NAME)
    def test_untrusted_base_cannot_be_used(self):
        for base in ('http://artifactory.ep.chehejia.com/app/','https://user:secret@artifactory.ep.chehejia.com/app/',
                     'https://evil.test/app/',BASE+'?token=private'):
            with self.subTest(base=base),self.assertRaises(GatewayError):normalize({**self.cfg,'livc_app_base_url':base},NAME)
    def test_incomplete_or_pathlike_names_are_rejected(self):
        for name in ('CCU_LIVC_AVM-release-XXXX','CCU_LIVC_AVM-../../evil.app.tar.gz','CCU_OTHER-release.app.tar.gz',
                     'CCU_LIVC_AVM-release.app.tar.gz?x=1','CCU_LIVC_AVM-release\\evil.app.tar.gz'):
            with self.subTest(name=name),self.assertRaises(GatewayError):normalize(self.cfg,name)
    def test_plain_filename_does_not_need_model_parsing(self):
        self.assertEqual({'action':'prepare','inputs':{'package':NAME}},quick(NAME))
    def test_livc_label_accepts_complete_filename(self):
        values,issue=extract('LIVC APP：'+NAME,require_app=True)
        self.assertFalse(issue);self.assertEqual(NAME,values['package'])
    def test_two_filenames_are_ambiguous(self):
        values,issue=extract(NAME+'\n'+NAME.replace('2737','2867'),require_app=True)
        self.assertFalse(values);self.assertIn('多个',issue)


if __name__=='__main__':unittest.main()
