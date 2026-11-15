from pathlib import Path
import tempfile
import unittest
from bench_gateway.service import Service
from bench_gateway.intake import Intake


class MaterialAckTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.user='ou_ack';self.service=Service({'state_root':self.temp.name,'m100_build_enabled':True})
        self.service.store.allow(self.user)
        self.values={'package':'APP-new','camera_so':'camera-new','lidar_so':'lidar-new',
                     'dataset':'共享18场景','scene_scope':'全部'}

    def test_complete_new_release_does_not_request_already_supplied_materials_again(self):
        Intake(self.service.store).update(self.user,self.values,new=True)
        for change in ({'new_release':True},{'new_release':True,'next_request':True}):
            text=self.service.material_ack(self.user,self.values,change)
            self.assertNotIn('重新提供',text);self.assertNotIn('还需要',text)
            self.assertIn('检查材料',text)

    def test_only_missing_role_is_requested(self):
        values={k:v for k,v in self.values.items() if k!='camera_so'}
        Intake(self.service.store).update(self.user,values,new=True)
        text=self.service.material_ack(self.user,values,{'new_release':True})
        self.assertIn('还需要：相机 SO',text);self.assertNotIn('还需要：雷达',text)
