import unittest
from bench_gateway.cloud_dataset_paths import parse_root,parse_scene,select_scenes

class CloudDatasetPathsTests(unittest.TestCase):
    def test_mounted_aliases_map_to_the_same_root_without_pinning_a_dataset(self):
        allowed=['jfs-dip-vfsc-dataset-ga']
        a=parse_root('/lpai/volumes/jfs-dip-vfsc-dataset-ga/tvg/Baidu_Data/PCM_eval_dataset',allowed)
        b=parse_root('/mnt/volumes/jfs-dip-vfsc-dataset-ga/tvg/Baidu_Data/PCM_eval_dataset',allowed)
        self.assertEqual(a['evaluation_path'],b['evaluation_path'])
        self.assertEqual('tvg/Baidu_Data/PCM_eval_dataset',a['relative_root'])
        c=parse_root('/lpai/volumes/jfs-dip-vfsc-dataset-ga/another/dataset',allowed)
        self.assertEqual('another/dataset',c['relative_root'])
    def test_reject_untrusted_or_ambiguous_paths(self):
        for p in ('/lpai/volumes/unknown/data','/lpai/volumes/good/../other',
                  '/lpai/volumes/good','//lpai/volumes/good/a','/tmp/good/a',
                  '/lpai/volumes/good/a\nb',' /lpai/volumes/good/a'):
            with self.subTest(path=p),self.assertRaises(ValueError):parse_root(p,['good'])
    def test_scene_selection_preserves_unknown_directories(self):
        values,unknown=select_scenes(['tools','2026-05-21_17-03-00_HLX33B166R1997378',
                                       '2026-02-30_10-00-00_VIN'])
        self.assertEqual('HLX33B166R1997378',values[0]['vin'])
        self.assertEqual('2026-05-21T17:03:00',values[0]['recorded_time'])
        self.assertEqual(['tools','2026-02-30_10-00-00_VIN'],unknown)
    def test_duplicate_scene_is_rejected(self):
        with self.assertRaises(ValueError):select_scenes(['2026-05-21_17-03-00_VIN']*2)
