import unittest

from bench_gateway.core import GatewayError
from bench_gateway.intake import CLEAR_YAML, SUBMISSION_TEMPLATE, validate
from bench_gateway.submission import parse_form


class SubmissionFormTests(unittest.TestCase):
    def test_full_form_maps_fixed_roles_and_preserves_url_colons(self):
        value=parse_form('''新建回灌任务
软件名称：软件包 A
软件包：https://ci.example/a.app.tar.gz?version=1:2
相机 SO：camera-A
雷达SO: /data/user/lidar.so
M100SO：/data/user/m100.so
数据集：共享18场景
场景范围：1,3,5
任务目标：回灌后评测
YAML修改：common_param.yaml 的 debug.label 改为 "camera:lidar"
交付方式：工作站目录
构建来源：build:2737 https://ci.example/build/2737''')
        self.assertEqual(value['action'],'new_task');inputs=value['inputs']
        self.assertEqual(inputs['package'],'https://ci.example/a.app.tar.gz?version=1:2')
        self.assertEqual(inputs['software_name'],'软件包 A')
        self.assertEqual(inputs['camera_so'],'camera-A')
        self.assertEqual(inputs['lidar_so'],'/data/user/lidar.so')
        self.assertEqual(inputs['m100_mode'],'provided')
        self.assertEqual(inputs['goal'],'replay_and_evaluate')
        self.assertEqual(inputs['lpai_target'],'none')
        self.assertIn('"camera:lidar"',inputs['yaml_request'])
        self.assertEqual(inputs['build_reference'],'build:2737 https://ci.example/build/2737')

    def test_supplement_only_contains_supplied_fields(self):
        self.assertEqual(parse_form('补充任务材料\n软件包：A\n构建来源：\nYAML修改：'),
                         {'action':'prepare','inputs':{'package':'A'}})

    def test_new_task_allows_incomplete_materials_to_be_collected(self):
        self.assertEqual(parse_form('新建回灌任务\n软件包：A'),
                         {'action':'new_task','inputs':{'package':'A'}})

    def test_title_without_fields_uses_existing_new_draft_action(self):
        self.assertEqual(parse_form('新建回灌任务'),{'action':'new_task','inputs':{}})

    def test_only_exact_first_line_activates_form(self):
        for text in ('我想新建回灌任务\n软件包：A',' 新建回灌任务\n软件包：A',
                     '新建回灌任务：\n软件包：A','请补充任务材料：软件包 A','软件包：A',''):
            self.assertIsNone(parse_form(text),text)

    def test_m100_compile_request_does_not_claim_a_built_file(self):
        self.assertEqual(parse_form('补充任务材料\nM100SO：尝试编译')['inputs'],
                         {'m100_mode':'build_if_missing'})

    def test_explicit_yaml_clear_uses_shared_clear_marker(self):
        self.assertEqual(parse_form('补充任务材料\nYAML修改：保留原配置')['inputs'],
                         {'yaml_request':CLEAR_YAML})

    def test_upload_targets_are_recorded_as_choices_only(self):
        for label,target in (('团队LPAI','team'),('个人LPAI','personal')):
            self.assertEqual(parse_form('补充任务材料\n交付方式：'+label)['inputs'],{'lpai_target':target})

    def test_unknown_duplicate_and_empty_required_fields_reject_entire_form(self):
        for text in ('新建回灌任务\n软件包：A\n包地址：B','新建回灌任务\n相机SO：A\n相机 SO：B',
                     '补充任务材料\n软件包：','新建回灌任务\n任务目标：','新建回灌任务\nM100SO：'):
            with self.assertRaises(GatewayError,msg=text):parse_form(text)

    def test_invalid_options_and_continuations_cannot_be_silently_lost(self):
        for text in ('补充任务材料\n任务目标：立即刷包','补充任务材料\n交付方式：任意用户空间',
                     '补充任务材料\nYAML修改：file.yaml\n  debug: true',
                     '补充任务材料\nYAML修改：file.yaml\n执行回灌'):
            with self.assertRaises(GatewayError,msg=text):parse_form(text)

    def test_software_reuse_name_has_bounded_plain_name_contract(self):
        self.assertEqual(validate({'software_name':'软件包 A_1.2-正式版'})['software_name'],'软件包 A_1.2-正式版')
        self.assertEqual(validate({'software_name':'名'*60})['software_name'],'名'*60)
        for name in ('a/b','a\\b','A\nB','A\rB','A\x00B','A\u2028B',' '*3,'A'*61):
            with self.assertRaises(GatewayError,msg=repr(name)):validate({'software_name':name})

    def test_build_reference_is_a_single_line_trace_string(self):
        self.assertEqual(validate({'build_reference':'2737 https://ci.example/job/2737'})['build_reference'],
                         '2737 https://ci.example/job/2737')
        for value in ('A\nB','A\rB','A\x00B','A\u2029B'):
            with self.assertRaises(GatewayError):validate({'build_reference':value})

    def test_unedited_template_is_rejected_instead_of_registering_placeholders(self):
        with self.assertRaisesRegex(GatewayError,'占位'):parse_form(SUBMISSION_TEMPLATE)


if __name__=='__main__':unittest.main()
