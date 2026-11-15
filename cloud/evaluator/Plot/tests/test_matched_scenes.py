from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np

from pointcloud_viewer.scene_index import build_frame_pairs, find_same_named_scene_dirs


def write_dense(frame_dir: Path) -> None:
    frame_dir.mkdir(parents=True, exist_ok=True)
    np.save(frame_dir / "dense_labeled_sample.npy", np.array([[1, 2, 0, 1, 1]], dtype=np.float32))


def write_pred_traj(frame_dir: Path) -> None:
    frame_dir.mkdir(parents=True, exist_ok=True)
    payload = np.array([1, 0, 0, 0], dtype="<i4").tobytes() + np.array([1, 2, 0, 1], dtype="<f4").tobytes()
    np.save(frame_dir / "pred_traj_sample.npy", np.frombuffer(payload, dtype=np.uint8))


class MatchedSceneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.gt_root = self.root / "gt_root"
        self.pred_root = self.root / "pred_root"
        self.gt_root.mkdir()
        self.pred_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_same_named_candidates_include_empty_scenes_in_sorted_order(self) -> None:
        for name in ("scene_c", "scene_a", "scene_b", "gt_only"):
            (self.gt_root / name / "dataset").mkdir(parents=True)
        for name in ("scene_a", "scene_b", "scene_c", "pred_only"):
            (self.pred_root / name).mkdir(parents=True)

        candidates = find_same_named_scene_dirs(self.gt_root, self.pred_root)
        self.assertEqual([candidate.scene_name for candidate in candidates], ["scene_a", "scene_b", "scene_c"])

        write_dense(self.gt_root / "scene_a" / "dataset" / "0_1782815972654")
        write_pred_traj(self.pred_root / "scene_a" / "0_1782815972654")
        write_dense(self.gt_root / "scene_c" / "dataset" / "0_1782815972754")
        write_pred_traj(self.pred_root / "scene_c" / "0_1782815972754")

        scan_a = build_frame_pairs(candidates[0].gt_scene_dir, candidates[0].pred_scene_dir)
        scan_b = build_frame_pairs(candidates[1].gt_scene_dir, candidates[1].pred_scene_dir)
        scan_c = build_frame_pairs(candidates[2].gt_scene_dir, candidates[2].pred_scene_dir)
        self.assertTrue(scan_a.pairs)
        self.assertFalse(scan_b.pairs)
        self.assertTrue(scan_c.pairs)
        self.assertEqual(scan_c.pairs[0].timestamp_ms, 1782815972754)


if __name__ == "__main__":
    unittest.main()
