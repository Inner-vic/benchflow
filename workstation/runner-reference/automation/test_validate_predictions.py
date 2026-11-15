from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from validate_predictions import PredictionError, validate_predictions, validate_scene


class ValidatePredictionsTests(unittest.TestCase):
    def make_scene(self, root: Path, count: int = 3) -> Path:
        scene = root / "scene-A"
        for index in range(count):
            frame = scene / f"{index}_{1700000000000 + index}"
            frame.mkdir(parents=True)
            (frame / f"pred_traj_{1700000000000 + index}.npy").write_bytes(b"NUMPY")
            (frame / f"traj_xy_{1700000000000 + index}.npy").write_bytes(b"NUMPY")
        return scene

    def test_valid_contiguous_scene(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_scene(root)
            result = validate_predictions(
                root,
                ["scene-A"],
                expected_frames=3,
                required_prefixes=("pred_traj_", "traj_xy_"),
            )
            self.assertEqual("PREDICTION_OUTPUT_VALID", result["state"])
            self.assertEqual(3, result["frame_count"])

    def test_missing_required_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = self.make_scene(Path(temp), count=1)
            with self.assertRaises(PredictionError):
                validate_scene(scene, required_prefixes=("pred_road_",))

    def test_zero_byte_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = self.make_scene(Path(temp), count=1)
            (next(scene.iterdir()) / "pred_road_1700000000000.npy").write_bytes(b"")
            with self.assertRaises(PredictionError):
                validate_scene(scene)

    def test_frame_count_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = self.make_scene(Path(temp), count=2)
            with self.assertRaises(PredictionError):
                validate_scene(scene, expected_frames=3)

    def test_task_name_maps_to_required_file_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scene = self.make_scene(root, count=1)
            frame = next(scene.iterdir())
            next(frame.glob("pred_traj_*.npy")).unlink()
            with self.assertRaises(PredictionError):
                validate_predictions(
                    root,
                    ["scene-A"],
                    expected_frames=1,
                    required_prefixes=(),
                    required_tasks=("trajectory",),
                )


if __name__ == "__main__":
    unittest.main()
