from __future__ import annotations

import tempfile
import unittest
import numpy as np
from pathlib import Path

from validate_predictions import PredictionError, validate_predictions, validate_scene


class ValidatePredictionsTests(unittest.TestCase):
    def make_scene(self, root: Path, count: int = 3) -> Path:
        scene = root / "scene-A"
        for index in range(count):
            frame = scene / f"{index}_{1700000000000 + index}"
            frame.mkdir(parents=True)
            np.save(frame / f"pred_traj_{1700000000000 + index}.npy", np.ones((4, 3)))
            np.save(frame / f"traj_xy_{1700000000000 + index}.npy", np.ones((4, 2)))
        return scene

    def test_invalid_contents_are_rejected(self) -> None:
        for value in (b"NUMPY", b"{", b'{"x": NaN}', b'{"x": 1e999}', b"{}"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                scene = self.make_scene(Path(temp), 1)
                (next(scene.iterdir()) / "extra.json").write_bytes(value)
                with self.assertRaises(PredictionError):
                    validate_scene(scene)

    def test_invalid_arrays_are_rejected(self) -> None:
        for value in (np.array([np.nan]), np.array([np.inf]), np.array([]), np.array([{}], dtype=object), np.array(["text"])):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                scene = self.make_scene(Path(temp), 1)
                np.save(next(scene.iterdir()) / "extra.npy", value)
                with self.assertRaises(PredictionError):
                    validate_scene(scene)

    def test_corrupt_npy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = self.make_scene(Path(temp), 1)
            (next(scene.iterdir()) / "extra.npy").write_bytes(b"NUMPY")
            with self.assertRaises(PredictionError):
                validate_scene(scene)

    def test_explicit_shapes_and_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = self.make_scene(Path(temp), 1)
            (next(scene.iterdir()) / "extra.json").write_text('{"points": [1, 2.5]}', encoding="utf-8")
            validate_scene(scene, expected_shapes={"pred_traj_": [4, 3]})
            with self.assertRaises(PredictionError):
                validate_scene(scene, expected_shapes={"pred_traj_": [4, 2]})
            with self.assertRaises(PredictionError):
                validate_scene(scene, expected_shapes={"pred_traj_": "wrong"})

    def test_duplicate_or_decreasing_timestamps_are_rejected(self) -> None:
        for timestamp in (1700000000000, 1699999999999):
            with self.subTest(timestamp=timestamp), tempfile.TemporaryDirectory() as temp:
                scene = self.make_scene(Path(temp), 2)
                (scene / "1_1700000000001").rename(scene / f"1_{timestamp}")
                with self.assertRaises(PredictionError):
                    validate_scene(scene)

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
