from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from pointcloud_viewer.image_overlay import TruthImageOverlay


class TruthImageOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_missing_image_shows_placeholder(self) -> None:
        overlay = TruthImageOverlay()
        overlay.set_image(None)
        self.assertIsNone(overlay.image_path)
        self.assertIn("未找到", overlay.image_label.text())

    def test_valid_image_loads_and_can_collapse(self) -> None:
        image_path = self.root / "truth.jpg"
        image = QImage(80, 40, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        self.assertTrue(image.save(str(image_path)))

        overlay = TruthImageOverlay()
        overlay.resize(300, 220)
        overlay.set_image(image_path)
        self.assertEqual(overlay.image_path, image_path)
        self.assertFalse(overlay.image_label.pixmap().isNull())
        self.assertTrue(overlay.is_expanded)
        overlay.toggle_collapsed()
        self.assertFalse(overlay.is_expanded)
        self.assertFalse(overlay.image_label.isVisible())
        overlay.toggle_collapsed()
        self.assertTrue(overlay.is_expanded)


if __name__ == "__main__":
    unittest.main()
