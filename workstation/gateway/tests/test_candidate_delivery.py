import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway import candidate_remote, remote_agent


class CandidateDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        source = Path(__file__).resolve().parents[1] / "vendor/run_stream_batch.py"
        spec = importlib.util.spec_from_file_location("isolated_candidate_delivery", source)
        self.batch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.batch)
        self.batch.BASE = self.directory / "workstation"
        self.batch.ROOT = self.directory / "data"
        self.run_id = "current-candidate"
        self.run = self.batch.ROOT / "runs" / self.run_id
        self.product = self.batch.ROOT / "cache/predictions" / self.run_id / "predictions"
        self.receipt_path = self.product.parent / "predictions.bench-eval-product.json"
        self.evidence = self.batch.BASE / "adapters" / self.run_id / "evidence" / self.run_id
        self.plan = {"run_id": self.run_id, "task_id": self.run_id, "source_run": "historical-template",
                     "scenes": [{"index": 1, "scene": "scene-one"}, {"index": 2, "scene": "scene-two"}],
                     "required_output_types": ["pred_traj", "traj_xy"], "resume_delivery": False}
        for scene in ("scene-one", "scene-two"):
            for kind in ("pred_traj", "traj_xy"):
                path = self.product / scene / (kind + "_0001.txt")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic " + scene + " " + kind, encoding="utf-8")
        self.write(self.run / "state.json", {"state": "COMPLETED_TO_REQUESTED_GOAL"})
        self.write(self.run / "restore-verification.json", {"restore_ok": True})
        self.write(self.evidence / "batch-result.json", {"expected": 2, "completed": [
            {"scene": "scene-one", "state": "REPLAY_COMPLETE"}, {"scene": "scene-two", "state": "REPLAY_COMPLETE"}]})
        self.write(self.run / "sw0058-profile-precheck.json", {"run_id": self.run_id, "required_output_types": ["pred_traj", "traj_xy"]})
        self.write(self.batch.BASE / "adapters" / self.run_id / "job.json", {"run_id": self.run_id})
        self.write(self.batch.BASE / "adapters" / self.run_id / "profile.json", {"profile_id": self.run_id})
        for relative in ("configured-inputs/common_param.probe.yaml", "logs/worker.log", "probe.log", "events.jsonl"):
            path = self.run / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic evidence\n", encoding="utf-8")
        (self.batch.ROOT / "tasks").mkdir(parents=True)
        self.seal_product()

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")

    def seal_product(self):
        files = [{"path": path.relative_to(self.product).as_posix(), "size": path.stat().st_size,
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in sorted(self.product.rglob("*")) if path.is_file()]
        inventory = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt = {"state": "SUCCEEDED", "producer_run_id": self.run_id, "workstation_product": str(self.product),
                   "inventory_sha256": inventory, "product_identity": "candidate-product-identity", "files": files, "file_count": len(files)}
        self.write(self.receipt_path, receipt)
        self.write(self.run / "workstation-predictions.json", {"state": "WORKSTATION_PREDICTIONS_COLLECTED",
                   "inventory_sha256": inventory, "product_identity": "candidate-product-identity", "file_count": len(files)})

    def historical(self, types):
        path = self.batch.ROOT / "cache/predictions" / self.plan["source_run"] / "predictions.bench-eval-product.json"
        self.write(path, {"files": [{"path": "old-scene/" + kind + "_0001.txt"} for kind in types]})
        return path

    def test_current_trajectory_outputs_pass_without_historical_receipt_or_pred_img(self):
        self.assertFalse((self.batch.ROOT / "cache/predictions/historical-template").exists())
        with patch("subprocess.run", side_effect=AssertionError("delivery validation must stay local")):
            task, receipt = self.batch.validate_and_retain(self.plan, 0)
        self.assertEqual(self.receipt_path, receipt)
        index = self.batch.read(task / "index.json")
        self.assertEqual("PASS", index["state"])
        self.assertEqual(4, index["file_count"])
        self.assertEqual({"pred_traj", "traj_xy"}, set(index["scenes"][0]["counts"]))
        self.assertTrue((task / "inputs/common_param.effective.yaml").is_file())

    def test_current_contract_ignores_historical_image_output_requirement(self):
        self.historical(["pred_img", "pred_road"])
        task, _ = self.batch.validate_and_retain(self.plan, 0)
        self.assertEqual("PASS", self.batch.read(task / "index.json")["state"])

    def test_current_precheck_wrong_run_or_types_are_rejected_without_legacy_fallback(self):
        self.historical(["pred_traj", "traj_xy"])
        for precheck in ({"run_id": "different-run", "required_output_types": ["pred_traj", "traj_xy"]},
                         {"run_id": self.run_id, "required_output_types": ["pred_img"]},
                         {"run_id": self.run_id, "required_output_types": []}):
            with self.subTest(precheck=precheck):
                self.write(self.run / "sw0058-profile-precheck.json", precheck)
                with self.assertRaisesRegex(RuntimeError, "current candidate output contract"):
                    self.batch.validate_and_retain(self.plan, 0)
                self.assertFalse((self.batch.ROOT / "tasks" / self.run_id).exists())

    def test_missing_current_precheck_cannot_fall_back_to_matching_history(self):
        self.historical(["pred_traj", "traj_xy"])
        (self.run / "sw0058-profile-precheck.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.batch.validate_and_retain(self.plan, 0)

    def test_each_scene_must_have_all_current_required_types(self):
        (self.product / "scene-two/traj_xy_0001.txt").unlink()
        self.seal_product()
        with self.assertRaisesRegex(RuntimeError, "lack expected prediction types"):
            self.batch.validate_and_retain(self.plan, 0)

    def test_invalid_current_required_type_inventory_is_rejected(self):
        for supplied in ([], ["pred_traj", "pred_traj"], ["unknown"], "pred_traj"):
            with self.subTest(supplied=supplied):
                plan = {**self.plan, "required_output_types": supplied}
                with self.assertRaisesRegex(RuntimeError, "current candidate output contract"):
                    self.batch.validate_and_retain(plan, 0)

    def test_legacy_plan_keeps_its_historical_output_requirement(self):
        plan = copy.deepcopy(self.plan)
        del plan["required_output_types"]
        self.historical(["pred_img"])
        with self.assertRaisesRegex(RuntimeError, "lack expected prediction types"):
            self.batch.validate_and_retain(plan, 0)
        self.historical(["pred_traj", "traj_xy"])
        self.write(self.run / "sw0058-profile-precheck.json", {"run_id": "ignored-for-legacy", "required_output_types": ["pred_img"]})
        task, _ = self.batch.validate_and_retain(plan, 0)
        self.assertEqual("PASS", self.batch.read(task / "index.json")["state"])

    def test_current_types_do_not_bypass_completion_restoration_or_product_hashes(self):
        for path, value in ((self.run / "state.json", {"state": "FAILED"}),
                            (self.run / "restore-verification.json", {"restore_ok": False})):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                self.write(path, value)
                with self.assertRaisesRegex(RuntimeError, "did not verify"):
                    self.batch.validate_and_retain(self.plan, 0)
                path.write_bytes(original)
        output = self.product / "scene-one/pred_traj_0001.txt"
        original = output.read_bytes()
        output.write_bytes(b"x" * len(original))
        with self.assertRaisesRegex(RuntimeError, "SHA-256 differs"):
            self.batch.validate_and_retain(self.plan, 0)

    def test_resume_revalidates_current_precheck_before_reusing_retained_task(self):
        task, _ = self.batch.validate_and_retain(self.plan, 0)
        plan = {**self.plan, "resume_delivery": True}
        again, _ = self.batch.validate_and_retain(plan, 0)
        self.assertEqual(task, again)
        self.write(self.run / "sw0058-profile-precheck.json", {"run_id": self.run_id, "required_output_types": ["pred_img"]})
        with self.assertRaisesRegex(RuntimeError, "current candidate output contract"):
            self.batch.validate_and_retain(plan, 0)


class CandidateExecutableControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def archive(self, mode):
        package = self.root / "package.tar.gz"
        with tarfile.open(package, "w:gz") as output:
            for name, raw, permissions in (("livc/" + candidate_remote.COMMON, b"common: true\n", 0o644),
                                            ("livc/bin/bench-replay-mode", b"#!/bin/sh\nexit 0\n", mode)):
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(raw), permissions
                output.addfile(item, io.BytesIO(raw))
        return package

    def test_inventory_rejects_existing_non_executable_control_target(self):
        with self.assertRaisesRegex(ValueError, "not executable"):
            candidate_remote.inventory(self.archive(0o644), [])
        entries, _, _ = candidate_remote.inventory(self.archive(0o755), [])
        self.assertEqual("file", entries["bin/bench-replay-mode"]["kind"])

    def test_formal_probe_checks_execute_mode_in_addition_to_bytes(self):
        target = self.root / "bin/bench-replay-mode"
        target.parent.mkdir()
        target.write_bytes(b"same reviewed bytes")
        entries = {"bin/bench-replay-mode": {"kind": "file", "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "executable": True}}
        transport = SimpleNamespace(command=lambda *args: ["fake-ssh"], environment=lambda *args: {})
        m = SimpleNamespace(BASE=self.root)
        real_stat = Path.stat
        permissions = [0o644]
        def stat_override(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == target:
                values = list(result)
                values[0] = (result.st_mode & ~0o777) | permissions[0]
                return os.stat_result(values)
            return result
        def subprocess_probe(argv, **kwargs):
            # Execute the exact generated read-only probe against synthetic
            # files, changing only its fixed bench-root path for this test.
            body = kwargs["input"].replace("pathlib.Path('/app/livc')", "pathlib.Path(" + repr(str(self.root)) + ")")
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                exec(compile(body, "isolated-formal-probe", "exec"), {})
            return SimpleNamespace(returncode=0, stdout=stream.getvalue())
        original_path = list(sys.path)
        try:
            with patch.dict(sys.modules, {"ssh_transport": transport}), patch.object(Path, "stat", stat_override), \
                 patch.object(remote_agent.subprocess, "run", side_effect=subprocess_probe):
                self.assertFalse(remote_agent.formal_matches(m, entries))
                permissions[0] = 0o755
                self.assertTrue(remote_agent.formal_matches(m, entries))
        finally:
            sys.path[:] = original_path


if __name__ == "__main__":
    unittest.main()
