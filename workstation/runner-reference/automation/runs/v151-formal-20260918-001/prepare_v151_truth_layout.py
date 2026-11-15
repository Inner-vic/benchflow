#!/usr/bin/env python3
"""Normalize v151 truth directories for the approved offline evaluator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


SCENE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(scene_root: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(scene_root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(scene_root.rglob("*"), key=lambda item: item.relative_to(scene_root).as_posix())
        if path.is_file() and path.name != "truth-manifest.json"
    ]


def inventory_identity(files: list[dict[str, object]]) -> str:
    payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_manifest(scene_root: Path, scene: str) -> dict[str, object]:
    files = inventory(scene_root)
    if not files:
        raise SystemExit("truth scene contains no files")
    value = {
        "schema_version": 1,
        "scene": scene,
        "state": "READY",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file_count": len(files),
        "total_bytes": sum(int(item["size_bytes"]) for item in files),
        "inventory_sha256": inventory_identity(files),
    }
    path = scene_root / "truth-manifest.json"
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(pending, path)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth-root", required=True)
    parser.add_argument("--scene", required=True)
    args = parser.parse_args()

    if not SCENE_RE.fullmatch(args.scene):
        raise SystemExit("unsafe scene name")

    truth_root = Path(args.truth_root).resolve()
    scene_root = (truth_root / args.scene).resolve()
    if scene_root.parent != truth_root or not scene_root.is_dir():
        raise SystemExit("scene must be an existing direct child of truth root")
    dataset = scene_root / "dataset"
    if not dataset.is_dir():
        raise SystemExit("scene is missing the evaluator-required dataset directory")

    renamed = 0
    already_normalized = 0
    for frame_dir in sorted((item for item in dataset.iterdir() if item.is_dir()), key=lambda item: item.name):
        match = re.fullmatch(r"(\d+)(?:_(\d{13}))?", frame_dir.name)
        if not match:
            continue
        frame_index, existing_timestamp = match.groups()
        dense_pattern = re.compile(rf"dense_labeled_{frame_index}_frame_(\d{{16}})_.*\.npy$")
        timestamps = {
            dense_match.group(1)
            for item in frame_dir.iterdir()
            if item.is_file() and (dense_match := dense_pattern.fullmatch(item.name))
        }
        if len(timestamps) != 1:
            raise SystemExit(f"{frame_dir}: expected exactly one dense timestamp, found {sorted(timestamps)}")
        timestamp_ms = str(int(next(iter(timestamps))) // 1000)
        if existing_timestamp is not None:
            if existing_timestamp != timestamp_ms:
                raise SystemExit(f"{frame_dir}: directory and dense timestamps disagree")
            already_normalized += 1
            continue
        target = dataset / f"{frame_index}_{timestamp_ms}"
        if target.exists():
            raise SystemExit(f"refusing to overwrite {target}")
        frame_dir.rename(target)
        renamed += 1

    manifest = write_manifest(scene_root, args.scene)
    print(json.dumps({
        "scene": args.scene,
        "renamed": renamed,
        "already_normalized": already_normalized,
        "truth_manifest": manifest,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

