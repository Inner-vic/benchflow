from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np


class SourceKind(str, Enum):
    DENSE_LABELED = "dense_labeled"
    OCC = "occ"
    PRED_ROAD = "pred_road"
    PRED_TRAJ = "pred_traj"

    @property
    def coordinate_description(self) -> str:
        if self is SourceKind.OCC:
            return "OCC grid indices (not physical metres)"
        return "physical coordinates (metres)"


@dataclass
class PointCloudData:
    source_id: str
    display_name: str
    kind: SourceKind
    xyz: np.ndarray
    labels: np.ndarray
    source_path: Path
    wheel_ids: Optional[np.ndarray] = None

    def __post_init__(self) -> None:
        self.xyz = np.asarray(self.xyz, dtype=np.float32)
        self.labels = np.asarray(self.labels, dtype=np.int32)
        if self.xyz.ndim != 2 or self.xyz.shape[1] != 3:
            raise ValueError(f"xyz must have shape [N, 3], got {self.xyz.shape}")
        if self.labels.ndim != 1 or self.labels.shape[0] != self.xyz.shape[0]:
            raise ValueError("labels must be one-dimensional and match xyz rows")
        if self.wheel_ids is not None:
            self.wheel_ids = np.asarray(self.wheel_ids, dtype=np.int8)
            if self.wheel_ids.shape != self.labels.shape:
                raise ValueError("wheel_ids must match labels shape")

    @property
    def point_count(self) -> int:
        return int(self.xyz.shape[0])

    @property
    def label_counts(self) -> dict[int, int]:
        values, counts = np.unique(self.labels, return_counts=True)
        return {int(label): int(count) for label, count in zip(values, counts)}
