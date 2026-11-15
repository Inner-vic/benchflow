from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyqtgraph.opengl as gl
from PySide6.QtGui import QVector3D

from .models import PointCloudData
from .palette import colorize_labels


@dataclass
class RenderSettings:
    enabled: bool = True
    point_size: float = 3.0
    opacity: int = 220


class PointCloudRenderer:
    """Owns one OpenGL scatter item per source and refreshes it after filtering."""

    def __init__(self, view: gl.GLViewWidget, source_id: str) -> None:
        self._view = view
        self.source_id = source_id
        self.cloud: PointCloudData | None = None
        self.settings = RenderSettings()
        self._item = gl.GLScatterPlotItem(pxMode=True)
        self._view.addItem(self._item)
        self.last_filtered_count = 0
        self.last_drawn_count = 0
        self.was_sampled = False
        self.drawn_indices = np.empty(0, dtype=np.int64)

    def set_cloud(self, cloud: PointCloudData | None) -> None:
        self.cloud = cloud

    def refresh(self, selected_labels: set[int], point_limit: int, show_invalid: bool) -> None:
        if self.cloud is None or not self.settings.enabled:
            self._clear()
            return

        labels = self.cloud.labels
        mask = np.isin(labels, list(selected_labels))
        if not show_invalid:
            mask &= (labels != 0) & (labels != 26)
        indices = np.flatnonzero(mask)
        self.last_filtered_count = int(indices.size)
        indices = stratified_sample_indices(labels, indices, point_limit)
        self.drawn_indices = indices
        self.last_drawn_count = int(indices.size)
        self.was_sampled = self.last_drawn_count < self.last_filtered_count

        if not indices.size:
            self._clear()
            return

        rgba = colorize_labels(labels[indices], self.settings.opacity).astype(np.float32) / 255.0
        self._item.setData(
            pos=self.cloud.xyz[indices],
            color=rgba,
            size=max(1.0, float(self.settings.point_size)),
            pxMode=True,
        )

    def pickable_points(self) -> tuple[np.ndarray, np.ndarray]:
        """Return the currently displayed original indices and their xyz positions."""
        if self.cloud is None or self.drawn_indices.size == 0:
            return self.drawn_indices, np.empty((0, 3), dtype=np.float32)
        return self.drawn_indices, self.cloud.xyz[self.drawn_indices]

    def _clear(self) -> None:
        self.last_filtered_count = 0
        self.last_drawn_count = 0
        self.was_sampled = False
        self.drawn_indices = np.empty(0, dtype=np.int64)
        self._item.setData(pos=np.empty((0, 3), dtype=np.float32))


def stratified_sample_indices(labels: np.ndarray, indices: np.ndarray, limit: int) -> np.ndarray:
    """Cap points while preserving every visible label when capacity permits."""
    if limit <= 0 or indices.size <= limit:
        return indices

    visible_labels, inverse, counts = np.unique(labels[indices], return_inverse=True, return_counts=True)
    # Allocate at least one sample per label. The rest is proportional to label frequency.
    allocations = np.ones(len(visible_labels), dtype=np.int64)
    remaining = max(0, limit - len(visible_labels))
    if remaining:
        ideal = counts / counts.sum() * remaining
        additions = np.floor(ideal).astype(np.int64)
        allocations += additions
        remainder = remaining - int(additions.sum())
        if remainder:
            allocations[np.argsort(-(ideal - additions))[:remainder]] += 1
    allocations = np.minimum(allocations, counts)

    rng = np.random.default_rng(20260818)
    selected: list[np.ndarray] = []
    for label_index, count in enumerate(allocations):
        group = indices[inverse == label_index]
        if count < group.size:
            selected.append(rng.choice(group, size=count, replace=False))
        else:
            selected.append(group)
    return np.concatenate(selected) if selected else np.empty(0, dtype=np.int64)


def reset_camera(view: gl.GLViewWidget, clouds: list[PointCloudData | None], selected_labels: set[int], show_invalid: bool) -> None:
    visible: list[np.ndarray] = []
    for cloud in clouds:
        if cloud is None:
            continue
        mask = np.isin(cloud.labels, list(selected_labels))
        if not show_invalid:
            mask &= (cloud.labels != 0) & (cloud.labels != 26)
        if np.any(mask):
            # Camera framing does not need every point.
            candidate = cloud.xyz[mask]
            visible.append(candidate[:: max(1, candidate.shape[0] // 20_000)])
    if not visible:
        return

    points = np.concatenate(visible, axis=0)
    lower, upper = points.min(axis=0), points.max(axis=0)
    center = (lower + upper) / 2.0
    extent = float(np.linalg.norm(upper - lower))
    view.setCameraPosition(pos=QVector3D(*map(float, center)), distance=max(5.0, extent * 1.5))
