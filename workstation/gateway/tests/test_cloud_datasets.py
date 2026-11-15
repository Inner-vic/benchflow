import copy,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from bench_gateway import cloud_dataset_worker as worker
from bench_gateway.cloud_dataset_paths import parse_root

SCENE='2026-05-21_17-02-26_HLX33B166R1997378'
SECOND='2026-05-21_17-03-00_HLX33B166R1997378'
SOURCE=parse_root('/lpai/volumes/team/custom',['team'])
LOCATION={'bucket':'private','prefix':'dataset/'}

class S3:
    def __init__(self):
        self.objects={};self.reads=[]
        for scene in (SCENE,SECOND):
            for name in ('calib_cam_params.json','calib_lidar_params.json','images/f_ca_120/tag2_2_7000_1779354146060_0.jpg',
                'images/f_ca_120/tag2_2_7001_1779354146093_0.jpg','VAL_Lidar_0_PointCloud/tag2_1779354146054701_2520.zst',
                'eps_mode_sts/1.bin','ins_info/1.bin','odom_data_for_pcm/1.bin'):
                self.objects['dataset/custom/'+scene+'/original/'+name]=b'replay'
            self.objects['dataset/custom/'+scene+'/dataset/frame/dense_labeled.npy']=b'GT must stay in cloud'
        self.objects['dataset/custom/original_data.tar.gz']=b'whole archive must not be downloaded'
    def list_objects_v2(self,**args):
        contents=[];folders=set();prefix=args['Prefix']
        for key,raw in sorted(self.objects.items()):
            if not key.startswith(prefix):continue
            rest=key[len(prefix):]
            if args.get('Delimiter')=='/' and '/' in rest:folders.add(prefix+rest.split('/')[0]+'/')
            else:contents.append({'Key':key,'Size':len(raw),'ETag':'"etag"'})
        return {'Contents':contents,'CommonPrefixes':[{'Prefix':x} for x in sorted(folders)],'IsTruncated':False}
    def get_object(self,**args):
        self.reads.append(args);return {'Body':io.BytesIO(self.objects[args['Key']])}

class CloudDatasetTests(unittest.TestCase):
    def setUp(self):self.client=S3()
    def plan(self):return worker.discover(self.client,LOCATION,SOURCE,[2])
    def test_only_selected_original_inputs_are_planned_not_gt_or_outer_archive(self):
        p=self.plan();self.assertEqual([SCENE,SECOND],p['scene_names']);self.assertEqual([2],p['source_indices'])
        self.assertEqual(1,len(p['scenes']));self.assertEqual(8,len(p['files']))
        self.assertTrue(all('/'+SECOND+'/original/' in f['key'] for f in p['files']))
        self.assertFalse(any('/dataset/' in f['key'] or '.tar.gz' in f['key'] for f in p['files']))
    def test_changed_plan_gt_injection_and_size_limit_are_rejected(self):
        p=self.plan()
        with self.assertRaises(ValueError):worker.validate_plan(p,LOCATION,SOURCE,1)
        p['files'][0]['relative']='../outside'
        with self.assertRaises(ValueError):worker.validate_plan(p,LOCATION,SOURCE,1000)
        p=self.plan();p['files'][0]['key']='dataset/custom/'+SECOND+'/dataset/secret'
        p['plan_id']=worker.digest({k:v for k,v in p.items() if k!='plan_id'})
        with self.assertRaises(ValueError):worker.validate_plan(p,LOCATION,SOURCE,1000)
    def test_date_versions_with_same_scene_are_not_guessed(self):
        raw=next(iter(self.client.objects.values()));self.client.objects['dataset/custom/2026-05-22/'+SCENE+'/original/extra']=raw
        with self.assertRaisesRegex(ValueError,'multiple dates'):worker.discover(self.client,LOCATION,SOURCE)
    def test_missing_signals_stop_before_any_download(self):
        for key in list(self.client.objects):
            if '/ins_info/' in key:del self.client.objects[key]
        with self.assertRaisesRegex(ValueError,'navigation'):self.plan()
        self.assertEqual([],self.client.reads)
    def test_navigation_or_camera_ambiguity_is_rejected(self):
        self.client.objects['dataset/custom/'+SECOND+'/original/f_ca_120/1779354146060000000.jpg']=b'other camera'
        with self.assertRaisesRegex(ValueError,'ambiguous'):self.plan()
    def test_transfer_uses_if_match_and_preserves_owner_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.plan();root=Path(d).resolve();seen={}
            def prepare(directory,plan):
                manifest=directory/'manifest.json';manifest.write_text('{}');seen['directory']=directory
                return {'manifest':str(manifest),'sha256':worker.hashlib.sha256(manifest.read_bytes()).hexdigest()}
            with patch.object(worker,'prepare_manifest',side_effect=prepare),patch.object(worker.shutil,'disk_usage') as free:
                free.return_value.free=100*1024**3
                result=worker.download(self.client,LOCATION,SOURCE,p,root,max_bytes=1024)
                count=len(self.client.reads);again=worker.download(self.client,LOCATION,SOURCE,p,root,max_bytes=1024)
            self.assertFalse(result['gt_downloaded']);self.assertTrue(again['reused']);self.assertEqual(count,len(self.client.reads))
            self.assertTrue(all(x['IfMatch']=='"etag"' for x in self.client.reads))
            self.assertTrue(root in Path(result['data']['manifest']).parents)
            self.assertFalse(any('/dataset/' in x['Key'] for x in self.client.reads))
    def test_unregistered_existing_input_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();p=self.plan();directory=root/'datasets'/('cloud-'+p['plan_id'][:24]);target=directory/p['files'][0]['relative'];target.parent.mkdir(parents=True);target.write_bytes(b'foreign')
            with patch.object(worker.shutil,'disk_usage') as free:
                free.return_value.free=100*1024**3
                with self.assertRaisesRegex(ValueError,'already exists'):worker.download(self.client,LOCATION,SOURCE,p,root,max_bytes=1024,workers=1)
            self.assertEqual(b'foreign',target.read_bytes())
    def test_camera_timestamp_units_and_duplicates_are_unambiguous(self):
        stamp=1779354146060000000
        for name in ('tag2_2_7572_1779354146060_0.jpg','camera_1779354146060000.jpeg','camera_1779354146060000000.jpg'):
            self.assertEqual(stamp,worker.timestamp(name))
        for name in ('frame.jpg','camera_1779354146060_1779354146061.jpg'):
            with self.assertRaises(ValueError):worker.timestamp(name)
    def test_cloud_paths_are_canonical_and_never_accept_parent_escapes(self):
        for value in ('/lpai/volumes/team/custom/','/mnt/volumes/team/custom//x','/mnt/volumes/team/../other','/mnt/volumes/unknown/custom'):
            with self.assertRaises(ValueError):parse_root(value,['team'])

if __name__=='__main__':unittest.main()
