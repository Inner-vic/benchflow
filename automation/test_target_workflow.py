from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from prediction_transfer import local_inventory, write_workstation_product_receipt
from runner_state import submit_job
from target_workflow import execute_target_workflow, validate_target_job


def transfer_case(root: Path):
    source_root = root / "bench"
    source = source_root / "predictions" / "scene-a"
    source.mkdir(parents=True)
    (source / "pred.bin").write_bytes(b"prediction")
    cache_root = root / "shared-products"
    lpai_root = root / "lpai"
    lpai_root.mkdir()
    profile = {
        "prediction_transfer": {
            "sets": {
                "local": {
                    "source": {"transport": "local", "allowed_root": str(source_root)},
                    "workstation_cache_root": str(cache_root),
                    "destination": {"transport": "local", "allowed_root": str(lpai_root)},
                }
            }
        }
    }
    return source, cache_root, lpai_root, profile


class TargetWorkflowTests(unittest.TestCase):
    def test_upload_only_runs_without_a_bench_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, cache_root, lpai_root, profile = transfer_case(root)
            del profile["prediction_transfer"]["sets"]["local"]["source"]
            shared = cache_root / "shared-source"
            shared.mkdir(parents=True)
            (shared / "pred.bin").write_bytes(b"prediction")
            write_workstation_product_receipt(
                shared.resolve(),
                local_inventory(shared),
                producer_run_id="producer-run",
                source="test-fixture",
            )
            job = {
                "schema_version": 1,
                "runner_protocol": "1.1",
                "run_id": "upload-only",
                "mode": "target_workflow",
                "workflow": {
                    "targets": ["lpai_predictions"],
                    "sources": {"workstation_predictions": str(shared)},
                },
                "predictions": {
                    "transfer_set_id": "local",
                    "destination_path": str(lpai_root / "upload-only"),
                },
            }
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            plan = {
                "runner_protocol": "1.1",
                "preflight": {"status": "READY", "blockers": []},
                "required_approvals": [],
                "workflow": {"requires_bench": False},
                "steps": [],
            }
            submit_job(
                job=job, source=root / "job.json", plan=plan, runs_dir=root / "runs",
                submitted_by="user", client="test",
            )
            result = execute_target_workflow(
                runs_dir=root / "runs", locks_dir=root / "locks", run_id="upload-only",
                profile_path=profile_path,
            )
            self.assertEqual("COMPLETED_TO_REQUESTED_GOAL", result["state"])
            self.assertIsNone(result["transaction"])
            self.assertTrue((lpai_root / "upload-only" / "pred.bin").is_file())
            self.assertFalse((root / "locks" / "bench.lock").exists())

    def test_workstation_only_validation_does_not_require_lpai_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _cache_root, _lpai_root, profile = transfer_case(root)
            del profile["prediction_transfer"]["sets"]["local"]["destination"]
            job = {
                "schema_version": 1,
                "runner_protocol": "1.1",
                "run_id": "collect-only",
                "mode": "target_workflow",
                "workflow": {"targets": ["workstation_predictions"]},
                "execution": {"command_set_id": "current"},
                "predictions": {
                    "transfer_set_id": "local",
                    "source_path": str(source),
                },
            }
            profile["execution"] = {
                "command_sets": {
                    "current": {
                        "strategy": "use_current_app",
                        "commands": {
                            step: {"transport": "local", "argv": ["true"]}
                            for step in ("precheck", "health_current", "replay")
                        },
                    }
                }
            }
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            resolved = validate_target_job(job, profile_path, root / "run")
            self.assertIsNotNone(resolved["collection"])
            self.assertIsNone(resolved["publication"])

    def test_declared_bench_strategy_must_match_profile_command_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _cache_root, lpai_root, profile = transfer_case(root)
            profile["execution"] = {
                "command_sets": {
                    "current": {
                        "strategy": "use_current_app",
                        "commands": {
                            step: {"transport": "local", "argv": ["true"]}
                            for step in ("precheck", "health_current", "replay")
                        },
                    }
                }
            }
            job = {
                "schema_version": 1,
                "runner_protocol": "1.1",
                "run_id": "strategy-mismatch",
                "mode": "target_workflow",
                "workflow": {
                    "targets": ["workstation_predictions"],
                    "bench_strategy": "isolated_candidate",
                },
                "execution": {"command_set_id": "current"},
                "predictions": {
                    "transfer_set_id": "local",
                    "source_path": str(source),
                    "destination_path": str(lpai_root / "strategy-mismatch"),
                },
            }
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "bench_strategy must match"):
                validate_target_job(job, profile_path, root / "run")

    def test_bench_target_requires_execution_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _cache_root, lpai_root, profile = transfer_case(root)
            job = {
                "schema_version": 1,
                "runner_protocol": "1.1",
                "run_id": "bench-target",
                "mode": "target_workflow",
                "workflow": {"targets": ["workstation_predictions"]},
                "predictions": {
                    "transfer_set_id": "local",
                    "source_path": str(source),
                    "destination_path": str(lpai_root / "bench-target"),
                },
            }
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "command_set_id"):
                validate_target_job(job, profile_path, root / "run")


if __name__ == "__main__":
    unittest.main()
