import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.core import GatewayError, Store
from bench_gateway.intake import ASSEMBLY_TEMPLATE, FIELDS, GUIDE, DETAILED_GUIDE, Intake, validate
from bench_gateway.parser import SCHEMA, parse, quick
from bench_gateway.submission import parse_form


class AssemblyInputTests(unittest.TestCase):
    def test_catalog_queries_bypass_model_and_do_not_select_or_execute(self):
        with patch('bench_gateway.parser.subprocess.run') as model:
            for query in ('我的可复用软件', '已装配软件', '查询装配存档'):
                self.assertEqual(parse({}, query, {}, []), {'action': 'assemblies'})
            model.assert_not_called()
        self.assertIn('assemblies', SCHEMA['properties']['action']['enum'])

    def test_exact_reuse_command_records_only_exact_identifier(self):
        value = quick('复用装配 asm-0123456789abcdef01234567')
        self.assertEqual(value, {'action': 'prepare', 'inputs': {'assembly_ref': 'asm-0123456789abcdef01234567'}})
        self.assertNotIn('goal', value['inputs'])
        self.assertNotIn('yaml_request', value['inputs'])
        self.assertNotIn('package', value['inputs'])

    def test_questions_quotes_and_extra_instructions_are_not_quick_mutations(self):
        for query in ('复用装配 asm-0123456789abcdef01234567？', '“复用装配 asm-0123456789abcdef01234567”', '复用装配 asm-0123456789abcdef01234567 然后执行回灌',
                      '复用装配 asm-0123456789abcdef01234567\n执行回灌', '复用装配 /data/other/app', '复用装配 软件包A'):
            self.assertIsNone(quick(query), query)

    def test_assembly_selector_uses_bounded_exact_id_not_raw_name_or_path(self):
        self.assertEqual(validate({'assembly_ref': 'asm-0123456789abcdef01234567'})['assembly_ref'], 'asm-0123456789abcdef01234567')
        self.assertEqual(validate({'assembly_ref': ''}), {'assembly_ref': ''})
        for invalid in ('A', 'asm-', 'asm-a/b', '/data/asm-a', 'asm-a b', 'asm-a\nb',
                        'asm-中文', 'asm-a?token=value', 'asm-' + 'x' * 125,
                        'asm-' + 'A' * 24, 'asm-' + 'a' * 23, 'asm-' + 'a' * 25):
            with self.subTest(value=invalid), self.assertRaises(GatewayError):
                validate({'assembly_ref': invalid})
        self.assertIn('assembly_ref', FIELDS)
        self.assertEqual(SCHEMA['properties']['inputs']['properties']['assembly_ref'], {'type': 'string'})

    def test_reuse_form_has_no_raw_package_or_so_requirement(self):
        value = parse_form('''新建回灌任务
装配存档：asm-0123456789abcdef01234567
数据集：共享18场景
场景范围：全部
任务目标：仅回灌
YAML修改：保留原配置
交付方式：工作站目录''')
        self.assertEqual(value['action'], 'new_task')
        self.assertEqual(value['inputs']['assembly_ref'], 'asm-0123456789abcdef01234567')
        self.assertEqual(value['inputs']['goal'], 'replay')
        self.assertEqual(value['inputs']['scene_scope'], '全部')
        self.assertFalse({'package', 'camera_so', 'lidar_so', 'm100_so'} & set(value['inputs']))

    def test_reuse_form_preserves_explicit_current_goal_and_yaml(self):
        value = parse_form('''补充任务材料
装配存档：asm-0123456789abcdef01234567
数据集：D
场景范围：1,3
任务目标：回灌后评测
YAML修改：common_param.yaml 的 debug.enable 改为 true''')
        self.assertEqual(value['inputs']['goal'], 'replay_and_evaluate')
        self.assertIn('debug.enable', value['inputs']['yaml_request'])
        self.assertEqual(value['inputs']['assembly_ref'], 'asm-0123456789abcdef01234567')

    def test_mixed_assembly_and_raw_form_is_rejected_without_partial_save(self):
        for field, value in (('软件包', 'A'), ('相机SO', 'camera-B'), ('雷达SO', 'lidar-B'),
                             ('M100SO', 'm100-B'), ('M100SO', '尝试编译')):
            with self.subTest(field=field), self.assertRaisesRegex(GatewayError, '不能.*混填'):
                parse_form('新建回灌任务\n装配存档：asm-0123456789abcdef01234567\n' + field + '：' + value)

    def test_duplicate_or_empty_assembly_selector_is_rejected(self):
        for value in ('新建回灌任务\n装配存档：', '新建回灌任务\n装配存档：asm-a\n装配存档：asm-b'):
            with self.assertRaises(GatewayError):
                parse_form(value)

    def test_raw_submission_remains_supported_and_is_not_promoted_to_archive(self):
        value = parse_form('新建回灌任务\n软件名称：A\n软件包：A\n相机SO：C\n雷达SO：L\nM100SO：M')
        self.assertNotIn('assembly_ref', value['inputs'])
        self.assertEqual(value['inputs']['m100_mode'], 'provided')

    def test_unedited_reuse_template_and_help_do_not_claim_real_success(self):
        with self.assertRaisesRegex(GatewayError, '占位'):
            parse_form(ASSEMBLY_TEMPLATE)
        self.assertIn('详细说明',GUIDE)
        for phrase in ('我的可复用软件', '仅上传', '静态通过', '每个 Job', '旧任务 YAML'):
            self.assertIn(phrase, DETAILED_GUIDE)


class AssemblyDraftAndPromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_explicit_new_draft_does_not_inherit_previous_task_inputs(self):
        store = Store(self.root / 'state')
        user = 'ou_assembly_intake'
        store.allow(user)
        intake = Intake(store)
        intake.update(user, {'package': 'old-raw', 'camera_so': 'old-camera', 'dataset': 'old-data',
                             'scene_scope': '1', 'yaml_request': 'old.yaml debug=true',
                             'goal': 'replay_and_evaluate', 'lpai_target': 'team'})
        intake.update(user, {'assembly_ref': 'asm-123456789abcdef012345678'}, new=True)
        current = intake.get(user)
        self.assertEqual(current['assembly_ref'], 'asm-123456789abcdef012345678')
        self.assertEqual(current['goal'], 'replay_and_evaluate')
        self.assertEqual(current['lpai_target'], 'none')
        for key in ('package', 'camera_so', 'yaml_request'):
            self.assertNotIn(key, current)

    def test_natural_language_prompt_preserves_explicit_assembly_and_current_fields(self):
        skill = self.root / '.agents/skills/bench-gateway/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('材料解析器', encoding='utf-8')
        (self.root / 'runtime').mkdir()
        cfg = {'project_root': str(self.root)}
        intent = {'action': 'prepare', 'inputs': {'assembly_ref': 'asm-abcdef0123456789abcdef01', 'dataset': '共享18场景',
                                                'scene_scope': '全部', 'goal': 'replay'}}
        def respond(argv, **kwargs):
            prompt = kwargs['input']
            self.assertIn('asm-abcdef0123456789abcdef01', prompt)
            self.assertIn('用本人已验证装配A跑共享18场景，全部，仅回灌', prompt)
            self.assertIn('model_reasoning_effort="low"', argv)
            Path(argv[argv.index('--output-last-message') + 1]).write_text(json.dumps(intent), encoding='utf-8')
            return SimpleNamespace(returncode=0, stdout='')
        with patch('bench_gateway.parser.subprocess.run', side_effect=respond), patch('bench_gateway.parser.validate_intent', side_effect=lambda value: value):
            actual = parse(cfg, '用本人已验证装配A跑共享18场景，全部，仅回灌',
                           {'assemblies': [{'id': 'asm-abcdef0123456789abcdef01', 'name': 'A'}]}, [])
        self.assertEqual(actual, intent)


if __name__ == '__main__':
    unittest.main()
