from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from evaluation_runner import EvaluationError, discover_evaluation_attempts, run_evaluation
from workflow_receipts import read_receipt


class FakeRunner:
    def __init__(self, report: Path, fail: str | None = None):
        self.report = report
        self.fail = fail
        self.steps = []

    def run(self, name, _command, context):
        self.steps.append(name)
        if name == "collect_report" and self.fail != name:
            shutil.copy2(self.report, context["local_report_path"])
        return {"step": name, "ok": self.fail != name, "exit_code": 1 if self.fail == name else 0}


def case(root: Path):
    job = {
        "run_id": "eval-test",
        "replay_input": {"scene": "scene-a"},
        "evaluation": {
            "evaluation_set_id": "lpai-test",
            "prediction_path": "/lpai/predictions/eval-test",
            "config_path": "/lpai/configs/eval-test.yaml",
            "acceptance_policy": {"vision_overall_map": {"minimum": 0.98}},
        },
    }
    command = {"transport": "local", "argv": ["fixed-command"]}
    profile = {
        "evaluation": {
            "sets": {
                "lpai-test": {
                    "allowed_prediction_root": "/lpai/predictions",
                    "allowed_config_root": "/lpai/configs",
                    "commands": {"evaluate": command, "collect_report": command},
                }
            }
        }
    }
    report = Path(__file__).resolve().parent / "fixtures" / "eval_report.sample.txt"
    return job, profile, report


