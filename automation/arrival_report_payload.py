#!/usr/bin/env python3
"""Prepare exact-precision report inputs; does not detect contacts or change rules."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def prepare(result: dict) -> dict:
    if result.get("state") != "CALCULATED":
        raise ValueError("a complete calculated batch is required for a complete report")
    # Keep ns epochs as text before JavaScript sees them (Number cannot hold 19 digits).
    def exact(value, key=""):
        if isinstance(value, dict):
            return {k: exact(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [exact(v, key) for v in value]
        if isinstance(value, int) and not isinstance(value, bool) and (key.endswith("_ns") or abs(value) > 2**53-1):
            return str(value)
        return value
    return exact(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = prepare(json.loads(args.result.read_text(encoding="utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
