from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workflow_planner import (
    WorkflowPlanError,
    build_target_steps,
    plan_metadata,
    required_approvals,
    requested_targets,
    validate_workflow,
)
from workflow_receipts import content_identity, verify_receipt, write_receipt


class WorkflowPlannerTests(unittest.TestCase):
    def target_job(self, targets: list[str], sources: dict[str, str] | None = None) -> dict:
        workflow = {"targets": targets, "so_policy": "auto", "reuse_policy": "verified_only"}
        if sources:
            workflow["sources"] = sources
        return {
            "schema_version": 1,
            "runner_protocol": "1.1",
            "run_id": "target-run",
            "mode": "target_workflow",
            "workflow": workflow,
        }

    def test_workstation_target_requests_bench_path(self) -> None:
        job = self.target_job(["workstation_predictions"])
        steps = {item["state"]: item for item in build_target_steps(job, stage_ready=True, replay_ready=True)}
        self.assertTrue(steps["REPLAY"]["requested"])
        self.assertTrue(steps["COLLECT_WORKSTATION"]["requested"])
        self.assertFalse(steps["PUBLISH_LPAI"]["requested"])
        self.assertFalse(steps["EVALUATE"]["requested"])
        self.assertEqual(["ACTIVATE", "REPLAY"], required_approvals(job))

    def test_upload_target_from_workstation_has_no_bench_dependency(self) -> None:
        job = self.target_job(
            ["lpai_predictions"],
            {"workstation_predictions": "/mnt/data/bench-eval/products/x"},
        )
        metadata = plan_metadata(job)
        steps = {item["state"]: item for item in build_target_steps(job, stage_ready=True, replay_ready=True)}
        self.assertFalse(metadata["requires_bench"])
        self.assertEqual([], required_approvals(job))
        self.assertEqual("NOT_REQUESTED", steps["COLLECT_WORKSTATION"]["status"])
        self.assertEqual("PENDING", steps["PUBLISH_LPAI"]["status"])

    def test_evaluation_from_lpai_has_only_evaluation_dependency(self) -> None:
        job = self.target_job(
            ["evaluation_report"],
            {"lpai_predictions": "/lpai/bench-eval/predictions/x"},
        )
        steps = {item["state"]: item for item in build_target_steps(job, stage_ready=True, replay_ready=True)}
        self.assertEqual("PENDING", steps["EVALUATE"]["status"])
        self.assertEqual([], steps["EVALUATE"]["dependencies"])
        self.assertEqual("NOT_REQUESTED", steps["REPLAY"]["status"])

    def test_legacy_protocol_preserves_full_target_set(self) -> None:
        job = {"runner_protocol": "1.0"}
        self.assertEqual(
            ("workstation_predictions", "lpai_predictions", "evaluation_report"),
            requested_targets(job),
        )
        steps = build_target_steps(job, stage_ready=True, replay_ready=True)
        self.assertEqual("COLLECT", steps[9]["state"])
        self.assertNotIn("status", steps[0])

    def test_workflow_bench_strategy_is_the_only_planner_strategy_input(self) -> None:
        job = self.target_job(["workstation_predictions"])
        job["workflow"]["bench_strategy"] = "use_current_app"
        job["execution_strategy"] = "isolated_candidate"
        steps = {
            item["state"]: item
            for item in build_target_steps(job, stage_ready=True, replay_ready=True)
        }
        self.assertTrue(steps["REPLAY"]["requested"])
        self.assertFalse(steps["RESTORE"]["requested"])
        self.assertFalse(steps["ACTIVATE"]["requested"])
        self.assertFalse(steps["STAGE"]["requested"])

    def test_formal_update_requests_activation_without_success_restore(self) -> None:
        job = self.target_job(["workstation_predictions"])
        job["workflow"]["bench_strategy"] = "formal_update"
        job["workflow"]["so_policy"] = "prebuilt"
        steps = {
            item["state"]: item
            for item in build_target_steps(job, stage_ready=True, replay_ready=True)
        }
        self.assertTrue(steps["CONFIGURE"]["requested"])
        self.assertTrue(steps["ACTIVATE"]["requested"])
        self.assertTrue(steps["REPLAY"]["requested"])
        self.assertFalse(steps["RESTORE"]["requested"])
        self.assertEqual("formal_update", plan_metadata(job)["bench_strategy"])

    def test_formal_update_requires_prebuilt_identity(self) -> None:
        job = self.target_job(["workstation_predictions"])
        job["workflow"]["bench_strategy"] = "formal_update"
        self.assertIn(
            "FORMAL_UPDATE_REQUIRES_PREBUILT",
            {item["code"] for item in validate_workflow(job)},
        )

    def test_invalid_target_and_policy_are_blocked(self) -> None:
        job = self.target_job(["unknown"])
        job["workflow"]["so_policy"] = "guess"
        codes = {item["code"] for item in validate_workflow(job)}
        self.assertEqual({"INVALID_WORKFLOW_TARGET", "INVALID_SO_POLICY"}, codes)
    def test_ambiguous_or_already_satisfied_sources_are_blocked(self) -> None:
        ambiguous = self.target_job(
            ["evaluation_report"],
            {
                "workstation_predictions": "/mnt/data/products/x",
                "lpai_predictions": "/lpai/predictions/x",
            },
        )
        self.assertIn(
            "AMBIGUOUS_PREDICTION_SOURCE",
            {item["code"] for item in validate_workflow(ambiguous)},
        )
        supplied_target = self.target_job(
            ["lpai_predictions"],
            {"lpai_predictions": "/lpai/predictions/x"},
        )
        self.assertIn(
            "TARGET_ALREADY_SUPPLIED",
            {item["code"] for item in validate_workflow(supplied_target)},
        )


class WorkflowReceiptTests(unittest.TestCase):
    def test_receipt_is_reused_only_for_matching_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            identity = content_identity({"scene": "scene-a", "files": [1, 2, 3]})
            write_receipt(
                run_dir,
                "COLLECT_WORKSTATION",
                "SUCCEEDED",
                targets=["workstation_predictions"],
                input_identity="input",
                output_identity=identity,
                verification={"method": "sha256-inventory-v1"},
            )
            reused = verify_receipt(
                run_dir,
                "COLLECT_WORKSTATION",
                input_identity="input",
                output_identity=identity,
            )
            self.assertEqual("REUSED_VERIFIED", reused["status"])
            invalid = verify_receipt(run_dir, "COLLECT_WORKSTATION", output_identity="different")
            self.assertEqual("INVALID_CACHE", invalid["status"])


if __name__ == "__main__":
    unittest.main()
