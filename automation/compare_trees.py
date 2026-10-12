#!/usr/bin/env python3
"""Compare an active application tree with an isolated package tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


class CompareError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_excluded(relative: str, excluded: set[str]) -> bool:
    first = relative.split("/", 1)[0]
    return first in excluded


def snapshot(root: Path, excluded: set[str]) -> dict[str, dict[str, Any]]:
    if not root.is_dir():
        raise CompareError(f"tree root does not exist: {root}")
    result: dict[str, dict[str, Any]] = {}
    for base, directories, files in os.walk(root, followlinks=False):
        base_path = Path(base)
        relative_base = base_path.relative_to(root).as_posix()
        if relative_base == ".":
            relative_base = ""
        directories[:] = [
            name for name in directories
            if not is_excluded(f"{relative_base}/{name}".lstrip("/"), excluded)
        ]
        for name in files:
            path = base_path / name
            relative = path.relative_to(root).as_posix()
            if is_excluded(relative, excluded):
                continue
            if path.is_symlink():
                result[relative] = {"type": "symlink", "target": os.readlink(path)}
            elif path.is_file():
                result[relative] = {
                    "type": "file",
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
    return result


def top_level_counts(paths: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(path.split("/", 1)[0] for path in paths).items()))


def compare(active_root: Path, candidate_root: Path, excluded: set[str]) -> dict[str, Any]:
    active = snapshot(active_root, excluded)
    candidate = snapshot(candidate_root, excluded)
    active_paths = set(active)
    candidate_paths = set(candidate)
    shared = active_paths & candidate_paths
    identical = sorted(path for path in shared if active[path] == candidate[path])
    changed = sorted(path for path in shared if active[path] != candidate[path])
    only_active = sorted(active_paths - candidate_paths)
    only_candidate = sorted(candidate_paths - active_paths)
    return {
        "schema_version": 1,
        "active_root": str(active_root),
        "candidate_root": str(candidate_root),
        "excluded_top_level": sorted(excluded),
        "summary": {
            "active_entries": len(active),
            "candidate_entries": len(candidate),
            "shared_entries": len(shared),
            "identical_entries": len(identical),
            "changed_entries": len(changed),
            "only_active_entries": len(only_active),
            "only_candidate_entries": len(only_candidate),
        },
        "top_level_difference_counts": {
            "changed": top_level_counts(changed),
            "only_active": top_level_counts(only_active),
            "only_candidate": top_level_counts(only_candidate),
        },
        "changed": [
            {"path": path, "active": active[path], "candidate": candidate[path]} for path in changed
        ],
        "only_active": [{"path": path, **active[path]} for path in only_active],
        "only_candidate": [{"path": path, **candidate[path]} for path in only_candidate],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare active and isolated application trees")
    parser.add_argument("active", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = compare(args.active.resolve(), args.candidate.resolve(), set(args.exclude))
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CompareError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
