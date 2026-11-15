from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyqtgraph.opengl as gl
from PySide6.QtCore import QPointF, Qt, Signal


CLICK_DRAG_THRESHOLD_PX = 4.0
PICK_RADIUS_PX = 12.0


@dataclass(frozen=True)
class PickCandidate:
    source_id: str
    original_index: int
    screen_distance_px: float
    depth: float


def is_click(start: tuple[float, float], end: tuple[float, float], threshold_px: float = CLICK_DRAG_THRESHOLD_PX) -> bool:
    return float(np.hypot(end[0] - start[0], end[1] - start[1])) <= threshold_px


def nearest_screen_candidate(
    candidates: list[PickCandidate],
    max_distance_px: float = PICK_RADIUS_PX,
) -> PickCandidate | None:
    """Choose the nearest pixel candidate; prediction wins exact distance ties."""
    valid = [candidate for candidate in candidates if candidate.screen_distance_px <= max_distance_px]
    if not valid:
        return None
    return min(
        valid,
        key=lambda candidate: (
            candidate.screen_distance_px,
            0 if candidate.source_id == "prediction" else 1,
            candidate.depth,
        ),
    )


def project_points_to_screen(view: gl.GLViewWidget, points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return Qt pixel coordinates, visible mask, and normalized depth for 3D points."""
    points = np.asarray(points, dtype=np.float64)
    if points.size == 0:
        return np.empty((0, 2)), np.empty(0, dtype=bool), np.empty(0)

    projection = np.asarray(view.projectionMatrix().data(), dtype=np.float64).reshape(4, 4, order="F")
    model_view = np.asarray(view.viewMatrix().data(), dtype=np.float64).reshape(4, 4, order="F")
    homogeneous = np.column_stack((points, np.ones(points.shape[0], dtype=np.float64)))
    clip = homogeneous @ (projection @ model_view).T
    w = clip[:, 3]
    visible = np.abs(w) > 1e-9
    ndc = np.zeros((points.shape[0], 3), dtype=np.float64)
    ndc[visible] = clip[visible, :3] / w[visible, None]
    visible &= (ndc[:, 0] >= -1.0) & (ndc[:, 0] <= 1.0)
    visible &= (ndc[:, 1] >= -1.0) & (ndc[:, 1] <= 1.0)
    visible &= (ndc[:, 2] >= -1.0) & (ndc[:, 2] <= 1.0)

    width = max(1, view.width())
    height = max(1, view.height())
    screen = np.empty((points.shape[0], 2), dtype=np.float64)
    screen[:, 0] = (ndc[:, 0] + 1.0) * 0.5 * width
    screen[:, 1] = (1.0 - ndc[:, 1]) * 0.5 * height
    return screen, visible, ndc[:, 2]


class PickingGLViewWidget(gl.GLViewWidget):
    pointClicked = Signal(QPointF)
    cameraInteractionFinished = Signal()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._press_position: QPointF | None = None
        self._press_button: Qt.MouseButton | None = None

    def mousePressEvent(self, event) -> None:
        self._press_position = event.position() if hasattr(event, "position") else event.localPos()
        self._press_button = event.button()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        release_position = event.position() if hasattr(event, "position") else event.localPos()
        if self._press_position is not None and self._press_button == Qt.MouseButton.LeftButton:
            start = (self._press_position.x(), self._press_position.y())
            end = (release_position.x(), release_position.y())
            if is_click(start, end):
                self.pointClicked.emit(release_position)
        self._press_position = None
        self._press_button = None
        self.cameraInteractionFinished.emit()
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        super().wheelEvent(event)
        self.cameraInteractionFinished.emit()
