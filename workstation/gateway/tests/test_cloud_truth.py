import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from bench_gateway.cloud_truth import POLICY,source_spec,binding_from_dataset,validate_binding,runtime_root
from bench_gateway import cloud_provider as cloud
from bench_gateway.core import GatewayError
import test_cloud_provider as fixtures

PROFILE={'gt_policy':POLICY,'gt_mount_roots':['/mnt/volumes/team-data','/mnt/volumes/other']}
DATA={'remote_path':'/workstation/selected','manifest':'/workstation/prepared.json','sha256':'a'*64}

class TruthTests(unittest.TestCase):
    def test_dataset_and_gt_are_bound_per_task_without_fixed_dataset(self):
        for root in ('/lpai/volumes/team-data/PCM','/mnt/volumes/team-data/another'):
            d={**DATA,'evaluation_source':source_spec(root)};b=binding_from_dataset(d,PROFILE)
            self.assertEqual(b['dataset_root'],b['gt_root']);self.assertIn(root.split('/')[-1],b['gt_root'])
        self.assertNotEqual(binding_from_dataset(d,PROFILE)['source_identity'],binding_from_dataset({**d,'sha256':'b'*64},PROFILE)['source_identity'])
    def test_no_implicit_v151_or_cross_mount_fallback(self):
        for d in (DATA,{**DATA,'evaluation_source':source_spec('/mnt/volumes/unapproved/PCM')}):
            with self.assertRaises(ValueError):binding_from_dataset(d,PROFILE)
        for path in ('/mnt/volumes/team-data/../other','/tmp/data','/mnt/volumes/team-data/data\ncmd'):
            with self.assertRaises(ValueError):source_spec(path)
    def test_manual_workstation_data_can_explicitly_bind_a_different_gt_root(self):
        b=binding_from_dataset({**DATA,'evaluation_source':source_spec('/lpai/volumes/team-data/raw','/mnt/volumes/other/labels')},PROFILE)
        self.assertEqual('/mnt/volumes/other/labels',b['gt_root'])
    def test_legacy_root_cannot_be_overridden_by_new_request(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Path(d).resolve(),runtime_root({}, {},d))
            with self.assertRaises(ValueError):runtime_root({'gt_binding':{}},{},d)
            with self.assertRaises(ValueError):runtime_root({},PROFILE,d)

class CloudTruthProviderTests(unittest.TestCase):
    setUp=fixtures.CloudProviderTests.setUp
    policy=fixtures.CloudProviderTests.policy
    def test_cutover_rejects_old_generic_gt_before_any_source_read_or_upload(self):
        self.policy();self.cfg['evaluation_gt_binding_required_after']=self.job['created']
        with patch.object(cloud.workstation,'resolve_task') as inspect,patch.object(cloud,'transport') as send:
            with self.assertRaisesRegex(GatewayError,'不能默认使用V151'):cloud.evaluate(self.cfg,self.store,self.job)
            inspect.assert_not_called();send.assert_not_called()

    def test_new_evaluation_freezes_explicit_gt_for_old_predictions_without_changing_old_assets(self):
        data={**DATA,'evaluation_source':source_spec('/mnt/volumes/team-data/labels')}
        binding=binding_from_dataset(data,PROFILE)
        job={'created':1,'payload':{'evaluation_gt_binding':binding}}
        context={'dataset':copy.deepcopy(DATA)}
        self.assertEqual(binding,cloud.frozen_gt_binding({},context,job,PROFILE))
        self.assertEqual(DATA,context['dataset'])
        context['dataset']['sha256']='b'*64
        with self.assertRaisesRegex(GatewayError,'原回灌数据源不一致'):cloud.frozen_gt_binding({},context,job,PROFILE)

