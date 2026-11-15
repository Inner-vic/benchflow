from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from batch_plan import BatchError, batch_status, build_batch, create_batch, submit_batch


def base_job() -> dict:
    return {
        "schema_version": 1,
        "runner_protocol": "1.0",
        "run_id": "{run_id}",
        "mode": "full_dataset_offline_evaluation",
        "execution": {"command_set_id": "approved-full-scene"},
        "replay_input": {"archive": "/data/raw.tar.gz", "sha256": "a" * 64},
        "release_package": {
            "source": "/data/app.tar.gz",
            "sha256": "b" * 64,
            "isolated_run": "/app/livc-test/{run_id}",
        },
        "predictions": {
            "transfer_set_id": "approved-transfer",
            "source_path": "/app/livc-test/{run_id}/outputs/eval_pred_result",
            "destination_path": "/lpai/predictions/{batch_id}/{scene}",
        },
        "evaluation": {
            "evaluation_set_id": "approved-eval",
            "prediction_path": "/lpai/predictions/{batch_id}/{scene}",
            "config_path": "/lpai/runs/{run_id}/evaluation.yaml",
        },
        "blocked_before_start": [],
    }


def inventory() -> dict:
    return {
        "archive": "/data/raw.tar.gz",
        "scenes": {
            "2026-07-07_09-49-21_TEST": {"files": 12, "bytes": 100},
            "2026-07-07_10-04-07_TEST": {"files": 12, "bytes": 200},
        },
    }


class BatchPlanTests(unittest.TestCase):
    def test_builds_unique_scene_jobs(self) -> None:
        manifest, jobs = build_batch(base_job(), inventory(), "batch-001")
        self.assertEqual(2, manifest["inventory"]["scene_count"])
        self.assertEqual(300, manifest["inventory"]["unpacked_bytes"])
        self.assertEqual("batch-001-s001", jobs[0]["run_id"])
        self.assertNotEqual(
            jobs[0]["predictions"]["destination_path"],
            jobs[1]["predictions"]["destination_path"],
        )
        self.assertEqual(0, jobs[0]["batch"]["scene_index"])

    def test_rejects_shared_prediction_destination(self) -> None:
        job = base_job()
        job["predictions"]["destination_path"] = "/lpai/predictions/fixed"
        with self.assertRaises(BatchError):
            build_batch(job, inventory(), "batch-001")

    def test_plan_status_and_dry_run_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "base.json"
            inv = root / "inventory.json"
            base.write_text(json.dumps(base_job()), encoding="utf-8")
            inv.write_text(json.dumps(inventory()), encoding="utf-8")
            batch_dir = root / "batch"
            create_batch(base, inv, "batch-001", batch_dir, None)
            status = batch_status(batch_dir, root / "runs")
            self.assertEqual("PLANNED", status["status"])
            self.assertEqual(2, status["counts"]["not_submitted"])
            submission = submit_batch(batch_dir, root / "runs", "tester", "unit-test", False)
            self.assertEqual("DRY_RUN", submission["status"])
            self.assertEqual(2, len(submission["would_submit"]))


if __name__ == "__main__":
    unittest.main()
