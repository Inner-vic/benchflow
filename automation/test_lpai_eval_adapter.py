from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from lpai_eval_adapter import AdapterError, collect, evaluate, prepare, truth_readiness


class LpaiEvalAdapterTests(unittest.TestCase):
    def args(self, root: Path) -> argparse.Namespace:
        prediction = root / "predictions" / "run"
        truth = root / "truth" / "scene-a"
        (prediction / "scene-a").mkdir(parents=True)
        truth.mkdir(parents=True)
        (truth / "frame.json").write_text("{}\n", encoding="utf-8")
        template = root / "template.yaml"
        template.write_text(
            "dataset:\n  pred_data_root: OLD\n  scene_folder_prefix: OLD\n  max_scenes_for_debug: 0\n",
            encoding="utf-8",
        )
        return argparse.Namespace(
            run_id="run-1", scene="scene-a", config=str(root / "runs" / "run-1" / "config.yaml"),
            config_root=str(root / "runs"), prediction=str(prediction), prediction_root=str(root / "predictions"),
            truth_root=str(root / "truth"), template=str(template), run_root=str(root / "runs"),
        )

    def test_prepare_writes_exact_scene_config_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.args(root)
            prepare(args)
            text = Path(args.config).read_text(encoding="utf-8")
            self.assertIn(str(Path(args.prediction).resolve()), text)
            self.assertIn('scene_folder_prefix: "scene-a"', text)
            self.assertIn("max_scenes_for_debug: 1", text)
            with self.assertRaises(AdapterError):
                prepare(args)

    def test_prepare_rejects_scene_directory_as_prediction_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.args(root)
            args.prediction = str(Path(args.prediction) / args.scene)
            with self.assertRaisesRegex(AdapterError, "prediction root must contain"):
                prepare(args)
            self.assertFalse(Path(args.config).exists())

    def test_collect_requires_one_successful_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.args(root)
            prepare(args)
            run = Path(args.config).parent
            (run / "exit_code.txt").write_text("0\n", encoding="ascii")
            (run / "eval_report").mkdir()
            (run / "eval_report" / "eval_report_x.txt").write_text("report\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                collect(args)
            self.assertEqual("report\n", output.getvalue())

    def test_truth_readiness_blocks_partial_and_layout_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.args(root)
            for suffix in ("partial", "layout"):
                marker = root / "truth" / f".scene-a.{suffix}"
                marker.mkdir()
                readiness = truth_readiness(args.truth_root, args.scene)
                self.assertEqual("BLOCKED_PARTIAL", readiness["state"])
                with self.assertRaisesRegex(AdapterError, "BLOCKED_PARTIAL"):
                    prepare(args)
                marker.rmdir()

    def test_truth_readiness_rejects_empty_scene(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.args(root)
            (Path(args.truth_root) / args.scene / "frame.json").unlink()
            readiness = truth_readiness(args.truth_root, args.scene)
            self.assertEqual("TRUTH_NOT_COMPLETE", readiness["state"])
            with self.assertRaisesRegex(AdapterError, "TRUTH_NOT_COMPLETE"):
                prepare(args)

    def test_truth_manifest_mismatch_is_invalid_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.args(root)
            manifest = Path(args.truth_root) / args.scene / "truth-manifest.json"
            manifest.write_text(
                '{"scene":"scene-a","inventory_sha256":"wrong"}\n',
                encoding="utf-8",
            )
            readiness = truth_readiness(args.truth_root, args.scene)
            self.assertEqual("INVALID_CACHE", readiness["state"])
            with self.assertRaisesRegex(AdapterError, "INVALID_CACHE"):
                prepare(args)

    def test_evaluate_rechecks_truth_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.args(root)
            prepare(args)
            marker = Path(args.truth_root) / ".scene-a.partial"
            marker.mkdir()
            args.run_script = str(root / "run.sh")
            args.library_path = str(root)
            with self.assertRaisesRegex(AdapterError, "BLOCKED_PARTIAL"):
                evaluate(args)
            run_root = Path(args.config).parent
            self.assertFalse((run_root / "exit_code.txt").exists())
            self.assertFalse((run_root / "eval_report").exists())


if __name__ == "__main__":
    unittest.main()
