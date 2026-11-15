from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from stage_existing_package import ExistingStageError, validate_archive


def add_file(archive: tarfile.TarFile, name: str) -> None:
    data = b"content"
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


class ExistingPackageTests(unittest.TestCase):
    def test_valid_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                add_file(archive, "livc/bin/app")
            result = validate_archive(path)
            self.assertEqual("livc", result["top_level"])
            self.assertEqual(1, result["file_count"])

    def test_multiple_roots_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                add_file(archive, "livc/bin/app")
                add_file(archive, "other/file")
            with self.assertRaises(ExistingStageError):
                validate_archive(path)


if __name__ == "__main__":
    unittest.main()
