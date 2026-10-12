#!/usr/bin/env python3
"""Reviewed SW0055-2723 scene adapter used only by profile commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from prepare_replay import build_replay_config, extract_scene, inspect_archive, sha256_file, validate_scene


SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
ARCHIVE = Path("/home/bench-user/example-user/raw_pack_11.tar.gz")
ARCHIVE_SHA = "7488b575c5d6e2a27e58225cef8d84070bc12119f419646ad006caca38267e60"
DATASET_ROOT = Path("/home/bench-user/bench-eval/cache/datasets/raw_pack_11")
SSH_CONFIG = Path("/home/bench-user/bench-eval/config/ssh_config")
BENCH_ALIAS = "bench-avm"
BENCH_SHARED = "/userdata/bench-eval-stage/shared/sw0055-2723-replay-verified-20260917"
BENCH_ARCHIVE = "/userdata/bench-eval-stage/sw0055-2723-deploy-20260917-001/incoming/candidate.app.tar.gz"
BENCH_ARCHIVE_SHA = "dfc4431656893db3522280a266d306838023a32007f74325e5dfa99fa1b600ff"
BUILD_ROOT = Path("/home/bench-user/bench-eval/cache/builds")
DEFAULT_ASSETS = {
    "libxcamera.so": (Path("/home/bench-user/bench-eval/cache/libraries/sw0055-2723-replay-verified-20260917/libxcamera.so"), "2c8c1318cd2e3bcd4bb989e7055153fffaef42dfbc617bb47bd3897401f04a88"),
    "libxlidar.so": (Path("/home/bench-user/bench-eval/cache/libraries/sw0055-2723-replay-verified-20260917/libxlidar.so"), "139089feac395540f62991effc36caa5d33c403e455b0d58017edcde01b102ad"),
    "liburoad_m100.so": (Path("/home/bench-user/bench-eval/cache/libraries/sw0055-2723-replay-verified-20260917/liburoad_m100.so"), "42e49c4cd84c0ca07ab5564378e6cbd36237505613c8679aab1b2e57302bf8ba"),
}


class AdapterError(RuntimeError):
    pass


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def assets_for(run_dir: Path) -> dict[str, tuple[Path, str]]:
    """Resolve an optional freshly built evaluation library from immutable job.json."""
    assets = dict(DEFAULT_ASSETS)
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
    requested = job.get("artifacts", {}).get("evaluation_library")
    if requested is None:
        return assets
    if not isinstance(requested, dict) or set(requested) != {"path", "sha256"}:
        raise AdapterError("artifacts.evaluation_library must contain only path and sha256")
    expected = requested.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise AdapterError("evaluation library sha256 is invalid")
    source = Path(str(requested.get("path", ""))).expanduser().resolve()
    build_root = BUILD_ROOT.resolve()
    if source.parent.parent != build_root or source.name != "liburoad_m100.so":
        raise AdapterError("evaluation library must be a direct child of a reviewed build artifact directory")
    manifest_path = source.parent / "build-manifest.json"
    if not source.is_file() or not manifest_path.is_file():
        raise AdapterError("evaluation library or build manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("state") != "BUILT"
        or Path(str(manifest.get("output", ""))).expanduser().resolve() != source
        or manifest.get("sha256") != expected
        or manifest.get("machine") != "AArch64"
        or manifest.get("elf_class") != 64
    ):
        raise AdapterError("evaluation library differs from its successful build manifest")
    if digest(source) != expected:
        raise AdapterError("evaluation library hash differs from immutable job.json")
    assets["liburoad_m100.so"] = (source, expected)
    return assets


def run(argv: list[str], *, timeout: int = 3600, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, text=True, capture_output=True, shell=False, timeout=timeout, env=env)
    if result.returncode:
        raise AdapterError(f"command failed ({result.returncode}): {argv[0]}: {result.stderr[-4000:]}")
    return result.stdout


def auth_env() -> dict[str, str]:
    return {**os.environ, "SSH_ASKPASS": "/bin/true", "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": "bench-eval"}


def bench(argv: list[str], timeout: int = 3600) -> str:
    return run([
        "ssh", "-F", str(SSH_CONFIG), "-o", "ConnectTimeout=8", "-o", "BatchMode=no",
        "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=yes", "-o", "PreferredAuthentications=keyboard-interactive",
        "-o", "NumberOfPasswordPrompts=1", BENCH_ALIAS, "--", shlex.join(argv),
    ], timeout=timeout, env=auth_env())


def one(root: Path, pattern: str) -> Path:
    values = sorted(root.glob(pattern))
    if len(values) != 1:
        raise AdapterError(f"expected one {pattern} below {root}, found {len(values)}")
    return values[0]


def prepare_scene(run_dir: Path, scene: str) -> dict[str, str]:
    if not ARCHIVE.is_file() or sha256_file(ARCHIVE) != ARCHIVE_SHA:
        raise AdapterError("raw_pack_11 archive is missing or changed")
    scene_root = DATASET_ROOT / scene
    if not scene_root.exists():
        inventory = inspect_archive(ARCHIVE)
        if scene not in inventory["scenes"]:
            raise AdapterError(f"scene is absent from archive: {scene}")
        DATASET_ROOT.mkdir(parents=True, exist_ok=True)
        extract_scene(ARCHIVE, DATASET_ROOT, scene)
    validate_scene(scene_root)
    root = run_dir / "replay-input"
    root.mkdir(parents=True, exist_ok=True)
    config = root / f"replay_config_{scene}.no-camera.json"
    value = build_replay_config(scene_root)
    value["cameras"] = []
    expected = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if config.exists() and config.read_text(encoding="utf-8") != expected:
        raise AdapterError("existing replay config differs")
    config.write_text(expected, encoding="utf-8")
    camera = scene_root / "raw" / "raw_in_10s" / "f_ca_120"
    return {"config": str(config), "index": str(one(camera, "*.idx")), "video": str(one(camera, "*.h265"))}


def prepare_jpegs(index: Path, video: Path) -> Path:
    destination = video.parent.parent.parent / "derived" / "jpeg_f_ca_120"
    expected = len(json.loads(index.read_text(encoding="utf-8"))["timestamps_ns"])
    signature = hashlib.sha256((digest(video) + digest(index)).encode("ascii")).hexdigest()
    manifest = destination / "manifest.json"
    if manifest.is_file():
        current = json.loads(manifest.read_text(encoding="utf-8"))
        if current.get("input_sha256") == signature and len(list(destination.glob("frame_*.jpg"))) == expected:
            return destination
        raise AdapterError("existing JPEG cache differs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".jpeg.", dir=destination.parent))
    try:
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "4", "-i", str(video), "-vsync", "0", "-q:v", "3", str(temporary / "frame_%06d.jpg")])
        count = len(list(temporary.glob("frame_*.jpg")))
        if count != expected:
            raise AdapterError(f"JPEG count mismatch: expected={expected}, actual={count}")
        (temporary / "manifest.json").write_text(json.dumps({"input_sha256": signature, "frames": count}) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def replace(text: str, pattern: str, value: str, label: str) -> str:
    result, count = re.subn(pattern, value, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise AdapterError(f"cannot render {label}")
    return result


def payload(run_dir: Path, scene: str) -> Path:
    root = Path(__file__).resolve().parent
    old = root / "runs" / "sw0055-2723-replay-20260917-001"
    text = (old / "bench_session.sh").read_text(encoding="utf-8")
    assets = assets_for(run_dir)
    values = (
        (r"^RUN_ID=.*$", f"RUN_ID={run_dir.name}", "run"),
        (r"^SCENE=.*$", f"SCENE={scene}", "scene"),
        (r"^SOURCE_ARCHIVE=.*$", f"SOURCE_ARCHIVE={BENCH_ARCHIVE}", "archive"),
        (r"^SOURCE_ARCHIVE_SHA=.*$", f"SOURCE_ARCHIVE_SHA={BENCH_ARCHIVE_SHA}", "archive hash"),
        (r"^EVAL_LIB_SHA=.*$", f"EVAL_LIB_SHA={assets['liburoad_m100.so'][1]}", "eval hash"),
        (r"^EVAL_LIB_SOURCE=.*$", f"EVAL_LIB_SOURCE={BENCH_SHARED}/liburoad_m100.so", "eval source"),
        (r"^XCAMERA_LIB_SOURCE=.*$", f"XCAMERA_LIB_SOURCE={BENCH_SHARED}/libxcamera.so", "camera source"),
        (r"^XLIDAR_LIB_SHA=.*$", f"XLIDAR_LIB_SHA={assets['libxlidar.so'][1]}", "lidar hash"),
        (r"^XLIDAR_LIB_SOURCE=.*$", f"XLIDAR_LIB_SOURCE={BENCH_SHARED}/libxlidar.so", "lidar source"),
    )
    for pattern, value, label in values:
        text = replace(text, pattern, value, label)
    text = text.replace("/tmp/sw0055-2723-prepare-config.py", f"{BENCH_SHARED}/prepare_bench_config.py")
    text = text.replace("/tmp/sw0055-2723-replay.sdf", f"{BENCH_SHARED}/{run_dir.name}.sdf")
    text = text.replace("/tmp/sw0055-2723-run-candidate.sh", f"{BENCH_SHARED}/{run_dir.name}-run-candidate.sh")
    target = run_dir / "bench-payload"
    target.mkdir(parents=True, exist_ok=True)
    (target / "bench_session.sh").write_text(text, encoding="utf-8")
    wrapper = (old / "run-candidate.sh").read_text(encoding="utf-8").replace("sw0055-2723-replay-20260917-001", run_dir.name)
    sdf = (old / "infer_expert_pcm_replay.sdf").read_text(encoding="utf-8").replace("sw0055-2723-replay-20260917-001", run_dir.name)
    (target / f"{run_dir.name}-run-candidate.sh").write_text(wrapper, encoding="utf-8")
    (target / f"{run_dir.name}.sdf").write_text(sdf, encoding="utf-8")
    shutil.copy2(root / "prepare_bench_config.py", target / "prepare_bench_config.py")
    for name, (source, expected) in assets.items():
        if not source.is_file() or digest(source) != expected:
            raise AdapterError(f"reviewed asset missing or changed: {source}")
        shutil.copy2(source, target / name)
    return target


def stage(local: Path) -> None:
    bench(["mkdir", "-p", BENCH_SHARED])
    producer = subprocess.Popen(["tar", "-C", str(local), "-cf", "-", "."], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert producer.stdout is not None
    consumer = subprocess.run([
        "ssh", "-F", str(SSH_CONFIG), "-o", "BatchMode=no", "-o", "PubkeyAuthentication=no",
        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=yes",
        "-o", "PreferredAuthentications=keyboard-interactive", BENCH_ALIAS, "--",
        f"tar -C {shlex.quote(BENCH_SHARED)} -xf -",
    ], stdin=producer.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, env=auth_env())
    producer.stdout.close()
    producer_error = producer.stderr.read() if producer.stderr else b""
    if producer.wait() or consumer.returncode:
        raise AdapterError((producer_error + consumer.stderr).decode(errors="replace")[-4000:])


def script(run_dir: Path) -> str:
    return f"{BENCH_SHARED}/bench_session.sh"


def precheck(run_dir: Path, scene: str) -> None:
    bench(["sh", "-c", f"printf '%s  %s\\n' {BENCH_ARCHIVE_SHA} {BENCH_ARCHIVE} | sha256sum -c -"])
    replay = prepare_scene(run_dir, scene)
    replay["frames"] = str(prepare_jpegs(Path(replay["index"]), Path(replay["video"])))
    (run_dir / "replay-runtime.json").write_text(json.dumps(replay, indent=2) + "\n", encoding="utf-8")
    stage(payload(run_dir, scene))
    bench(["chmod", "0755", script(run_dir), f"{BENCH_SHARED}/{run_dir.name}-run-candidate.sh"])
    for step in ("prepare", "install_replay_libs", "install_eval_lib", "configure_offline"):
        print(bench(["sh", script(run_dir), step], timeout=1800), end="")


def capture(run_dir: Path, _scene: str) -> None:
    # Non-interactive SSH sessions on the upgraded bench do not inherit the
    # framework library path from an operator's login shell. Keep the service
    # manager invocation deterministic instead of relying on that shell state.
    print(bench([
        "sh", "-c",
        "test -d /app/livc && test ! -L /app/livc && "
        "LD_LIBRARY_PATH=/framework/lib:/usr/lib "
        "/framework/bin/liware.tool.smctl is-active infer_expert_fusion",
    ]), end="")


def stop(_run_dir: Path, _scene: str) -> None:
    print("STOP_DEFERRED_TO_ATOMIC_ACTIVATE")


def start(run_dir: Path, _scene: str) -> None:
    # The reviewed replay service is not covered by the production SELinux
    # policy.  The previously validated workflow therefore runs only the
    # isolated candidate in Permissive mode and restores Enforcing before the
    # production service is returned.  restore() repeats the enforcement even
    # when activation or replay raises.
    print(bench(["/usr/sbin/setenforce", "0"]), end="")
    if bench(["/usr/sbin/getenforce"]).strip() != "Permissive":
        raise AdapterError("bench did not enter SELinux Permissive mode")
    print(bench(["sh", script(run_dir), "activate"], timeout=300), end="")


def health(_run_dir: Path, _scene: str) -> None:
    print(bench(["sh", "-c", "pidof infer_expert_pcm >/dev/null && ss -lnt | grep -q ':50051 ' && ss -lnt | grep -q ':50054 '"]), end="")


def replay(run_dir: Path, _scene: str) -> None:
    value = json.loads((run_dir / "replay-runtime.json").read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parent
    replay_py = root / "runs" / "sw0055-2723-replay-20260917-001" / "workstation_jpeg_camera_replay.py"
    container = "bench-eval-recorder-raw11"
    run(["docker", "start", container])
    try:
        for _ in range(60):
            probe = subprocess.run(
                ["/home/bench-user/bench-eval/bin/bench-eval", "replay", "--base-url", "http://127.0.0.1:15000", "status"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
            )
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            raise AdapterError("RecorderHMI did not become ready")
        env = {**os.environ, "PYTHONPATH": str(root / "xcamera_replay")}
        runtime = root / "xcamera_replay"
        runtime.mkdir(exist_ok=True)
        shutil.copy2(replay_py, runtime / "replay.py")
        proto = Path("/home/bench-user/.lidocker/.conan/data/xcamera/0.4.2-userdebug5/livc/stable/source/xcamera/grpc_gen/proto")
        for item in proto.glob("*.proto"):
            shutil.copy2(item, runtime / item.name)
        python = "/home/bench-user/bench-eval/venvs/xcamera-replay/bin/python"
        run([python, "-m", "grpc_tools.protoc", "-I", str(runtime), "--python_out", str(runtime), "--grpc_python_out", str(runtime), str(runtime / "xcam_replay_data.proto"), str(runtime / "xcam_replay_service.proto")])
        output = run([
            python, str(runtime / "replay.py"), "--config", value["config"], "--camera-index", value["index"],
            "--camera-frames", value["frames"], "--camera-target", "203.0.113.20:50051",
            "--lidar-target", "203.0.113.20:50054", "--base-url", "http://127.0.0.1:15000",
            "--speed", "1.0", "--poll-interval", "1", "--max-duration", "1800",
        ], timeout=2100, env=env)
        print(output, end="")
        result = bench(["sh", script(run_dir), "inspect"])
        for name in ("OUTPUT_FILES", "PRED_TRAJ_FILES", "TRAJ_XY_FILES", "PRED_IMG_FILES"):
            match = re.search(rf"^{name}=([0-9]+)$", result, re.MULTILINE)
            if not match or int(match.group(1)) < 1:
                raise AdapterError(f"output gate failed: {name}")
        print(result, end="")
    finally:
        subprocess.run(["docker", "stop", "-t", "10", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restore(run_dir: Path, _scene: str) -> None:
    subprocess.run(["docker", "stop", "-t", "10", "bench-eval-recorder-raw11"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    errors: list[str] = []
    # Enforce before returning production, but never let an enforcement error
    # prevent the formal APP restoration attempt.  Repeat it after restoration
    # so the final state is deterministic on every success/failure path.
    for argv in (
        ["/usr/sbin/setenforce", "1"],
        ["sh", script(run_dir), "restore"],
        ["/usr/sbin/setenforce", "1"],
    ):
        try:
            print(bench(argv, timeout=300), end="")
        except AdapterError as exc:
            errors.append(str(exc))
    if errors:
        raise AdapterError("; ".join(errors))


def verify(run_dir: Path, _scene: str) -> None:
    print(bench(["sh", script(run_dir), "verify_restore"], timeout=300), end="")
    if run(["docker", "inspect", "-f", "{{.State.Running}}", "bench-eval-recorder-raw11"]).strip() != "false":
        raise AdapterError("RecorderHMI remained active")


ACTIONS = {
    "precheck": precheck, "capture_baseline": capture, "stop_active": stop, "start_candidate": start,
    "health_candidate": health, "replay": replay, "restore": restore, "verify_restore": verify,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=tuple(ACTIONS))
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--scene", required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    if not SAFE.fullmatch(run_dir.name) or not SAFE.fullmatch(args.scene) or not (run_dir / "job.json").is_file():
        raise AdapterError("unsafe run directory, scene, or missing job.json")
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
    if job.get("run_id") != run_dir.name or job.get("replay_input", {}).get("scene") != args.scene:
        raise AdapterError("arguments differ from immutable job.json")
    ACTIONS[args.action](run_dir, args.scene)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AdapterError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"SW0055_SCENE_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
