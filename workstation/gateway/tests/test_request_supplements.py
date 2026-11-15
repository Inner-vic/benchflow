"""One-request chat overrides never silently become another task's defaults."""
import unittest
import test_incremental_inputs as fixtures
from bench_gateway import parser
from bench_gateway.service import Service


class RequestSupplementTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.IncrementalInputsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.c=self.f.c;self.c.store.request_scoped_supplements=True

    def test_next_dependency_change_retains_software_but_drops_last_request_overrides(self):
        old,job=self.f.base()
        self.f.intake.update(self.c.user,{'camera_so':'another-camera'})
        new=self.f.tasks.current(self.c.user)
        self.assertNotEqual(old['id'],new['id'])
        self.assertEqual(self.c.package['id'],new['data']['package'])
        self.assertEqual('全部',new['data']['scene_scope']);self.assertNotIn('yaml_request',new['data'])
        self.assertEqual(old,self.f.tasks.get(self.c.user,old['id']))
        self.assertEqual(job,self.c.store.job(self.c.user,job['id']))

    def test_next_yaml_request_is_not_combined_with_old_prepared_yaml(self):
        self.f.base()
        request='common.yaml debug.enabled=false'
        self.f.intake.update(self.c.user,{'yaml_request':request})
        new=self.f.tasks.current(self.c.user)
        self.assertEqual(request,new['data']['yaml_request']);self.assertEqual('全部',new['data']['scene_scope'])

    def test_same_unsubmitted_request_keeps_its_supplements_through_new_files(self):
        self.f.intake.update(self.c.user,{'scene_scope':'3','yaml_request':'common.yaml test.flag=true'})
        before=self.f.tasks.current(self.c.user)
        self.f.intake.update(self.c.user,{'lidar_so':'next-lidar'})
        after=self.f.tasks.current(self.c.user)
        self.assertEqual(before['id'],after['id']);self.assertEqual('3',after['data']['scene_scope'])
        self.assertIn('test.flag=true',after['data']['yaml_request'])

    def test_new_app_does_not_inherit_old_scope_or_yaml(self):
        self.f.base()
        self.f.intake.update(self.c.user,{'package':'https://example.test/new.app.tar.gz'})
        current=self.f.tasks.current(self.c.user)
        self.assertEqual('全部',current['data']['scene_scope']);self.assertNotIn('yaml_request',current['data'])

    def test_explicit_repeat_still_requests_the_same_snapshot(self):
        old,_=self.f.base()
        self.f.intake.update(self.c.user,{},incoming_kind='repeat',force_run=True)
        current=self.f.tasks.current(self.c.user)
        self.assertNotEqual(old['id'],current['id'])
        self.assertEqual(old['data']['scene_scope'],current['data']['scene_scope'])
        self.assertIn('/preparations/old/a.yaml',current['data']['yaml_request'])

    def test_inline_service_enables_scoping_and_chat_alias_is_whole_message_only(self):
        service=Service({**self.c.cfg,'inline_material_card':True})
        self.assertTrue(service.store.request_scoped_supplements)
        for text in ('本次只跑场景1,2','场景范围：1、2'):
            self.assertEqual({'action':'prepare','inputs':{'scene_scope':'1,2'}},parser.quick(text))
        self.assertIsNone(parser.quick('本次只跑场景1,2可以吗？'))


if __name__=='__main__':unittest.main()
