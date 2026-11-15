#!/usr/bin/env python3
"""Validate offline-evaluation prediction output before transfer."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FRAME_PATTERN = re.compile(r"^(?P<index>\d+)_(?P<timestamp>\d{10,16})$")
ALLOWED_SUFFIXES = {".npy", ".json"}
TASK_PREFIXES = {
    "trajectory": ("pred_traj_",),
    "vision": ("pred_img_",),
}


class PredictionError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_scene(
    scene_root: Path,
    *,
    expected_frames: int | None = None,
    required_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not scene_root.is_dir():
        raise PredictionError(f"scene output does not exist: {scene_root}")
    frames: list[dict[str, Any]] = []
    invalid_entries: list[str] = []
    for entry in scene_root.iterdir():
        if not entry.is_dir():
            invalid_entries.append(entry.name)
            continue
        match = FRAME_PATTERN.fullmatch(entry.name)
        if not match:
            invalid_entries.append(entry.name)
            continue
        files = [path for path in entry.iterdir() if path.is_file() and path.suffix in ALLOWED_SUFFIXES]
        empty = sorted(path.name for path in files if path.stat().st_size == 0)
        if empty:
            raise PredictionError(f"zero-byte prediction files in {entry}: {empty}")
        if not files:
            raise PredictionError(f"frame directory contains no prediction files: {entry}")
        names = [path.name for path in files]
        missing_prefixes = [prefix for prefix in required_prefixes if not any(name.startswith(prefix) for name in names)]
        if missing_prefixes:
            raise PredictionError(f"frame {entry.name} is missing required outputs: {missing_prefixes}")
        frames.append(
            {
                "directory": entry.name,
                "index": int(match.group("index")),
                "timestamp": match.group("timestamp"),
                "files": sorted(names),
                "bytes": sum(path.stat().st_size for path in files),
            }
        )
    if invalid_entries:
        raise PredictionError(f"unexpected entries below scene output: {sorted(invalid_entries)}")
    if not frames:
        raise PredictionError(f"scene contains no frame directories: {scene_root}")
    frames.sort(key=lambda item: item["index"])
    indices = [item["index"] for item in frames]
    if len(indices) != len(set(indices)):
        raise PredictionError(f"duplicate frame indices: {indices}")
    if expected_frames is not None:
        if expected_frames <= 0:
            raise PredictionError("expected frame count must be positive")
        if len(frames) != expected_frames:
            raise PredictionError(f"frame count mismatch: expected={expected_frames}, actual={len(frames)}")
        expected_indices = list(range(expected_frames))
        if indices != expected_indices:
            raise PredictionError(f"frame indices are not contiguous 0..{expected_frames - 1}: {indices}")
    return {
        "scene": scene_root.name,
        "path": str(scene_root),
        "frame_count": len(frames),
        "first_index": indices[0],
        "last_index": indices[-1],
        "total_bytes": sum(item["bytes"] for item in frames),
        "required_prefixes": list(required_prefixes),
        "frames": frames,
    }


def validate_predictions(
    root: Path,
    scenes: list[str],
    *,
    expected_frames: int | None,
    required_prefixes: tuple[str, ...],
    required_tasks: tuple[str, ...] = (),
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise PredictionError(f"prediction root does not exist: {root}")
    requested = list(dict.fromkeys(scenes))
    if not requested:
        requested = sorted(path.name for path in root.iterdir() if path.is_dir())
    if not requested:
        raise PredictionError(f"prediction root contains no scenes: {root}")
    unknown_tasks = sorted(set(required_tasks) - set(TASK_PREFIXES))
    if unknown_tasks:
        raise PredictionError(f"unsupported required prediction tasks: {unknown_tasks}")
    effective_prefixes = tuple(dict.fromkeys(
        [*required_prefixes, *(prefix for task in required_tasks for prefix in TASK_PREFIXES[task])]
    ))
    results = [
        validate_scene(root / scene, expected_frames=expected_frames, required_prefixes=effective_prefixes)
        for scene in requested
    ]
    return {
        "schema_version": 1,
        "checked_at": utc_now(),
        "state": "PREDICTION_OUTPUT_VALID",
        "root": str(root),
        "scene_count": len(results),
        "frame_count": sum(item["frame_count"] for item in results),
        "total_bytes": sum(item["total_bytes"] for item in results),
        "required_tasks": list(required_tasks),
        "required_prefixes": list(effective_prefixes),
        "scenes": results,
        "transfer_eligible": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate prediction output before transfer")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--scene", action="append", default=[])
    parser.add_argument("--expected-frames", type=int)
    parser.add_argument("--require-prefix", action="append", default=[])
    parser.add_argument("--required-task", action="append", default=[], choices=sorted(TASK_PREFIXES))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_predictions(
        args.root,
        args.scene,
        expected_frames=args.expected_frames,
        required_prefixes=tuple(args.require_prefix),
        required_tasks=tuple(args.required_task),
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PredictionError as exc:
        print(json.dumps({"state": "PREDICTION_OUTPUT_INVALID", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