class CloudPredictionSourceTests(unittest.TestCase):
    setUp=fixtures.CloudProviderTests.setUp
    policy=fixtures.CloudProviderTests.policy
    def test_retained_lineage_checks_without_reading_workstation_labels(self):
        import test_workstation_evaluation as w
        from bench_gateway.cloud_prediction_source import DISCOVERY
        f=w.RemoteSourceGuardTests();f.setUp();self.addCleanup(f.doCleanups)
        (f.product/'scene-a').mkdir()
        package=f.root/'runtime';(package/'etc').mkdir(parents=True)
        (package/'etc/evaluation_param.yaml').write_text('eval_vision: ON')
        f.worker.verify_pin=lambda pin:(package,None)
        f.worker.task_switches=lambda text:{'vision':True,'trajectory':False,'road':False}
        f.worker.discover=lambda *args:(_ for _ in ()).throw(AssertionError('must not load local GT'))
        from bench_gateway import workstation_evaluation as e
        needle="    discovery=worker.discover({'prediction_path':str(product),'scenes':None,'required_tasks':request.get('required_tasks')},pin)"
        namespace={};exec(e.REMOTE_GUARD.replace(needle,DISCOVERY),namespace)
        value=namespace['inspect_source'](f.request,copy.deepcopy(f.pin),f.worker)
        self.assertFalse(value['discovery']['workstation_gt_read'])
        self.assertEqual(['scene-a'],value['scene_names'])
        with self.assertRaises(ValueError):namespace['inspect_source']({**f.request,'expected_run':'other'},copy.deepcopy(f.pin),f.worker)
    def test_new_profile_requires_explicit_dataset_before_upload(self):
        p=self.policy();p.update(gt_policy=POLICY,gt_mount_roots=PROFILE['gt_mount_roots']);p.pop('gt_root')
        import hashlib
        p['profile_sha256']=hashlib.sha256(cloud.canonical({k:v for k,v in p.items() if k!='profile_sha256'})).hexdigest()
        with patch.object(cloud,'transport') as send,patch.object(cloud.workstation,'resolve_task') as inspect:
            with self.assertRaisesRegex(GatewayError,'明确关联云端GT'):cloud.evaluate(self.cfg,self.store,self.job)
            inspect.assert_not_called();send.assert_not_called()

    def test_explicit_gt_is_frozen_into_signed_request_and_no_local_truth_read(self):
        import hashlib
        from bench_gateway.core import EvaluationOutcomeUnknown
        p=self.policy();p.update(gt_policy=POLICY,gt_mount_roots=PROFILE['gt_mount_roots']);p.pop('gt_root')
        p['profile_sha256']=hashlib.sha256(cloud.canonical({k:v for k,v in p.items() if k!='profile_sha256'})).hexdigest()
        context=cloud.workstation._context(self.cfg,self.store,self.job)
        context['dataset']={**context['dataset'],'evaluation_source':source_spec('/mnt/volumes/team-data/another')}
        source=copy.deepcopy(self.source)
        source['discovery']['scene_inputs']=[{'scene':'scene-a'}]
        with patch.object(cloud.workstation,'_context',return_value=context),patch('bench_gateway.cloud_prediction_source.inspect',return_value=source),patch.object(cloud.workstation,'resolve_task',side_effect=AssertionError('must not read local GT')),patch.object(cloud,'ready'),patch.object(cloud,'export_predictions'),patch.object(cloud.subprocess,'run') as sign,patch.object(cloud,'transport',side_effect=GatewayError('unknown upload')):
            sign.return_value.returncode=0;self.cfg.update(cloud_node_binary='node')
            with self.assertRaises(EvaluationOutcomeUnknown):cloud.evaluate(self.cfg,self.store,self.job)
        _,local,_,_=cloud.paths(self.cfg,self.store,self.job,p)
        req=json.loads((local/'request.json').read_text())
        self.assertEqual('/mnt/volumes/team-data/another',req['gt_binding']['gt_root'])
        self.assertNotIn('truth_frames',req)
        self.assertNotIn('v151',json.dumps(req).lower())

    def test_cloud_report_requires_the_same_dataset_gt_binding(self):
        import hashlib
        receipt=fixtures.CloudProviderTests.receipt(self)
        p=self.cfg['cloud_evaluation_profiles'][0]['profile'];p.update(gt_policy=POLICY,gt_mount_roots=PROFILE['gt_mount_roots']);p.pop('gt_root')
        p['profile_sha256']=hashlib.sha256(cloud.canonical({k:v for k,v in p.items() if k!='profile_sha256'})).hexdigest()
        context=cloud.workstation._context(self.cfg,self.store,self.job)
        context['dataset']={**context['dataset'],'evaluation_source':source_spec('/mnt/volumes/team-data/another')}
        binding=binding_from_dataset(context['dataset'],p)
        receipt.update(profile_sha256=p['profile_sha256'],gt_binding=binding)
        receipt['native_receipt'].update(gt_binding=binding,gt_frames={'scene-a':2})
        with patch.object(cloud.workstation,'_context',return_value=context):
            self.assertEqual('COMPLETE',cloud.validate_receipt(self.cfg,self.store,self.job,receipt,source=self.source)['evaluation_state'])
            receipt['native_receipt']['gt_binding']={**binding,'gt_root':'/mnt/volumes/team-data/wrong'}
            with self.assertRaisesRegex(GatewayError,'GT与本次冻结数据集不一致'):cloud.validate_receipt(self.cfg,self.store,self.job,receipt,source=self.source)
