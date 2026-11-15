from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


_TIMESTAMP_MIN = 1_000_000_000_000  # 13-digit millisecond timestamps


class SceneIndexError(ValueError):
    """Raised when selected scene directories cannot be scanned."""


@dataclass(frozen=True)
class FramePair:
    timestamp_ms: int
    display_name: str
    gt_frame_dir: Path
    pred_frame_dir: Path
    gt_dense_path: Path
    pred_traj_path: Path
    image_path: Path | None = None


@dataclass(frozen=True)
class SceneScanResult:
    gt_scene_dir: Path
    gt_dataset_dir: Path
    pred_scene_dir: Path
    gt_frame_count: int
    pred_frame_count: int
    matching_timestamp_count: int
    pairs: tuple[FramePair, ...]


@dataclass(frozen=True)
class SceneDirectoryPair:
    scene_name: str
    gt_scene_dir: Path
    pred_scene_dir: Path


def find_same_named_scene_dirs(gt_scene_root: str | Path, pred_scene_root: str | Path) -> tuple[SceneDirectoryPair, ...]:
    gt_scene_root = Path(gt_scene_root)
    pred_scene_root = Path(pred_scene_root)
    if not gt_scene_root.is_dir() or not pred_scene_root.is_dir():
        raise SceneIndexError("真值或预测场景根目录不存在")
    gt_names = {entry.name for entry in gt_scene_root.iterdir() if entry.is_dir()}
    pred_names = {entry.name for entry in pred_scene_root.iterdir() if entry.is_dir()}
    return tuple(
        SceneDirectoryPair(scene_name, gt_scene_root / scene_name, pred_scene_root / scene_name)
        for scene_name in sorted(gt_names & pred_names)
    )


@dataclass(frozen=True)
class MatchedScenePair:
    scene_name: str
    gt_scene_dir: Path
    pred_scene_dir: Path
    scan_result: SceneScanResult


def find_matched_scene_pairs(gt_scene_root: str | Path, pred_scene_root: str | Path) -> tuple[MatchedScenePair, ...]:
    gt_scene_root = Path(gt_scene_root)
    pred_scene_root = Path(pred_scene_root)
    if not gt_scene_root.is_dir() or not pred_scene_root.is_dir():
        raise SceneIndexError("真值或预测场景根目录不存在")

    gt_names = {entry.name for entry in gt_scene_root.iterdir() if entry.is_dir()}
    pred_names = {entry.name for entry in pred_scene_root.iterdir() if entry.is_dir()}
    pairs: list[MatchedScenePair] = []
    for scene_name in sorted(gt_names & pred_names):
        gt_scene_dir = gt_scene_root / scene_name
        pred_scene_dir = pred_scene_root / scene_name
        try:
            scan_result = build_frame_pairs(gt_scene_dir, pred_scene_dir)
        except SceneIndexError:
            continue
        if scan_result.pairs:
            pairs.append(MatchedScenePair(scene_name, gt_scene_dir, pred_scene_dir, scan_result))
    return tuple(pairs)


def resolve_gt_dataset_dir(scene_dir: str | Path) -> Path:
    scene_dir = Path(scene_dir)
    if not scene_dir.is_dir():
        raise SceneIndexError(f"真值场景目录不存在或不是文件夹：{scene_dir}")
    dataset_dir = scene_dir / "dataset"
    return dataset_dir if dataset_dir.is_dir() else scene_dir


def parse_frame_timestamp(directory_name: str) -> int | None:
    """Parse <index>_<13-digit-ms> or a pure 13-digit millisecond timestamp."""
    match = re.fullmatch(r"(?:\d+_)?(\d+)", directory_name)
    if match is None:
        return None
    try:
        timestamp_ms = int(match.group(1))
    except ValueError:
        return None
    return timestamp_ms if timestamp_ms >= _TIMESTAMP_MIN else None


def find_first_with_prefix(directory: Path, prefix: str, suffix: str = ".npy") -> Path | None:
    if not directory.is_dir():
        return None
    return next(
        (
            entry
            for entry in sorted(directory.iterdir(), key=lambda item: item.name)
            if entry.is_file() and entry.name.startswith(prefix) and entry.name.endswith(suffix)
        ),
        None,
    )


def find_truth_image(frame_dir: Path) -> Path | None:
    """Return the preferred truth-frame image: f_ca_120 first, otherwise name-sorted."""
    if not frame_dir.is_dir():
        return None
    images = sorted(
        (
            entry
            for entry in frame_dir.iterdir()
            if entry.is_file() and entry.suffix.lower() in {".jpg", ".jpeg", ".png"}
        ),
        key=lambda item: item.name.lower(),
    )
    if not images:
        return None
    return next((image for image in images if "f_ca_120" in image.name.lower()), images[0])


def build_frame_pairs(gt_scene_dir: str | Path, pred_scene_dir: str | Path) -> SceneScanResult:
    gt_scene_dir = Path(gt_scene_dir)
    pred_scene_dir = Path(pred_scene_dir)
    gt_dataset_dir = resolve_gt_dataset_dir(gt_scene_dir)
    if not pred_scene_dir.is_dir():
        raise SceneIndexError(f"预测场景目录不存在或不是文件夹：{pred_scene_dir}")

    gt_frames = _index_frames(gt_dataset_dir, "dense_labeled_")
    pred_frames = _index_frames(pred_scene_dir, "pred_traj_")
    common_timestamps = sorted(set(gt_frames) & set(pred_frames))
    pairs = tuple(
        FramePair(
            timestamp_ms=timestamp,
            display_name=gt_frames[timestamp][0].name,
            gt_frame_dir=gt_frames[timestamp][0],
            pred_frame_dir=pred_frames[timestamp][0],
            gt_dense_path=gt_frames[timestamp][1],
            pred_traj_path=pred_frames[timestamp][1],
            image_path=find_truth_image(gt_frames[timestamp][0]),
        )
        for timestamp in common_timestamps
    )
    return SceneScanResult(
        gt_scene_dir=gt_scene_dir,
        gt_dataset_dir=gt_dataset_dir,
        pred_scene_dir=pred_scene_dir,
        gt_frame_count=len(gt_frames),
        pred_frame_count=len(pred_frames),
        matching_timestamp_count=len(common_timestamps),
        pairs=pairs,
    )


def _index_frames(root: Path, required_prefix: str) -> dict[int, tuple[Path, Path]]:
    indexed: dict[int, tuple[Path, Path]] = {}
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        timestamp_ms = parse_frame_timestamp(entry.name)
        if timestamp_ms is None:
            continue
        required_file = find_first_with_prefix(entry, required_prefix)
        if required_file is None:
            continue
        # If duplicate timestamp directories exist, retain the lexicographically first deterministic entry.
        indexed.setdefault(timestamp_ms, (entry, required_file))
    return indexed
