import unittest

from bench_gateway.core import GatewayError
from bench_gateway.intake import Intake
import test_yaml_preparation as fixtures


class BusinessDraftGuardTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.YamlReplayAdmissionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_pending_materials_cannot_fall_back_to_a_legacy_baseline(self):
        c=self.case
        for field,value in [('package','new-package'),('camera_so','new-camera'),
                            ('lidar_so','new-lidar'),('m100_so','new-m100'),
                            ('dataset','new-data'),('scene_scope','2,3')]:
            with self.subTest(field=field):
                Intake(c.store).update(c.user,{field:value},new=True)
                with self.assertRaisesRegex(GatewayError,'检查材料'):
                    c.service.handle(c.session,'执行旧基线',c.intent)
        self.assertEqual(c.store.jobs(c.user),[])

    def test_followup_message_cannot_drop_the_preceding_material_change(self):
        c=self.case
        changed={**c.intent,'inputs':{'camera_so':'/workstation/new-camera.so'}}
        with self.assertRaisesRegex(GatewayError,'新增材料已保存'):
            c.service.handle(c.session,'回灌并换相机',changed)
        with self.assertRaisesRegex(GatewayError,'检查材料'):
            c.service.handle(c.session,'那就执行回灌',c.intent)
        self.assertEqual(c.store.jobs(c.user),[])

    def test_legacy_path_without_pending_materials_is_retained(self):
        c=self.case
        Intake(c.store).update(c.user,{'goal':'replay'},new=True)
        self.assertIn('队列',c.service.handle(c.session,'执行旧已核验组合',c.intent))
        self.assertEqual(len(c.store.jobs(c.user)),1)
