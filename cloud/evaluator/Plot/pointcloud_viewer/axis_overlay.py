from __future__ import annotations

import math

import numpy as np
import pyqtgraph.opengl as gl
from PySide6.QtGui import QColor, QFont


_AXIS_COLORS = {
    "x": (1.0, 0.25, 0.25, 0.95),
    "y": (0.25, 1.0, 0.35, 0.95),
    "z": (0.25, 0.55, 1.0, 0.95),
}


def nice_tick_step(span: float, target_ticks: int = 8, minimum: float = 0.01) -> float:
    """Return a 1-2-5 tick step no smaller than one centimetre."""
    if not math.isfinite(span) or span <= 0:
        return minimum
    raw = max(minimum, span / max(1, target_ticks))
    exponent = math.floor(math.log10(raw))
    scale = 10.0**exponent
    normalized = raw / scale
    base = 1.0 if normalized <= 1.0 else 2.0 if normalized <= 2.0 else 5.0 if normalized <= 5.0 else 10.0
    return max(minimum, base * scale)


def tick_values(lower: float, upper: float, step: float, max_ticks: int = 14) -> np.ndarray:
    if not (math.isfinite(lower) and math.isfinite(upper) and step > 0):
        return np.empty(0, dtype=np.float64)
    lower, upper = min(lower, upper), max(lower, upper)
    first = math.ceil((lower - 1e-9) / step) * step
    count = int(math.floor((upper - first) / step + 1e-9)) + 1
    if count <= 0:
        return np.empty(0, dtype=np.float64)
    if count > max_ticks:
        stride = int(math.ceil(count / max_ticks))
        step *= stride
        first = math.ceil((lower - 1e-9) / step) * step
        count = int(math.floor((upper - first) / step + 1e-9)) + 1
    return first + np.arange(max(0, count), dtype=np.float64) * step


def _format_tick(value: float, step: float) -> str:
    if step < 0.1:
        return f"{value:.2f}"
    if step < 1.0:
        return f"{value:.1f}"
    return f"{value:.0f}" if abs(value - round(value)) < 1e-8 else f"{value:.1f}"


class AdaptiveAxisOverlay:
    """Small, readable x/y/z axes with adaptive numeric tick labels."""

    def __init__(self, view: gl.GLViewWidget) -> None:
        self._view = view
        self._lines: dict[str, gl.GLLinePlotItem] = {}
        self._ticks: dict[str, gl.GLLinePlotItem] = {}
        self._labels: list[gl.GLTextItem] = []
        self._unit = "m"
        for axis, color in _AXIS_COLORS.items():
            self._lines[axis] = gl.GLLinePlotItem(color=color, width=2.0, antialias=True, mode="lines")
            self._ticks[axis] = gl.GLLinePlotItem(color=color, width=1.0, antialias=True, mode="lines")
            view.addItem(self._lines[axis])
            view.addItem(self._ticks[axis])

    def clear(self) -> None:
        for item in [*self._lines.values(), *self._ticks.values()]:
            item.setData(pos=np.empty((0, 3), dtype=np.float32))
        for label in self._labels:
            self._view.removeItem(label)
        self._labels.clear()

    def update(self, lower: np.ndarray | None, upper: np.ndarray | None, unit: str = "m") -> None:
        self.clear()
        if lower is None or upper is None:
            return
        lower = np.asarray(lower, dtype=np.float64)
        upper = np.asarray(upper, dtype=np.float64)
        if lower.shape != (3,) or upper.shape != (3,) or not np.all(np.isfinite(lower)) or not np.all(np.isfinite(upper)):
            return
        self._unit = unit
        span = np.maximum(upper - lower, 0.01)
        # Keep axes just outside the lower/back edge of the visible cloud.
        origin = np.array([lower[0], lower[1], lower[2]], dtype=np.float64)
        pad = np.maximum(span * 0.04, 0.02)
        x_start = np.array([lower[0], lower[1] - pad[1], lower[2] - pad[2]])
        y_start = np.array([lower[0] - pad[0], lower[1], lower[2] - pad[2]])
        z_start = np.array([lower[0] - pad[0], lower[1] - pad[1], lower[2]])
        starts = {"x": x_start, "y": y_start, "z": z_start}

        for axis_index, axis in enumerate(("x", "y", "z")):
            start = starts[axis]
            end = start.copy()
            end[axis_index] = upper[axis_index]
            self._lines[axis].setData(pos=np.vstack((start, end)).astype(np.float32))

            step = nice_tick_step(float(span[axis_index]))
            values = tick_values(float(lower[axis_index]), float(upper[axis_index]), step)
            tick_half = max(float(span.max()) * 0.008, step * 0.12, 0.01)
            segments: list[np.ndarray] = []
            for value in values:
                point = start.copy()
                point[axis_index] = value
                tick_a, tick_b = point.copy(), point.copy()
                perpendicular = 1 if axis_index == 0 else 0
                tick_a[perpendicular] -= tick_half
                tick_b[perpendicular] += tick_half
                segments.extend((tick_a, tick_b))
                self._add_label(axis, point, _format_tick(float(value), step))
            self._ticks[axis].setData(pos=np.asarray(segments, dtype=np.float32), mode="lines")
            self._add_axis_label(axis, end)

    def _add_label(self, axis: str, position: np.ndarray, text: str) -> None:
        offset = np.array([0.0, 0.0, 0.0])
        if axis == "x":
            offset[1] -= 0.04
        elif axis == "y":
            offset[0] -= 0.04
        else:
            offset[0] -= 0.04
        item = gl.GLTextItem(
            pos=tuple(map(float, position + offset)),
            text=f"{text} {self._unit}",
            color=QColor(235, 235, 235),
            font=QFont("Arial", 8),
        )
        self._view.addItem(item)
        self._labels.append(item)

    def _add_axis_label(self, axis: str, position: np.ndarray) -> None:
        color = _AXIS_COLORS[axis]
        rgb = QColor.fromRgbF(color[0], color[1], color[2])
        item = gl.GLTextItem(
            pos=tuple(map(float, position)),
            text=f"{axis.upper()} ({self._unit})",
            color=rgb,
            font=QFont("Arial", 10, QFont.Weight.Bold),
        )
        self._view.addItem(item)
        self._labels.append(item)
