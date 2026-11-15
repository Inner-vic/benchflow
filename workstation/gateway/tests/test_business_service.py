"""Admission and the private task sheet use the same frozen business inputs."""
import copy
import json
import unittest
from unittest.mock import patch

from bench_gateway.core import GatewayError
from bench_gateway.intake import Intake
import test_bindings as fixtures


class BusinessServiceTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.BindingTests()
        self.case.setUp()
        self.case.receipt['execution_policy'] = {
            'protocol': 'reviewed-stream-v1',
            'fixed_configuration': {'replay.enable': True,
                                    'eval_dump.gt_trajectory.enable': False},
            'required_output_types': ['pred_traj', 'traj_xy'],
            'selected_scenes': [1, 2], 'precheck_scenes': [1],
            'runtime_compatibility': 'NOT_VERIFIED'}

    def tearDown(self):
        self.case.tearDown()

    def update_and_prepare(self, values):
        c = self.case
        Intake(c.store).update(c.user, values)
        c.preparation = c.make_preparation()
        c.receipt['path'] = c.store.remote_root(c.user, c.cfg['remote_users_root']) + '/preparations/' + c.preparation['id'] + '/binding.json'

    def submit(self, **intent):
        c = self.case
        return c.service.handle(c.session, '执行回灌', {'action': 'replay', **intent})

    def jobs(self):
        return self.case.store.jobs(self.case.user)

    def attach_operator_evaluator(self, baseline):
        c = self.case
        data = copy.deepcopy(baseline['data'])
        data['evaluator'] = {'argv': ['python', 'reviewed_operator_evaluator.py']}
        with c.store.connect() as connection:
            connection.execute('UPDATE baselines SET data=? WHERE id=? AND owner=?',
                               (json.dumps(data), baseline['id'], c.user))
        return c.store.baseline(c.user, baseline['name'])

    def completed_replay(self, baseline):
        """A retained source receipt fixture, without invoking a runner."""
        c = self.case
        payload = {'action': 'replay', 'baseline': baseline,
                   'assets': baseline['data']['assets'], 'scenes': [1, 2], 'evaluate_after': False}
        job, _ = c.store.submit(c.user, c.session, 'retained-source-fixture', payload)
        c.store.finish(job, 'SUCCEEDED', {'task': '/workstation/tasks/' + job['id']})
        return c.store.job(c.user, job['id'])

    def test_saved_evaluation_goal_is_rejected_before_replay_queue(self):
        c = self.case
        self.update_and_prepare({'goal': 'replay_and_evaluate'})
        c.bind()
        with self.assertRaisesRegex(GatewayError, '评测入口'):
            self.submit(evaluate_after=False)
        self.assertEqual([], self.jobs())
        self.assertEqual('replay_and_evaluate', Intake(c.store).get(c.user)['goal'])
        self.assertEqual('STATIC_PASSED', c.queue.latest(c.user)['state'])

    def test_this_message_evaluation_request_cannot_queue_predictable_failure(self):
        self.case.bind()
        with self.assertRaisesRegex(GatewayError, '不会先回灌'):
            self.submit(evaluate_after=True)
        self.assertEqual([], self.jobs())

    def test_independent_evaluation_is_rejected_without_new_job(self):
        c = self.case
        source = self.completed_replay(c.bind())
        before = self.jobs()
        with self.assertRaisesRegex(GatewayError, '接入评测器'):
            c.service.handle(c.session, '只评测已有结果', {'action': 'evaluate', 'task_id': source['id']})
        self.assertEqual(before, self.jobs())
        self.assertFalse(any(job['lane'] == 'postprocess' for job in self.jobs()))
        self.assertEqual(source['result']['task'], c.store.job(c.user, source['id'])['result']['task'])

    def test_upload_requirement_is_preserved_and_rejected_before_queue(self):
        c = self.case
        for target in ('team', 'personal'):
            with self.subTest(target=target):
                self.update_and_prepare({'lpai_target': target})
                c.bind()
                with self.assertRaisesRegex(GatewayError, 'LPAI') as error:
                    self.submit()
                self.assertIn('不会静默降级', str(error.exception))
                self.assertEqual([], self.jobs())
                self.assertEqual(target, Intake(c.store).get(c.user)['lpai_target'])

    def test_material_check_can_still_be_requested_with_unavailable_delivery(self):
        c = self.case
        Intake(c.store).update(c.user, {'goal': 'replay_and_evaluate', 'lpai_target': 'team'})
        reply = c.service.handle(c.session, '检查材料', {'action': 'check_materials'})
        self.assertIn('已受理材料检查', reply)
        self.assertEqual('QUEUED', c.queue.latest(c.user)['state'])
        self.assertEqual([], self.jobs())

    def test_replay_only_queues_with_complete_frozen_business_contract(self):
        c = self.case
        self.update_and_prepare({'software_name': '软件包A', 'build_reference': 'Release-build-2737'})
        baseline = c.bind()
        reply = self.submit()
        self.assertIn('本次材料和配置已绑定', reply)
        job, = self.jobs()
        contract = job['payload']['business_contract']
        self.assertEqual('QUEUED', job['state'])
        self.assertEqual(1, contract['schema_version'])
        self.assertEqual('replay', contract['goal'])
        self.assertIs(False, contract['delivery']['original_txt'])
        self.assertEqual('none', contract['delivery']['lpai_target'])
        self.assertEqual('软件包A', contract['software_name'])
        self.assertEqual('Release-build-2737', contract['build_reference'])
        self.assertEqual(c.preparation['id'], contract['preparation_id'])
        self.assertEqual(baseline['id'], contract['baseline_id'])
        self.assertEqual(c.package['id'], contract['materials'][0]['id'])
        self.assertEqual(c.package['data']['sha256'], contract['materials'][0]['sha256'])
        self.assertEqual(4, len(contract['materials']))
        self.assertEqual('lib/libxcamera.so', contract['materials'][1]['target'])
        self.assertEqual([1, 2], contract['scenes'])
        self.assertEqual(1, contract['precheck']['scene'])
        self.assertEqual('NOT_VERIFIED', contract['precheck']['status'])
        self.assertEqual(['pred_traj', 'traj_xy'], contract['execution_policy']['required_output_types'])
        self.assertEqual([], contract['issues'])

    def test_editing_draft_after_admission_never_rewrites_queued_contract(self):
        c = self.case
        c.bind()
        self.submit()
        job, = self.jobs()
        old_payload = copy.deepcopy(job['payload'])
        Intake(c.store).update(c.user, {'package': 'replacement-package', 'goal': 'replay_and_evaluate',
                                      'lpai_target': 'team', 'scene_scope': '3',
                                      'yaml_request': 'new explicit YAML request'},new=True)
        self.assertEqual(old_payload, c.store.job(c.user, job['id'])['payload'])
        self.assertEqual('replay', old_payload['business_contract']['goal'])
        self.assertEqual([1, 2], old_payload['business_contract']['scenes'])
        self.assertEqual(c.package['id'], old_payload['business_contract']['materials'][0]['id'])

    def test_valid_operator_evaluator_allows_requested_combined_job(self):
        c = self.case
        self.update_and_prepare({'goal': 'replay_and_evaluate'})
        self.attach_operator_evaluator(c.bind())
        self.submit()
        job, = self.jobs()
        self.assertIs(True, job['payload']['evaluate_after'])
        self.assertEqual('replay_and_evaluate', job['payload']['business_contract']['goal'])
        self.assertIs(True, job['payload']['business_contract']['delivery']['original_txt'])

    def test_valid_operator_evaluator_allows_existing_result_only(self):
        c = self.case
        source = self.completed_replay(self.attach_operator_evaluator(c.bind()))
        c.service.handle(c.session, '评测已有结果', {'action': 'evaluate', 'task_id': source['id']})
        evaluations = [job for job in self.jobs() if job['lane'] == 'postprocess']
        self.assertEqual(1, len(evaluations))
        self.assertEqual(source['id'], evaluations[0]['payload']['source_job'])
        self.assertEqual(source['result']['task'], evaluations[0]['payload']['source_task'])

    def test_task_review_reports_current_frozen_inputs_without_hardware_probe(self):
        c = self.case
        baseline = c.bind()
        with patch('bench_gateway.transfer.SSH.python', side_effect=AssertionError('review must stay local')) as transport:
            text = c.service.task_review(c.user)
        transport.assert_not_called()
        self.assertIn(c.preparation['id'], text)
        self.assertIn('A · v1', text)
        self.assertIn('SHA256 ' + c.package['data']['sha256'][:12], text)
        self.assertIn('正式场景：1,2', text)
        self.assertIn('先跑本任务小场景预检', text)
        self.assertIn('eval_dump.gt_trajectory.enable=False', text)
        self.assertEqual(baseline['id'], c.service.review_records(c.user)[2]['id'])
        self.assertEqual([], self.jobs())

    def test_task_review_after_edit_shows_new_sources_and_excludes_old_job(self):
        c = self.case
        c.bind()
        self.submit()
        old_job, = self.jobs()
        Intake(c.store).update(c.user, {'package': 'replacement-package'},new=True)
        text = c.service.task_review(c.user)
        self.assertIsNone(c.service.review_records(c.user)[1])
        self.assertIn('replacement-package', text)
        self.assertNotIn('A · v1', text)
        self.assertNotIn(old_job['id'], text)
        self.assertNotIn('下一步：发送“执行回灌”', text)

    def test_private_task_review_never_displays_foreign_draft_or_job(self):
        c = self.case
        c.bind()
        Intake(c.store).update(c.other, {'package': 'bob-private-package', 'dataset': 'bob-private-dataset'}, new=True)
        foreign_baseline = c.store.add_baseline(c.other, 'bob-private-baseline', {'gate_verified': True, 'asset_ids': {}})
        foreign, _ = c.store.submit(c.other, 'feishu:oc_bob:' + c.other, 'bob-job',
            {'action': 'replay', 'baseline': foreign_baseline, 'assets': {}, 'scenes': [1]})
        c.store.finish(foreign, 'SUCCEEDED', {'task': '/bob/private/result'})
        alice = c.service.task_review(c.user)
        bob = c.service.task_review(c.other)
        self.assertNotIn('bob-private', alice)
        self.assertNotIn('/bob/private/result', alice)
        self.assertNotIn(foreign['id'], alice)
        self.assertIn('bob-private-package', bob)
        self.assertNotIn(c.preparation['id'], bob)
        self.assertNotIn('A · v1', bob)

    def test_materials_action_returns_current_task_sheet_not_unchecked_draft(self):
        c = self.case
        c.bind()
        text = c.service.handle(c.session, '查看任务单', {'action': 'materials'})
        self.assertIn('本次台架任务单', text)
        self.assertIn('已冻结版本', text)
        self.assertIn('下一步：发送“执行回灌”', text)


if __name__ == '__main__':
    unittest.main()