class EvaluationRunnerTests(unittest.TestCase):
    def test_profile_commands_run_and_real_report_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            fake = FakeRunner(report)
            result = run_evaluation(job, profile, root / "run", execute=True, runner=fake)
            self.assertEqual(["evaluate", "collect_report"], fake.steps)
            self.assertEqual("EVALUATION_COMPLETED", result["state"])
            self.assertEqual("PASS", result["acceptance"]["decision"])
            self.assertAlmostEqual(0.3831, result["metrics"]["trajectory_z_mae_cm"])

    def test_failed_evaluation_does_not_collect_or_parse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            fake = FakeRunner(report, fail="evaluate")
            result = run_evaluation(job, profile, root / "run", execute=True, runner=fake)
            self.assertEqual(["evaluate"], fake.steps)
            self.assertEqual("EVALUATION_FAILED", result["state"])

    def test_plan_does_not_execute_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            fake = FakeRunner(report)
            result = run_evaluation(job, profile, root / "run", execute=False, runner=fake)
            self.assertEqual("EVALUATION_PLANNED", result["state"])
            self.assertEqual([], fake.steps)

    def test_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, _report = case(root)
            job["evaluation"]["prediction_path"] = "/lpai/other/path"
            with self.assertRaises(EvaluationError):
                run_evaluation(job, profile, root / "run", execute=False)

    def test_successful_process_with_missing_required_task_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            job["evaluation"]["validity_policy"] = {"required_tasks": ["trajectory", "vision"]}
            fresh = root / "vision-only.txt"
            fresh.write_text(
                "候选预测帧: 95\n至少完成一项任务: 17\n未匹配真值时间戳: 16\nOverall mAP: 1.0000\n",
                encoding="utf-8",
            )
            result = run_evaluation(job, profile, root / "run", execute=True, runner=FakeRunner(fresh))
            self.assertEqual("EVALUATION_INVALID", result["state"])
            self.assertEqual("NOT_EVALUATED", result["acceptance"]["decision"])

    def test_optional_runtime_precheck_runs_before_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            profile["evaluation"]["sets"]["lpai-test"]["commands"]["precheck"] = {
                "transport": "local",
                "argv": ["fixed-precheck"],
            }
            fake = FakeRunner(report)
            result = run_evaluation(job, profile, root / "run", execute=True, runner=fake)
            self.assertEqual(["precheck", "evaluate", "collect_report"], fake.steps)
            self.assertEqual("EVALUATION_COMPLETED", result["state"])

    def test_prepare_config_runs_before_precheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            commands = profile["evaluation"]["sets"]["lpai-test"]["commands"]
            commands["prepare_config"] = {"transport": "local", "argv": ["prepare", "{scene}"]}
            commands["precheck"] = {"transport": "local", "argv": ["precheck"]}
            fake = FakeRunner(report)
            result = run_evaluation(job, profile, root / "run", execute=True, runner=fake)
            self.assertEqual(["prepare_config", "precheck", "evaluate", "collect_report"], fake.steps)
            self.assertEqual("EVALUATION_COMPLETED", result["state"])

    def test_report_capture_may_only_target_local_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, _report = case(root)
            profile["evaluation"]["sets"]["lpai-test"]["commands"]["evaluate"]["capture_stdout_to"] = "bad"
            with self.assertRaises(EvaluationError):
                run_evaluation(job, profile, root / "run", execute=False)

    def test_success_is_reused_without_running_commands_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            fake = FakeRunner(report)
            first = run_evaluation(job, profile, root / "run", execute=True, runner=fake)
            second = run_evaluation(job, profile, root / "run", execute=True, runner=fake)
            self.assertEqual("EVALUATION_COMPLETED", first["state"])
            self.assertEqual("EVALUATION_REUSED", second["state"])
            self.assertEqual(["evaluate", "collect_report"], fake.steps)
            self.assertEqual("a001", second["attempt"])
            receipt = read_receipt(root / "run", "EVALUATE")
            self.assertEqual("SUCCEEDED", receipt["state"])
            self.assertEqual("a001", receipt["attempt"])

    def test_failure_allocates_a_new_attempt_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            run_dir = root / "run"
            first = run_evaluation(
                job, profile, run_dir, execute=True,
                runner=FakeRunner(report, fail="evaluate"),
            )
            failed_evidence = run_dir / "evaluation-attempts" / "scene-a" / "a001" / "evaluation-evaluate.json"
            original = failed_evidence.read_text(encoding="utf-8")
            second = run_evaluation(job, profile, run_dir, execute=True, runner=FakeRunner(report))
            self.assertEqual("EVALUATION_FAILED", first["state"])
            self.assertEqual("a001", first["attempt"])
            self.assertEqual("EVALUATION_COMPLETED", second["state"])
            self.assertEqual("a002", second["attempt"])
            self.assertEqual(original, failed_evidence.read_text(encoding="utf-8"))
            self.assertTrue((run_dir / "evaluation-attempts" / "scene-a" / "a002" / "attempt.json").is_file())

    def test_identity_change_does_not_reuse_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            run_dir = root / "run"
            first = run_evaluation(job, profile, run_dir, execute=True, runner=FakeRunner(report))
            job["evaluation"]["prediction_path"] = "/lpai/predictions/eval-test-v2"
            second_runner = FakeRunner(report)
            second = run_evaluation(job, profile, run_dir, execute=True, runner=second_runner)
            self.assertNotEqual(first["evaluation_identity"], second["evaluation_identity"])
            self.assertEqual("a002", second["attempt"])
            self.assertEqual(["evaluate", "collect_report"], second_runner.steps)

    def test_missing_attempt_report_invalidates_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            run_dir = root / "run"
            first = run_evaluation(job, profile, run_dir, execute=True, runner=FakeRunner(report))
            Path(first["local_report_path"]).unlink()
            second = run_evaluation(job, profile, run_dir, execute=True, runner=FakeRunner(report))
            self.assertEqual("EVALUATION_COMPLETED", second["state"])
            self.assertEqual("a002", second["attempt"])

    def test_invalid_attempt_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, _report = case(root)
            job["evaluation"]["validity_policy"] = {"required_tasks": ["trajectory", "vision"]}
            vision_only = root / "vision-only.txt"
            vision_only.write_text(
                "候选预测帧: 95\n至少完成一项任务: 17\n未匹配真值时间戳: 16\nOverall mAP: 1.0000\n",
                encoding="utf-8",
            )
            first = run_evaluation(job, profile, root / "run", execute=True, runner=FakeRunner(vision_only))
            second = run_evaluation(job, profile, root / "run", execute=True, runner=FakeRunner(vision_only))
            self.assertEqual("EVALUATION_INVALID", first["state"])
            self.assertEqual("a001", first["attempt"])
            self.assertEqual("EVALUATION_INVALID", second["state"])
            self.assertEqual("a002", second["attempt"])

    def test_discovery_reports_failed_and_reusable_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, profile, report = case(root)
            run_dir = root / "run"
            run_evaluation(job, profile, run_dir, execute=True, runner=FakeRunner(report, fail="evaluate"))
            successful = run_evaluation(job, profile, run_dir, execute=True, runner=FakeRunner(report))
            attempts = discover_evaluation_attempts(
                run_dir,
                identity=successful["evaluation_identity"],
                validity_policy=job["evaluation"].get("validity_policy"),
            )
            self.assertEqual(["a001", "a002"], [item["attempt"] for item in attempts])
            self.assertEqual([False, True], [item["reusable"] for item in attempts])


if __name__ == "__main__":
    unittest.main()
