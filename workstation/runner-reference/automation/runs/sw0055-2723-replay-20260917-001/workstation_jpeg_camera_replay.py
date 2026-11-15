#!/usr/bin/env python3
"""Replay RecorderHMI lidar/topics while sending predecoded JPEG camera frames."""

from __future__ import annotations

import argparse
from bisect import bisect_right
import json
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import grpc

import xcam_replay_data_pb2 as data_pb2
import xcam_replay_service_pb2_grpc as service_pb2_grpc


class ReplayFailure(RuntimeError):
    pass


class RecorderClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        encoded = None
        headers: dict[str, str] = {}
        if payload is not None:
            encoded = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + endpoint, data=encoded, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayFailure(f"{method} {endpoint} failed: {exc}") from exc
        if not isinstance(result, dict) or result.get("code") != 200:
            raise ReplayFailure(f"{method} {endpoint} rejected: {result}")
        return result

    def status(self) -> dict[str, Any]:
        return self.request("GET", "/replay/status")

    def connect(self, camera: str, lidar: str) -> dict[str, Any]:
        return self.request("POST", "/replay/connect", {"camera": camera, "lidar": lidar})

    def load(self, config: Path) -> dict[str, Any]:
        return self.request("POST", "/replay/load", {"config_file": str(config)})

    def start(self, mode: str, speed: float) -> dict[str, Any]:
        return self.request("POST", "/replay/start", {"mode": mode, "speed": speed})

    def stop(self) -> dict[str, Any]:
        return self.request("POST", "/replay/stop")


def load_camera_metadata(index_path: Path, frame_dir: Path) -> tuple[dict[str, Any], list[Path]]:
    metadata = json.loads(index_path.read_text(encoding="utf-8"))
    frames = sorted(frame_dir.glob("frame_*.jpg"))
    timestamps = metadata.get("timestamps_ns")
    if not isinstance(timestamps, list) or not timestamps:
        raise ReplayFailure("camera index has no timestamps_ns")
    if len(frames) != len(timestamps):
        raise ReplayFailure(f"JPEG/index frame count mismatch: jpeg={len(frames)} timestamps={len(timestamps)}")
    return metadata, frames


