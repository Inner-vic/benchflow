from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np

from pointcloud_viewer.scene_index import (
    build_frame_pairs,
    find_truth_image,
    parse_frame_timestamp,
    resolve_gt_dataset_dir,
)


def write_dense(frame_dir: Path) -> Path:
    frame_dir.mkdir(parents=True, exist_ok=True)
    path = frame_dir / "dense_labeled_sample.npy"
    np.save(path, np.array([[1.0, 2.0, 3.0, 1.0, 1.0]], dtype=np.float32))
    return path


def write_pred_traj(frame_dir: Path) -> Path:
    frame_dir.mkdir(parents=True, exist_ok=True)
    path = frame_dir / "pred_traj_sample.npy"
    # One trajectory point in the FL wheel; this is enough for path discovery tests.
    payload = np.array([1, 0, 0, 0], dtype="<i4").tobytes() + np.array([1, 2, 3, 1], dtype="<f4").tobytes()
    np.save(path, np.frombuffer(payload, dtype=np.uint8))
    return path


class SceneIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.gt_scene = self.root / "gt_scene"
        self.pred_scene = self.root / "pred_scene"
        self.gt_dataset = self.gt_scene / "dataset"
        self.gt_dataset.mkdir(parents=True)
        self.pred_scene.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_timestamp_formats(self) -> None:
        self.assertEqual(parse_frame_timestamp("7_1782815972654"), 1782815972654)
        self.assertEqual(parse_frame_timestamp("1782815972654"), 1782815972654)
        self.assertIsNone(parse_frame_timestamp("7"))
        self.assertIsNone(parse_frame_timestamp("raw"))
        self.assertIsNone(parse_frame_timestamp("7_bad"))

    def test_dataset_subdirectory_is_preferred(self) -> None:
        self.assertEqual(resolve_gt_dataset_dir(self.gt_scene), self.gt_dataset)
        fallback = self.root / "gt_without_dataset"
        fallback.mkdir()
        self.assertEqual(resolve_gt_dataset_dir(fallback), fallback)

    def test_exact_timestamp_pairs_are_sorted_and_require_both_files(self) -> None:
        write_dense(self.gt_dataset / "10_1782815972954")
        write_dense(self.gt_dataset / "7_1782815972654")
        write_dense(self.gt_dataset / "8_1782815972754")
        (self.gt_dataset / "raw").mkdir()
        write_pred_traj(self.pred_scene / "7_1782815972654")
        write_pred_traj(self.pred_scene / "10_1782815972954")
        write_pred_traj(self.pred_scene / "9_1782815972854")
        (self.pred_scene / "8_1782815972754").mkdir()  # timestamp exists but pred_traj is missing

        result = build_frame_pairs(self.gt_scene, self.pred_scene)
        self.assertEqual(result.gt_frame_count, 3)
        self.assertEqual(result.pred_frame_count, 3)
        self.assertEqual(result.matching_timestamp_count, 2)
        self.assertEqual([pair.display_name for pair in result.pairs], [
            "7_1782815972654",
            "10_1782815972954",
        ])
        self.assertEqual([pair.timestamp_ms for pair in result.pairs], [1782815972654, 1782815972954])
    def test_truth_image_prefers_f_ca_120_and_does_not_affect_pairing(self) -> None:
        frame = self.gt_dataset / "7_1782815972654"
        write_dense(frame)
        write_pred_traj(self.pred_scene / "7_1782815972654")
        (frame / "z_other.png").write_bytes(b"png")
        preferred = frame / "7_f_ca_120_1782815972712.jpg"
        preferred.write_bytes(b"jpg")
        (frame / "a_other.jpeg").write_bytes(b"jpeg")

        self.assertEqual(find_truth_image(frame), preferred)
        result = build_frame_pairs(self.gt_scene, self.pred_scene)
        self.assertEqual(len(result.pairs), 1)
        self.assertEqual(result.pairs[0].image_path, preferred)

    def test_missing_truth_image_keeps_valid_pair(self) -> None:
        write_dense(self.gt_dataset / "7_1782815972654")
        write_pred_traj(self.pred_scene / "7_1782815972654")
        result = build_frame_pairs(self.gt_scene, self.pred_scene)
        self.assertEqual(len(result.pairs), 1)
        self.assertIsNone(result.pairs[0].image_path)


if __name__ == "__main__":
    unittest.main()
