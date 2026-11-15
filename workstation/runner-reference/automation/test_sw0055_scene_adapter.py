from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import call, patch

import sw0055_scene_adapter as adapter


class Sw0055SceneAdapterTests(unittest.TestCase):
    def test_assets_for_accepts_matching_reviewed_build_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_root = root / "builds"
            artifact = build_root / "build-1"
            run_dir = root / "run-1"
            artifact.mkdir(parents=True)
            run_dir.mkdir()
            library = artifact / "liburoad_m100.so"
            library.write_bytes(b"evaluation-library")
            expected = adapter.digest(library)
            (artifact / "build-manifest.json").write_text(json.dumps({
                "state": "BUILT", "output": str(library), "sha256": expected,
                "machine": "AArch64", "elf_class": 64,
            }), encoding="utf-8")
            (run_dir / "job.json").write_text(json.dumps({
                "artifacts": {"evaluation_library": {"path": str(library), "sha256": expected}}
            }), encoding="utf-8")
            with patch.object(adapter, "BUILD_ROOT", build_root):
                assets = adapter.assets_for(run_dir)
            self.assertEqual((library.resolve(), expected), assets["liburoad_m100.so"])

    def test_assets_for_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_root = root / "builds"
            artifact = build_root / "build-1"
            run_dir = root / "run-1"
            artifact.mkdir(parents=True)
            run_dir.mkdir()
            library = artifact / "liburoad_m100.so"
            library.write_bytes(b"evaluation-library")
            wrong = "0" * 64
            (artifact / "build-manifest.json").write_text(json.dumps({
                "state": "BUILT", "output": str(library), "sha256": wrong,
                "machine": "AArch64", "elf_class": 64,
            }), encoding="utf-8")
            (run_dir / "job.json").write_text(json.dumps({
                "artifacts": {"evaluation_library": {"path": str(library), "sha256": wrong}}
            }), encoding="utf-8")
            with patch.object(adapter, "BUILD_ROOT", build_root):
                with self.assertRaisesRegex(adapter.AdapterError, "hash differs"):
                    adapter.assets_for(run_dir)

    def test_start_enters_permissive_before_candidate_activation(self) -> None:
        responses = iter(("", "Permissive\n", "ACTIVATE_OK\n"))
        with patch.object(adapter, "bench", side_effect=lambda *_args, **_kwargs: next(responses)) as bench:
            adapter.start(Path("/tmp/run-1"), "scene-a")
        self.assertEqual(
            [
                call(["/usr/sbin/setenforce", "0"]),
                call(["/usr/sbin/getenforce"]),
                call(["sh", f"{adapter.BENCH_SHARED}/bench_session.sh", "activate"], timeout=300),
            ],
            bench.call_args_list,
        )

    def test_start_refuses_activation_when_permissive_was_not_observed(self) -> None:
        responses = iter(("", "Enforcing\n"))
        with patch.object(adapter, "bench", side_effect=lambda *_args, **_kwargs: next(responses)) as bench:
            with self.assertRaisesRegex(adapter.AdapterError, "did not enter"):
                adapter.start(Path("/tmp/run-1"), "scene-a")
        self.assertEqual(2, bench.call_count)

    def test_restore_enforces_before_and_after_formal_restore(self) -> None:
        with patch.object(adapter.subprocess, "run"), patch.object(adapter, "bench", return_value="") as bench:
            adapter.restore(Path("/tmp/run-1"), "scene-a")
        self.assertEqual(
            [
                call(["/usr/sbin/setenforce", "1"], timeout=300),
                call(["sh", f"{adapter.BENCH_SHARED}/bench_session.sh", "restore"], timeout=300),
                call(["/usr/sbin/setenforce", "1"], timeout=300),
            ],
            bench.call_args_list,
        )

    def test_restore_attempts_every_safety_action_after_an_error(self) -> None:
        calls: list[list[str]] = []

        def fake_bench(argv: list[str], timeout: int = 3600) -> str:
            calls.append(argv)
            if len(calls) == 1:
                raise adapter.AdapterError("first enforcement failed")
            return ""

        with patch.object(adapter.subprocess, "run"), patch.object(adapter, "bench", side_effect=fake_bench):
            with self.assertRaisesRegex(adapter.AdapterError, "first enforcement failed"):
                adapter.restore(Path("/tmp/run-1"), "scene-a")
        self.assertEqual(
            [
                ["/usr/sbin/setenforce", "1"],
                ["sh", f"{adapter.BENCH_SHARED}/bench_session.sh", "restore"],
                ["/usr/sbin/setenforce", "1"],
            ],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
