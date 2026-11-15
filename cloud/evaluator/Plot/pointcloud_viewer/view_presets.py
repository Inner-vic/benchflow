from __future__ import annotations

from dataclasses import dataclass

import pyqtgraph.opengl as gl


@dataclass(frozen=True)
class ViewPreset:
    key: str
    display_name: str
    elevation: float
    azimuth: float
    is_planar: bool


VIEW_PRESETS: dict[str, ViewPreset] = {
    "yx": ViewPreset("yx", "Y-X", 90.0, 180.0, True),
    "xz": ViewPreset("xz", "X-Z", 0.0, 90.0, True),
    "yz": ViewPreset("yz", "Y-Z", 0.0, 180.0, True),
    "bird": ViewPreset("bird", "鸟瞰", 35.0, 180.0, False),
}


def apply_view_preset(view: gl.GLViewWidget, preset: ViewPreset) -> None:
    """Change only the observation direction; retain the current center and zoom distance."""
    view.opts["rotationMethod"] = "euler"
    view.setCameraPosition(
        pos=view.opts["center"],
        distance=view.opts["distance"],
        elevation=preset.elevation,
        azimuth=preset.azimuth,
    )
