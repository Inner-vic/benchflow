import unittest
import test_gateway as fixtures
from bench_gateway.evaluation_guidance import answer, GUIDE
from bench_gateway.parser import quick
from bench_gateway.intake import Intake


class EvaluationGuidanceTests(unittest.TestCase):
    def test_natural_source_questions_are_immediate_information_only(self):
        for text in ('评测数据说明', '我们挂载的是哪个数据卷，路径是什么？',
                     '开发空间是怎么匹配GT的？', 'V151的数据在哪里？', 'GT路径',
                     '默认评测数据在哪里？', '新增数据卷需要怎么做？'):
            with self.subTest(text=text):
                self.assertEqual(quick(text), {'action': 'help', 'reply': GUIDE})

    def test_whole_volume_is_distinct_from_task_date_and_scene_scope(self):
        self.assertNotIn('DataReorganized-V151', GUIDE)
        self.assertNotIn('/gt-analyze/v151', GUIDE)
        self.assertIn('不使用通用默认真值',GUIDE)
        self.assertNotIn('2026-09-11', GUIDE)
        for fragment in ('完整场景标识', '只评测本次场景', '多个版本', '不猜选'):
            self.assertIn(fragment, GUIDE)

    def test_cloud_guidance_keeps_lifecycle_and_partial_result_limits(self):
        for fragment in ('明确关联', '已有任务保留原后端', '无需用户填写',
                         '普通聊天不会自动挂载', '4小时', '部分结果'):
            self.assertIn(fragment, GUIDE)
        self.assertNotIn('只读挂载', GUIDE)
        self.assertNotIn('云端场景目录缺少', GUIDE)

    def test_material_updates_and_execution_controls_are_not_swallowed(self):
        for text in ('数据集：/mnt/data/v151', '把V151路径改为/mnt/data/new',
                     '开发空间怎么匹配GT？本次仅回灌', 'GT说明\n软件包：https://example.invalid/A.app.tar.gz',
                     'V151是不是需要替换libxlidar.so？', '修改config.yaml的GT路径为新值',
                     '把数据卷切换到其他卷', 'https://example.invalid/V151.app.tar.gz'):
            with self.subTest(text=text):
                self.assertIsNone(answer(text))
        self.assertEqual(quick('本次仅回灌')['action'], 'set_goal')
        self.assertEqual(quick('https://example.invalid/V151.app.tar.gz')['action'], 'prepare')

    def test_unrelated_questions_keep_existing_routing(self):
        for text in ('台架状态', '评测耗时多久', 'LIVC APP是什么', '当前配置'):
            self.assertIsNone(answer(text))


class EvaluationGuidanceStateTests(unittest.TestCase):
    setUp = fixtures.GatewayTests.setUp
    tearDown = fixtures.GatewayTests.tearDown

    def test_explanation_preserves_both_users_materials_and_jobs(self):
        intake = Intake(self.s)
        intake.update(fixtures.A, {'package': 'A', 'dataset': '/mnt/data/A'})
        intake.update(fixtures.B, {'package': 'B'})
        before = {user: (intake.get(user), self.s.jobs(user)) for user in (fixtures.A, fixtures.B)}
        text = '我们挂载的是哪个数据卷，路径是什么？'
        self.assertEqual(self.service.handle(fixtures.SESSION, text, quick(text)), GUIDE)
        self.assertEqual({user: (intake.get(user), self.s.jobs(user)) for user in before}, before)


if __name__ == '__main__':
    unittest.main()
