#!/usr/bin/env python3
"""Create an auditable offline-evaluation YAML candidate without editing its source."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    pass


SECTION_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(?:#.*)?$")
VALUE_PATTERN = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*):(\s*)(\S+)(.*)$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def patch_yaml_text(
    source: str,
    *,
    scene_root: str,
    output_dir: str,
    eval_mode: str = "preserve",
) -> tuple[str, list[dict[str, str]], dict[str, str]]:
    replacements: dict[tuple[str, str], str] = {
        ("replay", "enable"): "ON",
        ("speed_gate", "enable"): "false",
        ("perception_gate", "enable"): "false",
        ("apa_gate", "enable"): "false",
        ("gear_gate", "enable"): "false",
        ("eval_dump", "enable"): "ON",
        ("eval_dump", "output_dir"): output_dir,
        ("eval_dump", "scene_root"): scene_root,
    }
    if eval_mode != "preserve":
        replacements[("eval_mode", "enable")] = "ON" if eval_mode == "on" else "OFF"

    counts = {key: 0 for key in replacements}
    observed: dict[str, str] = {}
    changes: list[dict[str, str]] = []
    current_section: str | None = None
    output_lines: list[str] = []

    for line in source.splitlines(keepends=True):
        bare_line = line.rstrip("\r\n")
        ending = line[len(bare_line) :]
        section_match = SECTION_PATTERN.match(bare_line)
        if section_match:
            current_section = section_match.group(1)
            output_lines.append(line)
            continue
        value_match = VALUE_PATTERN.match(bare_line)
        if not value_match or current_section is None:
            output_lines.append(line)
            continue
        indent, key, spacing, old_value, suffix = value_match.groups()
        dotted = f"{current_section}.{key}"
        observed[dotted] = old_value
        replacement_key = (current_section, key)
        if replacement_key not in replacements:
            output_lines.append(line)
            continue
        counts[replacement_key] += 1
        new_value = replacements[replacement_key]
        output_lines.append(f"{indent}{key}:{spacing}{new_value}{suffix}{ending}")
        changes.append({"key": dotted, "from": old_value, "to": new_value})

    invalid = [f"{section}.{key}={count}" for (section, key), count in counts.items() if count != 1]
    if invalid:
        raise ConfigError("expected exactly one occurrence of each target key: " + ", ".join(invalid))
    return "".join(output_lines), changes, observed


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a strict offline-eval common_param.yaml candidate")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scene-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--eval-mode", choices=("preserve", "on", "off"), default="preserve")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_file():
        parser.error(f"source config does not exist: {source}")
    if output.exists():
        parser.error(f"output config already exists: {output}")
    if not args.scene_root.startswith("/app/livc-test/"):
        parser.error("scene-root must stay below /app/livc-test")
    if not args.output_dir.startswith("/app/livc-test/"):
        parser.error("output-dir must stay below /app/livc-test")

    source_text = source.read_text(encoding="utf-8")
    patched, changes, observed = patch_yaml_text(
        source_text,
        scene_root=args.scene_root,
        output_dir=args.output_dir,
        eval_mode=args.eval_mode,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(patched, encoding="utf-8")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "created_at": utc_now(),
        "state": "CONFIG_CANDIDATE_NOT_ACTIVATED",
        "source": {"path": str(source), "sha256": sha256_file(source)},
        "candidate": {"path": str(output), "sha256": sha256_file(output)},
        "changes": changes,
        "eval_mode": {
            "policy": args.eval_mode,
            "present_in_source": "eval_mode.enable" in observed,
            "observed_enable": observed.get("eval_mode.enable"),
            "observed_mode": observed.get("eval_mode.mode"),
            "requires_confirmation": args.eval_mode == "preserve" and "eval_mode.enable" in observed,
        },
        "activation_performed": False,
    }
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
