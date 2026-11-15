from __future__ import annotations

from pathlib import Path
import struct

import numpy as np

from .models import PointCloudData, SourceKind


class PointCloudLoadError(ValueError):
    """Raised when an input file is not a supported labeled point-cloud format."""


def load_point_cloud(path: str | Path, source_id: str) -> PointCloudData:
    path = Path(path)
    if not path.is_file():
        raise PointCloudLoadError(f"File does not exist: {path}")

    name = path.name.lower()
    if name.startswith("pred_traj_"):
        return _load_pred_traj(path, source_id)
    if path.suffix.lower() != ".npy":
        raise PointCloudLoadError("Supported files are .npy and pred_traj_*.npy byte streams")

    try:
        array = np.load(path, allow_pickle=False)
    except Exception as exc:
        raise PointCloudLoadError(f"Unable to load NPY file: {exc}") from exc

    if array.ndim != 2:
        raise PointCloudLoadError(f"Expected a two-dimensional NPY array, got shape {array.shape}")

    if name.startswith("dense_labeled_"):
        return _load_dense(path, source_id, array)
    if name.startswith("occ"):
        return _load_occ(path, source_id, array)
    if name.startswith("pred_road_"):
        return _load_pred_road(path, source_id, array)

    # Convenient fallback for files renamed outside the evaluator directory layout.
    if array.shape[1] == 5:
        return _load_dense(path, source_id, array)
    if array.shape[1] == 4:
        return _load_pred_road(path, source_id, array)
    raise PointCloudLoadError(
        f"Cannot infer format from '{path.name}'. Expected [N,5] dense/OCC or [N,4] pred road array."
    )


def _require_columns(path: Path, array: np.ndarray, columns: int, description: str) -> None:
    if array.shape[1] != columns:
        raise PointCloudLoadError(
            f"{description} must have shape [N,{columns}], got {array.shape} in {path.name}"
        )
    if array.shape[0] == 0:
        raise PointCloudLoadError(f"{description} is empty: {path.name}")


def _make_cloud(path: Path, source_id: str, kind: SourceKind, array: np.ndarray) -> PointCloudData:
    return PointCloudData(
        source_id=source_id,
        display_name=path.name,
        kind=kind,
        xyz=array[:, :3].astype(np.float32, copy=False),
        labels=np.rint(array[:, 3]).astype(np.int32, copy=False),
        source_path=path,
    )


def _load_dense(path: Path, source_id: str, array: np.ndarray) -> PointCloudData:
    _require_columns(path, array, 5, "dense labeled cloud")
    return _make_cloud(path, source_id, SourceKind.DENSE_LABELED, array)


def _load_occ(path: Path, source_id: str, array: np.ndarray) -> PointCloudData:
    _require_columns(path, array, 5, "OCC cloud")
    return _make_cloud(path, source_id, SourceKind.OCC, array)


def _load_pred_road(path: Path, source_id: str, array: np.ndarray) -> PointCloudData:
    _require_columns(path, array, 4, "predicted road cloud")
    return _make_cloud(path, source_id, SourceKind.PRED_ROAD, array)


def _load_pred_traj(path: Path, source_id: str) -> PointCloudData:
    """Read the evaluator dump format: four int32 counts plus wheel-wise float32 [x,y,z,label]."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PointCloudLoadError(f"Unable to read prediction trajectory: {exc}") from exc

    # np.save(|u1) writes an NPY header. Skip it when present; tolerate a raw byte stream too.
    payload = _npy_payload(raw, path)
    if len(payload) < 16:
        raise PointCloudLoadError(f"pred_traj payload is shorter than 16-byte wheel-count header: {path.name}")

    counts = struct.unpack_from("<4i", payload, 0)
    if any(count < 0 for count in counts):
        raise PointCloudLoadError(f"pred_traj has negative wheel count {counts}: {path.name}")
    expected = 16 + sum(counts) * 16
    if len(payload) != expected:
        raise PointCloudLoadError(
            f"pred_traj size mismatch: header requires {expected} bytes, found {len(payload)} bytes in {path.name}"
        )

    points: list[np.ndarray] = []
    wheels: list[np.ndarray] = []
    offset = 16
    for wheel_id, count in enumerate(counts):
        if count:
            wheel_points = np.frombuffer(payload, dtype="<f4", count=count * 4, offset=offset).reshape(count, 4)
            points.append(wheel_points)
            wheels.append(np.full(count, wheel_id, dtype=np.int8))
        offset += count * 16

    if not points:
        raise PointCloudLoadError(f"pred_traj contains zero points for all wheels: {path.name}")

    array = np.concatenate(points, axis=0)
    return PointCloudData(
        source_id=source_id,
        display_name=path.name,
        kind=SourceKind.PRED_TRAJ,
        xyz=array[:, :3].astype(np.float32, copy=False),
        labels=np.rint(array[:, 3]).astype(np.int32, copy=False),
        wheel_ids=np.concatenate(wheels),
        source_path=path,
    )


def _npy_payload(raw: bytes, path: Path) -> memoryview:
    if not raw.startswith(b"\x93NUMPY"):
        return memoryview(raw)
    if len(raw) < 10:
        raise PointCloudLoadError(f"Invalid short NPY header: {path.name}")
    major, minor = raw[6], raw[7]
    if major == 1:
        header_length = struct.unpack_from("<H", raw, 8)[0]
        offset = 10 + header_length
    elif major in (2, 3):
        if len(raw) < 12:
            raise PointCloudLoadError(f"Invalid NPY v{major}.{minor} header: {path.name}")
        header_length = struct.unpack_from("<I", raw, 8)[0]
        offset = 12 + header_length
    else:
        raise PointCloudLoadError(f"Unsupported NPY version {major}.{minor}: {path.name}")
    if offset > len(raw):
        raise PointCloudLoadError(f"NPY header exceeds file size: {path.name}")
    return memoryview(raw)[offset:]
