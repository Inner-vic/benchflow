import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import runner_workflow as workflow


def access_report():
    return {
        "contract": "bench-workstation-access/v1", "status": "READY",
        "safe_to_submit": True, "mutations_performed": False,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z",
    }


class GenericWorkflowTests(unittest.TestCase):
    def test_variable_package_scene_and_targets_are_not_pinned_to_sw0058(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "profile.json"
            profile.write_text("{}")
            job = {
                "run_id": "other-package-scene-071", "runner_protocol": "1.1",
                "mode": "target_workflow", "workflow": {
                    "targets": ["evaluation_report"],
                    "sources": {"lpai_predictions": "/other/product"},
                },
                "app_artifact": {"path": "/other/app", "sha256": "a" * 64},
                "replay_input": {"representative_scene": "other-scene"},
            }
            path = root / "job.json"
            path.write_text(json.dumps(job))
            self.assertEqual((job["run_id"], job), workflow.job_identity(path, profile))

    def test_prepare_uses_dynamic_bench_dependency_and_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job_path, profile, access = (root / name for name in ("job.json", "profile.json", "access.json"))
            job_path.write_text(json.dumps({"run_id": "new-run-71", "runner_protocol": "1.1",
                                            "mode": "target_workflow", "workflow": {
                                                "targets": ["evaluation_report"],
                                                "sources": {"lpai_predictions": "/product"}}}))
            profile.write_text("{}")
            access.write_text(json.dumps(access_report()))
            def fake_runner(*args, **_kwargs):
                if args[0] == "capabilities":
                    return {"runner_version": "1.8.2", "runner_protocol": "1.1"}
                if args[0] == "preflight":
                    return {"status": "READY", "replay_ready": True, "approval_required_for": []}
                return {"preflight": {"status": "READY"}, "workflow": {"requires_bench": False}}
            fake_target = types.SimpleNamespace(validate_target_job=lambda *_args: {
                "targets": {"evaluation_report"}, "sources": {"lpai_predictions": "/product"},
                "requires_bench": False,
            })
            with patch.object(workflow, "RUN_ROOT", root), patch.object(workflow, "WORK_ROOT", root / "work"), \
                    patch.object(workflow, "runner", side_effect=fake_runner), \
                    patch.dict(sys.modules, {"target_workflow": fake_target}):
                receipt = workflow.prepare(job_path, profile, access)
            self.assertEqual("READY_TO_SUBMIT", receipt["state"])
            self.assertFalse(receipt["requires_bench"])
            self.assertEqual([], receipt["approval_required_for"])
            self.assertEqual(["evaluation_report"], receipt["targets"])

    def test_publication_accepts_compatible_runner_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job_path, profile, access = (root / name for name in ("job.json", "profile.json", "access.json"))
            job_path.write_text(json.dumps({
                "run_id": "upload-only-74", "runner_protocol": "1.1", "mode": "target_workflow",
                "workflow": {"targets": ["lpai_predictions"],
                             "sources": {"workstation_predictions": "/shared/predictions"}},
            }))
            profile.write_text("{}")
            access.write_text(json.dumps(access_report()))
            version = "1.8.1"

            def fake_runner(*args, **_kwargs):
                if args[0] == "capabilities":
                    return {"runner_version": version, "runner_protocol": "1.1"}
                if args[0] == "preflight":
                    return {"status": "READY", "replay_ready": True, "approval_required_for": []}
                return {"preflight": {"status": "READY"}, "workflow": {"requires_bench": False}}

            fake_target = types.SimpleNamespace(validate_target_job=lambda *_args: {
                "targets": {"lpai_predictions"},
                "sources": {"workstation_predictions": "/shared/predictions"},
                "requires_bench": False,
            })
            with patch.object(workflow, "RUN_ROOT", root), patch.object(workflow, "WORK_ROOT", root / "work"), \
                    patch.object(workflow, "runner", side_effect=fake_runner), \
                    patch.dict(sys.modules, {"target_workflow": fake_target}):
                with self.assertRaisesRegex(workflow.WorkflowError, "Runner version or protocol differs"):
                    workflow.prepare(job_path, profile, access)
                version = "1.8.2"
                with patch.object(workflow, "write_once"):
                    receipt = workflow.prepare(job_path, profile, access)
                self.assertEqual("1.8.2", receipt["runner_version"])
                self.assertFalse(receipt["requires_bench"])
                for version in ("1.8.3", "1.9.0"):
                    with patch.object(workflow, "write_once"):
                        self.assertEqual(version, workflow.prepare(job_path, profile, access)["runner_version"])
                for version in ("2.0.0", "1.8.1", "invalid"):
                    with self.assertRaises(workflow.WorkflowError):
                        workflow.prepare(job_path, profile, access)

    def test_execute_only_approves_stages_in_prepared_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job_path = root / "job.json"
            job_path.write_text(json.dumps({"coordination": {"allowed_actors": ["alice"]}}))
            receipt = {"job_path": str(job_path), "profile_path": str(root / "profile.json"),
                       "requires_personal_lpai": False,
                       "approval_required_for": ["ACTIVATE", "REPLAY"]}
            calls = []
            def fake_runner(*args, **_kwargs):
                calls.append(args)
                if args[0] == "queue":
                    return {"active": None, "queued": [], "waiting": []}
                if args[0] == "worker":
                    return {"status": "PROCESSED", "run_id": "new-run-72"}
                if args[0] == "status":
                    return {"runner": {"run": {"state": "COMPLETED_TO_REQUESTED_GOAL"}}}
                return {"state": "READY"}
            with patch.object(workflow, "WORK_ROOT", root), \
                    patch.object(workflow, "prepared", return_value=receipt), \
                    patch.object(workflow, "runner", side_effect=fake_runner):
                result = workflow.execute("new-run-72", "alice", "codex-windows")
            self.assertEqual("new-run-72", result["worker"]["run_id"])
            self.assertEqual(["ACTIVATE", "REPLAY"], [call[call.index("--stage") + 1]
                                                    for call in calls if call[0] == "approve"])

    def test_submitter_does_not_require_optional_allowed_actors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = root / "job.json"
            job.write_text(json.dumps({"coordination": {"allowed_actors": ["original-owner"]}}))
            receipt = {"job_path": str(job), "profile_path": str(root / "profile.json"),
                       "requires_personal_lpai": False, "approval_required_for": []}
            def fake_runner(*args, **_kwargs):
                if args[0] == "queue":
                    return {"active": None, "queued": [], "waiting": []}
                if args[0] == "worker":
                    return {"status": "PROCESSED", "run_id": "new-run-73"}
                if args[0] == "status":
                    return {"runner": {"run": {"state": "COMPLETED_TO_REQUESTED_GOAL"}}}
                return {}
            with patch.object(workflow, "WORK_ROOT", root), \
                    patch.object(workflow, "prepared", return_value=receipt), \
                    patch.object(workflow, "runner", side_effect=fake_runner):
                result = workflow.execute("new-run-73", "alice", "codex-windows")
            self.assertEqual("PROCESSED", result["worker"]["status"])


if __name__ == "__main__":
    unittest.main()
