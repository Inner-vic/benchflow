from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from parse_eval_report import ReportError, compare_metrics, parse_report, validate_result


class ParseEvalReportTests(unittest.TestCase):
    def test_parses_historical_real_report(self) -> None:
        report = Path(__file__).resolve().parent / "fixtures" / "eval_report.sample.txt"
        result = parse_report(report)
        metrics = result["metrics"]
        self.assertEqual(["trajectory", "vision"], result["evaluated_tasks"])
        self.assertEqual(118, metrics["candidate_frames"])
        self.assertEqual(105, metrics["completed_frames"])
        self.assertAlmostEqual(0.3831, metrics["trajectory_z_mae_cm"])
        self.assertAlmostEqual(0.9891, metrics["vision_overall_map"])

    def test_parses_trajectory_only_report(self) -> None:
        report_text = """候选预测帧: 2352
至少完成一项任务: 2075
未匹配真值时间戳: 277
Overall(occupied)   59.8646
平均绝对误差: 0.5727 cm
合格率: 87.28%
通过率: 64.56%
"""
        with TemporaryDirectory() as directory:
            report = Path(directory) / "trajectory-only.txt"
            report.write_text(report_text, encoding="utf-8")
            result = parse_report(report)
        self.assertEqual(["trajectory"], result["evaluated_tasks"])
        self.assertNotIn("vision_overall_map", result["metrics"])
        self.assertAlmostEqual(64.56, result["metrics"]["trajectory_pass_rate_percent"])

    def test_rejects_incomplete_task_metrics(self) -> None:
        report_text = """候选预测帧: 1
至少完成一项任务: 1
未匹配真值时间戳: 0
平均绝对误差: 0.5 cm
"""
        with TemporaryDirectory() as directory:
            report = Path(directory) / "incomplete.txt"
            report.write_text(report_text, encoding="utf-8")
            with self.assertRaises(ReportError):
                parse_report(report)

    def test_no_policy_does_not_invent_a_release_decision(self) -> None:
        result = compare_metrics({"vision_overall_map": 0.9}, None)
        self.assertEqual("NOT_EVALUATED", result["decision"])

    def test_explicit_policy_can_pass_or_fail(self) -> None:
        metrics = {"vision_overall_map": 0.9891, "trajectory_z_mae_cm": 0.3831}
        passed = compare_metrics(metrics, {
            "vision_overall_map": {"minimum": 0.98},
            "trajectory_z_mae_cm": {"maximum": 0.4},
        })
        failed = compare_metrics(metrics, {"trajectory_z_mae_cm": {"maximum": 0.3}})
        self.assertEqual("PASS", passed["decision"])
        self.assertEqual("FAIL", failed["decision"])

    def test_missing_required_metric_is_rejected(self) -> None:
        with self.assertRaises(ReportError):
            compare_metrics({}, {"vision_overall_map": {"minimum": 0.9}})

    def test_validity_rejects_zero_frame_success_report(self) -> None:
        parsed = {
            "metrics": {"candidate_frames": 0, "completed_frames": 0},
            "evaluated_tasks": ["vision"],
        }
        self.assertEqual("INVALID", validate_result(parsed)["decision"])

    def test_validity_requires_requested_task_coverage(self) -> None:
        parsed = {
            "metrics": {"candidate_frames": 95, "completed_frames": 17},
            "evaluated_tasks": ["vision"],
        }
        validity = validate_result(parsed, {"required_tasks": ["trajectory", "vision"]})
        self.assertEqual("INVALID", validity["decision"])
        self.assertFalse(next(item for item in validity["checks"] if item.get("task") == "trajectory")["passed"])


if __name__ == "__main__":
    unittest.main()
