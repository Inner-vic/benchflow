from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from prepare_replay import PrepareError, build_replay_config, extract_scene, inspect_archive, validate_scene


SCENE = "2026-07-07_10-06-47_TESTCAR"


def add_bytes(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    archive.addfile(info, io.BytesIO(value))


def make_archive(path: Path, *, unsafe_name: str | None = None) -> None:
    root = f"{SCENE}/raw/raw_in_10s"
    with tarfile.open(path, "w:gz") as archive:
        for directory in (
            "f_ca_120",
            "VAL_Lidar_0_PointCloud",
            "Val_Navigation_Ins",
            "odom_data_for_pcm",
            "eps_mode_sts",
        ):
            add_bytes(archive, f"{root}/{directory}/sample.pack", directory.encode())
        add_bytes(archive, f"{root}/calib_cam_params.json", b"{}")
        add_bytes(archive, f"{root}/calib_lidar_params.json", b"{}")
        if unsafe_name:
            add_bytes(archive, unsafe_name, b"unsafe")


class PrepareReplayTests(unittest.TestCase):
    def test_inspect_and_extract_one_scene(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "data.tar.gz"
            make_archive(archive)
            inventory = inspect_archive(archive)
            self.assertEqual([SCENE], list(inventory["scenes"]))
            scene_root = extract_scene(archive, root / "dataset", SCENE)
            validation = validate_scene(scene_root)
            self.assertEqual(1, validation["signal_file_counts"]["f_ca_120"])

    def test_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "bad.tar.gz"
            make_archive(archive, unsafe_name="../escape")
            with self.assertRaises(PrepareError):
                inspect_archive(archive)

    def test_existing_scene_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "data.tar.gz"
            make_archive(archive)
            target = root / "dataset" / SCENE
            target.mkdir(parents=True)
            with self.assertRaises(PrepareError):
                extract_scene(archive, root / "dataset", SCENE)

    def test_generated_config_uses_posix_absolute_data_paths(self) -> None:
        scene_root = Path("/srv/replay") / SCENE
        config = build_replay_config(scene_root)
        encoded = json.dumps(config)
        self.assertIn(f"/srv/replay/{SCENE}/raw/raw_in_10s/f_ca_120", encoded)
        self.assertNotIn("D:\\\\", encoded)


if __name__ == "__main__":
    unittest.main()
