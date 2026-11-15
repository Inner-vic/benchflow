from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from compare_trees import compare


class CompareTreesTests(unittest.TestCase):
    def test_difference_categories_and_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = root / "active"
            candidate = root / "candidate"
            for tree in (active, candidate):
                (tree / "bin").mkdir(parents=True)
                (tree / "logs").mkdir()
                (tree / "bin/same").write_bytes(b"same")
            (active / "bin/changed").write_bytes(b"old")
            (candidate / "bin/changed").write_bytes(b"new")
            (active / "bin/old-only").write_bytes(b"old")
            (candidate / "bin/new-only").write_bytes(b"new")
            (active / "logs/runtime").write_bytes(b"ignored")

            result = compare(active, candidate, {"logs"})
            self.assertEqual(1, result["summary"]["identical_entries"])
            self.assertEqual(1, result["summary"]["changed_entries"])
            self.assertEqual(1, result["summary"]["only_active_entries"])
            self.assertEqual(1, result["summary"]["only_candidate_entries"])


if __name__ == "__main__":
    unittest.main()
