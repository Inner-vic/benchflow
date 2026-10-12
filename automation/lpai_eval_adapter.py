#!/usr/bin/env python3
"""Fail-closed adapter for the approved LPAI offline evaluator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_SCENE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class AdapterError(RuntimeError):
    pass


TRUTH_MANIFEST = "truth-manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        if path.is_file() and path.name != TRUTH_MANIFEST
    ]


def inventory_identity(files: list[dict[str, Any]]) -> str:
    payload = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _truth_marker_paths(root: Path, scene: str) -> tuple[Path, Path]:
    return root / f".{scene}.partial", root / f".{scene}.layout"


def truth_readiness(
    truth_root: str,
    scene: str,
    *,
    require_manifest: bool = False,
) -> dict[str, Any]:
    if not SAFE_SCENE.fullmatch(scene):
        raise AdapterError("unsafe scene name")
    root = Path(truth_root).resolve()
    truth = below(str(root / scene), str(root), "truth scene")
    markers = [str(path) for path in _truth_marker_paths(root, scene) if path.exists()]
    if markers:
        return {
            "state": "BLOCKED_PARTIAL",
            "scene": scene,
            "path": str(truth),
            "markers": markers,
            "ready": False,
        }
    if not truth.is_dir():
        return {
            "state": "TRUTH_NOT_COMPLETE",
            "scene": scene,
            "path": str(truth),
            "reason": "truth scene directory is missing",
            "ready": False,
        }
    files = inventory(truth)
    if not files:
        return {
            "state": "TRUTH_NOT_COMPLETE",
            "scene": scene,
            "path": str(truth),
            "reason": "truth scene contains no data files",
            "ready": False,
        }
    manifest_path = truth / TRUTH_MANIFEST
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "state": "INVALID_CACHE",
                "scene": scene,
                "path": str(truth),
                "reason": f"invalid truth manifest: {exc}",
                "ready": False,
            }
        actual = inventory_identity(files)
        expected = manifest.get("inventory_sha256") if isinstance(manifest, dict) else None
        manifest_scene = manifest.get("scene") if isinstance(manifest, dict) else None
        if expected != actual or manifest_scene not in (None, scene):
            return {
                "state": "INVALID_CACHE",
                "scene": scene,
                "path": str(truth),
                "reason": "truth manifest does not match the scene inventory",
                "inventory_sha256": actual,
                "ready": False,
            }
        method = "truth-manifest-sha256-v1"
    elif require_manifest:
        return {
            "state": "TRUTH_NOT_COMPLETE",
            "scene": scene,
            "path": str(truth),
            "reason": "truth manifest is required but missing",
            "ready": False,
        }
    else:
        actual = inventory_identity(files)
        method = "legacy-nonempty-truth-v1"
    return {
        "state": "READY",
        "scene": scene,
        "path": str(truth),
        "file_count": len(files),
        "inventory_sha256": actual,
        "verification_method": method,
        "manifest_path": str(manifest_path) if manifest_path.is_file() else None,
        "ready": True,
    }


def require_truth_ready(args: argparse.Namespace) -> Path:
    readiness = truth_readiness(
        args.truth_root,
        args.scene,
        require_manifest=bool(getattr(args, "require_truth_manifest", False)),
    )
    if not readiness["ready"]:
        raise AdapterError(
            f"{readiness['state']}: truth scene {args.scene} is not ready: "
            f"{readiness.get('reason') or readiness.get('markers')}"
        )
    return Path(readiness["path"])


def require_prediction_scene(prediction_root: Path, scene: str) -> None:
    if not (prediction_root / scene).is_dir():
        raise AdapterError("prediction root must contain the exact scene directory")


def below(value: str, root: str, label: str, *, allow_root: bool = False) -> Path:
    path = Path(value).resolve()
    allowed = Path(root).resolve()
    try:
        relative = path.relative_to(allowed)
    except ValueError as exc:
        raise AdapterError(f"{label} must stay below {allowed}") from exc
    if not allow_root and not relative.parts:
        raise AdapterError(f"{label} must not equal {allowed}")
    return path


def replace_one(text: str, pattern: str, replacement: str, label: str) -> str:
    compiled = re.compile(pattern, flags=re.MULTILINE)
    result, count = compiled.subn(lambda match: match.group(1) + replacement, text, count=1)
    if count != 1:
        raise AdapterError(f"template must contain exactly one {label}")
    return result


def prepare(args: argparse.Namespace) -> None:
    if not SAFE_ID.fullmatch(args.run_id) or not SAFE_SCENE.fullmatch(args.scene):
        raise AdapterError("unsafe run id or scene name")
    config = below(args.config, args.config_root, "config")
    prediction = below(args.prediction, args.prediction_root, "prediction")
    truth = require_truth_ready(args)
    template = Path(args.template).resolve()
    if not template.is_file() or not prediction.is_dir() or not truth.is_dir():
        raise AdapterError("template, prediction scene, or truth scene is missing")
    require_prediction_scene(prediction, args.scene)
    if config.exists():
        raise AdapterError(f"refusing to overwrite config: {config}")
    text = template.read_text(encoding="utf-8")
    text = replace_one(text, r"^(\s*pred_data_root:\s*).*$", str(prediction), "pred_data_root")
    text = replace_one(text, r"^(\s*scene_folder_prefix:\s*).*$", f'"{args.scene}"', "scene_folder_prefix")
    text = replace_one(text, r"^(\s*max_scenes_for_debug:\s*).*$", "1", "max_scenes_for_debug")
    config.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
    print(f"CONFIG_PREPARED={config}")


def precheck(args: argparse.Namespace) -> None:
    if not SAFE_ID.fullmatch(args.run_id) or not SAFE_SCENE.fullmatch(args.scene):
        raise AdapterError("unsafe run id or scene name")
    config = below(args.config, args.config_root, "config")
    prediction = below(args.prediction, args.prediction_root, "prediction")
    truth = require_truth_ready(args)
    run_script = Path(args.run_script).resolve()
    executable = Path(args.evaluator_binary).resolve()
    required = (config, prediction, truth, run_script, executable)
    if any(not item.exists() for item in required):
        raise AdapterError("evaluation precheck found a missing required path")
    require_prediction_scene(prediction, args.scene)
    completed = subprocess.run(
        ["ldd", str(executable)], text=True, capture_output=True, shell=False,
        env={**os.environ, "LD_LIBRARY_PATH": args.library_path},
    )
    if completed.returncode != 0 or "not found" in completed.stdout + completed.stderr:
        raise AdapterError("evaluator has unresolved shared-library dependencies")
    print("LPAI_EVALUATION_PRECHECK_OK")


def evaluate(args: argparse.Namespace) -> None:
    if not SAFE_ID.fullmatch(args.run_id) or not SAFE_SCENE.fullmatch(args.scene):
        raise AdapterError("unsafe run id or scene name")
    config = below(args.config, args.config_root, "config")
    prediction = below(args.prediction, args.prediction_root, "prediction")
    truth = require_truth_ready(args)
    if not config.is_file() or not prediction.is_dir() or not truth.is_dir():
        raise AdapterError("config, prediction scene, or truth scene is missing")
    require_prediction_scene(prediction, args.scene)
    run_root = below(str(config.parent), args.run_root, "evaluation run")
    exit_file = run_root / "exit_code.txt"
    report_dir = run_root / "eval_report"
    if exit_file.exists() or report_dir.exists():
        raise AdapterError("refusing to overwrite an existing evaluation attempt")
    report_dir.mkdir(parents=False, exist_ok=False)
    stdout = run_root / "stdout.log"
    stderr = run_root / "stderr.log"
    with stdout.open("x", encoding="utf-8") as out, stderr.open("x", encoding="utf-8") as err:
        completed = subprocess.run(
            [str(Path(args.run_script).resolve()), str(config)], cwd=run_root,
            stdin=subprocess.DEVNULL, stdout=out, stderr=err, shell=False,
            env={**os.environ, "LD_LIBRARY_PATH": args.library_path},
        )
    exit_file.write_text(f"{completed.returncode}\n", encoding="ascii")
    if completed.returncode != 0:
        raise AdapterError(f"run.sh failed with exit code {completed.returncode}")
    print("LPAI_EVALUATION_RUN_OK")


def collect(args: argparse.Namespace) -> None:
    config = below(args.config, args.config_root, "config")
    run_root = below(str(config.parent), args.run_root, "evaluation run")
    if (run_root / "exit_code.txt").read_text(encoding="ascii").strip() != "0":
        raise AdapterError("evaluation exit code is not zero")
    reports = sorted((run_root / "eval_report").glob("eval_report_*.txt"))
    if len(reports) != 1 or not reports[0].is_file():
        raise AdapterError(f"expected exactly one evaluation report, found {len(reports)}")
    sys.stdout.write(reports[0].read_text(encoding="utf-8"))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="action", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", required=True)
    common.add_argument("--config-root", required=True)
    common.add_argument("--prediction", required=True)
    common.add_argument("--prediction-root", required=True)
    common.add_argument("--truth-root", required=True)
    common.add_argument("--scene", required=True)
    common.add_argument("--run-id", required=True)
    common.add_argument("--require-truth-manifest", action="store_true")
    prepare_parser = sub.add_parser("prepare", parents=[common])
    prepare_parser.add_argument("--template", required=True)
    precheck_parser = sub.add_parser("precheck", parents=[common])
    precheck_parser.add_argument("--run-script", required=True)
    precheck_parser.add_argument("--evaluator-binary", required=True)
    precheck_parser.add_argument("--library-path", required=True)
    execute_parser = sub.add_parser("evaluate", parents=[common])
    execute_parser.add_argument("--run-root", required=True)
    execute_parser.add_argument("--run-script", required=True)
    execute_parser.add_argument("--library-path", required=True)
    collect_parser = sub.add_parser("collect", parents=[common])
    collect_parser.add_argument("--run-root", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    {"prepare": prepare, "precheck": precheck, "evaluate": evaluate, "collect": collect}[args.action](args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AdapterError, OSError, subprocess.SubprocessError) as exc:
        print(f"LPAI_EVALUATION_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
