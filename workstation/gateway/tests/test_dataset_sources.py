import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bench_gateway.dataset_sources import CATALOG_CODE,VALIDATOR_CODE
from bench_gateway.remote_agent import dataset_input
from bench_gateway.service import Service,public_asset
from bench_gateway.core import GatewayError


class LocalSSH:
    def python(self,code,args=(),timeout=120):
        r=subprocess.run([sys.executable,'-c',code,*args],capture_output=True,text=True,encoding='utf-8',
                         env={**os.environ,'PYTHONUTF8':'1'},timeout=timeout)
        if r.returncode:raise ValueError(r.stderr)
        return r.stdout


class DatasetSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.data=self.root/'source';self.data.mkdir();self.scene=self.data/'scene1';self.scene.mkdir()
        self.prepared=self.root/'prepared';self.prepared.mkdir();self.manifest=self.prepared/'manifest.json'
        self.manifest.write_text(json.dumps({'prepared_scenes':[{'scene':'scene1','scene_root':str(self.scene),'camera_frames':1}]}),encoding='utf-8')
        self.catalog=self.root/'catalog.json';self.ssh=LocalSSH()
        self.cfg={'project_root':str(self.root),'state_root':str(self.root/'state'),'remote_users_root':str(self.root/'users'),
                  'dataset_catalog':str(self.catalog),'mode':'simulate','live_enabled':False}
        self.service=Service(self.cfg);self.service.datasets.ssh=self.ssh
        self.service.store.allow('ou_alice');self.service.store.allow('ou_bob')

    def tearDown(self):self.tmp.cleanup()

    def call(self,owner,op,**values):
        req={'catalog':str(self.catalog),'owner':owner,'op':op,'users_root':str(self.root/'users'),**values}
        encoded=base64.b64encode(json.dumps(req).encode()).decode()
        return json.loads(self.ssh.python(CATALOG_CODE,(encoded,)))

    def register(self,owner='shared',name='18场景'):
        return self.call(owner,'register',name=name,data={'remote_path':str(self.data),'manifest':str(self.manifest),'camera_roots':[]})

    def test_shared_is_available_without_personal_dataset_registration(self):
        a=self.register()
        for user in ('ou_alice','ou_bob'):
            self.assertEqual(self.service.asset(user,'dataset','共享18场景')['id'],a['id'])
            self.assertEqual(self.service.store.list_assets(user),[])
        self.assertEqual(public_asset(a)['name'],'共享:18场景')

    def test_private_external_source_is_only_visible_to_owner(self):
        a=self.register('ou_alice','private')
        self.assertEqual(self.service.asset('ou_alice','dataset','private')['id'],a['id'])
        self.assertEqual(self.call('ou_bob','list'),[])
        with self.assertRaises(GatewayError):self.service.asset('ou_bob','dataset','private')

    def test_repeat_registration_reuses_version(self):
        self.assertEqual(self.register()['id'],self.register()['id'])

    def test_explicit_cloud_gt_association_is_versioned_and_frozen(self):
        from bench_gateway.cloud_truth import source_spec
        ds=self.service.datasets
        a=ds.register('shared','custom',str(self.data),str(self.manifest),
                      cloud_dataset_root='/lpai/volumes/team/PCM')
        self.assertEqual(source_spec('/lpai/volumes/team/PCM'),a['data']['evaluation_source'])
        self.assertEqual(a,ds.validate('ou_alice',a))
        b=ds.register('shared','custom',str(self.data),str(self.manifest),
                      cloud_dataset_root='/lpai/volumes/team/new',cloud_gt_root='/lpai/volumes/team/labels')
        self.assertEqual(a['version']+1,b['version'])
        self.assertEqual(a,ds.validate('ou_alice',a))
        self.assertEqual('/mnt/volumes/team/labels',b['data']['evaluation_source']['gt_root'])
        with self.assertRaises(GatewayError):ds.register('shared','bad',str(self.data),str(self.manifest),cloud_gt_root='/lpai/volumes/team/labels')

    def test_explicit_dataset_policy_does_not_inject_shared_source_into_new_user(self):
        from bench_gateway.intake import Intake
        configured=Service({**self.cfg,'require_explicit_dataset':True,'default_dataset_path':'/mnt/old/v151'})
        intake=Intake(configured.store);intake.update('ou_alice',{'package':'A'},new=True)
        self.assertEqual('',intake.get('ou_alice')['dataset'])

    def test_evaluation_rejects_unassociated_source_before_queue_but_replay_still_works(self):
        import hashlib
        from bench_gateway.cloud_provider import canonical
        d=self.register();s=self.service.store;p=s.add_asset('ou_alice','package','A',{})
        s.add_baseline('ou_alice','adapter',{'gate_verified':True,'asset_ids':{'package':p['id'],'dataset':d['id'],'dependencies':[]}})
        profile={'gt_policy':'task-dataset-v1','gt_mount_roots':['/mnt/volumes/team']}
        profile['profile_sha256']=hashlib.sha256(canonical(profile)).hexdigest()
        self.service.cfg.update(evaluation_gt_binding_required_after=0,
            cloud_evaluation_profiles=[{'effective_after':0,'profile':profile}])
        intent={'action':'replay','package':'A','dataset':'共享18场景','scenes':[1],'baseline':'adapter'}
        with self.assertRaisesRegex(GatewayError,'明确关联云端GT'):
            self.service.handle('feishu:chat:ou_alice','确认本次评测',{**intent,'evaluate_after':True})
        self.assertEqual([],s.jobs('ou_alice'))
        self.service.handle('feishu:chat:ou_alice','仅回灌',intent)
        self.assertEqual('QUEUED',s.jobs('ou_alice')[0]['state'])

    def test_append_evaluation_can_freeze_new_gt_association_without_changing_old_replay(self):
        import hashlib,copy
        from bench_gateway.cloud_provider import canonical
        d=self.register();s=self.service.store;p=s.add_asset('ou_alice','package','A',{})
        baseline=s.add_baseline('ou_alice','adapter',{'gate_verified':True,'evaluator':{'argv':['fixed-evaluator']},'asset_ids':{'package':p['id'],'dataset':d['id'],'dependencies':[]}})
        old,_=s.submit('ou_alice','feishu:chat:ou_alice','original',{'action':'replay','assets':{'package':p,'dataset':d,'dependencies':[]},'baseline':baseline,'scenes':[1],'evaluate_after':False})
        s.finish(old,'SUCCEEDED',{'task':'/tasks/retained'});old=s.job('ou_alice',old['id']);frozen=copy.deepcopy(old)
        associated=self.service.datasets.register('shared',d['name'],str(self.data),str(self.manifest),cloud_dataset_root='/mnt/volumes/team/labels')
        profile={'gt_policy':'task-dataset-v1','gt_mount_roots':['/mnt/volumes/team']}
        profile['profile_sha256']=hashlib.sha256(canonical(profile)).hexdigest()
        self.service.cfg.update(evaluation_gt_binding_required_after=0,cloud_evaluation_profiles=[{'effective_after':0,'profile':profile}])
        self.service.handle('feishu:chat:ou_alice','评测已有结果',{'action':'evaluate','task_id':old['id']})
        new=next(j for j in s.jobs('ou_alice') if j['payload']['action']=='evaluate')
        self.assertEqual('/mnt/volumes/team/labels',new['payload']['evaluation_gt_binding']['gt_root'])
        self.assertEqual(d,new['payload']['source_assets']['dataset'])
        self.assertEqual(frozen,s.job('ou_alice',old['id']))

    def camera_source(self):
        camera=self.data/'camera';camera.mkdir();(camera/'frame.jpg').write_bytes(b'frame')
        data={'remote_path':str(self.data),'manifest':str(self.manifest),'camera_roots':[str(camera)]}
        asset=self.call('shared','register',name='camera-data',data=data)
        return camera,asset

    def test_registered_reuse_and_remote_gate_do_not_enumerate_frames(self):
        camera,a=self.camera_source();namespace={};exec(VALIDATOR_CODE,namespace)
        req={'owner':'ou_alice','dataset_catalog':str(self.catalog),'dataset_validator':VALIDATOR_CODE}
        with patch.object(Path,'iterdir',side_effect=AssertionError('routine reuse must not enumerate frames')):
            self.assertEqual(a['data'],namespace['check_registered_source'](a['data']))
            self.assertEqual(self.manifest,dataset_input(req,self.root,a)[0])
        self.assertEqual(a,self.call('ou_alice','validate',id=a['id']))

    def test_registration_is_scene_level_and_full_frame_diagnostic_is_explicit(self):
        camera,a=self.camera_source();(camera/'frame.jpg').unlink()
        self.assertEqual(a,self.call('ou_alice','validate',id=a['id']))
        self.assertEqual(a,self.call('shared','register',name='camera-data',data=a['data']))
        namespace={};exec(VALIDATOR_CODE,namespace)
        with self.assertRaises(ValueError):namespace['check_source'](a['data'],inspect_frames=True)

    def test_first_registration_never_enumerates_frames(self):
        camera=self.data/'empty-camera';camera.mkdir()
        request={'catalog':str(self.catalog),'owner':'shared','op':'register','name':'scene-only',
                 'users_root':str(self.root/'users'),'data':{'remote_path':str(self.data),
                 'manifest':str(self.manifest),'camera_roots':[str(camera)]}}
        guard="import pathlib\ndef forbid(self):raise AssertionError('registration must not enumerate frames')\npathlib.Path.iterdir=forbid\n"
        encoded=base64.b64encode(json.dumps(request).encode()).decode()
        a=json.loads(self.ssh.python(guard+CATALOG_CODE,(encoded,)))
        self.assertEqual(1,a['data']['scene_count']);self.assertEqual([str(camera)],a['data']['camera_roots'])
        namespace={};exec(VALIDATOR_CODE,namespace)
        with patch.object(Path,'iterdir',side_effect=AssertionError('default check must not enumerate')):
            self.assertEqual(a['data'],namespace['check_source'](a['data']))

    def test_registered_camera_directory_must_still_exist(self):
        camera,a=self.camera_source();(camera/'frame.jpg').unlink();camera.rmdir()
        with self.assertRaises(ValueError):self.call('ou_alice','validate',id=a['id'])

    def test_remote_gate_rejects_changed_manifest_even_without_frame_scan(self):
        _,a=self.camera_source();self.manifest.write_text(self.manifest.read_text()+' ')
        req={'owner':'ou_alice','dataset_catalog':str(self.catalog),'dataset_validator':VALIDATOR_CODE}
        with self.assertRaises(ValueError):dataset_input(req,self.root,a)

    def test_source_cannot_point_at_other_gateway_user(self):
        other=self.root/'users/bob';other.mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.call('shared','register',name='bad',data={'remote_path':str(other),'manifest':str(self.manifest)})

    def test_manifest_path_escape_is_rejected(self):
        self.manifest.write_text(json.dumps({'prepared_scenes':[{'scene':'bad','raw_root':str(self.prepared)}]}),encoding='utf-8')
        with self.assertRaises(ValueError):self.register()

    def test_changed_manifest_rejects_previously_registered_version(self):
        a=self.register();self.manifest.write_text(self.manifest.read_text(encoding='utf-8')+' ',encoding='utf-8')
        with self.assertRaises(ValueError):self.call('ou_alice','validate',id=a['id'])

    def test_external_replay_config_requires_explicit_operator_root(self):
        configs=self.root/'configs';configs.mkdir();config=configs/'scene.json';config.write_text('{}')
        value=json.loads(self.manifest.read_text());value['prepared_scenes'][0]['replay_config']=str(config)
        self.manifest.write_text(json.dumps(value))
        with self.assertRaises(ValueError):self.register()
        a=self.call('shared','register',name='18场景',data={'remote_path':str(self.data),
                    'manifest':str(self.manifest),'camera_roots':[],'replay_config_roots':[str(configs)]})
        self.assertEqual(self.call('ou_alice','validate',id=a['id']),a)

    def test_remote_gate_rejects_forged_shared_reference(self):
        a=self.register();req={'owner':'ou_alice','dataset_catalog':str(self.catalog),'dataset_validator':VALIDATOR_CODE}
        manifest,roots=dataset_input(req,self.root,a)
        self.assertEqual(manifest,self.manifest)
        self.assertIn(self.data,roots)
        a['data']['remote_path']=str(self.prepared)
        with self.assertRaises(ValueError):dataset_input(req,self.root,a)

    def test_shared_dataset_does_not_allow_foreign_software(self):
        self.register();self.service.store.add_asset('ou_bob','package','B',{})
        with self.assertRaises(GatewayError):self.service.asset('ou_alice','package','B')

    def test_replay_snapshots_shared_reference(self):
        d=self.register();s=self.service.store;p=s.add_asset('ou_alice','package','A',{})
        s.add_baseline('ou_alice','adapter',{'gate_verified':True,'asset_ids':{'package':p['id'],'dataset':d['id'],'dependencies':[]}})
        self.service.handle('feishu:chat:ou_alice','用共享18场景回灌第一个场景',
                            {'action':'replay','package':'A','dataset':'共享18场景','scenes':[1],'baseline':'adapter'})
        job=s.jobs('ou_alice')[0]
        self.assertEqual(job['payload']['assets']['dataset']['owner'],'shared')
        self.assertEqual(job['payload']['assets']['dataset']['data']['sha256'],d['data']['sha256'])
        self.assertEqual(job['state'],'QUEUED')


if __name__=='__main__':unittest.main()
