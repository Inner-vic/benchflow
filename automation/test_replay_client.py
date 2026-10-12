from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from replay_client import ReplayError, run_replay, validate_replay_config


class StartFailureClient:
    def __init__(self) -> None:
        self.stop_calls = 0

    def status(self):
        return {"code": 200, "data": {"state": "STOPPED"}}

    def connect(self, camera, lidar):
        return {"code": 200}

    def load(self, config):
        return {"code": 200}

    def start(self, mode, speed):
        raise ReplayError("start failed")

    def stop(self):
        self.stop_calls += 1
        return {"code": 200}


class ReplayClientTests(unittest.TestCase):
    def make_config(self, root: Path) -> Path:
        folders = {}
        for name in ("camera", "lidar", "nav", "odom", "eps"):
            folder = root / name
            folder.mkdir()
            (folder / "sample.pack").write_bytes(b"data")
            folders[name] = folder.as_posix()
        config = {
            "type": "replay_config",
            "fps": 10,
            "cameras": [{"folder": folders["camera"]}],
            "lidars": [{"folder": folders["lidar"]}],
            "topics": [
                {"bin_folder": folders["nav"]},
                {"bin_folder": folders["odom"]},
                {"bin_folder": folders["eps"]},
            ],
        }
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_valid_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = validate_replay_config(self.make_config(Path(temp)))
            self.assertEqual(5, len(result["inputs"]))

    def test_missing_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_config(Path(temp))
            config = json.loads(path.read_text(encoding="utf-8"))
            config["cameras"][0]["folder"] = "/does/not/exist"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ReplayError):
                validate_replay_config(path)

    def test_start_failure_stops_recorder(self) -> None:
        client = StartFailureClient()
        with self.assertRaises(ReplayError):
            run_replay(
                client,
                config=Path("/tmp/replay.json"),
                camera="203.0.113.20:50053",
                lidar="203.0.113.20:50052",
                mode="frame_by_frame",
                speed=1.0,
                poll_interval=0.01,
                max_duration=1.0,
            )
        self.assertEqual(1, client.stop_calls)


if __name__ == "__main__":
    unittest.main()
