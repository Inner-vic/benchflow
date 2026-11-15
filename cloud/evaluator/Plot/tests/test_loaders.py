from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np

from pointcloud_viewer.loaders import PointCloudLoadError, load_point_cloud
from pointcloud_viewer.models import SourceKind


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_dense_labeled(self) -> None:
        path = self.root / "dense_labeled_example.npy"
        raw = np.array([[1.0, 2.0, 3.0, 16.0, 1.0], [4.0, 5.0, 6.0, 1.0, 1.0]], dtype=np.float32)
        np.save(path, raw)
        cloud = load_point_cloud(path, "ground_truth")
        self.assertEqual(cloud.kind, SourceKind.DENSE_LABELED)
        np.testing.assert_allclose(cloud.xyz, raw[:, :3])
        np.testing.assert_array_equal(cloud.labels, np.array([16, 1], dtype=np.int32))

    def test_load_pred_road(self) -> None:
        path = self.root / "pred_road_example.npy"
        raw = np.array([[1.0, 2.0, 3.0, 23.0]], dtype=np.float32)
        np.save(path, raw)
        cloud = load_point_cloud(path, "prediction")
        self.assertEqual(cloud.kind, SourceKind.PRED_ROAD)
        self.assertEqual(cloud.point_count, 1)
        self.assertEqual(cloud.labels[0], 23)

    def test_load_pred_traj_npy_payload(self) -> None:
        path = self.root / "pred_traj_example.npy"
        counts = (1, 2, 0, 1)
        payload = bytearray(struct.pack("<4i", *counts))
        points = [
            (1.0, 2.0, 3.0, 1.0),
            (4.0, 5.0, 6.0, 16.0),
            (7.0, 8.0, 9.0, 23.0),
            (10.0, 11.0, 12.0, 24.0),
        ]
        for point in points:
            payload.extend(struct.pack("<4f", *point))
        np.save(path, np.frombuffer(payload, dtype=np.uint8))

        cloud = load_point_cloud(path, "prediction")
        self.assertEqual(cloud.kind, SourceKind.PRED_TRAJ)
        self.assertEqual(cloud.point_count, 4)
        np.testing.assert_array_equal(cloud.wheel_ids, np.array([0, 1, 1, 3], dtype=np.int8))
        np.testing.assert_array_equal(cloud.labels, np.array([1, 16, 23, 24], dtype=np.int32))

    def test_reject_invalid_shape(self) -> None:
        path = self.root / "unknown.npy"
        np.save(path, np.zeros((3, 2), dtype=np.float32))
        with self.assertRaises(PointCloudLoadError):
            load_point_cloud(path, "ground_truth")


if __name__ == "__main__":
    unittest.main()
