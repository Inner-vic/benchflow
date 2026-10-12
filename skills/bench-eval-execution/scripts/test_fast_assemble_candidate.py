"""A small archive test for the cached-input assembly fast path."""

import io
import json
import os
import shutil
import sys
import tarfile
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
os.environ["BENCH_EVAL_AUTOMATION"] = str(ROOT / "automation")
sys.path.insert(0, str(ROOT / "automation"))
from artifact_pipeline import APPROVED_LIBRARIES, sha256_file  # noqa: E402
from fast_assemble_candidate import assemble  # noqa: E402


class FastAssembleTests(unittest.TestCase):
    def test_packaged_candidate_contains_exact_replacements(self) -> None:
        root = ROOT / "delivery" / ("fast-assemble-test-" + uuid4().hex)
        assert root.resolve().is_relative_to(ROOT.resolve())
        root.mkdir(parents=True)
        try:
            app = root / "app.app.tar.gz"
            with tarfile.open(app, "w:gz") as archive:
                for name in sorted(APPROVED_LIBRARIES):
                    payload = ("original-" + name).encode()
                    item = tarfile.TarInfo("livc/lib/" + name)
                    item.size = len(payload)
                    archive.addfile(item, io.BytesIO(payload))
            libraries = {}
            for name in sorted(APPROVED_LIBRARIES):
                file = root / name
                file.write_bytes(("replacement-" + name).encode())
                libraries[name] = {"cached_path": str(file), "sha256": sha256_file(file)}
            manifest = root / "artifact-manifest.json"
            manifest.write_text(json.dumps({
                "state": "REGISTERED", "artifact_id": "fixture",
                "app": {"cached_path": str(app), "sha256": sha256_file(app)},
                "libraries": libraries,
            }), encoding="utf-8")
            runs = root / "runs"
            planned = assemble(manifest, "candidate-001", runs, False)
            self.assertEqual(planned["state"], "PLANNED")
            self.assertFalse(runs.exists())
            result = assemble(manifest, "candidate-001", runs, True)
            self.assertEqual(result["state"], "CANDIDATE_ASSEMBLED")
            self.assertFalse(result["bench_contacted"])
            package = runs / "candidate-001/candidate.app.tar.gz"
            self.assertEqual(result["candidate_sha256"], sha256_file(package))
            with tarfile.open(package, "r:gz") as archive:
                for name in sorted(APPROVED_LIBRARIES):
                    contents = archive.extractfile("livc/lib/" + name).read()
                    self.assertEqual(contents, ("replacement-" + name).encode())
            self.assertEqual(json.loads((runs / "candidate-001/candidate-manifest.json").read_text(encoding="utf-8"))["state"], "CANDIDATE_ASSEMBLED")
        finally:
            assert root.resolve().is_relative_to(ROOT.resolve())
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
