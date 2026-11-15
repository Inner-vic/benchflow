from __future__ import annotations

import unittest

import numpy as np

from pointcloud_viewer.palette import colorize_labels, label_style
from pointcloud_viewer.renderer import stratified_sample_indices


class PaletteTests(unittest.TestCase):
    def test_known_colors_are_stable_and_distinct(self) -> None:
        self.assertEqual(label_style(17).name, "manhole_cover")
        self.assertNotEqual(label_style(1).rgb, label_style(23).rgb)
        self.assertEqual(label_style(999).name, "unknown_999")

    def test_colorize_preserves_alpha(self) -> None:
        rgba = colorize_labels(np.array([1, 16, 23, 26]), alpha=123)
        self.assertEqual(rgba.shape, (4, 4))
        self.assertTrue(np.all(rgba[:, 3] == 123))
        self.assertFalse(np.array_equal(rgba[0, :3], rgba[1, :3]))

    def test_stratified_sample_keeps_visible_labels(self) -> None:
        labels = np.array([1] * 100 + [16] * 5 + [23] * 3, dtype=np.int32)
        chosen = stratified_sample_indices(labels, np.arange(labels.size), limit=12)
        self.assertLessEqual(chosen.size, 12)
        self.assertTrue({1, 16, 23}.issubset(set(labels[chosen])))


if __name__ == "__main__":
    unittest.main()
