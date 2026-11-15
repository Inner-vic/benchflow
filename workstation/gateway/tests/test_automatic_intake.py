"""Automatic private-chat boundaries preserve submitted inputs and avoid unnecessary prompts."""
import copy
import unittest
from unittest.mock import patch
from bench_gateway.core import GatewayError
from bench_gateway.intake import Intake
from bench_gateway.parser import quick
from bench_gateway.service import Service
from bench_gateway.tasks import Tasks
import test_attachment_intake as fixtures


class AutomaticIntakeTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.AttachmentIntakeTests();self.case.setUp()
        self.addCleanup(self.case.tearDown)
        c=self.case
        self.intake=Intake(c.store);self.tasks=Tasks(c.store)
        self.service=Service({**c.cfg,'state_root':str(c.store.root),'mode':'simulate'})

    def submitted(self,state='QUEUED'):
        c=self.case
        self.intake.update(c.user,{'package':'A','camera_so':'old-camera','lidar_so':'old-lidar',
            'm100_so':'old-m100','dataset':'shared','scene_scope':'全部','yaml_request':'common.yaml field=true'})
        task=self.tasks.current(c.user)
        job,_=c.store.submit(c.user,c.session,state,{'action':'replay','assets':copy.deepcopy(task['data'])})
        self.tasks.link(c.user,job)
        if state!='QUEUED':c.store.finish(job,state,{'test_fixture':True})
        return self.tasks.get(c.user,task['id']),c.store.job(c.user,job['id'])

    def test_so_first_then_package_and_yaml_share_one_draft_without_execution(self):
        c=self.case
        c.inbox.receive(c.user,c.session,str(c.file()))
        first=self.tasks.current(c.user)
        reply=self.service.handle(c.session,'https://example.test/A.app.tar.gz',quick('https://example.test/A.app.tar.gz'))
        self.service.update_inputs(c.user,{'yaml_request':'common.yaml field=true'})
        current=self.tasks.current(c.user)
        self.assertEqual(current['id'],first['id'])
        self.assertEqual(current['data']['camera_so'],'a-test-libxcamera.so')
        self.assertEqual(current['data']['package'],'https://example.test/A.app.tar.gz')
        self.assertNotIn('新建任务',reply)
        self.assertNotIn('task-',reply)
        self.assertEqual(c.store.jobs(c.user),[])

    def test_submitted_states_roll_over_once_and_preserve_original_snapshot(self):
        c=self.case
        for state in ('QUEUED','RUNNING','SUCCEEDED','FAILED','NEEDS_REVIEW','SIMULATED'):
            with self.subTest(state=state):
                self.intake.update(c.user,{},new=True)
                old,job=self.submitted(state)
                reply=self.service.handle(c.session,'换相机库',{'action':'prepare','inputs':{'camera_so':'new-camera'}})
                following=self.tasks.current(c.user)
                self.service.update_inputs(c.user,{'yaml_request':'new.yaml x=2'})
                self.assertNotEqual(old['id'],following['id'])
                self.assertEqual(self.tasks.current(c.user)['id'],following['id'])
                self.assertEqual(following['data']['package'],'A')
                self.assertEqual(self.tasks.get(c.user,old['id']),old)
                self.assertEqual(c.store.job(c.user,job['id']),job)
                self.assertIn('下一次回灌',reply)

    def test_new_release_resets_package_specific_materials_but_keeps_workload(self):
        c=self.case
        self.intake.update(c.user,{'package':'A','camera_so':'C','m100_so':'M','yaml_request':'old config','dataset':'shared','scene_scope':'全部'})
        old=self.tasks.current(c.user)
        reply=self.service.handle(c.session,'软件包：https://example.test/B.app.tar.gz',quick('软件包：https://example.test/B.app.tar.gz'))
        current=self.tasks.current(c.user)
        self.assertNotEqual(old['id'],current['id'])
        self.assertEqual(self.tasks.get(c.user,old['id'])['data'],old['data'])
        self.assertEqual(current['data']['dataset'],'shared')
        self.assertEqual('C',current['data']['camera_so'])
        for key in ('m100_so','yaml_request'):self.assertNotIn(key,current['data'])
        self.assertIn('还需要：',reply)
        self.assertNotIn('还需要：相机 SO',reply)
        self.assertIn('M100 SO',reply)

    def test_yaml_attachment_after_submit_replaces_copied_operation_only_in_next_task(self):
        c=self.case
        first=c.file('common_param.yaml',b'a: 1\n')
        c.registry.ssh.upload.return_value=c.store.remote_root(c.user)+'/tasks/one/asset.bin'
        c.inbox.receive(c.user,c.session,str(first))
        old=self.tasks.current(c.user)
        job,_=c.store.submit(c.user,c.session,'freeze',{'action':'replay'})
        self.tasks.link(c.user,job)
        second=c.file('common_param.yaml',b'a: 2\n',mid='om_next_yaml_002')
        c.registry.ssh.upload.return_value=c.store.remote_root(c.user)+'/tasks/two/asset.bin'
        reply=c.inbox.receive(c.user,c.session,str(second))
        current=self.tasks.current(c.user)
        self.assertNotEqual(old['id'],current['id'])
        self.assertIn('/tasks/one/',self.tasks.get(c.user,old['id'])['data']['yaml_request'])
        self.assertNotIn('/tasks/one/',current['data']['yaml_request'])
        self.assertIn('/tasks/two/',current['data']['yaml_request'])
        self.assertIn('下一次回灌',reply)

    def test_yaml_target_can_be_answered_naturally_without_identifier(self):
        c=self.case
        c.inbox.receive(c.user,c.session,str(c.file('custom.yaml',b'a: 1\n')))
        reply=c.inbox.command(c.user,'替换 APP 内 etc/uroad/config/custom.yaml')
        self.assertIn('etc/uroad/config/custom.yaml',reply)
        self.assertIn('yaml_request',self.intake.get(c.user))

    def test_bare_known_sources_are_quick_intake_but_questions_do_not_mutate(self):
        cases={'https://example.test/A.app.tar.gz':'package','/mnt/data/libxcamera.so':'camera_so',
               'https://example.test/libxlidar.so':'lidar_so','M100SO：https://example.test/build.so':'m100_so'}
        with patch('bench_gateway.parser.subprocess.run') as model:
            for text,field in cases.items():
                intent=quick(text)
                self.assertEqual(intent['action'],'prepare');self.assertIn(field,intent['inputs'])
            model.assert_not_called()
        for text in ('https://example.test/A.app.tar.gz？','https://example.test/A.app.tar.gz?可以吗',
                     '这个链接可以吗 https://example.test/A.app.tar.gz','软件包是不是要先发？'):
            self.assertIsNone(quick(text),text)


if __name__=='__main__':unittest.main()
