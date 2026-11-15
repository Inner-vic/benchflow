import io
import json
import tarfile
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from artifact_pipeline import (
    APPROVED_LIBRARIES,
    ArtifactError,
    acquire,
    assemble,
    intake,
    normalize_artifactory_url,
    parse_dynamic_symbols,
    safe_archive_members,
    sha256_file,
    verify_replay_abi,
)


def write_elf(path: Path) -> None:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[18:20] = (183).to_bytes(2, "little")
    path.write_bytes(bytes(header) + b"payload")


class ArtifactPipelineTests(unittest.TestCase):
    def create_app(self, root: Path) -> Path:
        app = root / "sample.app.tar.gz"
        with tarfile.open(app, "w:gz") as archive:
            for name in sorted(APPROVED_LIBRARIES):
                payload = b"original-" + name.encode()
                info = tarfile.TarInfo(f"livc/lib/{name}")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return app

    def test_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "bad.app.tar.gz"
            with tarfile.open(app, "w:gz") as archive:
                info = tarfile.TarInfo("../escape")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(ArtifactError):
                safe_archive_members(app)

    def test_normalizes_jfrog_native_browser_url(self) -> None:
        self.assertEqual(
            normalize_artifactory_url("https://artifacts.example/ui/native/repo/path/sample.lock"),
            "https://artifacts.example/artifactory/repo/path/sample.lock",
        )

    def test_acquire_plan_and_execute_without_persisting_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"verified-artifact"
            source = root / "source.app.tar.gz"
            source.write_bytes(payload)
            expected = sha256_file(source)
            netrc = root / "jfrog.netrc"
            netrc.write_text("machine artifacts.example login user password token\n", encoding="utf-8")
            netrc.chmod(0o600)
            args = Namespace(
                artifact_id="download-1",
                url="https://artifacts.example/ui/native/repo/source.app.tar.gz",
                filename=None,
                expected_sha256=expected,
                netrc_file=netrc,
                curl_binary="curl",
                cache_root=root / "downloads",
                execute=False,
            )
            planned = acquire(args)
            self.assertEqual(planned["state"], "PLANNED")
            self.assertNotIn("token", json.dumps(planned))

            def fake_run(command, check):
                destination = Path(command[command.index("--output") + 1])
                destination.write_bytes(payload)
                return Namespace(returncode=0)

            args.execute = True
            with patch("artifact_pipeline.subprocess.run", side_effect=fake_run):
                downloaded = acquire(args)
            self.assertEqual(downloaded["state"], "DOWNLOADED")
            self.assertEqual(downloaded["sha256"], expected)
            self.assertTrue(Path(downloaded["destination"]).is_file())
            self.assertNotIn("token", json.dumps(downloaded))

    def test_acquire_rejects_credentials_in_url(self) -> None:
        with self.assertRaises(ArtifactError):
            normalize_artifactory_url("https://user:secret@artifacts.example/repo/sample.lock")

    def test_parses_dynamic_symbols(self) -> None:
        output = """
   Num:    Value          Size Type    Bind   Vis      Ndx Name
     1: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND _ZN6XLidar3runEv
     2: 0000000000001000    24 FUNC    GLOBAL DEFAULT   12 _ZN7XCamera4openEv
     3: 0000000000000000     0 FUNC    WEAK   DEFAULT  UND optional_symbol
"""
        exported, required = parse_dynamic_symbols(output)
        self.assertEqual(exported, {"_ZN7XCamera4openEv"})
        self.assertEqual(required, {"_ZN6XLidar3runEv"})

    def test_rejects_abi_incompatible_xlidar(self) -> None:
        libraries = {name: Path(name) for name in APPROVED_LIBRARIES}
        outputs = {
            "liburoad_m100.so": " 1: 0000000000000000 0 FUNC GLOBAL DEFAULT UND _ZN6XLidar29SetSceneSwitchPrepareCallbackEv\n",
            "libxlidar.so": " 1: 0000000000001000 4 FUNC GLOBAL DEFAULT 12 _ZN6XLidar3runEv\n",
            "libxcamera.so": " 1: 0000000000001000 4 FUNC GLOBAL DEFAULT 12 _ZN7XCamera4openEv\n",
        }

        with patch("artifact_pipeline.readelf_output", side_effect=lambda _binary, _option, path: outputs[path.name]):
            with self.assertRaisesRegex(ArtifactError, "SetSceneSwitchPrepareCallback"):
                verify_replay_abi(libraries)

    def test_accepts_abi_compatible_replay_libraries(self) -> None:
        required = "_ZN6XLidar29SetSceneSwitchPrepareCallbackEv"
        libraries = {name: Path(name) for name in APPROVED_LIBRARIES}
        outputs = {
            "liburoad_m100.so": f" 1: 0000000000000000 0 FUNC GLOBAL DEFAULT UND {required}\n",
            "libxlidar.so": f" 1: 0000000000001000 4 FUNC GLOBAL DEFAULT 12 {required}\n",
            "libxcamera.so": " 1: 0000000000001000 4 FUNC GLOBAL DEFAULT 12 _ZN7XCamera4openEv\n",
        }

        with patch("artifact_pipeline.readelf_output", side_effect=lambda _binary, _option, path: outputs[path.name]):
            result = verify_replay_abi(libraries)
        self.assertEqual(result["status"], "COMPATIBLE")
        self.assertEqual(result["providers"]["libxlidar.so"]["matched_symbol_count"], 1)

    def test_intake_and_assemble(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = self.create_app(root)
            lock = root / "sample.lock"
            lock.write_text("lock", encoding="utf-8")
            specs = []
            for name in sorted(APPROVED_LIBRARIES):
                library = root / name
                write_elf(library)
                specs.append(f"{name}={library}")
            with patch(
                "artifact_pipeline.verify_replay_abi",
                return_value={"status": "COMPATIBLE", "method": "unit-test"},
            ):
                registered = intake(
                    Namespace(
                        artifact_id="sample-1",
                        app=app,
                        lock=lock,
                        library=specs,
                        expected_app_sha256=None,
                        expected_lock_sha256=None,
                        cache_root=root / "cache",
                        execute=True,
                    )
                )
            self.assertEqual(registered["state"], "REGISTERED")
            self.assertEqual(registered["abi_compatibility"]["status"], "COMPATIBLE")
            manifest = root / "cache" / "sample-1" / "artifact-manifest.json"
            built = assemble(Namespace(manifest=manifest, run_id="run-1", runs_dir=root / "runs", execute=True))
            self.assertEqual(built["state"], "CANDIDATE_ASSEMBLED")
            self.assertTrue((root / "runs" / "run-1" / "candidate.app.tar.gz").is_file())
            saved = json.loads((root / "runs" / "run-1" / "candidate-manifest.json").read_text())
            self.assertEqual(set(saved["changes"]), APPROVED_LIBRARIES)


if __name__ == "__main__":
    unittest.main()
