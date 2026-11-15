#!/usr/bin/env python3
"""Prepare selected replay scenes from a validated raw data archive.

The tool is designed to run on the Ubuntu workstation. It extracts only the
requested top-level scene directories, refuses links and unsafe paths, checks
the signal layout expected by RecorderHMI, and creates immutable per-run
configuration and evidence files. It never connects to or changes the bench.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from uuid import uuid4


SCENE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_.+$")
REQUIRED_DIRECTORIES = (
    "f_ca_120",
    "VAL_Lidar_0_PointCloud",
    "Val_Navigation_Ins",
    "odom_data_for_pcm",
    "eps_mode_sts",
)
REQUIRED_FILES = ("calib_cam_params.json", "calib_lidar_params.json")
SPACE_MARGIN_BYTES = 512 * 1024 * 1024


class PrepareError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_member_path(name: str) -> PurePosixPath:
    if "\\" in name:
        raise PrepareError(f"archive member uses a backslash path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise PrepareError(f"unsafe archive member path: {name!r}")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts:
        raise PrepareError("archive contains an empty member path")
    return PurePosixPath(*parts)


def inspect_archive(path: Path) -> dict[str, Any]:
    scenes: dict[str, dict[str, int]] = {}
    member_count = 0
    file_count = 0
    unpacked_bytes = 0

    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                member_count += 1
                member_path = safe_member_path(member.name)
                if member.isdev() or member.isfifo() or member.issym() or member.islnk():
                    raise PrepareError(f"unsupported archive member type: {member.name!r}")
                if not (member.isdir() or member.isfile()):
                    raise PrepareError(f"unknown archive member type: {member.name!r}")

                top_level = member_path.parts[0]
                if not SCENE_PATTERN.fullmatch(top_level):
                    raise PrepareError(f"unexpected top-level entry: {top_level!r}")
                stats = scenes.setdefault(top_level, {"members": 0, "files": 0, "bytes": 0})
                stats["members"] += 1
                if member.isfile():
                    stats["files"] += 1
                    stats["bytes"] += member.size
                    file_count += 1
                    unpacked_bytes += member.size
    except (tarfile.TarError, OSError) as exc:
        raise PrepareError(f"cannot read archive {path}: {exc}") from exc

    if not scenes or file_count == 0:
        raise PrepareError("archive contains no replay scene files")
    return {
        "member_count": member_count,
        "file_count": file_count,
        "unpacked_bytes": unpacked_bytes,
        "scenes": dict(sorted(scenes.items())),
    }


def copy_stream(source: BinaryIO, destination: Path) -> None:
    with destination.open("xb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def extract_scene(archive_path: Path, dataset_root: Path, scene: str) -> Path:
    if not SCENE_PATTERN.fullmatch(scene):
        raise PrepareError(f"invalid scene name: {scene!r}")
    dataset_root.mkdir(parents=True, exist_ok=True)
    final_scene = dataset_root / scene
    if final_scene.exists():
        raise PrepareError(f"scene destination already exists: {final_scene}")

    partial_root = dataset_root / f".{scene}.partial-{uuid4().hex[:8]}"
    partial_root.mkdir(parents=False, exist_ok=False)
    matched = 0
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                member_path = safe_member_path(member.name)
                if member_path.parts[0] != scene:
                    continue
                matched += 1
                if member.isdev() or member.isfifo() or member.issym() or member.islnk():
                    raise PrepareError(f"unsupported archive member type: {member.name!r}")
                target = partial_root.joinpath(*member_path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise PrepareError(f"unknown archive member type: {member.name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise PrepareError(f"cannot read archive member: {member.name!r}")
                with source:
                    copy_stream(source, target)
                os.chmod(target, member.mode & 0o777)
    except (tarfile.TarError, OSError) as exc:
        raise PrepareError(f"failed while extracting {scene}: {exc}") from exc

    extracted_scene = partial_root / scene
    if matched == 0 or not extracted_scene.is_dir():
        raise PrepareError(f"scene is not present in archive: {scene}")
    extracted_scene.rename(final_scene)
    partial_root.rmdir()
    return final_scene


def validate_scene(scene_root: Path) -> dict[str, Any]:
    raw_root = scene_root / "raw" / "raw_in_10s"
    missing: list[str] = []
    signals: dict[str, int] = {}
    for name in REQUIRED_DIRECTORIES:
        signal_dir = raw_root / name
        if not signal_dir.is_dir():
            missing.append(str(signal_dir))
            continue
        signals[name] = sum(1 for path in signal_dir.iterdir() if path.is_file())
        if signals[name] == 0:
            missing.append(f"{signal_dir} (empty)")
    for name in REQUIRED_FILES:
        required_file = raw_root / name
        if not required_file.is_file() or required_file.stat().st_size == 0:
            missing.append(str(required_file))
    if missing:
        raise PrepareError("scene is missing required replay input: " + ", ".join(missing))
    return {"raw_root": str(raw_root), "signal_file_counts": signals}


def build_replay_config(scene_root: Path) -> dict[str, Any]:
    raw_root = scene_root / "raw" / "raw_in_10s"
    raw_posix = raw_root.as_posix()
    return {
        "version": "1.0",
        "type": "replay_config",
        "fps": 10,
        "cameras": [{"camera_id": "2", "folder": f"{raw_posix}/f_ca_120"}],
        "lidars": [{"lidar_id": "0", "folder": f"{raw_posix}/VAL_Lidar_0_PointCloud"}],
        "topics": [
            {
                "topic_name": "/soa/adpositioninfo",
                "idl_file": "parse_vbs_topic/vbs_idl/ADPositionInfo.idl",
                "bin_folder": f"{raw_posix}/Val_Navigation_Ins",
                "struct_name": "GNSSLOCInfo",
            },
            {
                "topic_name": "OdomDataForPCM",
                "idl_file": "parse_vbs_topic/vbs_idl/IMUAndOdomDataService.idl",
                "bin_folder": f"{raw_posix}/odom_data_for_pcm",
                "struct_name": "OdomDataForPCM",
            },
            {
                "topic_name": "EPSModeSts",
                "idl_file": "parse_vbs_topic/vbs_idl/SteeringSystem.idl",
                "bin_folder": f"{raw_posix}/eps_mode_sts",
                "struct_name": "EPSModeSts",
            },
        ],
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely prepare selected RecorderHMI replay scenes")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory", help="Validate and list scenes without extraction")
    inventory_parser.add_argument("--archive", required=True, type=Path)
    inventory_parser.add_argument("--output", type=Path)

    prepare_parser = subparsers.add_parser("prepare", help="Extract scenes and create replay job files")
    prepare_parser.add_argument("--archive", required=True, type=Path)
    prepare_parser.add_argument("--expected-sha256", required=True)
    prepare_parser.add_argument("--dataset-root", required=True, type=Path)
    prepare_parser.add_argument("--job-dir", required=True, type=Path)
    prepare_parser.add_argument("--scene", required=True, action="append")
    args = parser.parse_args()

    archive_path = args.archive.expanduser().resolve()
    if not archive_path.is_file():
        parser.error(f"archive does not exist: {archive_path}")
    if not archive_path.name.endswith(".tar.gz"):
        parser.error("archive name must end with .tar.gz")

    inventory = inspect_archive(archive_path)
    if args.command == "inventory":
        result = {"archive": str(archive_path), **inventory}
        if args.output:
            write_json(args.output.expanduser().resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    expected_sha256 = args.expected_sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        parser.error("expected SHA-256 must contain exactly 64 hexadecimal characters")
    actual_sha256 = sha256_file(archive_path)
    if actual_sha256 != expected_sha256:
        raise PrepareError(
            f"archive SHA-256 mismatch: expected={expected_sha256}, actual={actual_sha256}"
        )

    requested_scenes = list(dict.fromkeys(args.scene))
    unknown_scenes = [scene for scene in requested_scenes if scene not in inventory["scenes"]]
    if unknown_scenes:
        raise PrepareError(f"requested scenes are not present: {unknown_scenes}")

    dataset_root = args.dataset_root.expanduser().resolve()
    job_dir = args.job_dir.expanduser().resolve()
    if job_dir.exists():
        raise PrepareError(f"job directory already exists: {job_dir}")
    dataset_root.mkdir(parents=True, exist_ok=True)
    required_bytes = sum(inventory["scenes"][scene]["bytes"] for scene in requested_scenes)
    required_bytes += SPACE_MARGIN_BYTES
    available_bytes = shutil.disk_usage(dataset_root).free
    if available_bytes < required_bytes:
        raise PrepareError(
            f"insufficient dataset space: need {required_bytes} bytes including margin, "
            f"available {available_bytes} bytes"
        )
    replay_config_dir = job_dir / "replay_configs"
    eval_scene_dir = job_dir / "eval_scene_dirs"
    replay_config_dir.mkdir(parents=True, exist_ok=False)
    eval_scene_dir.mkdir(parents=True, exist_ok=False)

    prepared: list[dict[str, Any]] = []
    for scene in requested_scenes:
        scene_root = extract_scene(archive_path, dataset_root, scene)
        validation = validate_scene(scene_root)
        config_path = replay_config_dir / f"replay_config_{scene}.json"
        write_json(config_path, build_replay_config(scene_root))
        (eval_scene_dir / scene).mkdir(parents=False, exist_ok=False)
        prepared.append(
            {
                "scene": scene,
                "dataset_path": str(scene_root),
                "replay_config": str(config_path),
                "bench_scene_directory_source": str(eval_scene_dir / scene),
                "archive_stats": inventory["scenes"][scene],
                **validation,
            }
        )

    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "state": "REPLAY_INPUT_PREPARED",
        "archive": {
            "path": str(archive_path),
            "size_bytes": archive_path.stat().st_size,
            "sha256": actual_sha256,
        },
        "prepared_scenes": prepared,
        "disk": {
            "available_bytes_before": available_bytes,
            "required_bytes_including_margin": required_bytes,
            "margin_bytes": SPACE_MARGIN_BYTES,
        },
        "safety": {
            "bench_contacted": False,
            "source_archive_modified": False,
            "existing_scene_overwritten": False,
        },
    }
    manifest_path = job_dir / "prepare-manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PrepareError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
