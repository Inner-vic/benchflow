import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import test_incremental_inputs as fixtures
from bench_gateway.attachments import Attachments
from bench_gateway.material_summary import describe,text,dataset_details
from bench_gateway.tasks import Tasks


class MaterialSummaryTests(unittest.TestCase):
    setUp=fixtures.IncrementalInputsTests.setUp
    base=fixtures.IncrementalInputsTests.base

    def test_yaml_only_names_inherited_dependencies_and_selected_software(self):
        self.base();c=self.c;self.intake.update(c.user,{'yaml_request':'config/new.yaml flag=false'})
        value=describe(c.store,c.user)
        self.assertEqual('A',value['software']);self.assertIn('相机、雷达、M100',value['dependencies'])
        self.assertIn('new.yaml',value['changes']);self.assertNotIn('a-',value['software'])

    def test_new_app_sensors_are_candidates_and_m100_is_prepared_again(self):
        self.base();c=self.c;self.intake.update(c.user,{'package':'https://example.test/B.app.tar.gz?token=secret'})
        value=describe(c.store,c.user)
        self.assertEqual('B',value['software']);self.assertIn('沿用本人相机、雷达',value['dependencies'])
        self.assertIn('M100自动准备',value['dependencies']);self.assertNotIn('secret',text(value))

    def test_other_owner_has_no_implicit_software_version(self):
        self.base();c=self.c;self.intake.update(c.other,{'yaml_request':'config/new.yaml flag=true'})
        value=describe(c.store,c.other)
        self.assertEqual('待提供 APP',value['software']);self.assertNotIn('沿用',value['dependencies'])

    def test_dataset_shows_registered_shared_name_and_exact_source(self):
        asset={'name':'18场景','owner':'shared','data':{'remote_path':'/data/shared/scenes'}}
        value=dataset_details('ds-123',asset,'ou_me')
        self.assertEqual('共享:18场景',value['dataset']);self.assertEqual('/data/shared/scenes',value['dataset_source'])

    def test_dataset_private_directory_is_retained_and_foreign_asset_is_not_used(self):
        asset={'name':'other private','owner':'ou_other','data':{'remote_path':'/data/other'}}
        value=dataset_details('/data/mine/scenes',asset,'ou_me')
        self.assertEqual('scenes',value['dataset']);self.assertEqual('/data/mine/scenes',value['dataset_source'])

    def test_upload_without_current_material_record_restores_own_successful_version(self):
        self.base();c=self.c
        with c.store.connect() as db:
            db.execute('DELETE FROM task_focus WHERE owner=?',(c.user,));db.execute('DELETE FROM material_drafts WHERE owner=?',(c.user,))
        self.upload_yaml()
        data=self.tasks.current(c.user)['data']
        self.assertEqual(c.package['id'],data['package']);self.assertEqual(c.sos['camera_so']['id'],data['camera_so'])
        self.assertEqual('1,2',data['scene_scope']);self.assertIn('common_param.yaml',describe(c.store,c.user)['changes'])

    def test_explicit_reset_prevents_attachment_from_silently_restoring_old_package(self):
        self.base();c=self.c;self.intake.update(c.user,{},new=True);self.upload_yaml()
        self.assertNotIn('package',self.tasks.current(c.user)['data'])

    def upload_yaml(self):
        c=self.c;p=c.root/'.cc-connect/attachments/om_yaml_receipt_001/common_param.yaml';p.parent.mkdir(parents=True);p.write_text('log:\n  concise: true\n',encoding='utf-8')
        message={'message_id':p.parent.name,'msg_type':'file','chat_id':'oc_alice','sender':{'id':c.user,'sender_type':'user'},'body':{'content':json.dumps({'file_name':p.name,'file_key':'file_test'})}}
        registry=Mock();registry.ssh.upload.return_value=c.store.remote_root(c.user,c.cfg['remote_users_root'])+'/tasks/new/inputs/config.yaml'
        Attachments(c.cfg,c.store,registry,client=SimpleNamespace(message=lambda mid:message)).receive(c.user,c.session,str(p))


if __name__=='__main__':unittest.main()
