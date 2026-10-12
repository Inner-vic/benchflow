"""Copy verified workstation products into one task directory.

Input is final verification JSON with state=PASS and a results array. Each
result needs scene, run_id, status=PASS, file_count, inventory_sha256 and
product_identity. Supply scene_id or use a run ID ending in -sNNN[-rNNN].
The YAML manifest maps each scene ID to the effective YAML used for that run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath


SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
SCENE_SUFFIX = re.compile(r"-(s[0-9]+)(?:-r[0-9]+)?$")


def scene_id_for(entry: dict) -> str:
    match = SCENE_SUFFIX.search(str(entry.get("run_id", "")))
    scene_id = entry.get("scene_id") or (match.group(1) if match else "")
    if not isinstance(scene_id, str) or not SAFE_ID.fullmatch(scene_id):
        raise ValueError(f"missing or unsafe scene ID: {entry.get('run_id')}")
    return scene_id


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_product(root: Path, entry: dict) -> tuple[Path, Path, dict]:
    run_id = entry["run_id"]
    product = Path(entry.get("product") or root / "cache" / "predictions" / run_id / "predictions")
    receipt_path = product.with_name(product.name + ".bench-eval-product.json")
    if not product.is_absolute() or not product.is_dir() or product.is_symlink() or not receipt_path.is_file() or receipt_path.is_symlink():
        raise RuntimeError(f"missing canonical product or receipt: {run_id}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        receipt.get("schema_version") != 1
        or receipt.get("product_type") != "workstation_predictions"
        or receipt.get("producer_run_id") != run_id
        or receipt.get("workstation_product") != str(product)
        or receipt.get("inventory_sha256") != entry["inventory_sha256"]
        or receipt.get("product_identity") != entry["product_identity"]
        or receipt.get("state") not in {"SUCCEEDED", "REUSED_VERIFIED"}
    ):
        raise RuntimeError(f"receipt identity mismatch: {run_id}")

    listed = receipt.get("files")
    if not isinstance(listed, list) or len(listed) != entry["file_count"]:
        raise RuntimeError(f"receipt file count mismatch: {run_id}")
    actual_paths = []
    for path in product.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"symlink inside canonical product: {path}")
        if path.is_file():
            actual_paths.append(path.relative_to(product).as_posix())
        elif not path.is_dir():
            raise RuntimeError(f"unexpected entry inside canonical product: {path}")
    expected_paths = [item["path"] for item in listed]
    if len(set(expected_paths)) != len(expected_paths):
        raise RuntimeError(f"duplicate receipt paths: {run_id}")
    for relative in expected_paths:
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or ".." in parsed.parts or relative in {"", "."}:
            raise RuntimeError(f"unsafe receipt path: {relative}")
    if sorted(actual_paths) != sorted(expected_paths):
        raise RuntimeError(f"product path inventory mismatch: {run_id}")
    digest = hashlib.sha256(
        json.dumps(listed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if digest != entry["inventory_sha256"]:
        raise RuntimeError(f"inventory digest mismatch: {run_id}")
    return product, receipt_path, receipt


def verify_copy(copy: Path, receipt: dict) -> None:
    listed = receipt["files"]
    expected = {item["path"]: item for item in listed}
    observed: set[str] = set()
    for path in copy.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"symlink inside task copy: {path}")
        if path.is_file():
            relative = path.relative_to(copy).as_posix()
            item = expected.get(relative)
            if item is None or path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
                raise RuntimeError(f"task copy differs from producer receipt: {path}")
            observed.add(relative)
        elif not path.is_dir():
            raise RuntimeError(f"unexpected entry inside task copy: {path}")
    if observed != set(expected):
        raise RuntimeError(f"task copy file set differs from producer receipt: {copy}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--verification", type=Path, required=True)
    parser.add_argument("--yaml-manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("/mnt/data/bench-eval"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not SAFE_ID.fullmatch(args.task_id):
        raise RuntimeError("task ID must be a safe lowercase slug")

    evidence = json.loads(args.verification.read_text(encoding="utf-8"))
    entries = evidence.get("results")
    if evidence.get("state") != "PASS" or not isinstance(entries, list) or not entries:
        raise RuntimeError("final verification must be PASS with nonempty results")
    if evidence.get("scenes") is not None and evidence["scenes"] != len(entries):
        raise RuntimeError("declared scene count differs from results")
    if any(
        item.get("status") != "PASS"
        or not SAFE_ID.fullmatch(str(item.get("run_id", "")))
        or not item["run_id"].startswith(args.task_id + "-")
        for item in entries
    ):
        raise RuntimeError("unexpected task or non-PASS scene")
    scene_ids = [scene_id_for(item) for item in entries]
    if len({item["run_id"] for item in entries}) != len(entries) or len(set(scene_ids)) != len(entries):
        raise RuntimeError("duplicate run IDs or scene IDs")

    yaml_manifest = json.loads(args.yaml_manifest.read_text(encoding="utf-8"))
    effective = yaml_manifest.get("effective_yaml_by_scene")
    if not isinstance(effective, dict) or set(effective) != set(scene_ids):
        raise RuntimeError("YAML manifest must map every final scene ID exactly once")
    for scene_id, value in effective.items():
        path = Path(value)
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise RuntimeError(f"missing effective YAML for {scene_id}: {path}")
    source_yaml = yaml_manifest.get("source_yaml")
    if source_yaml is not None:
        source_path = Path(source_yaml)
        if not source_path.is_absolute() or not source_path.is_file() or source_path.is_symlink():
            raise RuntimeError(f"missing source YAML: {source_path}")

    verified = [verify_product(args.root, item) for item in entries]
    task_dir = args.root / "tasks" / args.task_id
    index = {
        "schema_version": 1,
        "task_id": args.task_id,
        "state": "PASS",
        "scene_count": len(entries),
        "file_count": sum(item["file_count"] for item in entries),
        "scenes": [
            {
                "scene_id": scene_id,
                "scene": entry["scene"],
                "run_id": entry["run_id"],
                "product": str(product),
                "producer_receipt": str(receipt),
                "task_copy": str(task_dir / "scenes" / scene_id / "predictions"),
                "task_receipt_copy": str(task_dir / "scenes" / scene_id / "producer-receipt.json"),
                "task_effective_yaml": str(task_dir / "inputs" / "yaml" / f"{scene_id}.effective.yaml"),
                "file_count": entry["file_count"],
                "inventory_sha256": entry["inventory_sha256"],
                "product_identity": entry["product_identity"],
            }
            for scene_id, entry, (product, receipt, _) in zip(scene_ids, entries, verified)
        ],
    }
    if not args.apply:
        print(json.dumps({"status": "READY", "task_dir": str(task_dir), "scene_count": len(entries), "file_count": index["file_count"]}))
        return
    if task_dir.exists() or task_dir.is_symlink():
        raise RuntimeError(f"task directory already exists; refusing to overwrite: {task_dir}")
    task_dir.parent.mkdir(parents=True, exist_ok=True)
    pending = task_dir.with_name(f".{args.task_id}.pending-{uuid.uuid4().hex}")
    pending.mkdir()
    scenes_dir = pending / "scenes"
    scenes_dir.mkdir()
    yaml_dir = pending / "inputs" / "yaml"
    yaml_dir.mkdir(parents=True)
    yaml_sums: list[str] = []
    if source_yaml is not None:
        source_copy = pending / "inputs" / "common_param.source.yaml"
        shutil.copy2(source_path, source_copy)
        yaml_sums.append(f"{sha256_file(source_copy)}  common_param.source.yaml")
    for scene_id in scene_ids:
        copied_yaml = yaml_dir / f"{scene_id}.effective.yaml"
        shutil.copy2(Path(effective[scene_id]), copied_yaml)
        yaml_sums.append(f"{sha256_file(copied_yaml)}  yaml/{copied_yaml.name}")
    (pending / "inputs" / "SHA256SUMS").write_text("\n".join(yaml_sums) + "\n", encoding="utf-8")
    for scene_id, (product, receipt, receipt_data) in zip(scene_ids, verified):
        scene_dir = scenes_dir / scene_id
        scene_dir.mkdir()
        copied = scene_dir / "predictions"
        shutil.copytree(product, copied, symlinks=True, copy_function=shutil.copy2)
        verify_copy(copied, receipt_data)
        shutil.copy2(receipt, scene_dir / "producer-receipt.json")
    (pending / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (pending / "README.md").write_text(
        "# " + args.task_id + "\n\n"
        "Each scene's `predictions` is a physical copy of the verified product, "
        "with no symlinks. `producer-receipt.json` is a copy of the original receipt. "
        "Use the canonical `product` path in `index.json` for downstream Runner jobs; "
        "that receipt remains bound to the original Cache path. Effective YAML "
        "for each scene is copied into `inputs/yaml/`; original products, failed "
        "attempts and partial evidence remain under their original run IDs.\n",
        encoding="utf-8",
    )
    pending.rename(task_dir)
    print(json.dumps({"status": "CREATED", "task_dir": str(task_dir), "scene_count": len(entries), "file_count": index["file_count"]}))


if __name__ == "__main__":
    main()
