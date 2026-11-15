#!/usr/bin/env python3
"""Content identities and immutable receipts for target workflow nodes."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


RECEIPT_SCHEMA_VERSION = 1
RECEIPT_STATES = {
    "NOT_REQUESTED",
    "REUSED_VERIFIED",
    "SUCCEEDED",
    "FAILED",
    "BLOCKED",
    "INVALID_CACHE",
}


class ReceiptError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_identity(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_inventory(root: Path, patterns: Iterable[str] | None = None) -> list[dict[str, Any]]:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise ReceiptError(f"inventory root is not a directory: {resolved}")
    candidates: set[Path] = set()
    if patterns is None:
        candidates.update(path for path in resolved.rglob("*") if path.is_file())
    else:
        for pattern in patterns:
            candidates.update(path for path in resolved.rglob(pattern) if path.is_file())
    return [
        {
            "path": path.relative_to(resolved).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(candidates, key=lambda item: item.relative_to(resolved).as_posix())
    ]


def inventory_identity(files: list[dict[str, Any]]) -> str:
    return content_identity(files)


def _normalized_component(value: str, label: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if not normalized or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-." for character in normalized):
        raise ReceiptError(f"invalid {label}: {value}")
    return normalized


def receipt_path(run_dir: Path, node: str) -> Path:
    normalized = _normalized_component(node, "receipt node")
    return run_dir.expanduser().resolve() / "receipts" / f"{normalized}.json"


def attempt_root(run_dir: Path, node: str, scope: str | None = None) -> Path:
    normalized_node = _normalized_component(node, "attempt node")
    root = run_dir.expanduser().resolve() / f"{normalized_node}-attempts"
    if scope is not None:
        safe_scope = scope.strip()
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        if not safe_scope or any(character not in allowed for character in safe_scope):
            raise ReceiptError(f"invalid attempt scope: {scope}")
        root /= safe_scope
    return root


def allocate_attempt(
    run_dir: Path,
    node: str,
    *,
    scope: str | None = None,
    maximum: int = 9999,
) -> tuple[str, Path]:
    root = attempt_root(run_dir, node, scope)
    root.mkdir(parents=True, exist_ok=True)
    for number in range(1, maximum + 1):
        attempt = f"a{number:03d}"
        path = root / attempt
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return attempt, path
    raise ReceiptError(f"no free attempt remains below {root}")


def write_receipt(
    run_dir: Path,
    node: str,
    state: str,
    *,
    targets: Iterable[str] = (),
    input_identity: str | None = None,
    output_identity: str | None = None,
    source: str | None = None,
    verification: dict[str, Any] | None = None,
    attempt: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if state not in RECEIPT_STATES:
        raise ReceiptError(f"invalid receipt state: {state}")
    value: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "node": node,
        "state": state,
        "targets": sorted(set(targets)),
        "recorded_at": utc_now(),
        "input_identity": input_identity,
        "output_identity": output_identity,
        "source": source,
        "verification": verification or {},
        "attempt": attempt,
    }
    if details:
        value["details"] = details
    path = receipt_path(run_dir, node)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(pending, path)
    return value


def read_receipt(run_dir: Path, node: str) -> dict[str, Any] | None:
    path = receipt_path(run_dir, node)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"invalid receipt {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ReceiptError(f"unsupported receipt schema: {path}")
    return value


def verify_receipt(
    run_dir: Path,
    node: str,
    *,
    input_identity: str | None = None,
    output_identity: str | None = None,
) -> dict[str, Any]:
    try:
        value = read_receipt(run_dir, node)
    except ReceiptError as exc:
        return {"status": "INVALID_CACHE", "reason": str(exc), "receipt": None}
    if value is None:
        return {"status": "MISSING", "reason": "receipt does not exist", "receipt": None}
    if value.get("state") not in {"SUCCEEDED", "REUSED_VERIFIED"}:
        return {
            "status": "INVALID_CACHE",
            "reason": f"receipt state is not reusable: {value.get('state')}",
            "receipt": value,
        }
    if input_identity is not None and value.get("input_identity") != input_identity:
        return {"status": "INVALID_CACHE", "reason": "receipt input identity mismatch", "receipt": value}
    if output_identity is not None and value.get("output_identity") != output_identity:
        return {"status": "INVALID_CACHE", "reason": "receipt output identity mismatch", "receipt": value}
    return {"status": "REUSED_VERIFIED", "reason": "receipt identity verified", "receipt": value}
