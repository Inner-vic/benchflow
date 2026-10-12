"""Safety and failure behavior for one-lock multi-scene replay."""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
from uuid import uuid4

from batch_lease import BatchLeaseError, common_identity, replay_batch, sha256, validate_gate, validated_inputs
from bench_transaction import execute_profile_transaction
from prepare_batch_lease import prepare
from prediction_transfer import local_inventory, write_workstation_product_receipt
from recover_batch_prefix import recover


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def child_job(run_id: str, scene: str) -> dict:
    return {
        "schema_version": 1, "runner_protocol": "1.1", "run_id": run_id, "mode": "target_workflow",
        "workflow": {"targets": ["workstation_predictions"], "so_policy": "installed",
                     "bench_strategy": "isolated_candidate"},
        "app_artifact": {"sha256": "a" * 64},
        "lockfile_artifact": {"sha256": "b" * 64},
        "artifacts": {"evaluation_library": {"sha256": "c" * 64}},
        "configuration_input": {"sha256": "d" * 64},
        "configuration_policy": {"reviewed_offline_keys": ["eval_dump.enable"]},
        "replay_input": {"dataset_root": "/data", "prepared_manifest": "/manifest.json", "scene": scene},
        "predictions": {"source_path": f"/app/outputs/batch/{scene}"},
    }


class BatchLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        test_root = Path(__file__).resolve().parent / "runs" / "batch-lease-dev-20260928-001" / "test-tmp"
        test_root.mkdir(parents=True, exist_ok=True)
        root = test_root / f"case-{uuid4().hex}"
        root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(root) if root.resolve().parent == test_root.resolve() else None)
        self.batch_dir = root / "batch"
        self.run_dir = root / "runs" / "batch-held-001"
        self.locks_dir = root / "locks"
        self.adapter = root / "adapter.py"
        self.adapter.write_text(
            "import argparse,json,sys\n"
            "p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('--parent-run-dir');"
            "p.add_argument('--job');p.add_argument('--scene');a=p.parse_args()\n"
            "if a.scene == 'bad': print('scene failed',file=sys.stderr);sys.exit(3)\n"
            "print(json.dumps({'state':'SUCCEEDED','scene':a.scene,'inventory_sha256':'e'*64}))\n",
            encoding="utf-8",
        )
        self.adapter_sha = sha256(self.adapter)
        self.scenes = ["one", "two", "three"]
        self.build()

    def build(self) -> None:
        entries = []
        hashes = {}
        for index, scene in enumerate(self.scenes, 1):
            run_id = f"batch-s{index:03d}"
            path = self.batch_dir / "jobs" / f"{run_id}.json"
            write(path, child_job(run_id, scene))
            hashes[run_id] = sha256(path)
            entries.append({"scene_number": index, "scene": scene, "run_id": run_id,
                            "job_file": f"jobs/{run_id}.json"})
        manifest_path = self.batch_dir / "batch.json"
        write(manifest_path, {"schema_version": 1, "batch_id": "batch", "scenes": entries})
        gate_run = self.batch_dir.parent / "runs" / "gate-001"
        write(gate_run / "job.json", child_job("gate-001", "gate"))
        write(gate_run / "state.json", {"state": "COMPLETED_TO_REQUESTED_GOAL"})
        write(gate_run / "restore-verification.json", {"restore_ok": True})
        write(gate_run / "receipts" / "report.json", {"state": "SUCCEEDED"})
        write(gate_run / "receipts" / "collect-workstation.json", {"state": "SUCCEEDED"})
        parent = child_job(self.run_dir.name, "batch")
        parent["predictions"]["source_path"] = "/app/outputs/batch"
        parent["batch_lease"] = {
            "batch_id": "batch", "manifest_path": str(manifest_path),
            "manifest_sha256": sha256(manifest_path), "child_job_sha256": hashes,
            "common_identity_sha256": common_identity(child_job("gate-001", "gate")),
            "gate": {"run_dir": str(gate_run)},
            "max_replay_seconds": 600,
            "required_output_types": ["pred_traj", "traj_xy", "pred_img"],
        }
        write(self.run_dir / "job.json", parent)
        write(self.run_dir / "state.json", {"state": "RUNNING"})
        write(self.locks_dir / "bench.lock", {"run_id": self.run_dir.name})

    def test_three_scenes_keep_one_parent_lock_and_write_evidence(self) -> None:
        result = replay_batch(self.run_dir, self.locks_dir, self.adapter, self.adapter_sha, 10)
        self.assertEqual(result["state"], "SCENES_COMPLETED")
        self.assertEqual(result["scene_count"], 3)
        self.assertTrue((self.locks_dir / "bench.lock").is_file())
        for index in range(1, 4):
            receipt = self.run_dir / "batch-scene-results" / f"batch-s{index:03d}.json"
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["state"], "SUCCEEDED")
        with self.assertRaisesRegex(BatchLeaseError, "already exists"):
            replay_batch(self.run_dir, self.locks_dir, self.adapter, self.adapter_sha, 10)

    def test_failure_stops_later_scenes_and_preserves_receipts(self) -> None:
        self.scenes = ["one", "bad", "three"]
        self.build()
        with self.assertRaisesRegex(BatchLeaseError, "batch-s002"):
            replay_batch(self.run_dir, self.locks_dir, self.adapter, self.adapter_sha, 10)
        self.assertEqual(json.loads((self.run_dir / "batch-scene-results/batch-s001.json").read_text())["state"], "SUCCEEDED")
        self.assertEqual(json.loads((self.run_dir / "batch-scene-results/batch-s002.json").read_text())["state"], "FAILED")
        self.assertFalse((self.run_dir / "batch-scene-results/batch-s003.json").exists())
        self.assertTrue((self.locks_dir / "bench.lock").exists())

    def test_changed_input_and_wrong_lock_owner_are_blocked(self) -> None:
        first = self.batch_dir / "jobs/batch-s001.json"
        first.write_text(first.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(BatchLeaseError, "changed"):
            validated_inputs(self.run_dir, self.locks_dir, self.adapter, self.adapter_sha)
        self.build()
        write(self.locks_dir / "bench.lock", {"run_id": "other-run"})
        with self.assertRaisesRegex(BatchLeaseError, "does not own"):
            validated_inputs(self.run_dir, self.locks_dir, self.adapter, self.adapter_sha)

    def test_gate_product_must_be_verified(self) -> None:
        gate_report = self.batch_dir.parent / "runs/gate-001/receipts/collect-workstation.json"
        write(gate_report, {"state": "FAILED"})
        with self.assertRaisesRegex(BatchLeaseError, "Gate workstation"):
            validated_inputs(self.run_dir, self.locks_dir, self.adapter, self.adapter_sha)

    def test_gate_accepts_only_verified_collection_recovery(self) -> None:
        gate = self.batch_dir.parent / "runs/gate-001"
        write(gate / "state.json", {"state": "WORKSTATION_COLLECTION_FAILED"})
        write(gate / "logs/replay.json", {"ok": True})
        (gate / "events.jsonl").write_text(
            json.dumps({"event": "BENCH_RELEASED", "reason": "RESTORE_VERIFIED"}) + "\n"
            + json.dumps({"event": "WORKSTATION_COLLECTION_FAILED",
                          "message": "hop-1 SHA-256 inventory mismatch; partial cache preserved"}) + "\n",
            encoding="utf-8",
        )
        product = self.batch_dir.parent / "predictions"
        product.mkdir()
        (product / "one.npy").write_bytes(b"test")
        gate_source = read_json(gate / "job.json")["predictions"]["source_path"]
        receipt = write_workstation_product_receipt(
            product, local_inventory(product), producer_run_id=gate.name, source=gate_source)
        recovery = gate / "recovery-001"
        write(recovery / "workstation-predictions.json", {
            "state": "WORKSTATION_PREDICTIONS_REUSED", "run_id": gate.name,
            "producer_run_id": gate.name, "workstation_product": str(product),
            "source": gate_source, "inventory_sha256": receipt["inventory_sha256"],
        })
        identity = common_identity(read_json(gate / "job.json"))
        validate_gate(gate, identity, recovery)
        write(recovery / "workstation-predictions.json", {
            "state": "WORKSTATION_PREDICTIONS_REUSED", "run_id": gate.name,
            "producer_run_id": gate.name, "workstation_product": str(product),
            "source": gate_source, "inventory_sha256": "0" * 64,
        })
        with self.assertRaisesRegex(BatchLeaseError, "differs"):
            validate_gate(gate, identity, recovery)

    def test_frozen_runner_transaction_restores_after_held_batch(self) -> None:
        parent = json.loads((self.run_dir / "job.json").read_text(encoding="utf-8"))
        parent["execution"] = {"command_set_id": "held-batch"}
        write(self.run_dir / "job.json", parent)
        noop = self.batch_dir.parent / "noop.py"
        noop.write_text("import sys\nprint(sys.argv[1])\n", encoding="utf-8")
        tool = Path(__file__).resolve().parent / "batch_lease.py"
        commands = {}
        for step in ("precheck", "capture_baseline", "stop_active", "start_candidate",
                     "health_candidate", "replay", "restore", "verify_restore"):
            if step in {"precheck", "replay"}:
                argv = [sys.executable, str(tool), step, "--run-dir", "{run_dir}",
                        "--locks-dir", str(self.locks_dir), "--adapter", str(self.adapter),
                        "--adapter-sha256", self.adapter_sha]
            else:
                argv = [sys.executable, str(noop), step]
            commands[step] = {"transport": "local", "argv": argv, "timeout_seconds": 60}
        profile = self.batch_dir.parent / "profile.json"
        write(profile, {"execution": {"command_sets": {
            "held-batch": {"strategy": "isolated_candidate", "commands": commands}
        }}})
        result = execute_profile_transaction(runs_dir=self.run_dir.parent, locks_dir=self.locks_dir,
                                             run_id=self.run_dir.name, profile_path=profile)
        self.assertEqual(result["status"], "PROCESSED")
        self.assertFalse((self.locks_dir / "bench.lock").exists())
        self.assertTrue((self.run_dir / "restore-verification.json").is_file())
        self.assertTrue((self.run_dir / "batch-scene-summary.json").is_file())

    def test_parent_job_is_pinned_and_long_batch_is_split(self) -> None:
        parent = json.loads((self.run_dir / "job.json").read_text(encoding="utf-8"))
        parent.pop("batch_lease")
        parent["execution"] = {"command_set_id": "held-batch"}
        template = self.batch_dir.parent / "parent-template.json"
        write(template, parent)
        commands = {step: {"transport": "local", "argv": [sys.executable, "-c", "print('ok')"],
                           "timeout_seconds": 1200 if step == "replay" else 60}
                    for step in ("precheck", "capture_baseline", "stop_active", "start_candidate",
                                 "health_candidate", "replay", "restore", "verify_restore")}
        profile = self.batch_dir.parent / "parent-profile.json"
        write(profile, {"execution": {"command_sets": {
            "held-batch": {"strategy": "isolated_candidate", "commands": commands}
        }}})
        gate = self.batch_dir.parent / "runs/gate-001"
        output = self.batch_dir.parent / "parent-ready.json"
        result = prepare(batch_dir=self.batch_dir, gate_run=gate, parent_template=template,
                         profile_path=profile, output=output, gate_replay_seconds=120,
                         adapter_mode="scene", required_output_types=("pred_traj", "traj_xy", "pred_img"))
        self.assertEqual(result["state"], "PARENT_JOB_READY")
        self.assertEqual(len(read_json(output)["batch_lease"]["child_job_sha256"]), 3)
        with self.assertRaisesRegex(BatchLeaseError, "single-command limit"):
            prepare(batch_dir=self.batch_dir, gate_run=gate, parent_template=template,
                    profile_path=profile, output=self.batch_dir.parent / "too-long.json",
                    gate_replay_seconds=1000, adapter_mode="scene",
                    required_output_types=("pred_traj", "traj_xy", "pred_img"))

    def test_one_sweep_process_holds_lock_and_records_scene_prefix(self) -> None:
        sweep = self.batch_dir.parent / "sweep.py"
        sweep.write_text(
            "import argparse,json,sys\n"
            "p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('--parent-run-dir');"
            "p.add_argument('--batch-manifest');a=p.parse_args()\n"
            "scenes=json.load(open(a.batch_manifest))['scenes'];done=[]\n"
            "for e in scenes:\n"
            " if e['scene']=='bad': break\n"
            " done.append({'scene':e['scene'],'counts':{'pred_traj':2,'traj_xy':2,'pred_img':2}})\n"
            "print(json.dumps({'state':'SUCCEEDED' if len(done)==len(scenes) else 'FAILED','completed':done}))\n"
            "sys.exit(0 if len(done)==len(scenes) else 3)\n",
            encoding="utf-8",
        )
        parent = read_json(self.run_dir / "job.json")
        parent["batch_lease"].update(adapter_mode="sweep",
                                     required_output_types=["pred_traj", "traj_xy", "pred_img"])
        write(self.run_dir / "job.json", parent)
        result = replay_batch(self.run_dir, self.locks_dir, sweep, sha256(sweep), 10)
        self.assertEqual(result["state"], "SCENES_REPLAYED")
        self.assertEqual(len(list((self.run_dir / "batch-scene-results").glob("*.json"))), 3)
        self.assertTrue((self.locks_dir / "bench.lock").exists())

    def test_sweep_failure_preserves_completed_prefix(self) -> None:
        self.scenes = ["one", "bad", "three"]
        self.build()
        sweep = self.batch_dir.parent / "sweep.py"
        sweep.write_text(
            "import argparse,json,sys\n"
            "p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('--parent-run-dir');"
            "p.add_argument('--batch-manifest');a=p.parse_args()\n"
            "scenes=json.load(open(a.batch_manifest))['scenes'];first=scenes[0]\n"
            "print(json.dumps({'state':'FAILED','completed':[{'scene':first['scene'],"
            "'counts':{'pred_traj':2,'traj_xy':2,'pred_img':2}}]}));sys.exit(3)\n",
            encoding="utf-8",
        )
        parent = read_json(self.run_dir / "job.json")
        parent["batch_lease"].update(adapter_mode="sweep",
                                     required_output_types=["pred_traj", "traj_xy", "pred_img"])
        write(self.run_dir / "job.json", parent)
        with self.assertRaisesRegex(BatchLeaseError, "batch-s002"):
            replay_batch(self.run_dir, self.locks_dir, sweep, sha256(sweep), 10)
        self.assertEqual(read_json(self.run_dir / "batch-scene-results/batch-s001.json")["state"],
                         "REPLAY_COMPLETED")
        self.assertFalse((self.run_dir / "batch-scene-results/batch-s003.json").exists())
        self.assertEqual(read_json(self.run_dir / "batch-sweep-failure.json")["failed_run_id"], "batch-s002")

    def test_completed_prefix_collects_after_verified_restore_without_replay(self) -> None:
        replay_batch(self.run_dir, self.locks_dir, self.adapter, self.adapter_sha, 10)
        write(self.run_dir / "restore-verification.json",
              {"run_id": self.run_dir.name, "restore_ok": True})
        (self.locks_dir / "bench.lock").unlink()
        profile = self.batch_dir.parent / "profile.json"
        write(profile, {"prediction_transfer": {}})
        with patch("recover_batch_prefix.collect_predictions_to_workstation") as collection:
            collection.return_value = {
                "state": "WORKSTATION_PREDICTIONS_COLLECTED",
                "workstation_product": "/workstation/predictions",
                "inventory_sha256": "f" * 64,
            }
            result = recover(self.run_dir, self.locks_dir, self.adapter,
                             self.adapter_sha, profile)
        self.assertEqual(result["scene_count"], 3)
        self.assertEqual(collection.call_count, 3)
        self.assertEqual(collection.call_args_list[0].args[0]["replay_input"]["scene"], "one")

    def test_prefix_recovery_refuses_a_live_bench_lock(self) -> None:
        write(self.run_dir / "restore-verification.json",
              {"run_id": self.run_dir.name, "restore_ok": True})
        with self.assertRaisesRegex(BatchLeaseError, "bench lock remains"):
            recover(self.run_dir, self.locks_dir, self.adapter, self.adapter_sha,
                    self.batch_dir.parent / "profile.json")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
