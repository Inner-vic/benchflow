"""External stage exposes its /app path only after verified extraction."""

import io
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import stage_package


class ExternalStageTests(unittest.TestCase):
    def test_link_is_created_after_package_hash_extract_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "candidate.app.tar.gz"
            with tarfile.open(package, "w:gz") as archive:
                member = tarfile.TarInfo("livc/bin/app")
                payload = b"sample"
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            digest = stage_package.sha256_file(package)
            events: list[str] = []

            def fake_ssh(_host, command, **_kwargs):
                events.append(command)
                if command.startswith("if [ -e"):
                    return subprocess.CompletedProcess([], 0, "ABSENT\n", "")
                if command == "date +%s":
                    return subprocess.CompletedProcess([], 0, str(int(time.time())), "")
                if command.startswith("df -Pk /map"):
                    return subprocess.CompletedProcess([], 0, "fs 99999999 0 99999999 0% /map\n", "")
                if command.startswith("sha256sum"):
                    return subprocess.CompletedProcess([], 0, f"{digest}  package\n", "")
                if command.startswith("printf 'files='"):
                    return subprocess.CompletedProcess([], 0,
                                                       "files=1\ndisk_kib=1\nextract_warning_lines=0\n", "")
                return subprocess.CompletedProcess([], 0, "", "")

            def fake_run(command, **_kwargs):
                events.append(" ".join(command))
                return subprocess.CompletedProcess(command, 0, "", "")

            argv = ["stage_package.py", "--package", str(package), "--bench", "bench-avm",
                    "--run-id", "external-stage-001", "--runs-dir", str(root / "runs"),
                    "--storage-root", "/map/bench-eval/isolated-stage", "--extract"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(stage_package, "ssh", side_effect=fake_ssh), \
                 mock.patch.object(stage_package, "run", side_effect=fake_run):
                self.assertEqual(stage_package.main(), 0)

            linked = next(i for i, item in enumerate(events) if "ln -s" in item)
            hashed = next(i for i, item in enumerate(events) if item.startswith("sha256sum"))
            extracted = next(i for i, item in enumerate(events) if "tar -xzf" in item)
            manifested = next(i for i, item in enumerate(events) if "manifest.json" in item and
                              item.startswith("rsync "))
            self.assertGreater(linked, max(hashed, extracted, manifested))
            self.assertIn("/map/bench-eval/isolated-stage/external-stage-001",
                          events[linked])
            self.assertIn("/app/livc-test/external-stage-001", events[linked])


if __name__ == "__main__":
    unittest.main()
