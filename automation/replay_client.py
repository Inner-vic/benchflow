#!/usr/bin/env python3
"""Non-interactive RecorderHMI HTTP client for an auditable bench workflow."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ReplayError(RuntimeError):
    pass


def validate_replay_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ReplayError(f"replay config does not exist: {path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayError(f"cannot read replay config {path}: {exc}") from exc

    if config.get("type") != "replay_config":
        raise ReplayError("config type must be 'replay_config'")
    if not isinstance(config.get("fps"), (int, float)) or config["fps"] <= 0:
        raise ReplayError("config fps must be a positive number")

    inputs: list[dict[str, Any]] = []
    for group, path_key in (("cameras", "folder"), ("lidars", "folder"), ("topics", "bin_folder")):
        values = config.get(group)
        if not isinstance(values, list) or not values:
            raise ReplayError(f"config {group} must be a non-empty list")
        for index, value in enumerate(values):
            if not isinstance(value, dict) or not isinstance(value.get(path_key), str):
                raise ReplayError(f"config {group}[{index}].{path_key} is missing")
            input_dir = Path(value[path_key])
            if not input_dir.is_absolute() or not input_dir.is_dir():
                raise ReplayError(f"input directory is missing or not absolute: {input_dir}")
            files = sum(1 for candidate in input_dir.iterdir() if candidate.is_file())
            if files == 0:
                raise ReplayError(f"input directory is empty: {input_dir}")
            inputs.append({"group": group, "index": index, "path": str(input_dir), "files": files})
    return {"config": str(path), "fps": config["fps"], "inputs": inputs}


class RecorderClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        unsupported = sorted(set(kwargs) - {"json"})
        if unsupported:
            raise ReplayError(f"unsupported HTTP options: {unsupported}")
        body = None
        headers: dict[str, str] = {}
        if "json" in kwargs:
            body = json.dumps(kwargs["json"], ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{endpoint}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", response.getcode())
                encoded = response.read()
            if not 200 <= int(status) < 300:
                raise ReplayError(f"{method} {endpoint} returned HTTP {status}")
            payload = json.loads(encoded.decode("utf-8"))
        except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayError(f"{method} {endpoint} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise ReplayError(f"{method} {endpoint} returned a non-object JSON response")
        if payload.get("code") != 200:
            raise ReplayError(f"{method} {endpoint} rejected: {payload}")
        return payload

    def status(self) -> dict[str, Any]:
        return self.request("GET", "/replay/status")

    def load(self, config: Path) -> dict[str, Any]:
        return self.request("POST", "/replay/load", json={"config_file": str(config)})

    def connect(self, camera: str, lidar: str) -> dict[str, Any]:
        return self.request("POST", "/replay/connect", json={"camera": camera, "lidar": lidar})

    def start(self, mode: str, speed: float) -> dict[str, Any]:
        return self.request("POST", "/replay/start", json={"mode": mode, "speed": speed})

    def stop(self) -> dict[str, Any]:
        return self.request("POST", "/replay/stop")


def run_replay(
    client: RecorderClient,
    *,
    config: Path,
    camera: str,
    lidar: str,
    mode: str,
    speed: float,
    poll_interval: float,
    max_duration: float,
) -> dict[str, Any]:
    initial = client.status().get("data", {})
    if initial.get("state") not in ("IDLE", "STOPPED"):
        raise ReplayError(f"recorder is not idle: {initial}")
    cleanup_required = True
    try:
        client.connect(camera, lidar)
        loaded = client.load(config)
        client.start(mode, speed)

        started = time.monotonic()
        consecutive_errors = 0
        last_status: dict[str, Any] = {}
        while time.monotonic() - started <= max_duration:
            time.sleep(poll_interval)
            try:
                last_status = client.status().get("data", {})
                consecutive_errors = 0
            except ReplayError:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    raise
                continue
            state = last_status.get("state")
            current = int(last_status.get("current_frame_index", 0))
            total = int(last_status.get("total_frames", 0))
            print(json.dumps({"state": state, "current": current, "total": total}, ensure_ascii=False))
            if state in ("IDLE", "STOPPED") and total > 0 and current >= total - 1:
                client.stop()
                cleanup_required = False
                return {"loaded": loaded, "final_status": last_status}
            if state in ("ERROR", "FAILED"):
                raise ReplayError(f"recorder entered failure state: {last_status}")
        raise ReplayError(f"replay exceeded max duration of {max_duration} seconds")
    except BaseException:
        if cleanup_required:
            try:
                client.stop()
            except ReplayError as stop_error:
                print(f"WARNING: recorder stop failed: {stop_error}")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="RecorderHMI HTTP replay client")
    parser.add_argument("--base-url", default="http://127.0.0.1:15000")
    parser.add_argument("--http-timeout", type=float, default=10.0)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Read the RecorderHMI status endpoint")
    validate_parser = subparsers.add_parser("validate", help="Validate a replay config and its inputs")
    validate_parser.add_argument("--config", required=True, type=Path)

    load_parser = subparsers.add_parser("load", help="Load a config without connecting or starting replay")
    load_parser.add_argument("--config", required=True, type=Path)
    load_parser.add_argument("--execute", action="store_true", help="Required for the HTTP POST")

    run_parser = subparsers.add_parser("run", help="Connect, load and execute one scene")
    run_parser.add_argument("--config", required=True, type=Path)
    run_parser.add_argument("--camera", required=True)
    run_parser.add_argument("--lidar", required=True)
    run_parser.add_argument("--mode", choices=("frame_by_frame", "stream"), default="frame_by_frame")
    run_parser.add_argument("--speed", type=float, default=1.0)
    run_parser.add_argument("--poll-interval", type=float, default=2.0)
    run_parser.add_argument("--max-duration", type=float, default=1800.0)
    run_parser.add_argument("--execute", action="store_true", help="Required to connect and start replay")
    args = parser.parse_args()

    client = RecorderClient(args.base_url, args.http_timeout)
    if args.command == "status":
        result = client.status()
    elif args.command == "validate":
        result = validate_replay_config(args.config.expanduser().resolve())
    elif args.command == "load":
        config = args.config.expanduser().resolve()
        validation = validate_replay_config(config)
        if not args.execute:
            raise ReplayError("load is a state-changing request; add --execute after validation")
        result = {"validation": validation, "response": client.load(config)}
    else:
        config = args.config.expanduser().resolve()
        validation = validate_replay_config(config)
        if not args.execute:
            raise ReplayError("run sends data to the bench; add --execute after preflight approval")
        result = {
            "validation": validation,
            "replay": run_replay(
                client,
                config=config,
                camera=args.camera,
                lidar=args.lidar,
                mode=args.mode,
                speed=args.speed,
                poll_interval=args.poll_interval,
                max_duration=args.max_duration,
            ),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReplayError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
