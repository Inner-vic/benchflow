"""Verify task indexing creates independent files and includes the run YAML."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("index_workstation_task.py")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class TaskCopyTest(unittest.TestCase):
    def test_predictions_and_yaml_are_regular_independent_copies(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            task_id = "sample-task"
            run_id = task_id + "-s001"
            product = root / "cache" / "predictions" / run_id / "predictions"
            source_file = product / "frame" / "prediction.npy"
            source_file.parent.mkdir(parents=True)
            source_file.write_bytes(b"prediction-data")
            files = [{"path": "frame/prediction.npy", "size": source_file.stat().st_size,
                      "sha256": hashlib.sha256(source_file.read_bytes()).hexdigest()}]
            inventory = digest(files)
            identity = digest({"product_type": "workstation_predictions", "files": files})
            receipt = {
                "schema_version": 1, "product_type": "workstation_predictions",
                "state": "SUCCEEDED", "producer_run_id": run_id,
                "workstation_product": str(product), "inventory_sha256": inventory,
                "product_identity": identity, "files": files,
            }
            product.with_name("predictions.bench-eval-product.json").write_text(json.dumps(receipt))
            verification = root / "verification.json"
            verification.write_text(json.dumps({"state": "PASS", "scenes": 1, "results": [{
                "status": "PASS", "scene": "scene-one", "run_id": run_id,
                "file_count": 1, "inventory_sha256": inventory,
                "product_identity": identity,
            }]}))
            yaml = root / "effective.yaml"
            yaml.write_text("eval_dump:\n  enable: ON\n")
            manifest = root / "yaml-manifest.json"
            manifest.write_text(json.dumps({"effective_yaml_by_scene": {"s001": str(yaml)}}))
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--task-id", task_id,
                 "--verification", str(verification), "--yaml-manifest", str(manifest),
                 "--root", str(root), "--apply"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            task = root / "tasks" / task_id
            copy = task / "scenes" / "s001" / "predictions" / "frame" / "prediction.npy"
            self.assertFalse(copy.is_symlink())
            self.assertEqual(copy.read_bytes(), b"prediction-data")
            self.assertEqual((task / "inputs" / "yaml" / "s001.effective.yaml").read_text(), yaml.read_text())
            copy.write_bytes(b"changed")
            self.assertEqual(source_file.read_bytes(), b"prediction-data")


if __name__ == "__main__":
    unittest.main()
