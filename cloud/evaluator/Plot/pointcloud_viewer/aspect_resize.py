from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QWidget


@dataclass(frozen=True)
class AspectResizeResult:
    width: int
    height: int


def aspect_resized_size(
    start_width: int,
    start_height: int,
    dx: int,
    dy: int,
    aspect_ratio: float,
    min_width: int,
    min_height: int,
    max_width: int,
    max_height: int,
) -> AspectResizeResult:
    """Calculate a bounded resize while preserving content aspect ratio."""
    ratio = aspect_ratio if aspect_ratio > 0 else 4.0 / 3.0
    target_width = max(start_width + dx, int((start_height + dy) * ratio))
    target_width = max(min_width, min(target_width, max_width))
    target_height = max(min_height, min(round(target_width / ratio), max_height))
    target_width = max(min_width, min(round(target_height * ratio), max_width))
    return AspectResizeResult(int(target_width), int(target_height))


class AspectRatioSizeGrip(QWidget):
    """Bottom-right resize grip that asks its owner to retain image aspect ratio."""

    resized = Signal(int, int)

    def __init__(self, owner, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._owner = owner
        self._start_global = QPoint()
        self._start_size = (0, 0)
        self.setFixedSize(20, 20)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("拖动等比例调整图片窗口大小")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._owner.is_expanded:
            self._start_global = event.globalPosition().toPoint()
            self._start_size = (self._owner.width(), self._owner.height())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not self._start_size:
            super().mouseMoveEvent(event)
            return
        delta = event.globalPosition().toPoint() - self._start_global
        width, height = self._owner.aspect_resize(delta.x(), delta.y(), *self._start_size)
        self._owner.resize(width, height)
        self.resized.emit(width, height)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._start_size = (0, 0)
        event.accept()
