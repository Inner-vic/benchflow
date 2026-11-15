from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

# Matches the class IDs used by the offline evaluator's IoU table.
LABEL_NAMES: dict[int, str] = {
    0: "invalid",
    1: "asphalt",
    2: "cement",
    3: "epoxy_floor",
    4: "brick_road",
    5: "gravel",
    6: "cobbles",
    7: "grass",
    8: "dirt",
    9: "hard_sand",
    10: "soft_sand",
    11: "water",
    12: "ice",
    13: "snow",
    14: "other_surface",
    15: "ignored_noise",
    16: "speed_bump",
    17: "manhole_cover",
    18: "steel_plate",
    19: "drain",
    20: "road_beam",
    21: "railway",
    22: "step",
    23: "bridge_joint",
    24: "moving_obstacle",
    25: "fixed_obstacle",
    26: "empty",
}

# Bright, distinct RGB colours.  Invalid/empty values intentionally use gray.
LABEL_COLORS: dict[int, tuple[int, int, int]] = {
    0: (120, 120, 120),
    1: (255, 215, 0),
    2: (0, 170, 255),
    3: (255, 105, 180),
    4: (255, 140, 0),
    5: (139, 69, 19),
    6: (200, 160, 120),
    7: (0, 220, 80),
    8: (150, 90, 45),
    9: (255, 238, 130),
    10: (244, 164, 96),
    11: (30, 144, 255),
    12: (175, 238, 238),
    13: (245, 250, 255),
    14: (170, 100, 255),
    15: (255, 80, 80),
    16: (255, 0, 180),
    17: (0, 230, 210),
    18: (0, 180, 150),
    19: (190, 80, 255),
    20: (230, 80, 30),
    21: (255, 70, 70),
    22: (255, 180, 0),
    23: (145, 40, 255),
    24: (255, 40, 110),
    25: (80, 220, 255),
    26: (105, 105, 105),
}

_FALLBACK_COLORS = np.array(
    [
        (255, 64, 64), (64, 255, 64), (64, 128, 255), (255, 64, 255),
        (255, 255, 64), (64, 255, 255), (255, 128, 64), (160, 64, 255),
    ],
    dtype=np.uint8,
)


@dataclass(frozen=True)
class LabelStyle:
    label: int
    name: str
    rgb: tuple[int, int, int]


def label_style(label: int) -> LabelStyle:
    label = int(label)
    color = LABEL_COLORS.get(label)
    if color is None:
        color = tuple(int(v) for v in _FALLBACK_COLORS[abs(label) % len(_FALLBACK_COLORS)])
    return LabelStyle(label, LABEL_NAMES.get(label, f"unknown_{label}"), color)


def colorize_labels(labels: np.ndarray, alpha: int = 255) -> np.ndarray:
    labels = np.asarray(labels, dtype=np.int32)
    rgba = np.empty((labels.size, 4), dtype=np.uint8)
    for label in np.unique(labels):
        mask = labels == label
        rgb = label_style(int(label)).rgb
        rgba[mask, :3] = rgb
    rgba[:, 3] = np.uint8(np.clip(alpha, 0, 255))
    return rgba


def all_known_labels(extra_labels: Iterable[int] = ()) -> list[int]:
    return sorted(set(LABEL_NAMES) | {int(label) for label in extra_labels})
