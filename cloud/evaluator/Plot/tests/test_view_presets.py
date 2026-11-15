from __future__ import annotations

import unittest

import numpy as np
from PySide6.QtGui import QVector3D
from PySide6.QtWidgets import QApplication

from pointcloud_viewer.interaction import PickingGLViewWidget, project_points_to_screen
from pointcloud_viewer.view_presets import VIEW_PRESETS, apply_view_preset


class ViewPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.view = PickingGLViewWidget()
        cls.view.resize(800, 600)
        cls.view.show()
        cls.app.processEvents()
        cls.points = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)

    def _screen_deltas(self, key: str) -> np.ndarray:
        self.view.setCameraPosition(pos=QVector3D(0, 0, 0), distance=10)
        apply_view_preset(self.view, VIEW_PRESETS[key])
        screen, _, _ = project_points_to_screen(self.view, self.points)
        return screen[1:] - screen[0]

    def test_yx_orientation(self) -> None:
        delta = self._screen_deltas("yx")
        self.assertLess(delta[0, 1], 0)  # +X from screen bottom to top
        self.assertLess(delta[1, 0], 0)  # +Y from screen right to left

    def test_xz_orientation(self) -> None:
        delta = self._screen_deltas("xz")
        self.assertLess(delta[2, 1], 0)  # +Z bottom to top
        self.assertLess(delta[0, 0], 0)  # +X right to left

    def test_yz_orientation(self) -> None:
        delta = self._screen_deltas("yz")
        self.assertLess(delta[2, 1], 0)  # +Z bottom to top
        self.assertLess(delta[1, 0], 0)  # +Y right to left

    def test_bird_orientation_and_retains_center_distance(self) -> None:
        self.view.setCameraPosition(pos=QVector3D(2, 3, 4), distance=17)
        apply_view_preset(self.view, VIEW_PRESETS["bird"])
        screen, _, _ = project_points_to_screen(self.view, self.points)
        delta = screen[1:] - screen[0]
        self.assertLess(delta[0, 1], 0)  # +X toward screen front/up
        self.assertLess(delta[1, 0], 0)  # +Y toward screen left
        self.assertLess(delta[2, 1], 0)  # +Z toward screen up
        self.assertEqual(self.view.opts["distance"], 17)
        self.assertEqual(tuple(self.view.opts["center"].toTuple()), (2.0, 3.0, 4.0))


if __name__ == "__main__":
    unittest.main()
