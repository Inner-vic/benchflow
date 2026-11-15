from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QSettings

from pointcloud_viewer.settings import load_last_scene_state, save_scene_state


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, self.temp_dir.name)
        QSettings("uroad", "pointcloud_viewer").clear()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_scene_paths_and_timestamp_round_trip(self) -> None:
        root = Path(self.temp_dir.name)
        gt = root / "gt"
        pred = root / "pred"
        gt.mkdir()
        pred.mkdir()
        save_scene_state(gt, pred, 1782815972654)
        restored_gt, restored_pred, timestamp = load_last_scene_state()
        self.assertEqual(restored_gt, gt)
        self.assertEqual(restored_pred, pred)
        self.assertEqual(timestamp, 1782815972654)

    def test_missing_paths_are_not_restored(self) -> None:
        save_scene_state(Path(self.temp_dir.name) / "missing_gt", Path(self.temp_dir.name) / "missing_pred", None)
        gt, pred, timestamp = load_last_scene_state()
        self.assertIsNone(gt)
        self.assertIsNone(pred)
        self.assertIsNone(timestamp)


if __name__ == "__main__":
    unittest.main()
