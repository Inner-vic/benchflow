"""Offline checks for the unified one-run Stream batch workflow."""

import hashlib
import argparse
import json
import shutil
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import run_stream_batch as batch


@contextmanager
def workspace_temp():
    parent = Path(__file__).parent.resolve()
    path = parent / ("_batch_test_" + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield str(path)
    finally:
        if path.resolve().parent != parent:
            raise RuntimeError("test directory left the skill workspace")
        shutil.rmtree(path)


class StreamBatchTests(unittest.TestCase):
    def test_blocker_report_survives_nonzero_exit(self):
        response = mock.Mock(returncode=2, stdout='{"issues":[{"code":"BENCH_OCCUPIED"}]}', stderr="")
        with mock.patch.object(batch.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "BENCH_OCCUPIED"):
                batch.command("doctor")

    def test_long_worker_is_not_killed_before_restore(self):
        with workspace_temp() as temporary:
            root = Path(temporary)
            (root / "runs/new-run").mkdir(parents=True)
            worker = mock.MagicMock()
            worker.__enter__.return_value = worker
            worker.poll.side_effect = [None, 0]
            worker.returncode = 0
            queue = {"queued": [{"run_id": "new-run", "state": "READY"}]}
            replies = [{"status": "READY"}, {}, {}, {}, {}, queue]
            p = {"run_id": "new-run", "actor": "colleague", "scenes": [{"scene": "scene-a"}]}
            with mock.patch.object(batch, "ROOT", root), \
                    mock.patch.object(batch, "command", side_effect=replies), \
                    mock.patch.object(batch, "bench_capacity"), \
                    mock.patch.object(batch, "offline_validate"), \
                    mock.patch.object(batch, "prepare", return_value=(root / "job.json", root / "profile.json")), \
                    mock.patch.object(batch, "RunProgress"), \
                    mock.patch.object(batch, "progress") as progress, \
                    mock.patch.object(batch, "finish_delivery", return_value={"state": "PASS"}), \
                    mock.patch.object(batch.subprocess, "Popen", return_value=worker), \
                    mock.patch.object(batch.time, "monotonic", side_effect=[0, 7201]), \
                    mock.patch.object(batch.time, "sleep"):
                self.assertEqual("PASS", batch.execute(p)["state"])
                self.assertTrue(any("7200" in call.args[0] for call in progress.call_args_list))
            worker.kill.assert_not_called()
    def test_new_actor_can_plan_without_source_admission(self):
        with workspace_temp() as temporary:
            camera = Path(temporary) / "camera-s001"
            camera.mkdir()
            (camera / "frame.jpg").write_bytes(b"frame")
            job = {"coordination": {"allowed_actors": ["original-owner"]}}
            args = argparse.Namespace(source_run="source", run_id="new-run",
                task_id="new-task", actor="colleague", scenes="all", resume_delivery=False)
            contract = (job, {}, "", [{"scene": "scene-a", "camera_frames": 1}], temporary + "/")
            with mock.patch.object(batch, "source_contract", return_value=contract), \
                    mock.patch.object(batch, "derive", return_value=({}, {}, "")):
                result = batch.plan(args)
            self.assertEqual(result["actor"], "colleague")
            self.assertEqual(job["coordination"]["allowed_actors"], ["original-owner"])

    def test_derived_job_records_current_actor_and_preserves_resume(self):
        source = "source-run"
        job = {"run_id": source, "execution": {"command_set_id": source},
            "coordination": {"allowed_actors": ["original-owner"]},
            "replay_input": {"representative_scene": "scene-a"},
            "batch_stream": {"batch_id": source}, "diagnostic": {}}
        profile = {"profile_id": source, "execution": {"command_sets": {
            source: {"commands": {"replay": {"timeout_seconds": 3600}}}}}}
        selection = {"source_run": source, "run_id": "new-run", "actor": "colleague",
            "job": job, "profile": profile, "scene_count": 1,
            "scenes": [{"scene": "scene-a", "camera": "/data/camera-s001", "archive_stats": {}}],
            "adapter": 'SCENES = frozenset({"scene-a"})\n'}
        derived, _, _ = batch.derive(selection)
        self.assertEqual(derived["coordination"]["allowed_actors"], ["colleague"])
        self.assertEqual(job["coordination"]["allowed_actors"], ["original-owner"])
        selection["resume_delivery"] = True
        resumed, _, _ = batch.derive(selection)
        self.assertEqual(resumed["coordination"]["allowed_actors"], ["original-owner"])
        selection["resume_coordination"] = derived["coordination"]
        resumed, _, _ = batch.derive(selection)
        self.assertEqual(resumed, derived)

    def test_source_requires_verified_restore_and_transfer(self):
        with workspace_temp() as temporary:
            original_base, original_root = batch.BASE, batch.ROOT
            base, root = Path(temporary) / "base", Path(temporary) / "root"
            batch.BASE, batch.ROOT = base, root
            try:
                root.mkdir()
                source = "source-run"
                source_dir = base / "adapters" / source
                source_dir.mkdir(parents=True)
                manifest = root / "manifest.json"
                batch.write(manifest, {"prepared_scenes": [{"scene": "scene-a", "camera_frames": 1}]})
                batch.write(source_dir / "job.json", {
                    "run_id": source, "workflow": {"targets": ["workstation_predictions"],
                    "bench_strategy": "isolated_candidate"},
                    "replay_input": {"scene_count": 1, "prepared_manifest": str(manifest),
                                     "representative_scene": "scene-a",
                                     "archive_stats": {"scene": "scene-a", "camera_frames": 1,
                                                       "timestamped_jpeg_frames": "/data/camera-s001"}},
                    "batch_stream": {"mode": "stream", "scenes": [{
                        "timestamped_jpeg_frames": "/data/camera-s001"}]},
                })
                batch.write(source_dir / "profile.json", {"profile_id": source})
                (source_dir / "small_stream_adapter.py").write_text(
                    'SCENES = frozenset({"scene-a"})\n# len(entries) != 1\n')
                (source_dir / "sw0058_gate_probe_driver_reference.py").write_text("# driver\n")
                run_dir = root / "runs" / source
                run_dir.mkdir(parents=True)
                batch.write(run_dir / "state.json", {"state": "COMPLETED_TO_REQUESTED_GOAL"})
                batch.write(run_dir / "restore-verification.json", {"restore_ok": False})
                batch.write(run_dir / "workstation-predictions.json", {
                    "state": "WORKSTATION_PREDICTIONS_COLLECTED",
                    "inventory_sha256": "inventory", "product_identity": "identity", "file_count": 1})
                receipt = root / "cache/predictions" / source / "predictions.bench-eval-product.json"
                receipt.parent.mkdir(parents=True)
                batch.write(receipt, {"state": "SUCCEEDED", "producer_run_id": source,
                                      "inventory_sha256": "inventory", "product_identity": "identity",
                                      "files": [{"path": "scene-a/1/pred_traj_1.bin", "size": 1}]})
                with self.assertRaisesRegex(RuntimeError, "source run and product"):
                    batch.source_contract(source)
                batch.write(run_dir / "restore-verification.json", {"restore_ok": True})
                self.assertEqual(batch.source_contract(source)[3][0]["scene"], "scene-a")
                batch.write(manifest, {"prepared_scenes": [{"scene": "scene-a", "camera_frames": 2}]})
                with self.assertRaisesRegex(RuntimeError, "prepared manifest source scene"):
                    batch.source_contract(source)
            finally:
                batch.BASE, batch.ROOT = original_base, original_root

    def test_progress_follows_runner_evidence_without_duplicates(self):
        with workspace_temp() as temporary:
            run_dir = Path(temporary)
            messages = []
            reporter = batch.RunProgress(run_dir, [{"scene": "scene-a"}, {"scene": "scene-b"}], messages.append)
            events = run_dir / "events.jsonl"
            probe = run_dir / "probe.log"
            events.write_text(json.dumps({"event": "TRANSACTION_STEP_STARTED", "step": "replay"}) + "\n")
            probe.write_text(json.dumps({"recorder": {"current_frame_index": 30, "total_frames": 120}}) + "\n")
            reporter.poll()
            self.assertEqual(reporter.stage, "回放")
            self.assertEqual((reporter.frame, reporter.total_frames), (30, 120))
            with probe.open("a") as stream:
                stream.write(json.dumps({"scene": "scene-a", "state": "REPLAY_COMPLETE"}) + "\n")
            reporter.poll()
            reporter.poll()
            self.assertEqual(reporter.completed, 1)
            self.assertEqual(sum("场景 1/2 完成" in message for message in messages), 1)
            with events.open("a") as stream:
                stream.write(json.dumps({"event": "WORKSTATION_COLLECTION_STARTED"}) + "\n")
            with probe.open("a") as stream:
                stream.write(json.dumps({"scene": "scene-b", "state": "REPLAY_COMPLETE"}) + "\n")
            reporter.poll()
            self.assertEqual(reporter.completed, 2)
            self.assertEqual(reporter.stage, "收集工作站预测")

    def test_scene_selection(self):
        self.assertEqual(batch.selected_indices("all", 3), [1, 2, 3])
        self.assertEqual(batch.selected_indices("2", 3), [2])
        self.assertEqual(batch.selected_indices("1,3", 3), [1, 3])
        for value in ("0", "3,1", "1,1", "4", "1-3"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                batch.selected_indices(value, 3)

    def test_two_scenes_retain_separate_hard_links(self):
        with workspace_temp() as temporary:
            original_base, original_root = batch.BASE, batch.ROOT
            root = Path(temporary) / "root"
            base = Path(temporary) / "base"
            batch.BASE, batch.ROOT = base, root
            try:
                run, source, task = "run-new", "run-source", "task-new"
                scenes = ["scene-a", "scene-b"]
                product = root / "cache/predictions" / run / "predictions"
                files = []
                for scene in scenes:
                    for kind in ("pred_img", "pred_road", "pred_traj", "traj_xy"):
                        path = product / scene / "1" / (kind + "_1.bin")
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes((scene + kind).encode())
                        files.append({"path": path.relative_to(product).as_posix(),
                                      "size": path.stat().st_size,
                                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                inventory = hashlib.sha256(json.dumps(files, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                receipt_path = product.with_name("predictions.bench-eval-product.json")
                batch.write(receipt_path, {"state": "SUCCEEDED", "producer_run_id": run,
                    "workstation_product": str(product), "source": "/app/livc-test/run-new/outputs/eval_pred_result",
                    "files": files, "inventory_sha256": inventory, "product_identity": "identity"})
                source_receipt = root / "cache/predictions" / source / receipt_path.name
                source_receipt.parent.mkdir(parents=True)
                batch.write(source_receipt, {"files": files[:4]})

                run_dir = root / "runs" / run
                run_dir.mkdir(parents=True)
                batch.write(run_dir / "state.json", {"state": "COMPLETED_TO_REQUESTED_GOAL"})
                batch.write(run_dir / "restore-verification.json", {"restore_ok": True})
                batch.write(run_dir / "workstation-predictions.json", {
                    "state": "WORKSTATION_PREDICTIONS_COLLECTED",
                    "inventory_sha256": inventory, "product_identity": "identity",
                    "file_count": len(files)})
                (run_dir / "configured-inputs").mkdir()
                (run_dir / "configured-inputs/common_param.probe.yaml").write_text("eval_dump: on\n")
                (run_dir / "logs").mkdir()
                (run_dir / "logs/worker-cli.log").write_text("PROCESSED\n")
                (run_dir / "probe.log").write_text("COMPLETE\n")
                (run_dir / "events.jsonl").write_text("{}\n")
                adapter = base / "adapters" / run
                adapter.mkdir(parents=True)
                batch.write(adapter / "job.json", {"run_id": run})
                batch.write(adapter / "profile.json", {"profile_id": run})
                evidence = adapter / "evidence" / run
                evidence.mkdir(parents=True)
                batch.write(evidence / "batch-result.json", {"expected": 2,
                    "completed": [{"scene": scene, "state": "REPLAY_COMPLETE"} for scene in scenes]})
                (root / "tasks").mkdir()

                selection = {"run_id": run, "source_run": source, "task_id": task,
                    "scenes": [{"index": index, "scene": scene}
                               for index, scene in enumerate(scenes, 1)]}
                target, returned_receipt = batch.validate_and_retain(selection, 0)
                self.assertEqual(returned_receipt, receipt_path)
                index = batch.read(target / "index.json")
                self.assertEqual(index["scene_count"], 2)
                self.assertEqual([item["scene"] for item in index["scenes"]], scenes)
                for number, scene in enumerate(scenes, 1):
                    linked = target / f"scenes/s{number:03d}/predictions" / scene / "1/pred_traj_1.bin"
                    self.assertEqual(linked.stat().st_ino,
                                     (product / scene / "1/pred_traj_1.bin").stat().st_ino)

                # Simulate cache and tasks on different filesystems. The
                # fallback must retain byte-identical copies and resume them.
                copied_selection = {**selection, "task_id": "task-copied"}
                with mock.patch.object(batch, "same_filesystem", return_value=False):
                    copied_target, _ = batch.validate_and_retain(copied_selection, 0)
                    copied_index = batch.read(copied_target / "index.json")
                    self.assertEqual(copied_index["storage"],
                                     "copied_files_in_tasks; original_cache_retained")
                    for number, scene in enumerate(scenes, 1):
                        copied = copied_target / f"scenes/s{number:03d}/predictions" / scene / "1/pred_traj_1.bin"
                        self.assertEqual(copied.read_bytes(),
                                         (product / scene / "1/pred_traj_1.bin").read_bytes())
                        self.assertNotEqual(copied.stat().st_ino,
                                            (product / scene / "1/pred_traj_1.bin").stat().st_ino)
                    copied_selection["resume_delivery"] = True
                    batch.validate_and_retain(copied_selection, 0)

                # A delivery retry must accept the existing task and must not
                # start cleanup again when its receipt already exists.
                cleanup_id = run + "-cleanup"
                cleanup_receipt = root / "maintenance" / (cleanup_id + ".json")
                cleanup_receipt.parent.mkdir()
                batch.write(cleanup_receipt, {"batch_id": cleanup_id, "accepted": [{
                    "run_id": run, "source": "/app/livc-test/run-new/outputs/eval_pred_result",
                    "workstation_product": str(product),
                    "product_receipt": str(receipt_path), "inventory_sha256": inventory,
                }], "removed": ["/app/livc-test/run-new/outputs/eval_pred_result"]})
                selection["resume_delivery"] = True
                result = batch.finish_delivery(selection, 0)
                self.assertEqual(result["state"], "PASS")
                self.assertEqual(Path(result["task"]), target)
                self.assertTrue((target / "logs/bench-output-cleanup.json").is_file())
                # Equal size does not prove content integrity.
                changed = product / scenes[0] / "1/pred_traj_1.bin"
                original = changed.read_bytes()
                changed.write_bytes(b"x" * len(original))
                with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                    batch.validate_and_retain(selection, 0)
            finally:
                batch.BASE, batch.ROOT = original_base, original_root


if __name__ == "__main__":
    unittest.main()