def load_lidar_timestamps(config_path: Path) -> list[int]:
    """Read LiDAR frame timestamps from RecorderHMI's LPAK container."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    lidars = config.get("lidars")
    if not isinstance(lidars, list) or not lidars:
        raise ReplayFailure("replay config has no lidar source")
    folder = Path(lidars[0]["folder"])
    packs = sorted(folder.glob("*.pack"))
    if len(packs) != 1:
        raise ReplayFailure(f"expected one LiDAR pack in {folder}, found {len(packs)}")

    timestamps: list[int] = []
    with packs[0].open("rb") as stream:
        header = stream.read(16)
        if len(header) != 16 or header[:4] != b"LPAK":
            raise ReplayFailure(f"invalid LiDAR pack header: {packs[0]}")
        frame_count = struct.unpack_from("<H", header, 6)[0]
        for frame_index in range(frame_count):
            frame_header = stream.read(24)
            if len(frame_header) != 24:
                raise ReplayFailure(f"truncated LiDAR frame header at {frame_index}")
            timestamp_ns, _sequence, _points, payload_size = struct.unpack("<QIIQ", frame_header)
            timestamps.append(timestamp_ns)
            stream.seek(payload_size, 1)
    if not timestamps:
        raise ReplayFailure("LiDAR pack contains no frames")
    return timestamps


def pair_camera_to_lidar(
    camera_timestamps: list[int],
    frames: list[Path],
    lidar_timestamps: list[int],
    terminal_fallback_max_ns: int | None = None,
) -> tuple[list[int], list[Path]]:
    """Choose the first camera frame strictly after each LiDAR scan timestamp."""
    selected_timestamps: list[int] = []
    selected_frames: list[Path] = []
    previous_index = -1
    for lidar_index, lidar_timestamp in enumerate(lidar_timestamps):
        camera_index = bisect_right(camera_timestamps, lidar_timestamp, lo=previous_index + 1)
        if camera_index >= len(camera_timestamps):
            terminal_gap = lidar_timestamp - camera_timestamps[-1]
            terminal_fallback = (
                terminal_fallback_max_ns is not None
                and lidar_index == len(lidar_timestamps) - 1
                and 0 <= terminal_gap <= terminal_fallback_max_ns
            )
            if terminal_fallback:
                if previous_index < len(camera_timestamps) - 1:
                    camera_index = len(camera_timestamps) - 1
                else:
                    # Do not fabricate a timestamp or send the same image
                    # twice. A bounded terminal LiDAR scan may legitimately
                    # have no following camera sample at the recording edge.
                    continue
            else:
                raise ReplayFailure(
                    f"no camera frame after LiDAR timestamp {lidar_timestamp}; "
                    f"paired={len(selected_frames)}/{len(lidar_timestamps)}; "
                    f"terminal_gap_ns={terminal_gap}"
                )
        selected_timestamps.append(camera_timestamps[camera_index])
        selected_frames.append(frames[camera_index])
        previous_index = camera_index
    return selected_timestamps, selected_frames


def send_camera_frames(
    *,
    target: str,
    config_path: Path,
    index_path: Path,
    frame_dir: Path,
    base_url: str,
    start_event: threading.Event,
    result: dict[str, Any],
    terminal_fallback_ms: float = 0.0,
) -> None:
    sent = 0
    try:
        metadata, frames = load_camera_metadata(index_path, frame_dir)
        all_timestamps = [int(value) for value in metadata["timestamps_ns"]]
        lidar_timestamps = load_lidar_timestamps(config_path)
        fallback_ns = int(terminal_fallback_ms * 1_000_000) if terminal_fallback_ms > 0 else None
        timestamps, frames = pair_camera_to_lidar(
            all_timestamps, frames, lidar_timestamps, terminal_fallback_max_ns=fallback_ns
        )
        result.update(
            {
                "pairing": "first-camera-after-lidar-with-bounded-terminal-fallback" if fallback_ns else "first-camera-after-lidar",
                "terminal_fallback_max_ns": fallback_ns,
                "source_camera_frames": len(all_timestamps),
                "lidar_frames": len(lidar_timestamps),
                "terminal_camera_omitted": len(lidar_timestamps) - len(frames),
            }
        )
        channel = grpc.insecure_channel(
            target,
            options=(("grpc.max_send_message_length", 64 * 1024 * 1024), ("grpc.max_receive_message_length", 64 * 1024 * 1024)),
        )
        grpc.channel_ready_future(channel).result(timeout=20)
        stub = service_pb2_grpc.SensorDataServiceStub(channel)
        configured = stub.ConfigureReplayMode(
            data_pb2.ConfigureReplayRequest(mode=data_pb2.FRAME_BY_FRAME_MODE), timeout=10
        )
        if not configured.success:
            raise ReplayFailure(f"camera mode configuration failed: {configured.message}")

        result.update({"ready": True})
        if not start_event.wait(timeout=30):
            raise ReplayFailure("camera sender start signal timed out")

        recorder = RecorderClient(base_url, timeout=5)
        result.update({"sync": "recorder-current-frame-index"})
        for sequence, (timestamp_ns, frame_path) in enumerate(zip(timestamps, frames)):
            wait_deadline = time.monotonic() + 30.0
            while True:
                replay_state = recorder.status().get("data", {})
                state_name = replay_state.get("state")
                current = int(replay_state.get("current_frame_index", -1))
                if state_name in ("ERROR", "FAILED"):
                    raise ReplayFailure(f"recorder failed while waiting for camera frame {sequence}: {replay_state}")
                if state_name in ("PLAYING", "STOPPED") and current >= sequence:
                    break
                if time.monotonic() >= wait_deadline:
                    raise ReplayFailure(
                        f"timed out waiting for LiDAR frame {sequence}: {replay_state}"
                    )
                time.sleep(0.01)
            response = stub.ReplayCameraFrame(
                data_pb2.CameraFrame(
                    camera_id=int(metadata["camera_id"]),
                    # The xcamera replay adapter forwards this proto field into
                    # Uroad's sensor_timestamp_us without converting units.
                    # Recorder indices store epoch nanoseconds, so send epoch
                    # microseconds here even though the proto field says `_ns`.
                    timestamp_ns=timestamp_ns // 1_000,
                    # xcamera 0.4.2 decodes the JPEG but forwards the dimensions
                    # supplied by gRPC, so send the source frame dimensions.
                    width=int(metadata["width"]),
                    height=int(metadata["height"]),
                    image_data=frame_path.read_bytes(),
                    sequence_number=sequence,
                ),
                timeout=15,
            )
            if not response.success:
                raise ReplayFailure(f"camera frame {sequence} rejected: {response.message}")
            sent += 1
            result.update({"sent": sent, "total": len(frames)})
        result.update({"sent": sent, "total": len(frames), "ok": True})
        channel.close()
    except BaseException as exc:  # propagate through shared result after join
        result.update({"sent": sent, "ok": False, "error": repr(exc)})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--camera-index", required=True, type=Path)
    parser.add_argument("--camera-frames", required=True, type=Path)
    parser.add_argument("--camera-target", required=True)
    parser.add_argument("--lidar-target", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:15000")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--max-duration", type=float, default=300.0)
    parser.add_argument("--terminal-camera-fallback-ms", type=float, default=0.0)
    args = parser.parse_args()

    client = RecorderClient(args.base_url, timeout=20)
    state = client.status().get("data", {})
    if state.get("state") not in ("IDLE", "STOPPED"):
        raise ReplayFailure(f"recorder is not idle: {state}")

    camera_result: dict[str, Any] = {}
    camera_start_event = threading.Event()
    camera_thread = threading.Thread(
        target=send_camera_frames,
        kwargs={
            "target": args.camera_target,
            "config_path": args.config,
            "index_path": args.camera_index,
            "frame_dir": args.camera_frames,
            "base_url": args.base_url,
            "start_event": camera_start_event,
            "result": camera_result,
            "terminal_fallback_ms": args.terminal_camera_fallback_ms,
        },
        name="jpeg-camera-replay",
    )

    cleanup_required = True
    try:
        connected = client.connect(args.camera_target, args.lidar_target)
        loaded = client.load(args.config)
        camera_thread.start()

        # Complete gRPC setup first, but do not send camera frame 0 yet. Start
        # RecorderHMI so LiDAR scan 0 is cached, then release the camera sender.
        # This prevents the continuously-triggered EDS graph from consuming and
        # discarding the paired camera frame before its LiDAR cloud arrives.
        camera_ready_deadline = time.monotonic() + 15.0
        while not camera_result.get("ready"):
            if camera_result.get("ok") is False:
                raise ReplayFailure(f"camera sender failed during warmup: {camera_result}")
            if time.monotonic() >= camera_ready_deadline:
                raise ReplayFailure(f"camera sender warmup timed out: {camera_result}")
            time.sleep(0.01)

        client.start("frame_by_frame", args.speed)
        camera_start_event.set()

        started = time.monotonic()
        final_status: dict[str, Any] = {}
        while time.monotonic() - started <= args.max_duration:
            time.sleep(args.poll_interval)
            final_status = client.status().get("data", {})
            print(json.dumps({"recorder": final_status}, ensure_ascii=False), flush=True)
            state_name = final_status.get("state")
            current = int(final_status.get("current_frame_index", 0))
            total = int(final_status.get("total_frames", 0))
            if state_name in ("ERROR", "FAILED"):
                raise ReplayFailure(f"recorder entered failure state: {final_status}")
            if state_name in ("IDLE", "STOPPED") and total > 0 and current >= total - 1:
                break
        else:
            raise ReplayFailure(f"recorder exceeded {args.max_duration}s")

        camera_thread.join(timeout=30)
        if camera_thread.is_alive():
            raise ReplayFailure("camera sender did not finish")
        if not camera_result.get("ok"):
            raise ReplayFailure(f"camera sender failed: {camera_result}")
        client.stop()
        cleanup_required = False
        print(json.dumps({"connected": connected, "loaded": loaded, "camera": camera_result, "final": final_status}, ensure_ascii=False, indent=2))
        return 0
    finally:
        camera_start_event.set()
        if cleanup_required:
            try:
                client.stop()
            except ReplayFailure as exc:
                print(f"WARNING: recorder stop failed: {exc}", file=sys.stderr)
        if camera_thread.is_alive():
            camera_thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
