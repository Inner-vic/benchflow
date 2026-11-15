from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

_SETTINGS_ORGANIZATION = "uroad"
_SETTINGS_APPLICATION = "pointcloud_viewer"
_GT_SCENE_KEY = "scene_browser/ground_truth_scene"
_PRED_SCENE_KEY = "scene_browser/prediction_scene"
_FRAME_TIMESTAMP_KEY = "scene_browser/last_timestamp_ms"


def _settings() -> QSettings:
    return QSettings(_SETTINGS_ORGANIZATION, _SETTINGS_APPLICATION)


def load_last_scene_state() -> tuple[Path | None, Path | None, int | None]:
    settings = _settings()
    gt_value = settings.value(_GT_SCENE_KEY, "", type=str)
    pred_value = settings.value(_PRED_SCENE_KEY, "", type=str)
    timestamp_value = settings.value(_FRAME_TIMESTAMP_KEY, None)
    gt_scene = Path(gt_value) if gt_value and Path(gt_value).is_dir() else None
    pred_scene = Path(pred_value) if pred_value and Path(pred_value).is_dir() else None
    try:
        timestamp_ms = int(timestamp_value) if timestamp_value is not None else None
    except (TypeError, ValueError):
        timestamp_ms = None
    return gt_scene, pred_scene, timestamp_ms


def save_scene_state(gt_scene: Path | None, pred_scene: Path | None, timestamp_ms: int | None) -> None:
    settings = _settings()
    settings.setValue(_GT_SCENE_KEY, str(gt_scene) if gt_scene is not None else "")
    settings.setValue(_PRED_SCENE_KEY, str(pred_scene) if pred_scene is not None else "")
    if timestamp_ms is None:
        settings.remove(_FRAME_TIMESTAMP_KEY)
    else:
        settings.setValue(_FRAME_TIMESTAMP_KEY, str(timestamp_ms))
    settings.sync()
