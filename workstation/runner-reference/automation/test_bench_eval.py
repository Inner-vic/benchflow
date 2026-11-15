from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bench_eval import (
    RUNNER_PROTOCOL_VERSION,
    audit_workflow,
    build_plan,
    embedded_secret_fields,
    embedded_command_fields,
    evaluate_job,
    read_status,
    todo_fields,
)


class BenchEvalTests(unittest.TestCase):
    def test_preflight_separates_stage_and_replay_readiness(self) -> None:
        job = {
            "schema_version": 1,
            "runner_protocol": "1.0",
            "run_id": "run-001",
            "mode": "offline",
            "release_package": {
                "source": "/app/release.app.tar.gz",
                "sha256": "a" * 64,
                "isolated_run": "/app/livc-test/run-001",
                "activated": False,
            },
            "blocked_before_start": ["confirm approved stop command"],
        }
        result = evaluate_job(job)
        self.assertTrue(result["stage_ready"])
        self.assertFalse(result["replay_ready"])
        self.assertEqual("BLOCKED", result["status"])

    def test_todos_and_embedded_secrets_are_rejected(self) -> None:
        job = {
            "schema_version": 1,
            "runner_protocol": "1.0",
            "run_id": "run-001",
            "mode": "offline",
            "host": "TODO: alias",
            "password": "do-not-store-this",
        }
        self.assertEqual(["host"], todo_fields(job))
        self.assertEqual(["password"], embedded_secret_fields(job))
        codes = {item["code"] for item in evaluate_job(job)["blockers"]}
        self.assertEqual({"UNRESOLVED_TODO", "EMBEDDED_SECRET"}, codes)

    def test_active_stage_path_is_rejected(self) -> None:
        result = evaluate_job({"release_package": {"isolated_run": "/app/livc"}})
        self.assertIn("UNSAFE_STAGE_ROOT", {item["code"] for item in result["blockers"]})

    def test_job_cannot_embed_commands(self) -> None:
        job = {
            "schema_version": 1,
            "runner_protocol": "1.0",
            "run_id": "run-001",
            "mode": "offline",
            "execution": {"command_set_id": "approved", "argv": ["sh", "-c", "unsafe"]},
        }
        self.assertEqual(["execution.argv"], embedded_command_fields(job))
        self.assertIn("JOB_EMBEDDED_COMMAND", {item["code"] for item in evaluate_job(job)["blockers"]})

    def test_runner_protocol_mismatch_is_rejected(self) -> None:
        result = evaluate_job({"runner_protocol": "99.0"})
        self.assertIn("RUNNER_PROTOCOL_MISMATCH", {item["code"] for item in result["blockers"]})
        plan = build_plan({"run_id": "run-001", "runner_protocol": RUNNER_PROTOCOL_VERSION, "schema_version": 1, "mode": "offline", "workflow": {"targets": ["workstation_predictions"]}}, Path("job.json"))
        self.assertEqual(RUNNER_PROTOCOL_VERSION, plan["runner_protocol"])

    def test_plan_marks_mutating_approval_points(self) -> None:
        job = {"run_id": "run-001", "runner_protocol": "1.0", "blocked_before_start": []}
        plan = build_plan(job, Path("job.json"))
        approvals = [step["state"] for step in plan["steps"] if step["explicit_approval_required"]]
        self.assertEqual(["ACTIVATE", "REPLAY"], approvals)
        states = [step["state"] for step in plan["steps"]]
        self.assertLess(states.index("RESTORE"), states.index("REPORT"))
        self.assertTrue(plan["safety"]["success_requires_restore_verification_after_activation"])

    def test_target_plan_omits_bench_for_existing_workstation_product(self) -> None:
        job = {
            "schema_version": 1,
            "runner_protocol": RUNNER_PROTOCOL_VERSION,
            "run_id": "upload-only",
            "mode": "target_workflow",
            "workflow": {
                "targets": ["lpai_predictions"],
                "sources": {"workstation_predictions": "/shared/predictions/product"},
                "reuse_policy": "verified_only",
            },
        }
        plan = build_plan(job, Path("job.json"))
        self.assertEqual([], plan["required_approvals"])
        self.assertFalse(plan["workflow"]["requires_bench"])
        statuses = {step["state"]: step["status"] for step in plan["steps"]}
        self.assertEqual("PENDING", statuses["PUBLISH_LPAI"])
        self.assertEqual("NOT_REQUESTED", statuses["REPLAY"])
        self.assertEqual("NOT_REQUESTED", statuses["ACTIVATE"])

    def test_formal_update_plan_authorizes_verified_retention(self) -> None:
        job = {
            "schema_version": 1,
            "runner_protocol": RUNNER_PROTOCOL_VERSION,
            "run_id": "formal-update",
            "mode": "target_workflow",
            "workflow": {
                "targets": ["workstation_predictions"],
                "so_policy": "prebuilt",
                "reuse_policy": "verified_only",
                "bench_strategy": "formal_update",
            },
        }
        plan = build_plan(job, Path("job.json"))
        self.assertEqual("READY", plan["preflight"]["status"])
        self.assertTrue(plan["safety"]["active_path_write_allowed"])
        self.assertTrue(plan["safety"]["success_requires_formal_activation_verification"])
        self.assertFalse(plan["safety"]["success_requires_restore_verification_after_activation"])
        statuses = {step["state"]: step["status"] for step in plan["steps"]}
        self.assertEqual("PENDING", statuses["ACTIVATE"])
        self.assertEqual("NOT_REQUESTED", statuses["RESTORE"])

    def test_evaluation_target_uses_existing_lpai_product(self) -> None:
        job = {
            "schema_version": 1,
            "runner_protocol": RUNNER_PROTOCOL_VERSION,
            "run_id": "evaluate-only",
            "mode": "target_workflow",
            "workflow": {
                "targets": ["evaluation_report"],
                "sources": {"lpai_predictions": "/lpai/predictions/product"},
            },
        }
        plan = build_plan(job, Path("job.json"))
        statuses = {step["state"]: step["status"] for step in plan["steps"]}
        self.assertEqual("PENDING", statuses["EVALUATE"])
        self.assertEqual("NOT_REQUESTED", statuses["PUBLISH_LPAI"])
        self.assertEqual("NOT_REQUESTED", statuses["REPLAY"])

    def test_protocol_11_requires_targets(self) -> None:
        result = evaluate_job({
            "schema_version": 1,
            "runner_protocol": RUNNER_PROTOCOL_VERSION,
            "run_id": "missing-targets",
            "mode": "target_workflow",
        })
        self.assertIn("MISSING_WORKFLOW_TARGETS", {item["code"] for item in result["blockers"]})

    def test_audit_does_not_claim_live_end_to_end_automation(self) -> None:
        audit = audit_workflow({"run_id": "run-001", "blocked_before_start": []})
        self.assertTrue(audit["offline_development_ready"])
        self.assertFalse(audit["live_end_to_end_automated"])
        missing_stages = {item["stage"] for item in audit["missing_adapters"]}
        self.assertTrue({"ACTIVATE", "COLLECT", "EVALUATE", "RESTORE"}.issubset(missing_stages))
        self.assertEqual("IMPLEMENTED_WORKSTATION_LIDOCKER", audit["scope"]["evaluation_library_compilation"])

    def test_status_reads_last_event_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run-001"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
            (run_dir / "events.jsonl").write_text(
                json.dumps({"state": "PREFLIGHT_OK"}) + "\n" + json.dumps({"state": "STAGED"}) + "\n",
                encoding="utf-8",
            )
            status = read_status(Path(temp), "run-001")
            self.assertEqual("STAGED", status["last_event"]["state"])
            self.assertIn("manifest.json", status["artifacts"])


if __name__ == "__main__":
    unittest.main()
