#!/usr/bin/env python3
"""One-call workstation workflow for one or more Stream scenes in one Runner run.

Run on the workstation.  The default plan is read-only; --execute creates a new immutable
run, submits it to the existing Runner, then retains its product and logs.
The Runner and adapter remain responsible for bench locking and restoration.
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

BASE = Path("/home/bench-user/bench-eval")
ROOT = Path("/mnt/data/bench-eval")
CLI = BASE / "bin/bench-eval"
SAFE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
SCENE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]+$")


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON object required: {path}")
    return value


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command(*args: str, timeout: int = 120) -> dict:
    result = subprocess.run([str(CLI), *args], text=True, capture_output=True,
                            stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    if result.returncode:
        # doctor returns a useful JSON blocker report on stdout with exit code 2.
        raise RuntimeError(f"bench-eval {' '.join(args[:2])}: {result.stdout[-3000:]} {result.stderr[-1200:]}")
    return json.loads(result.stdout)


def source_contract(source: str) -> tuple[dict, dict, str, list[dict], str]:
    source_dir = BASE / "adapters" / source
    job = read(source_dir / "job.json")
    profile = read(source_dir / "profile.json")
    adapter = (source_dir / "small_stream_adapter.py").read_text(encoding="utf-8")
    driver = source_dir / "sw0058_gate_probe_driver_reference.py"
    if not driver.is_file():
        raise RuntimeError("verified source driver is missing")
    if (job.get("run_id") != source or profile.get("profile_id") != source
            or job.get("batch_stream", {}).get("mode") != "stream"
            or job.get("replay_input", {}).get("scene_count") != 1
            or len(job.get("batch_stream", {}).get("scenes", [])) != 1
            or job.get("workflow", {}).get("targets") != ["workstation_predictions"]
            or job.get("workflow", {}).get("bench_strategy") != "isolated_candidate"):
        raise RuntimeError("source is not a verified one-scene Stream template")
    if ("len(entries) != 1" not in adapter or "SCENES = frozenset" not in adapter):
        raise RuntimeError("source adapter contract changed; use the general workflow")
    source_state = read(ROOT / "runs" / source / "state.json")
    source_restore = read(ROOT / "runs" / source / "restore-verification.json")
    source_transfer = read(ROOT / "runs" / source / "workstation-predictions.json")
    receipt = read(ROOT / "cache/predictions" / source / "predictions.bench-eval-product.json")
    if (source_state.get("state") != "COMPLETED_TO_REQUESTED_GOAL"
            or source_restore.get("restore_ok") is not True
            or source_transfer.get("state") != "WORKSTATION_PREDICTIONS_COLLECTED"
            or receipt.get("state") != "SUCCEEDED"
            or receipt.get("producer_run_id") != source
            or source_transfer.get("inventory_sha256") != receipt.get("inventory_sha256")
            or source_transfer.get("product_identity") != receipt.get("product_identity")
            or not isinstance(receipt.get("files"), list)
            or len(receipt["files"]) != source_transfer.get("file_count")):
        raise RuntimeError("source run and product must be successful")
    manifest = read(Path(job["replay_input"]["prepared_manifest"]))
    scenes = manifest.get("prepared_scenes")
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("prepared manifest has no scenes")
    anchor = next((entry for entry in scenes if entry.get("scene") ==
                   job["replay_input"].get("representative_scene")), None)
    recorded_anchor = dict(job["replay_input"].get("archive_stats") or {})
    recorded_anchor.pop("timestamped_jpeg_frames", None)
    if anchor != recorded_anchor:
        raise RuntimeError("prepared manifest source scene differs from the verified Job")
    old_camera = job["batch_stream"]["scenes"][0]["timestamped_jpeg_frames"]
    old_index = re.search(r"camera-s[0-9]+$", old_camera)
    if old_index is None:
        raise RuntimeError("source camera directory has no scene index")
    return job, profile, adapter, scenes, old_camera[:old_index.start()]


def selected_indices(spec: str, count: int) -> list[int]:
    if spec == "all":
        return list(range(1, count + 1))
    if not re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", spec):
        raise RuntimeError("--scenes must be all or ascending manifest indices, e.g. 1,2,3")
    indices = [int(value) for value in spec.split(",")]
    if indices != sorted(set(indices)) or indices[-1] > count:
        raise RuntimeError("scene indices must be unique, ascending and inside the manifest")
    return indices


def plan(args: argparse.Namespace) -> dict:
    resume = args.resume_delivery
    args.task_id = args.task_id or args.run_id
    for value in (args.source_run, args.run_id, args.task_id, args.actor):
        if not SAFE.fullmatch(value):
            raise RuntimeError(f"unsafe identifier: {value}")
    if args.run_id == args.source_run:
        raise RuntimeError("a new immutable run ID is required")
    if len(args.run_id) + len("-cleanup") > 95:
        raise RuntimeError("run ID is too long for the cleanup receipt")
    job, profile, adapter, scenes, camera_prefix = source_contract(args.source_run)
    indices = selected_indices(args.scenes, len(scenes))
    selections = []
    for index in indices:
        item = scenes[index - 1]
        scene = item.get("scene")
        if not isinstance(scene, str) or not SCENE.fullmatch(scene):
            raise RuntimeError("unsafe scene name in manifest")
        camera = Path(camera_prefix + f"camera-s{index:03d}")
        if not resume:
            if not camera.is_dir() or camera.is_symlink():
                raise RuntimeError(f"timestamped camera directory unavailable: {camera}")
            if len(list(camera.iterdir())) != item.get("camera_frames"):
                raise RuntimeError(f"camera frame count differs from prepared manifest: {scene}")
        selections.append({"index": index, "scene": scene, "camera": str(camera),
                           "camera_frames": item["camera_frames"], "archive_stats": item})
    if len({item["scene"] for item in selections}) != len(selections):
        raise RuntimeError("selected scene names are not unique")
    if not resume:
        if (BASE / "adapters" / args.run_id).exists() or (ROOT / "runs" / args.run_id).exists():
            raise RuntimeError("run ID is already in use")
        if (ROOT / "tasks" / args.task_id).exists():
            raise RuntimeError("task ID is already in use")
    result = {"source_run": args.source_run, "run_id": args.run_id,
            "task_id": args.task_id, "scene_count": len(selections),
            "scenes": selections, "actor": args.actor,
            "job": job, "profile": profile, "adapter": adapter,
            "resume_delivery": resume}
    if resume:
        result["resume_coordination"] = read(BASE / "adapters" / args.run_id / "job.json").get("coordination")
    derived_job, derived_profile, _ = derive(result)
    if resume:
        existing = BASE / "adapters" / args.run_id
        if (read(existing / "job.json") != derived_job
                or read(existing / "profile.json") != derived_profile):
            raise RuntimeError("existing Job/Profile differs from selected source and scenes")
    return result


def derive(p: dict) -> tuple[dict, dict, str]:
    old, new = p["source_run"], p["run_id"]
    source_job, source_profile = p["job"], p["profile"]
    old_scene = source_job["replay_input"]["representative_scene"]
    # Replace run identity only in a proven source template, then set all
    # scene-specific fields from the authoritative prepared manifest.
    job = json.loads(json.dumps(source_job).replace(old, new))
    profile = json.loads(json.dumps(source_profile).replace(old, new))
    if not p.get("resume_delivery"):
        # A source run provides validated inputs, not admission for its owner.
        # Existing delivery resumes must retain their immutable Job identity.
        job.setdefault("coordination", {})["allowed_actors"] = [p["actor"]]
    elif "resume_coordination" in p:
        if p["resume_coordination"] is None:
            job.pop("coordination", None)
        else:
            job["coordination"] = p["resume_coordination"]
    first = p["scenes"][0]
    job["replay_input"]["representative_scene"] = first["scene"]
    job["replay_input"]["scene_count"] = p["scene_count"]
    job["replay_input"]["archive_stats"] = {
        **first["archive_stats"], "timestamped_jpeg_frames": first["camera"]}
    job["batch_stream"]["scenes"] = [
        {"scene": entry["scene"], "timestamped_jpeg_frames": entry["camera"]}
        for entry in p["scenes"]]
    job["diagnostic"]["reason"] = "Verified same-software held Stream batch"
    scene_set = "SCENES = frozenset({" + ", ".join(
        json.dumps(entry["scene"]) for entry in p["scenes"]) + "})"
    adapter = p["adapter"].replace(old, new).replace(
        f'SCENES = frozenset({{"{old_scene}"}})',
        scene_set)
    adapter = adapter.replace("len(entries) != 1", "len(entries) < 1").replace(
        'len({item.get("scene") for item in entries if isinstance(item, dict)}) != 1',
        'len({item.get("scene") for item in entries if isinstance(item, dict)}) != len(entries)')
    # The reviewed Runner allows at most 3600 seconds per transaction step;
    # preserve the adapter's 3500-second guard and let Runner restore on timeout.
    profile["execution"]["command_sets"][new]["commands"]["replay"]["timeout_seconds"] = 3600
    if (old in json.dumps(job) or old in json.dumps(profile) or old in adapter
            or scene_set not in adapter or "len(entries) != 1" in adapter
            or '}) != 1' in adapter
            or job["run_id"] != new or job["execution"]["command_set_id"] != new
            or job["batch_stream"]["batch_id"] != new
            or profile["profile_id"] != new
            or list(profile["execution"]["command_sets"]) != [new]
            or job["replay_input"]["scene_count"] != len(job["batch_stream"]["scenes"])):
        raise RuntimeError("derived Job/Profile/adapter has stale identity")
    compile(adapter, f"{new}/stream_batch_adapter.py", "exec")
    return job, profile, adapter


def offline_validate(p: dict) -> dict:
    """Validate generated Job/Profile against the installed Runner without a run."""
    job, profile, _adapter = derive(p)
    with tempfile.TemporaryDirectory(prefix="bench-eval-batch-plan-") as name:
        job_path = Path(name) / "job.json"
        profile_path = Path(name) / "profile.json"
        write(job_path, job)
        write(profile_path, profile)
        preflight = command("preflight", "--job", str(job_path))
        if preflight.get("status") != "READY":
            raise RuntimeError(f"Runner preflight blocked: {preflight.get('blockers')}")
        sys.path.insert(0, str(BASE / "current/automation"))
        from target_workflow import validate_target_job
        resolved = validate_target_job(job, profile_path, ROOT / "runs" / p["run_id"])
        if resolved.get("requires_bench") is not True or resolved.get("targets") != {"workstation_predictions"}:
            raise RuntimeError("Runner resolved an unexpected target or bench dependency")
    return {"runner_preflight": "READY", "profile": "VALID", "replay_timeout_seconds": 3600}


def prepare(p: dict) -> tuple[Path, Path]:
    job, profile, adapter = derive(p)
    directory = BASE / "adapters" / p["run_id"]
    directory.mkdir(mode=0o750)
    write(directory / "job.json", job)
    write(directory / "profile.json", profile)
    (directory / "small_stream_adapter.py").write_text(adapter, encoding="utf-8")
    shutil.copy2(BASE / "adapters" / p["source_run"] / "sw0058_gate_probe_driver_reference.py",
                 directory / "sw0058_gate_probe_driver_reference.py")
    return directory / "job.json", directory / "profile.json"


def bench_capacity() -> None:
    env = {**os.environ, "SSH_ASKPASS": "/bin/true", "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": "bench-eval"}
    ssh = ["ssh", "-F", str(BASE / "config/ssh_config"), "-o", "BatchMode=no",
           "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no",
           "-o", "KbdInteractiveAuthentication=yes", "-o", "PreferredAuthentications=keyboard-interactive",
           "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=5", "bench-avm",
           "df -Pk /app /map"]
    result = subprocess.run(ssh, env=env, text=True, capture_output=True,
                            timeout=20, check=False)
    if result.returncode:
        raise RuntimeError(f"bench capacity check failed: {result.stderr[-500:]}")
    rows = [line.split() for line in result.stdout.splitlines()[1:]]
    if len(rows) != 2:
        raise RuntimeError("unexpected bench capacity output")
    for row in rows:
        if len(row) < 6 or int(row[3]) < 5 * 1024 * 1024 or int(row[4].rstrip("%")) > 80:
            raise RuntimeError("bench /app or /map has insufficient free space")


def progress(message: str) -> None:
    print(f"[progress] {message}", file=sys.stderr, flush=True)


def same_filesystem(source: Path, destination: Path) -> bool:
    return source.stat().st_dev == destination.stat().st_dev


class RunProgress:
    """Report existing Runner evidence without affecting the worker or its result."""

    STAGES = {
        "precheck": "预检", "start_candidate": "启动候选应用",
        "replay": "回放", "restore": "恢复正式服务",
        "verify_restore": "核验恢复",
    }

    def __init__(self, run_dir: Path, scenes: list[dict], emit=progress) -> None:
        self.run_dir = run_dir
        self.scenes = scenes
        self.emit = emit
        self.offsets = {}
        self.pending = {}
        self.started = time.monotonic()
        self.last_report = self.started
        self.stage = "等待 Runner 启动"
        self.completed = 0
        self.frame = None
        self.total_frames = None

    def _lines(self, path: Path) -> list[str]:
        if not path.is_file():
            return []
        offset = self.offsets.get(path, 0)
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read()
            self.offsets[path] = stream.tell()
        if not chunk:
            return []
        data = self.pending.pop(path, b"") + chunk
        lines = data.split(b"\n")
        if lines[-1]:
            self.pending[path] = lines.pop()
        else:
            lines.pop()
        return [line.decode("utf-8", errors="replace") for line in lines]

    def _stage(self, value: str) -> None:
        if value != self.stage:
            self.stage = value
            self.emit(f"{value}；已完成 {self.completed}/{len(self.scenes)} 场景")
            self.last_report = time.monotonic()

    def poll(self) -> None:
        for line in self._lines(self.run_dir / "events.jsonl"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "TRANSACTION_STEP_STARTED":
                stage = self.STAGES.get(event.get("step"))
                if stage:
                    self._stage(stage)
            elif event.get("event") == "WORKSTATION_COLLECTION_STARTED":
                self._stage("收集工作站预测")
        for line in self._lines(self.run_dir / "probe.log"):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            recorder = item.get("recorder") if isinstance(item, dict) else None
            if isinstance(recorder, dict):
                self.frame = recorder.get("current_frame_index")
                self.total_frames = recorder.get("total_frames")
            if not isinstance(item, dict) or item.get("state") != "REPLAY_COMPLETE":
                continue
            if (self.completed < len(self.scenes)
                    and item.get("scene") == self.scenes[self.completed]["scene"]):
                self.completed += 1
                self.frame = None
                self.total_frames = None
                elapsed = int(time.monotonic() - self.started)
                self.emit(f"场景 {self.completed}/{len(self.scenes)} 完成；累计 {elapsed // 60} 分 {elapsed % 60} 秒")
                self.last_report = time.monotonic()
        now = time.monotonic()
        if now - self.last_report >= 30:
            frame = (f"；当前帧 {self.frame}/{self.total_frames}"
                     if self.stage == "回放" and isinstance(self.frame, int)
                     and isinstance(self.total_frames, int) else "")
            elapsed = int(now - self.started)
            self.emit(f"{self.stage}；已完成 {self.completed}/{len(self.scenes)} 场景{frame}；累计 {elapsed // 60} 分 {elapsed % 60} 秒")
            self.last_report = now


def validate_and_retain(p: dict, exit_code: int) -> tuple[Path, Path]:
    run = p["run_id"]
    run_dir = ROOT / "runs" / run
    product = ROOT / "cache/predictions" / run / "predictions"
    receipt_path = product.with_name("predictions.bench-eval-product.json")
    receipt = read(receipt_path)
    state = read(run_dir / "state.json")
    restore = read(run_dir / "restore-verification.json")
    transfer = read(run_dir / "workstation-predictions.json")
    if (exit_code != 0 or state.get("state") != "COMPLETED_TO_REQUESTED_GOAL"
            or restore.get("restore_ok") is not True
            or transfer.get("state") != "WORKSTATION_PREDICTIONS_COLLECTED"
            or receipt.get("state") != "SUCCEEDED"
            or receipt.get("producer_run_id") != run
            or receipt.get("workstation_product") != str(product)
            or transfer.get("inventory_sha256") != receipt.get("inventory_sha256")
            or transfer.get("product_identity") != receipt.get("product_identity")):
        raise RuntimeError("Runner, restoration or producer receipt did not verify")
    files = receipt.get("files")
    if (not isinstance(files, list) or not files
            or len(files) != transfer.get("file_count")
            or (receipt.get("file_count") is not None and len(files) != receipt["file_count"])):
        raise RuntimeError("producer receipt file count mismatch")
    expected_scenes = [entry["scene"] for entry in p["scenes"]]
    evidence = BASE / "adapters" / run / "evidence" / run
    report = read(evidence / "batch-result.json")
    completed = report.get("completed") or []
    if (report.get("expected") != len(expected_scenes)
            or [entry.get("scene") for entry in completed] != expected_scenes
            or any(entry.get("state") != "REPLAY_COMPLETE" for entry in completed)):
        raise RuntimeError("batch replay evidence is incomplete or out of order")
    listed = {}
    by_scene = {scene: {} for scene in expected_scenes}
    if any(path.is_symlink() for path in product.rglob("*")):
        raise RuntimeError("canonical product contains links")
    for item in files:
        relative = PurePosixPath(item["path"])
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2 or relative.parts[0] not in by_scene:
            raise RuntimeError("unsafe or unexpected receipt path")
        path = product.joinpath(*relative.parts)
        if not path.is_file() or path.is_symlink() or path.stat().st_size != item["size"]:
            raise RuntimeError(f"product differs from producer receipt: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != item.get("sha256"):
            raise RuntimeError(f"product SHA-256 differs from producer receipt: {relative}")
        listed[str(relative)] = path
        scene_files = by_scene[relative.parts[0]]
        name = relative.name
        kind = next((prefix for prefix in ("pred_img", "pred_road", "pred_traj", "traj_xy")
                     if name.startswith(prefix + "_")), "other")
        scene_files[kind] = scene_files.get(kind, 0) + 1
    if len(listed) != len(files):
        raise RuntimeError("duplicate paths in producer receipt")
    source_receipt = read(ROOT / "cache/predictions" / p["source_run"] / "predictions.bench-eval-product.json")
    source_types = {prefix for item in source_receipt["files"]
                    for prefix in ("pred_img", "pred_road", "pred_traj", "traj_xy")
                    if PurePosixPath(item["path"]).name.startswith(prefix + "_")}
    if not source_types or any(not source_types.issubset(set(by_scene[scene]))
                               for scene in expected_scenes):
        raise RuntimeError("one or more scenes lack expected prediction types")
    actual = {str(path.relative_to(product).as_posix()) for path in product.rglob("*") if path.is_file()}
    if actual != set(listed):
        raise RuntimeError("product file inventory differs from receipt")
    inventory = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    if inventory != receipt["inventory_sha256"]:
        raise RuntimeError("producer receipt inventory digest differs")
    if not evidence.is_dir() or any(path.is_symlink() for path in evidence.rglob("*")):
        raise RuntimeError("adapter evidence is missing or contains links")
    tasks = ROOT / "tasks"
    hard_links = same_filesystem(product, tasks)
    storage = ("hard_links_in_tasks; original_cache_retained" if hard_links else
               "copied_files_in_tasks; original_cache_retained")
    target = tasks / p["task_id"]
    if target.exists():
        if not p.get("resume_delivery"):
            raise RuntimeError("task ID already in use")
        index = read(target / "index.json")
        if (index.get("state") != "PASS" or index.get("task_id") != p["task_id"]
                or index.get("run_id") != run or index.get("file_count") != len(files)
                or index.get("inventory_sha256") != inventory
                or index.get("product_identity") != receipt["product_identity"]
                or index.get("canonical_product") != str(product)
                or index.get("producer_receipt") != str(receipt_path)
                or index.get("storage") != storage
                or [(item.get("scene_id"), item.get("scene")) for item in index.get("scenes", [])]
                != [(f"s{entry['index']:03d}", entry["scene"]) for entry in p["scenes"]]):
            raise RuntimeError("existing task does not match the verified product")
        for relative, source in listed.items():
            scene = PurePosixPath(relative).parts[0]
            number = next(entry["index"] for entry in p["scenes"] if entry["scene"] == scene)
            linked = target / f"scenes/s{number:03d}/predictions" / relative
            if not linked.is_file() or linked.is_symlink():
                raise RuntimeError("existing task file is missing or is a link")
            if hard_links:
                if (linked.stat().st_dev, linked.stat().st_ino) != (source.stat().st_dev, source.stat().st_ino):
                    raise RuntimeError("existing task hard links differ from canonical product")
            elif not filecmp.cmp(source, linked, shallow=False):
                raise RuntimeError("existing task copy differs from canonical product")
        if not (target / "logs").is_dir() or not (target / "inputs/common_param.effective.yaml").is_file():
            raise RuntimeError("existing task logs or effective configuration are missing")
        return target, receipt_path
    pending = tasks / ("." + p["task_id"] + ".pending-" + uuid.uuid4().hex)
    pending.mkdir()
    try:
        for entry in p["scenes"]:
            scene = entry["scene"]
            scene_dir = pending / f"scenes/s{entry['index']:03d}/predictions" / scene
            shutil.copytree(product / scene, scene_dir, copy_function=os.link if hard_links else shutil.copy2)
            for name, source in listed.items():
                if PurePosixPath(name).parts[0] != scene:
                    continue
                copied = scene_dir.joinpath(*PurePosixPath(name).parts[1:])
                if hard_links:
                    if (copied.stat().st_dev, copied.stat().st_ino) != (source.stat().st_dev, source.stat().st_ino):
                        raise RuntimeError("task file is not a hard link to cache")
                elif not filecmp.cmp(source, copied, shallow=False):
                    raise RuntimeError("task copy differs from canonical product")
        inputs = pending / "inputs"
        inputs.mkdir()
        shutil.copy2(run_dir / "configured-inputs/common_param.probe.yaml", inputs / "common_param.effective.yaml")
        for name in ("job.json", "profile.json"):
            shutil.copy2(BASE / "adapters" / run / name, inputs / name)
        shutil.copy2(receipt_path, pending / receipt_path.name)
        logs = pending / "logs"
        shutil.copytree(run_dir / "logs", logs / "runner")
        for name in ("probe.log", "events.jsonl", "restore-verification.json", "workstation-predictions.json"):
            shutil.copy2(run_dir / name, logs / name)
        shutil.copytree(evidence, logs / "adapter-evidence")
        write(pending / "index.json", {
            "schema_version": 1, "state": "PASS", "task_id": p["task_id"],
            "run_id": run, "scene_count": len(expected_scenes), "file_count": len(files),
            "canonical_product": str(product), "producer_receipt": str(receipt_path),
            "inventory_sha256": inventory, "product_identity": receipt["product_identity"],
            "scenes": [{"scene_id": f"s{entry['index']:03d}", "scene": entry["scene"],
                        "run_id": run, "file_count": sum(by_scene[entry["scene"]].values()),
                        "counts": by_scene[entry["scene"]],
                        "task_copy": str(target / f"scenes/s{entry['index']:03d}/predictions" / entry["scene"])}
                       for entry in p["scenes"]],
            "logs": str(target / "logs"),
            "storage": storage,
        })
        pending.rename(target)
    except BaseException:
        shutil.rmtree(pending)
        raise
    return target, receipt_path


def finish_delivery(p: dict, exit_code: int) -> dict:
    run = p["run_id"]
    task, receipt = validate_and_retain(p, exit_code)
    cleanup_id = run + "-cleanup"
    cleanup_receipt = ROOT / "maintenance" / f"{cleanup_id}.json"
    if not cleanup_receipt.is_file():
        cleanup = subprocess.run([sys.executable, str(ROOT / "tools/cleanup_bench_outputs.py"),
                                  "--batch-id", cleanup_id, str(receipt)], text=True,
                                 capture_output=True, timeout=300, check=False)
        if cleanup.returncode:
            raise RuntimeError(f"task retained at {task}; bench cleanup failed: {cleanup.stderr[-1000:]}")
    cleanup_data = read(cleanup_receipt)
    product = str(receipt.parent / "predictions")
    producer = read(receipt)
    accepted = cleanup_data.get("accepted")
    if (cleanup_data.get("batch_id") != cleanup_id or not isinstance(accepted, list)
            or len(accepted) != 1 or accepted[0].get("run_id") != run
            or accepted[0].get("source") != producer["source"]
            or accepted[0].get("workstation_product") != product
            or accepted[0].get("product_receipt") != str(receipt)
            or accepted[0].get("inventory_sha256") != producer["inventory_sha256"]
            or producer["source"] not in cleanup_data.get("removed", [])):
        raise RuntimeError("bench cleanup receipt does not match this run")
    shutil.copy2(cleanup_receipt, task / "logs/bench-output-cleanup.json")
    return {"state": "PASS", "run_id": run, "task": str(task),
            "product": product, "receipt": str(receipt),
            "logs": str(task / "logs"), "cleanup_receipt": str(cleanup_receipt)}


def execute(p: dict) -> dict:
    progress("检查 Runner、容量和队列")
    doctor = command("doctor")
    if doctor.get("status") != "READY":
        raise RuntimeError("bench-eval doctor is not READY")
    bench_capacity()
    queue = command("queue")
    if queue.get("active") or queue.get("queued") or queue.get("waiting"):
        raise RuntimeError("shared Runner queue is not empty")
    offline_validate(p)
    job_path, profile_path = prepare(p)
    run = p["run_id"]
    command("submit", "--job", str(job_path), "--submitted-by", p["actor"], "--client", "codex")
    for stage in ("ACTIVATE", "REPLAY"):
        command("approve", "--run-id", run, "--stage", stage, "--approved-by", p["actor"])
    queue = command("queue")
    queued = queue.get("queued") or []
    if (queue.get("active") or len(queued) != 1 or queued[0].get("run_id") != run
            or queued[0].get("state") != "READY"):
        raise RuntimeError("new run is not the sole READY queue item")
    progress(f"已提交 {run}；场景 {len(p['scenes'])} 个")
    logs = ROOT / "runs" / run / "logs"
    logs.mkdir(exist_ok=True)
    worker_log = logs / "worker-cli.log"
    if worker_log.exists():
        raise RuntimeError("worker log already exists")
    with worker_log.open("x", encoding="utf-8") as output:
        with subprocess.Popen([str(CLI), "worker", "once", "--executor", "target-workflow",
                               "--profile", str(profile_path)], stdin=subprocess.DEVNULL,
                              stdout=output, stderr=subprocess.STDOUT) as worker:
            reporter = RunProgress(ROOT / "runs" / run, p["scenes"])
            deadline = time.monotonic() + 7200
            deadline_reported = False
            while worker.poll() is None:
                try:
                    reporter.poll()
                except (OSError, ValueError, TypeError, KeyError):
                    pass  # Progress is advisory; Runner evidence is validated after exit.
                if time.monotonic() >= deadline and not deadline_reported:
                    deadline_reported = True
                    progress(f"Runner 超过 7200 秒；继续等待 Runner 恢复与收据，不终止 worker。证据：{ROOT / 'runs' / run}")
                time.sleep(2)
            try:
                reporter.poll()
            except (OSError, ValueError, TypeError, KeyError):
                pass
            exit_code = worker.returncode
    (logs / "worker-exit-code.txt").write_text(str(exit_code) + "\n", encoding="utf-8")
    if exit_code:
        raise RuntimeError(f"Runner worker failed with exit {exit_code}; evidence: {ROOT / 'runs' / run}")
    progress("Runner 已完成；验收并留存工作站产品")
    result = finish_delivery(p, exit_code)
    progress("交付与台架输出清理完成")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", help="defaults to the run ID")
    parser.add_argument("--scenes", default="all", help="all or ascending manifest indices, e.g. 1,2,3")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume-delivery", action="store_true")
    args = parser.parse_args()
    try:
        if args.execute and args.resume_delivery:
            raise RuntimeError("--execute and --resume-delivery are mutually exclusive")
        p = plan(args)
        result = finish_delivery(p, 0) if args.resume_delivery else execute(p) if args.execute else {
            "state": "PLAN_READY", "source_run": p["source_run"],
            "run_id": p["run_id"], "task_id": p["task_id"],
            "scene_count": p["scene_count"], "actor": p["actor"],
            "scenes": [{key: entry[key] for key in ("index", "scene", "camera", "camera_frames")}
                       for entry in p["scenes"]],
            **offline_validate(p),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"state": "BLOCKED", "run_id": args.run_id, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
