"""Business task sheets never invent readiness or associate historical jobs."""
import copy
import unittest

from bench_gateway.business import (capability_issues, make_contract, missing_inputs,
                                     render_contract, render_review)


def fixture():
    draft = {'goal': 'replay', 'lpai_target': 'none', 'package': 'A',
             'camera_so': 'camera', 'lidar_so': 'lidar', 'm100_so': 'm100',
             'm100_mode': 'provided', 'dataset': '共享18场景', 'scene_scope': '2,5',
             'software_name': 'Release A', 'build_reference': 'build-2737',
             'yaml_request': '将 common_param.yaml 的 debug.enable 改为 true'}
    def asset(role, name, target=None):
        data = {'sha256': {'package': 'a', 'camera_so': 'b', 'lidar_so': 'c', 'm100_so': 'd', 'dataset': 'e'}[role] * 64}
        if target:
            data['target'] = target
        return {'owner': 'u-current', 'id': 'a-' + role, 'name': name, 'version': 3,
                'kind': 'package' if role == 'package' else 'dependency', 'data': data}
    assets = {'package': asset('package', 'A'),
              'camera_so': asset('camera_so', 'camera', 'lib/libxcamera.so'),
              'lidar_so': asset('lidar_so', 'lidar', 'lib/libxlidar.so'),
              'm100_so': asset('m100_so', 'm100', 'lib/liburoad_m100.so')}
    dataset = asset('dataset', '18场景')
    dataset.update(owner='shared', kind='dataset', origin='dataset_catalog')
    dataset['data'].update(scene_count=18, remote_path='/shared/data', manifest='/shared/data/manifest.json')
    preparation = {'id': 'prep-current', 'owner': 'u-current', 'state': 'STATIC_PASSED',
                   'payload': copy.deepcopy(draft), 'result': {
                       'status': 'STATIC_PASSED', 'assets': assets, 'dataset': dataset,
                       'scenes': [2, 5], 'issues': [], 'adapter_binding': {'state': 'READY_FOR_GATE'},
                       'yaml_preparation': {'status': 'PREPARED', 'outputs': [{
                           'target': 'conf/common_param.yaml', 'path': '/own/prep/yaml/candidate.yaml',
                           'sha256': 'f' * 64, 'diff_path': '/own/prep/yaml/change.diff',
                           'diff_sha256': '9' * 64}],
                           'changes': [{'target': 'conf/common_param.yaml', 'operation': 'set',
                                        'path': ['debug', 'enable'], 'value': True}]}}}
    baseline = {'id': 'b-current', 'name': 'prepared-current', 'owner': 'u-current', 'data': {
        'kind': 'candidate-v1', 'readiness': 'READY_FOR_GATE', 'gate_verified': False,
        'preparation_id': 'prep-current', 'assets': {'package': assets['package'],
            'dependencies': [assets[role] for role in ('camera_so', 'lidar_so', 'm100_so')], 'dataset': dataset},
        'scenes': [2, 5], 'binding': {'input_digest': '0' * 64},
        'execution_policy': {'protocol': 'reviewed-stream-v1', 'fixed_configuration': {
            'replay.enable': True, 'eval_dump.gt_trajectory.enable': False,
            'eval_dump.output_dir': '/app/livc-test/<run_id>/outputs/eval_pred_result'},
            'required_output_types': ['pred_traj', 'traj_xy'], 'runtime_compatibility': 'NOT_VERIFIED'}}}
    return draft, preparation, baseline


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.draft, self.preparation, self.baseline = fixture()

    def job(self, state='QUEUED', preparation='prep-current', baseline='b-current', **result):
        return {'id': 'job-current', 'owner': 'u-current', 'created': 10, 'updated': 11,
                'state': state, 'payload': {'action': 'replay', 'baseline': {
                    'id': baseline, 'name': 'prepared-current', 'data': {'preparation_id': preparation}}},
                'result': result}

    def test_all_required_material_sources_are_explicit(self):
        self.assertEqual(['package', 'camera_so', 'lidar_so', 'm100_so', 'dataset', 'scene_scope'],
                         [issue['field'] for issue in missing_inputs({})])
        self.assertEqual([], missing_inputs(self.draft))

    def test_m100_build_request_is_not_counted_as_ready(self):
        draft = {**self.draft, 'm100_so': '', 'm100_mode': 'build_if_missing'}
        issue, = missing_inputs(draft)
        self.assertEqual('M100_BUILD_REQUIRED', issue['code'])
        self.assertIn('核对已验收配方', issue['message'])
        self.assertIn('liburoad_m100.so', issue['next_action'])

    def test_whitespace_source_does_not_satisfy_requirement(self):
        self.assertEqual('package', missing_inputs({**self.draft, 'package': '  '})[0]['field'])

    def test_eval_ability_requires_nonempty_operator_argv(self):
        draft = {**self.draft, 'goal': 'replay_and_evaluate'}
        for evaluator in (None, {}, {'argv': []}, {'argv': ['']}, {'argv': ['run', 7]}, {'argv': 'run'}):
            with self.subTest(evaluator=evaluator):
                self.assertEqual('EVALUATOR_NOT_READY', capability_issues(draft, evaluator)[0]['code'])
        self.assertEqual([], capability_issues(draft, {'argv': ['python', 'reviewed_adapter.py']}))

    def test_replay_does_not_require_evaluator(self):
        self.assertEqual([], capability_issues(self.draft))

    def test_team_and_personal_upload_are_never_silently_downgraded(self):
        for target in ('team', 'personal'):
            with self.subTest(target=target):
                issue, = capability_issues({**self.draft, 'lpai_target': target})
                self.assertEqual('LPAI_NOT_READY', issue['code'])
                self.assertIn('明确', issue['next_action'])
                self.assertIn('取消 LPAI', issue['next_action'])

    def test_contract_freezes_versions_role_targets_and_range(self):
        contract = make_contract(self.draft, self.preparation, self.baseline)
        self.assertEqual(1, contract['schema_version'])
        self.assertEqual('FROZEN', contract['input_state'])
        self.assertEqual('a-package', contract['materials'][0]['id'])
        self.assertEqual(3, contract['materials'][0]['version'])
        self.assertEqual('lib/libxcamera.so', contract['materials'][1]['target'])
        self.assertEqual('shared', contract['dataset']['scope'])
        self.assertEqual('/shared/data', contract['dataset']['remote_path'])
        self.assertEqual([2, 5], contract['scenes'])
        self.assertEqual(2, contract['precheck']['scene'])
        self.assertEqual('NOT_VERIFIED', contract['precheck']['status'])
        self.assertEqual('build-2737', contract['build_reference'])
        self.assertEqual([], contract['issues'])

    def test_contract_is_detached_from_mutable_records(self):
        contract = make_contract(self.draft, self.preparation, self.baseline)
        contract['scenes'].append(9)
        contract['execution_policy']['fixed_configuration']['replay.enable'] = False
        contract['yaml']['changes'][0]['value'] = False
        self.assertEqual([2, 5], self.baseline['data']['scenes'])
        self.assertIs(True, self.baseline['data']['execution_policy']['fixed_configuration']['replay.enable'])
        self.assertIs(True, self.preparation['result']['yaml_preparation']['changes'][0]['value'])

    def test_reviewed_protocol_completion_is_visible_separately_from_user_yaml(self):
        normalization={'schema_version':1,'rule_id':'sw0058-build2737-m1004-gt-off-v1',
            'source':'reviewed-protocol-default','target':'etc/uroad/config/common_param.yaml',
            'before_sha256':'a'*64,'after_sha256':'f'*64,
            'fields':{'eval_dump.gt_trajectory.enable':False}}
        self.baseline['data']['execution_policy']['protocol_normalization']=normalization
        contract=make_contract(self.draft,self.preparation,self.baseline)
        text=render_contract(contract)
        self.assertIn('协议配置补齐：本次任务副本新增 eval_dump.gt_trajectory.enable=false',text)
        self.assertIn('配置哈希：aaaaaaaaaaaa → ffffffffffff',text)
        self.assertIn('用户 YAML：'+self.draft['yaml_request'],text)
        contract['execution_policy']['protocol_normalization']['fields']['eval_dump.gt_trajectory.enable']=True
        self.assertIs(False,normalization['fields']['eval_dump.gt_trajectory.enable'])

    def test_partial_or_wrong_protocol_completion_cannot_claim_completed(self):
        for value in (True,{}, {'rule_id':'sw0058-build2737-m1004-gt-off-v1'},
                      {'rule_id':'unknown','fields':{'eval_dump.gt_trajectory.enable':False}}):
            self.baseline['data']['execution_policy']['protocol_normalization']=value
            text=render_contract(make_contract(self.draft,self.preparation,self.baseline))
            self.assertNotIn('协议配置补齐：本次任务副本新增',text)

    def test_contract_keeps_selected_delivery_and_yaml_diff(self):
        draft = {**self.draft, 'goal': 'replay_and_evaluate', 'lpai_target': 'team'}
        contract = make_contract(draft, self.preparation, self.baseline)
        self.assertIs(True, contract['delivery']['original_txt'])
        self.assertEqual('team', contract['delivery']['lpai_target'])
        self.assertEqual('/own/prep/yaml/change.diff', contract['yaml']['outputs'][0]['diff_path'])
        self.assertEqual({'EVALUATOR_NOT_READY', 'LPAI_NOT_READY'}, {issue['code'] for issue in contract['issues']})

    def test_effective_goal_override_is_recorded_in_contract(self):
        self.assertEqual('replay_and_evaluate', make_contract(self.draft, evaluate_after=True)['goal'])
        self.assertEqual('replay', make_contract({**self.draft, 'goal': 'replay_and_evaluate'}, evaluate_after=False)['goal'])

    def test_saved_frozen_contract_can_be_rendered_without_live_draft(self):
        contract = make_contract({}, self.preparation, self.baseline)
        self.assertEqual([], contract['issues'])
        self.assertIn('A · v3', render_contract(contract))

    def test_unchecked_source_is_recorded_not_frozen(self):
        text = render_review(self.draft)
        self.assertIn('已记录来源，尚待核验', text)
        self.assertIn('下一步：发送“检查材料”', text)
        self.assertNotIn('候选已绑定', text)

    def test_ready_review_explains_this_job_gate_and_effective_config(self):
        text = render_review(self.draft, self.preparation, self.baseline)
        self.assertIn('候选已绑定，待提交本次小场景预检', text)
        self.assertIn('不表示本任务小场景预检或实机兼容已通过', text)
        self.assertIn('eval_dump.gt_trajectory.enable=False', text)
        self.assertIn('验收产物：pred_traj、traj_xy', text)
        self.assertIn('原位引用', text)
        self.assertIn('下一步：发送“执行回灌”', text)
        self.assertEqual(1, text.count('下一步：'))

    def test_partial_materials_stay_in_collection_and_report_m100(self):
        text = render_review({'package': 'A', 'm100_mode': 'build_if_missing'})
        self.assertIn('M100 尚待准备', text)
        self.assertIn('阻塞项', text)
        self.assertNotIn('主要材料来源已记录', text)

    def test_preparation_in_progress_does_not_instruct_duplicate_check(self):
        for state in ('QUEUED', 'RUNNING'):
            prep = {**self.preparation, 'state': state}
            text = render_review(self.draft, prep)
            self.assertIn('下一步：发送“查看准备状态”', text)
            self.assertNotIn('下一步：发送“执行回灌”', text)

    def test_binding_failure_exposes_actionable_reason(self):
        prep = copy.deepcopy(self.preparation)
        prep['result']['adapter_binding'] = {'state': 'NEEDS_INPUT', 'message': 'YAML 与审核协议冲突'}
        text = render_review(self.draft, prep)
        self.assertIn('适配器绑定未完成', text)
        self.assertIn('YAML 与审核协议冲突', text)
        self.assertNotIn('下一步：发送“执行回灌”', text)

    def test_requested_unsupported_delivery_blocks_submit_language(self):
        text = render_review({**self.draft, 'lpai_target': 'team'}, self.preparation, self.baseline)
        self.assertIn('LPAI 团队空间', text)
        self.assertIn('上传能力待接入', text)
        self.assertNotIn('下一步：发送“执行回灌”', text)

    def test_stale_review_uses_current_draft_not_old_frozen_asset(self):
        text = render_review({**self.draft, 'package': 'NEW-PACKAGE'}, self.preparation, self.baseline,
                             [self.job('SUCCEEDED', task='/old/retained')], stale=True)
        self.assertIn('旧检查及绑定已过期', text)
        self.assertIn('NEW-PACKAGE', text)
        self.assertNotIn('A · v3', text)
        self.assertNotIn('/old/retained', text)
        self.assertNotIn('任务：job-current', text)

    def test_unrelated_and_wrong_owner_jobs_are_not_associated(self):
        jobs = [self.job('SUCCEEDED', preparation='prep-old', task='/secret/old'),
                self.job('SUCCEEDED', baseline='b-old', task='/secret/other-baseline'),
                {**self.job('SUCCEEDED', task='/secret/other-owner'), 'owner': 'u-other'}]
        text = render_review(self.draft, self.preparation, self.baseline, jobs)
        self.assertNotIn('/secret/', text)
        self.assertNotIn('任务：job-current', text)
        self.assertIn('待提交本次小场景预检', text)

    def test_business_contract_identity_can_associate_job(self):
        job = self.job('RUNNING', stage='小场景预检中')
        job['payload']['baseline'] = {}
        job['payload']['business_contract'] = {'preparation_id': 'prep-current'}
        text = render_review(self.draft, self.preparation, self.baseline, [job])
        self.assertIn('任务：job-current', text)
        self.assertIn('执行阶段：小场景预检中', text)
        self.assertNotIn('下一步：发送“执行回灌”', text)

    def test_unknown_outcome_requests_reconciliation_never_replay(self):
        text = render_review(self.draft, self.preparation, self.baseline,
                             [self.job('NEEDS_REVIEW', error='SSH 中断')])
        self.assertIn('执行结果待核验', text)
        self.assertIn('SSH 中断', text)
        self.assertIn('不要重新提交回灌', text)

    def test_queued_sheet_uses_admitted_goal_not_live_draft_default(self):
        admitted = copy.deepcopy(self.baseline)
        admitted['data']['evaluator'] = {'argv': ['reviewed-evaluator']}
        saved = make_contract(self.draft, self.preparation, admitted, evaluate_after=True)
        self.assertEqual([], saved['issues'])
        job = self.job('QUEUED')
        job['payload']['business_contract'] = saved
        text = render_review(self.draft, self.preparation, self.baseline, [job])
        self.assertIn('目标：回灌后评测', text)
        self.assertIn('要求交付：工作站结果目录、原始 TXT', text)
        self.assertNotIn('目标：仅数据回灌', text)
        self.assertNotIn('尚未绑定有效的评测入口', text)

    def test_queued_sheet_keeps_frozen_policy_after_operator_metadata_changes(self):
        saved = make_contract(self.draft, self.preparation, self.baseline)
        job = self.job('RUNNING')
        job['payload']['business_contract'] = copy.deepcopy(saved)
        live = copy.deepcopy(self.baseline)
        live['data']['execution_policy']['fixed_configuration']['eval_dump.gt_trajectory.enable'] = True
        live['data']['execution_policy']['required_output_types'] = ['changed-output']
        text = render_review(self.draft, self.preparation, live, [job])
        self.assertIn('eval_dump.gt_trajectory.enable=False', text)
        self.assertNotIn('eval_dump.gt_trajectory.enable=True', text)
        self.assertIn('验收产物：pred_traj、traj_xy', text)
        self.assertNotIn('changed-output', text)
        self.assertEqual(saved, job['payload']['business_contract'])

    def test_completed_replay_returns_retained_directory(self):
        text = render_review(self.draft, self.preparation, self.baseline,
                             [self.job('SUCCEEDED', task='/workstation/tasks/current')])
        self.assertIn('任务完成', text)
        self.assertIn('已保留工作站结果：/workstation/tasks/current', text)
        self.assertNotIn('原始 TXT：', text)

    def test_simulated_result_never_appears_as_real_result_directory(self):
        text = render_review(self.draft, self.preparation, self.baseline,
                             [self.job('SIMULATED', task='SIMULATION/job-current')])
        self.assertIn('未执行台架', text)
        self.assertIn('没有真实回灌结果', text)
        self.assertNotIn('已保留工作站结果', text)


if __name__ == '__main__':
    unittest.main()
