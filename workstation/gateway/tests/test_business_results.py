"""Failure evidence is useful without weakening receipt or recovery boundaries."""
import unittest

from bench_gateway.core import format_result


def replay_job():
    return {'id': 'gw-result-fixture', 'payload': {'action': 'replay'}}


class BusinessResultTests(unittest.TestCase):
    def diagnostics(self):
        return {'category': 'ABI_SYMBOL_MISMATCH',
                'summary': '日志出现动态符号错误，根因仍需用户核验。',
                'evidence': [{'path': '/runs/current/logs/worker-cli.log',
                              'excerpt': 'undefined symbol: camera_example'}],
                'next_action': '请提供与本软件包匹配的相机 SO；本轮不自动换库或重试。',
                'logs': ['/runs/current/logs/worker-cli.log']}

    def test_failed_gate_returns_observed_evidence_and_specific_next_action(self):
        result = {'error': '小场景预检失败，全量未执行。', 'diagnostics': self.diagnostics()}
        text = format_result(replay_job(), 'FAILED', result)
        self.assertIn('诊断分类：ABI_SYMBOL_MISMATCH', text)
        self.assertIn('undefined symbol: camera_example', text)
        self.assertIn('/runs/current/logs/worker-cli.log', text)
        self.assertIn('下一步：请提供与本软件包匹配的相机 SO', text)
        self.assertNotIn('回灌完成。', text)

    def test_failure_diagnostic_excerpt_count_and_text_are_bounded(self):
        diagnostics = {'category': 'TIMEOUT', 'summary': 'S' * 601, 'next_action': 'N' * 801,
                       'evidence': [{'path': '/runs/current/log-' + str(index),
                                     'excerpt': 'entry-' + str(index) + ':' + 'X' * 500}
                                    for index in range(5)]}
        text = format_result(replay_job(), 'FAILED', {'diagnostics': diagnostics})
        self.assertEqual(3, text.count('证据：'))
        self.assertNotIn('entry-3:', text)
        self.assertNotIn('entry-4:', text)
        self.assertNotIn('S' * 601, text)
        self.assertNotIn('N' * 801, text)
        self.assertNotIn('X' * 401, text)
        self.assertLess(len(text), 3500)

    def test_unknown_reason_still_exposes_checked_log_path(self):
        diagnostics = {'category': 'UNKNOWN', 'summary': '未发现可可靠分类的错误证据。',
                       'evidence': [], 'logs': ['/runs/current/logs/worker-cli.log'],
                       'next_action': '请核对本任务日志并提供错误证据。'}
        text = format_result(replay_job(), 'FAILED', {'diagnostics': diagnostics})
        self.assertIn('UNKNOWN', text)
        self.assertIn('/runs/current/logs/worker-cli.log', text)
        self.assertIn('请核对本任务日志', text)

    def test_needs_review_keeps_global_recovery_gate_despite_diagnostic_advice(self):
        result = {'error': 'SSH 响应中断。', 'diagnostics': self.diagnostics()}
        text = format_result(replay_job(), 'NEEDS_REVIEW', result)
        self.assertIn('已暂停自动派发', text)
        self.assertIn('undefined symbol: camera_example', text)
        self.assertIn('即使连接恢复', text)
        self.assertIn('核验 Runner 和恢复收据', text)
        self.assertIn('不自动重跑', text)

    def test_needs_review_without_diagnostics_preserves_existing_gate(self):
        text = format_result(replay_job(), 'NEEDS_REVIEW', {'error': '执行结果未知'})
        self.assertIn('执行结果未知', text)
        self.assertIn('待核验', text)
        self.assertIn('不自动重跑', text)
        self.assertNotIn('诊断分类', text)

    def test_safe_failure_keeps_already_retained_workstation_result(self):
        text = format_result(replay_job(), 'PARTIAL_SUCCESS', {
            'error': '后处理未完成', 'task': '/workstation/tasks/retained'})
        self.assertIn('后处理未完成', text)
        self.assertIn('已保留回灌结果：/workstation/tasks/retained', text)

    def test_replay_success_delivers_actual_directory_without_txt_claim(self):
        text = format_result(replay_job(), 'SUCCEEDED', {'task': '/workstation/tasks/actual'})
        self.assertIn('回灌完成。', text)
        self.assertIn('结果目录：/workstation/tasks/actual', text)
        self.assertNotIn('原始 TXT', text)
        self.assertNotIn('诊断分类', text)

    def test_evaluation_success_delivers_original_txt_and_source_identity(self):
        job = {'id': 'gw-evaluation-fixture', 'payload': {'action': 'evaluate', 'source_job': 'gw-replay-source'}}
        text = format_result(job, 'SUCCEEDED', {'task': '/workstation/tasks/actual', 'report': '/own/reports/original.txt'})
        self.assertIn('评测完成。', text)
        self.assertIn('源回灌任务：gw-replay-source', text)
        self.assertIn('原始 TXT：/own/reports/original.txt', text)
        self.assertIn('结果目录：/workstation/tasks/actual', text)

    def test_pending_evaluation_first_delivers_replay_directory(self):
        text = format_result(replay_job(), 'POSTPROCESSING', {
            'task': '/workstation/tasks/actual', 'evaluation_state': 'QUEUED',
            'evaluation_job': 'gw-evaluation-fixture'})
        self.assertIn('回灌完成，评测等待执行', text)
        self.assertIn('不占用台架队列', text)
        self.assertIn('结果目录：/workstation/tasks/actual', text)
        self.assertIn('评测任务：gw-evaluation-fixture', text)
        self.assertNotIn('原始 TXT', text)

    def test_simulated_replay_never_claims_real_execution_or_success(self):
        text = format_result(replay_job(), 'SIMULATED', {'simulated': True, 'task': 'SIMULATION/current'})
        self.assertIn('模拟验证完成（未派发台架任务）', text)
        self.assertNotIn('回灌完成。', text)
        self.assertNotIn('结果目录：', text)
        self.assertNotIn('原始 TXT：', text)

    def test_deployment_probe_attachment_is_not_misreported_as_evaluation(self):
        job = {'id': 'gw-deployment-fixture', 'payload': {'action': 'deployment_check'}}
        text = format_result(job, 'SIMULATED', {'local_report': '/own/probe.txt'})
        self.assertIn('随附 TXT 仅用于验证文件交付', text)
        self.assertIn('未回灌或评测', text)
        self.assertNotIn('评测完成', text)
        self.assertNotIn('结果目录：', text)


if __name__ == '__main__':
    unittest.main()
