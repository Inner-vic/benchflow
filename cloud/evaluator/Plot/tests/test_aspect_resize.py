from __future__ import annotations

import unittest

from pointcloud_viewer.aspect_resize import aspect_resized_size


class AspectResizeTests(unittest.TestCase):
    def test_resize_preserves_image_ratio(self) -> None:
        result = aspect_resized_size(400, 225, 120, 20, 16 / 9, 120, 80, 1000, 800)
        self.assertAlmostEqual(result.width / result.height, 16 / 9, places=2)

    def test_resize_obeys_bounds(self) -> None:
        result = aspect_resized_size(400, 300, 10_000, 10_000, 4 / 3, 160, 120, 640, 480)
        self.assertLessEqual(result.width, 640)
        self.assertLessEqual(result.height, 480)
        self.assertAlmostEqual(result.width / result.height, 4 / 3, places=2)


if __name__ == "__main__":
    unittest.main()
