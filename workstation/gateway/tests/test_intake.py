import unittest
import test_gateway as fixtures
from bench_gateway.core import GatewayError
from bench_gateway.intake import Intake,GUIDE,CLEAR_YAML,FIELDS
from bench_gateway.parser import quick
from bench_gateway.service import validate_intent


class IntakeTests(unittest.TestCase):
    setUp=fixtures.GatewayTests.setUp
    tearDown=fixtures.GatewayTests.tearDown

    def test_materials_persist_across_messages_without_queue_submission(self):
        s=self.service
        s.handle(fixtures.SESSION,'新建回灌任务',{'action':'new_task'})
        s.handle(fixtures.SESSION,'包链接',{'action':'prepare','inputs':{'package':'https://example.invalid/A.app.tar.gz'}})
        result=s.handle(fixtures.SESSION,'相机文件',{'action':'prepare','inputs':{'camera_so':'camera-A'}})
        draft=Intake(self.s).get(fixtures.A)
        self.assertEqual(draft['package'],'https://example.invalid/A.app.tar.gz')
        self.assertEqual(draft['camera_so'],'camera-A')
        self.assertIn('相机 SO',result);self.assertLess(len(result),300)
        self.assertEqual(self.s.jobs(fixtures.A),[])

    def test_drafts_are_private_and_not_reusable_config_assets(self):
        Intake(self.s).update(fixtures.A,{'yaml_request':'common_param.yaml 的 eval_dump.enable 改成 ON'})
        self.assertIsNone(Intake(self.s).get(fixtures.B))
        self.assertNotIn('eval_dump',str(self.service.context(fixtures.B)))
        self.assertFalse(any(a['kind']=='configuration' for a in self.s.list_assets(fixtures.A)))

    def test_new_task_does_not_inherit_previous_materials(self):
        intake=Intake(self.s);intake.update(fixtures.A,{'package':'old','yaml_request':'old changes','m100_so':'old.so','lpai_target':'team'})
        intake.update(fixtures.A,new=True)
        self.assertNotIn('package',intake.get(fixtures.A));self.assertNotIn('yaml_request',intake.get(fixtures.A))
        self.assertNotIn('m100_so',intake.get(fixtures.A));self.assertEqual(intake.get(fixtures.A)['lpai_target'],'none')

    def test_explicit_yaml_clear_preserves_other_materials_and_other_user(self):
        intake=Intake(self.s)
        intake.update(fixtures.A,{'package':'A','yaml_request':'original.yaml fields.enabled=true'})
        intake.update(fixtures.B,{'yaml_request':'bob.yaml field=false'})
        result=self.service.handle(fixtures.SESSION,'取消 YAML 修改',quick('取消 YAML 修改'))
        self.assertNotIn('yaml_request',intake.get(fixtures.A))
        self.assertEqual(intake.get(fixtures.A)['package'],'A')
        self.assertEqual(intake.get(fixtures.B)['yaml_request'],'bob.yaml field=false')
        self.assertIn('保留原配置',result)
        self.assertEqual(self.s.jobs(fixtures.A),[])

    def test_unspecified_empty_schema_fields_do_not_clear_previous_choices(self):
        intake=Intake(self.s)
        original={'package':'A','yaml_request':'a.yaml mode=ON','m100_so':'M100.so','lpai_target':'team'}
        intake.update(fixtures.A,original)
        intake.update(fixtures.A,{key:'' for key in FIELDS})
        for key,value in original.items():self.assertEqual(intake.get(fixtures.A)[key],value)

    def test_clear_marker_only_matches_complete_yaml_request(self):
        intake=Intake(self.s)
        change='config.yaml 的 label 字段改成“'+CLEAR_YAML+'”'
        intake.update(fixtures.A,{'yaml_request':change})
        self.assertEqual(intake.get(fixtures.A)['yaml_request'],change)

    def test_clear_draft_command_resets_only_current_user(self):
        intake=Intake(self.s)
        intake.update(fixtures.A,{'package':'old-A'});intake.update(fixtures.B,{'package':'B'})
        self.service.handle(fixtures.SESSION,'清空本次材料',quick('清空本次材料'))
        self.assertNotIn('package',intake.get(fixtures.A));self.assertEqual(intake.get(fixtures.B)['package'],'B')

    def test_supplied_m100_overrides_build_and_explicit_build_clears_it(self):
        intake=Intake(self.s);intake.update(fixtures.A,{'m100_so':'provided.so'})
        self.assertEqual(intake.get(fixtures.A)['m100_mode'],'provided')
        self.service.handle(fixtures.SESSION,'改为自动编译 M100',quick('改为自动编译 M100'))
        self.assertNotIn('m100_so',intake.get(fixtures.A))
        self.assertEqual(intake.get(fixtures.A)['m100_mode'],'build_if_missing')
        self.assertIn('已验收',intake.summary(fixtures.A))

    def test_complete_draft_never_claims_preparation_or_enqueues(self):
        text=Intake(self.s).update(fixtures.A,{'package':'A','camera_so':'C','lidar_so':'L','dataset':'共享:18场景','scene_scope':'全部','lpai_target':'team'})
        self.assertIn('尚未下载/编译/入队',text);self.assertIn('待核验',text)
        self.assertEqual(self.s.jobs(fixtures.A),[])

    def test_schema_rejects_commands_identity_and_invalid_options(self):
        for inputs in ({'owner':fixtures.B},{'command':'sh build.sh'},{'lpai_target':'anyone'},{'package':5}):
            with self.assertRaises(GatewayError):validate_intent({'action':'prepare','inputs':inputs})

    def test_help_explains_optional_inputs_and_screenshot_limit(self):
        result=self.service.handle(fixtures.SESSION,'帮助',{'action':'help'})
        self.assertLess(len(result),500)
        for fragment in ('默认回灌后评测','自动检查并排队','详细说明','YAML'):
            self.assertIn(fragment,result)
        from bench_gateway.intake import DETAILED_GUIDE
        result=self.service.handle(fixtures.SESSION,'详细说明',{'action':'help','reply':DETAILED_GUIDE})
        for fragment in ('libxcamera.so','libxlidar.so','liburoad_m100.so','YAML','图片中的文件名不是下载地址','默认回灌后评测'):
            self.assertIn(fragment,result)
        for fragment in ('自动检查','查看准备状态','已验收','不连接台架'):
            self.assertIn(fragment,result)
        self.assertNotIn('计划使用已接入',result)

    def test_public_help_routes_describe_confirmation_without_creating_work(self):
        self.cfg['require_material_confirmation']=True
        before=self.s.jobs(fixtures.A)
        for text in ('你好','使用说明','帮助','详细说明','软件提交规范'):
            with self.subTest(text=text):
                reply=self.service.handle(fixtures.SESSION,text,quick(text))
                self.assertIn('确认执行',reply)
                self.assertIn('YAML',reply)
                self.assertNotIn('无需发送“材料发完了”或确认执行',reply)
                self.assertNotIn('材料齐备后自动检查、绑定并进入执行队列',reply)
                self.assertNotIn('无待处理项时自动提交',reply)
        self.assertEqual(self.s.jobs(fixtures.A),before)
        with self.s.connect() as connection:
            exists=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparations'").fetchone()
            if exists:self.assertEqual(connection.execute('SELECT COUNT(*) FROM preparations').fetchone()[0],0)


if __name__=='__main__':unittest.main()
