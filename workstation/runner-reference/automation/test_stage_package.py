from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from stage_package import StageError, parse_available_bytes, sha256_file, validate_archive


class StagePackageTests(unittest.TestCase):
    def make_archive(self, members: list[tuple[tarfile.TarInfo, bytes]]) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        archive_path = Path(temp_dir.name) / "package.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for info, payload in members:
                if info.isfile():
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                else:
                    archive.addfile(info)
        return archive_path

    def test_valid_single_root_archive(self) -> None:
        archive = self.make_archive([(tarfile.TarInfo("livc/bin/app"), b"payload")])
        result = validate_archive(archive)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["unpacked_bytes"], 7)
        self.assertEqual(len(sha256_file(archive)), 64)

    def test_rejects_parent_traversal(self) -> None:
        archive = self.make_archive([(tarfile.TarInfo("../escape"), b"payload")])
        with self.assertRaises(StageError):
            validate_archive(archive)

    def test_rejects_absolute_member(self) -> None:
        archive = self.make_archive([(tarfile.TarInfo("/tmp/escape"), b"payload")])
        with self.assertRaises(StageError):
            validate_archive(archive)

    def test_rejects_unsafe_symlink(self) -> None:
        link = tarfile.TarInfo("livc/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../etc/passwd"
        archive = self.make_archive([(link, b""), (tarfile.TarInfo("livc/app"), b"payload")])
        with self.assertRaises(StageError):
            validate_archive(archive)

    def test_rejects_multiple_top_level_directories(self) -> None:
        archive = self.make_archive(
            [
                (tarfile.TarInfo("livc/app"), b"one"),
                (tarfile.TarInfo("other/app"), b"two"),
            ]
        )
        with self.assertRaises(StageError):
            validate_archive(archive)

    def test_parses_busybox_df_output(self) -> None:
        output = "Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 50329600 22364872 27964728 45% /app\n"
        self.assertEqual(parse_available_bytes(output), 27964728 * 1024)


if __name__ == "__main__":
    unittest.main()
