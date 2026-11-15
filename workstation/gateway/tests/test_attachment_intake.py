import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from bench_gateway.core import Store,GatewayError
from bench_gateway.attachments import Attachments,TRAILER
from bench_gateway.intake import Intake
from bench_gateway.tasks import Tasks


def elf():
    data=bytearray(64);data[:6]=b'\x7fELF\x02\x01';data[18:20]=(183).to_bytes(2,'little')
    return bytes(data)+b'test library'


class AttachmentIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'state');self.user='ou_attachment_owner';self.other='ou_attachment_other'
        self.store.allow(self.user);self.store.allow(self.other)
        self.cfg={'project_root':str(self.root),'remote_users_root':'/mnt/data/bench-gateway/users'}
        self.session='feishu:oc_private:'+self.user;self.messages={}
        self.client=SimpleNamespace(message=lambda mid:copy.deepcopy(self.messages[mid]))
        self.registry=Mock();self.registry.ssh.upload.return_value=self.store.remote_root(self.user)+'/tasks/test/input/asset.bin'
        self.registry.record.side_effect=lambda user,kind,name,data:{'id':'a-test-'+name,'owner':user,'kind':kind,'name':name,'data':data}
        self.inbox=Attachments(self.cfg,self.store,self.registry,self.client)

    def tearDown(self):self.tmp.cleanup()

    def file(self,name='libxcamera.so',data=None,mid='om_test_message_001',owner=None):
        p=self.root/'.cc-connect/attachments'/mid/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(elf() if data is None else data)
        self.messages[mid]={'message_id':mid,'msg_type':'file','chat_id':'oc_private',
            'sender':{'id':owner or self.user,'sender_type':'user'},'body':{'content':json.dumps({'file_name':name,'file_key':'file_test_key'})}}
        return p

    def test_so_gets_verified_owner_task_and_workstation_asset(self):
        p=self.file();answer=self.inbox.prompt(self.user,self.session,'相机库'+TRAILER+str(p)+')')
        task=Tasks(self.store).current(self.user)
        self.assertIn('相机 SO',answer)
        self.assertEqual(Intake(self.store).get(self.user)['camera_so'],'a-test-libxcamera.so')
        args=self.registry.ssh.upload.call_args
        self.assertEqual(args.args[1]['owner'],self.user)
        self.assertIn(task['id'],args.args[1]['kind'])
        self.assertEqual(args.args[1]['data']['sha256'],hashlib.sha256(elf()).hexdigest())
        self.assertEqual(self.registry.record.call_args.args[3]['target'],'lib/libxcamera.so')

    def test_native_attachment_only_placeholder_bypasses_model_for_so_and_yaml(self):
        self.inbox.caption_handler=Mock(side_effect=AssertionError('transport text must not reach the model'))
        for name,data,mid in [('libxcamera.so',None,'om_native_file_so_001'),('common_param.yaml',b'log:\n  concise: false\n','om_native_file_yaml_001')]:
            p=self.file(name,data,mid)
            self.inbox.prompt(self.user,self.session,'Please analyze the attached file(s).'+TRAILER+str(p)+')')
        self.inbox.caption_handler.assert_not_called()
        with self.store.connect() as c:
            self.assertEqual(['ASSOCIATED','ASSOCIATED'],[r['state'] for r in c.execute('SELECT state FROM task_attachments ORDER BY created')])

    def test_sensor_real_name_without_lib_prefix_is_preserved_and_associated(self):
        p=self.file('xlidar.so');self.inbox.receive(self.user,self.session,str(p))
        data=self.registry.record.call_args.args[3]
        self.assertEqual('xlidar.so',data['filename']);self.assertEqual('lib/libxlidar.so',data['target'])

    def test_spoofed_other_user_file_is_refused_before_transfer(self):
        p=self.file(owner=self.other)
        with self.assertRaisesRegex(GatewayError,'归属不匹配'):self.inbox.receive(self.user,self.session,str(p))
        self.registry.ssh.upload.assert_not_called()
        self.assertIsNone(Tasks(self.store).current(self.user))

    def test_other_chat_or_mismatched_filename_is_refused(self):
        for field,value in [('chat_id','oc_other'),('msg_type','text')]:
            p=self.file();self.messages[p.parent.name][field]=value
            with self.assertRaises(GatewayError):self.inbox.receive(self.user,self.session,str(p))
        self.registry.ssh.upload.assert_not_called()

    def test_yaml_is_task_scoped_and_never_registered_as_asset(self):
        p=self.file('common_param.yaml',b'abc: true\n')
        answer=self.inbox.receive(self.user,self.session,str(p))
        self.registry.record.assert_not_called()
        self.assertIn('etc/uroad/config/common_param.yaml',Intake(self.store).get(self.user)['yaml_request'])
        self.assertIn('本次执行时将替换',answer)

    def test_unknown_yaml_target_requires_explicit_relative_target(self):
        p=self.file('custom.yaml',b'a: 1\n');self.inbox.receive(self.user,self.session,str(p))
        with self.store.connect() as c:row=dict(c.execute('SELECT * FROM task_attachments').fetchone())
        self.assertEqual(row['state'],'NEEDS_TARGET')
        with self.assertRaises(GatewayError):self.inbox.associate(self.user,row['id'],'yaml','../escape.yaml')
        answer=self.inbox.command(self.user,'YAML目标 '+row['id']+' etc/custom.yaml')
        self.assertIn('已保存',answer)

    def test_same_message_is_idempotent_and_other_owner_cannot_assign_it(self):
        p=self.file();a=self.inbox.receive(self.user,self.session,str(p));b=self.inbox.receive(self.user,self.session,str(p))
        self.assertEqual(a,b);self.assertEqual(self.registry.ssh.upload.call_count,1)
        with self.store.connect() as c:aid=c.execute('SELECT id FROM task_attachments').fetchone()[0]
        with self.assertRaises(GatewayError):self.inbox.associate(self.other,aid,'lidar')

    def test_transfer_failure_keeps_copy_and_explicit_retry_uses_original_task(self):
        p=self.file();self.registry.ssh.upload.side_effect=RuntimeError('network')
        with self.assertRaisesRegex(GatewayError,'重试附件'):self.inbox.receive(self.user,self.session,str(p))
        with self.store.connect() as c:row=dict(c.execute('SELECT * FROM task_attachments').fetchone())
        self.assertEqual(row['state'],'FAILED');self.assertTrue(Path(json.loads(row['data'])['local_path']).is_file())
        from bench_gateway.progress import deliver
        stale=[];deliver(self.store,lambda r:stale.append(r['text']) or True)
        self.assertEqual(stale,[], 'a failed transfer must not later announce that it is still transferring')
        original=row['task_id'];Intake(self.store).update(self.user,{'software_name':'another'},new=True)
        self.registry.ssh.upload.side_effect=None
        self.inbox.retry(self.user,row['id'])
        self.assertNotIn('camera_so',Intake(self.store).get(self.user))
        self.assertIn('camera_so',Tasks(self.store).get(self.user,original)['data'])

    def test_oversize_non_elf_and_non_yaml_never_transfer(self):
        self.cfg['attachment_max_bytes']=20
        p=self.file()
        with self.assertRaises(GatewayError):self.inbox.receive(self.user,self.session,str(p))
        self.cfg['attachment_max_bytes']=1024
        for name,data in [('libxcamera.so',b'wrong library'),('dataset.zip',b'zip')]:
            p=self.file(name,data)
            with self.assertRaises(GatewayError):self.inbox.receive(self.user,self.session,str(p))
        self.registry.ssh.upload.assert_not_called()

    def test_second_same_named_upload_is_distinct(self):
        self.inbox.receive(self.user,self.session,str(self.file()))
        self.inbox.receive(self.user,self.session,str(self.file(data=elf()+b'new',mid='om_test_message_002')))
        with self.store.connect() as c:rows=c.execute('SELECT data FROM task_attachments').fetchall()
        self.assertEqual(len(rows),2)
        self.assertNotEqual(json.loads(rows[0][0])['sha256'],json.loads(rows[1][0])['sha256'])

    def test_caption_assigns_the_new_file_instead_of_another_pending_file(self):
        self.inbox.receive(self.user,self.session,str(self.file('unknown-first.so')))
        second=self.file('unknown-second.so',mid='om_test_caption_002')
        answer=self.inbox.prompt(self.user,self.session,'这是雷达SO'+TRAILER+str(second)+')')
        self.assertIn('雷达 SO',answer)
        with self.store.connect() as c:rows={r['filename']:r['state'] for r in c.execute('SELECT * FROM task_attachments')}
        self.assertEqual(rows['unknown-first.so'],'NEEDS_ROLE');self.assertEqual(rows['unknown-second.so'],'ASSOCIATED')

    def test_ambiguous_role_reply_does_not_choose_latest(self):
        for index in (1,2):self.inbox.receive(self.user,self.session,str(self.file('unknown.so',mid='om_ambiguous_'+str(index))))
        answer=self.inbox.command(self.user,'这是雷达库')
        self.assertIn('2 个附件',answer)
        self.registry.record.assert_not_called()

    def test_conflicting_caption_is_rejected_before_transfer(self):
        p=self.file('libxcamera.so')
        with self.assertRaisesRegex(GatewayError,'角色不一致'):
            self.inbox.prompt(self.user,self.session,'这是雷达SO'+TRAILER+str(p)+')')
        self.registry.ssh.upload.assert_not_called()

    def test_later_yaml_replaces_prior_file_operation_without_losing_other_edits(self):
        first=self.file('common_param.yaml',b'a: 1\n')
        self.registry.ssh.upload.return_value=self.store.remote_root(self.user)+'/tasks/test/one.bin'
        self.inbox.receive(self.user,self.session,str(first))
        original=Intake(self.store).get(self.user)['yaml_request']
        Intake(self.store).update(self.user,{'yaml_request':original+'；另外修改 debug.enabled 为 true'})
        second=self.file('common_param.yaml',b'a: 2\n',mid='om_yaml_replacement_002')
        self.registry.ssh.upload.return_value=self.store.remote_root(self.user)+'/tasks/test/two.bin'
        self.inbox.receive(self.user,self.session,str(second))
        request=Intake(self.store).get(self.user)['yaml_request']
        self.assertNotIn('one.bin',request);self.assertIn('two.bin',request);self.assertIn('debug.enabled',request)
        with self.store.connect() as c:old=dict(c.execute('SELECT * FROM task_attachments WHERE message_id=?',(first.parent.name,)).fetchone())
        self.assertEqual(old['state'],'SUPERSEDED')
        self.assertIn('替代',self.inbox.receive(self.user,self.session,str(first)))

    def test_finished_attachment_suppresses_late_transferring_message(self):
        self.inbox.receive(self.user,self.session,str(self.file()))
        from bench_gateway.progress import deliver
        sent=[];deliver(self.store,lambda row:sent.append(row['text']) or True)
        self.assertEqual(sent,[])

    def test_equivalent_owner_asset_does_not_reupload_same_so(self):
        first=self.file('libxcamera.so');self.inbox.receive(self.user,self.session,str(first))
        content_hash=hashlib.sha256(first.read_bytes()).hexdigest()
        equivalent=self.store.add_asset(self.user,'dependency','历史同内容相机',{'filename':'libxcamera.so','sha256':content_hash,'target':'lib/libxcamera.so'})
        task=Tasks(self.store).current(self.user);Tasks(self.store).update(self.user,task['id'],{'camera_so':equivalent['id']})
        before=Tasks(self.store).current(self.user);calls=self.registry.ssh.upload.call_count
        second=self.file('libxcamera.so',mid='om_duplicate_alias_001');answer=self.inbox.receive(self.user,self.session,str(second))
        self.assertIn('内容与当前材料一致',answer);self.assertEqual(calls,self.registry.ssh.upload.call_count)
        self.assertEqual(before,Tasks(self.store).current(self.user))
        with self.store.connect() as c:self.assertEqual('DUPLICATE',c.execute('SELECT state FROM task_attachments WHERE message_id=?',(second.parent.name,)).fetchone()[0])

    def test_equivalent_asset_hash_cannot_cross_owner_or_dependency_role(self):
        first=self.file('libxcamera.so');self.inbox.receive(self.user,self.session,str(first))
        content_hash=hashlib.sha256(first.read_bytes()).hexdigest()
        other=self.store.add_asset(self.other,'dependency','其他人文件',{'sha256':content_hash,'target':'lib/libxcamera.so'})
        task=Tasks(self.store).current(self.user);Tasks(self.store).update(self.user,task['id'],{'camera_so':other['id']})
        self.assertIsNone(self.inbox.duplicate(self.user,content_hash,'libxcamera.so','camera','lib/libxcamera.so'))
        wrong=self.store.add_asset(self.user,'dependency','其他用途',{'sha256':content_hash,'target':'lib/libxlidar.so'})
        Tasks(self.store).update(self.user,task['id'],{'camera_so':wrong['id']})
        self.assertIsNone(self.inbox.duplicate(self.user,content_hash,'libxcamera.so','camera','lib/libxcamera.so'))

    def seed(self, owner=None, target='lib/libxcamera.so'):
        p=self.file();h=hashlib.sha256(p.read_bytes()).hexdigest()
        self.inbox.receive(self.user,self.session,str(p))
        asset=self.store.add_asset(owner or self.user,'dependency','历史文件',{'filename':'libxcamera.so','sha256':h,'target':target})
        task=Tasks(self.store).current(self.user)
        Tasks(self.store).update(self.user,task['id'],{'camera_so':asset['id']})
        with self.store.connect() as c:c.execute('DELETE FROM task_attachments')
        return h,asset

    def test_asset_only_duplicate_after_history_cleanup(self):
        h,asset=self.seed();before=Tasks(self.store).current(self.user)
        calls=self.registry.ssh.upload.call_count;registrations=self.registry.record.call_count
        p=self.file(mid='om_asset_only_duplicate')
        self.assertIn('内容与当前材料一致',self.inbox.receive(self.user,self.session,str(p)))
        self.assertEqual(before,Tasks(self.store).current(self.user))
        self.assertEqual(calls,self.registry.ssh.upload.call_count)
        self.assertEqual(registrations,self.registry.record.call_count)
        with self.store.connect() as c:
            row=c.execute('SELECT * FROM task_attachments').fetchone()
            self.assertEqual('DUPLICATE',row['state'])
            import json
            self.assertEqual(asset['id'],json.loads(row['data'])['asset_id'])

    def test_asset_only_cross_owner_is_rejected(self):
        h,_=self.seed(owner=self.other)
        self.assertIsNone(self.inbox.duplicate(self.user,h,'libxcamera.so','camera','lib/libxcamera.so'))

    def test_asset_only_wrong_role_is_rejected(self):
        h,_=self.seed(target='lib/libxlidar.so')
        self.assertIsNone(self.inbox.duplicate(self.user,h,'libxcamera.so','camera','lib/libxcamera.so'))

    def test_asset_only_different_content_is_not_duplicate(self):
        self.seed()
        self.assertIsNone(self.inbox.duplicate(self.user,'0'*64,'libxcamera.so','camera','lib/libxcamera.so'))


if __name__=='__main__':unittest.main()
