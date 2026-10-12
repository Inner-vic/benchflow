#!/usr/bin/env python3
"""SW0058 real-bench gate probe via a temporary bind-mount of the validated 054 candidate.

Mirrors the validated sw0058 bind-transaction recipe:
  capture initial -> stage control files + eval config into candidate ->
  stop formal app -> mount --bind candidate over /app/livc -> start ->
  health (camera/lidar endpoints) -> run our controlled-replay PROBE (1 scene)
  -> always restore (stop, umount, restart formal, verify hashes) -> release lock.

On ANY failure the finally block still attempts a safe restore; restore failure
is reported separately and fail-closed (never leaves the bench mounted).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

RUN_ROOT = Path("/home/bench-user/bench-eval")
BENCH = "bench-avm"
SSH_CONFIG = RUN_ROOT / "config/ssh_config"
BASE_SRC = RUN_ROOT / "cache/uploads/v151-prebuilt-20260921-006/v151-prebuilt-20260921-transaction.py"
SWEEP = RUN_ROOT / "skills/bench-eval-execution/scripts/run_controlled_sweep.py"
BENCH_IP = "203.0.113.20"

# The existing, already-validated SW0058 candidate on the bench (063 hashes).
CANDIDATE = "/app/livc-test/sw0058-2737-schu-minimal-20260921-054/unpacked/livc"
CANDIDATE_CFG = f"{CANDIDATE}/etc/uroad/config/common_param.yaml"
CANDIDATE_SOS = {
    "liburoad_m100.so": "170f881d7f686e3c2b2c2ba9cdb9639794ee44f922a8c00501054207145af484",
    "libxcamera.so": "8a4b2c3300bac029497a601f236c7290f024da041cdc6882f0f6e0d049a4e974",
    "libxlidar.so": "139089feac395540f62991effc36caa5d33c403e455b0d58017edcde01b102ad",
}
CONTROL_HASHES = {
    "bin/bench-replay-mode": "cb0d617cfcf0a48cab87e8ff042cf50d3a5e26fd8dc78b3b1f6144b761aafcac",
    "bin/formal-replay-wrapper.sh": "4d97a9f376a4a50b91c13c5098426976a94e5bc165059eb43a4662f76c4d49ed",
    "sdf/infer_expert_pcm_replay.sdf": "7c0eb2c9bc53a188336522d047ac7dfb4caa1364ffc182a0465242b12d7dc225",
}


def now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def bench(command: str, *, timeout: int = 600) -> str:
    env = {**os.environ, "SSH_ASKPASS": "/bin/true", "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": "bench-eval"}
    result = subprocess.run(
        ["ssh", "-F", str(SSH_CONFIG), "-o", "ConnectTimeout=8", "-o", "BatchMode=no",
         "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no",
         "-o", "KbdInteractiveAuthentication=yes", "-o", "PreferredAuthentications=keyboard-interactive",
         "-o", "NumberOfPasswordPrompts=1", BENCH, "--", f"sh -c {shlex.quote(command)}"],
        stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=timeout, env=env)
    if result.returncode:
        raise RuntimeError(f"bench failed ({result.returncode}): {command}\n{result.stderr[-3000:]}")
    return result.stdout


def scp_to(path: Path, destination: str) -> None:
    proxy = ["-F", str(SSH_CONFIG), "-o", "KbdInteractiveAuthentication=yes",
             "-o", "PreferredAuthentications=keyboard-interactive"]
    subprocess.run(["scp", *proxy, str(path), f"{BENCH}:{destination}"],
                   check=True, timeout=180)


def wait_tcp_open(port: int, seconds: int = 90) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check = subprocess.run(
            ["bash", "-c", f"timeout 3 bash -c 'echo > /dev/tcp/{BENCH_IP}/{port}' 2>/dev/null"],
            capture_output=True)
        if check.returncode == 0:
            return True
        time.sleep(2)
    return False


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(pending, path)


def prepare_probe_config(source: Path, destination: Path, job: dict) -> dict:
    """Patch reviewed bench-only switches and this run's output paths.

    The 048 evaluation library explicitly supports bench_mode as a fallback
    when ODO deskew samples do not cover a frame. Probe 003 proved that a
    transient ODO miss at sequence 92 otherwise drops an entire prediction.
    This setting is recorded in the config receipt and confined to the
    temporary candidate; the engineer's source config remains untouched.
    """
    import re as _re
    formal = job["formal_app"]
    replacements = {
        ("replay", "enable"): "ON",
        ("bench_mode", "enable"): "True",
        # Evaluated on the bench, the vehicle reports 0 kph, so every gate would
        # inhibit inference and stall the ordered camera-ack handshake (seen live:
        # skip_reason=speed_gate, 0 predictions). Force the four gates off while
        # keeping every other engineer-shipped key unchanged.
        ("speed_gate", "enable"): "false",
        ("perception_gate", "enable"): "false",
        ("apa_gate", "enable"): "false",
        ("gear_gate", "enable"): "false",
        ("eval_dump", "enable"): "ON",
        ("eval_dump", "output_dir"): str(formal["prediction_root"]),
        ("eval_dump", "scene_root"): str(formal["scene_root"]),
    }
    counts = {key: 0 for key in replacements}
    changes: list[dict[str, str]] = []
    current_section = ""
    output_lines: list[str] = []
    section_pattern = _re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(?:#.*)?$")
    value_pattern = _re.compile(r"^(  )([A-Za-z_][A-Za-z0-9_]*):(\s*)(\S+)(.*)$")
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        ending = line[len(bare):]
        section = section_pattern.match(bare)
        if section:
            current_section = section.group(1)
            output_lines.append(line)
            continue
        value = value_pattern.match(bare)
        if not value or not current_section:
            output_lines.append(line)
            continue
        indent, key, spacing, old_value, suffix = value.groups()
        replacement_key = (current_section, key)
        if replacement_key not in replacements:
            output_lines.append(line)
            continue
        counts[replacement_key] += 1
        new_value = replacements[replacement_key]
        line_out = f"{indent}{key}:{spacing}{new_value}{suffix}{ending}"
        # Do not duplicate a trailing comment when patching below an existing set value.
        output_lines.append(line_out)
        changes.append({"key": f"{current_section}.{key}", "from": old_value, "to": new_value})
    for (section, key), count in counts.items():
        if count != 1:
            raise ProbeError(f"expected exactly one occurrence of {section}.{key}, found {count}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ProbeError(f"configured YAML already exists: {destination}")
    destination.write_text("".join(output_lines), encoding="utf-8")
    receipt = {
        "schema_version": 1, "state": "CONFIG_CANDIDATE_NOT_ACTIVATED",
        "created_at": now(), "source": {"path": str(source), "sha256": sha256_local(source)},
        "candidate": {"path": str(destination), "sha256": sha256_local(destination)},
        "changes": changes,
    }
    return receipt


def sha256_local(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_file_strings(path: Path, hashes: dict[str, str]) -> None:
    path.write_text("".join(f"{digest}  {target}\n" for target, digest in hashes.items()), encoding="ascii")


def sha256_remote(paths: list[str]) -> dict[str, str]:
    out = bench("sha256sum " + " ".join(shlex.quote(p) for p in paths))
    result = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 2 and len(fields[0]) == 64:
            result[fields[1]] = fields[0]
    return result


class ProbeError(RuntimeError):
    pass


def require_bench_claim(run_id: str) -> None:
    """Refuse any bench mutation unless Runner assigned this run the lock."""
    status = subprocess.run(
        [str(RUN_ROOT / "bin" / "bench-eval"), "status", "--run-id", run_id],
        capture_output=True, text=True, timeout=30,
    )
    if status.returncode:
        raise ProbeError(f"Runner status failed for {run_id}: {status.stderr[-500:]}")
    status_document = json.loads(status.stdout)
    state = status_document.get("runner") or status_document
    if not (state.get("owns_bench") is True
            and (state.get("run") or {}).get("state") == "BENCH_CLAIMED"
            and (state.get("bench_lock") or {}).get("run_id") == run_id):
        raise ProbeError(f"run {run_id} does not own a BENCH_CLAIMED Runner lock")


SMCTL = "env LD_LIBRARY_PATH=/framework/lib:/usr/lib /framework/bin/liware.tool.smctl"
XRECORDER_PATTERN = "^\\./bin/x_recorder --enable_mosaic=false --use_generic_topic_sensor=true --use_lidar_pack_mode=true$"


def formal_status() -> dict[str, str | bool]:
    """Observe the official App without assuming it contains our replay wrapper."""
    return {
        "fusion": bench(f"{SMCTL} is-active infer_expert_fusion").strip(),
        "replay": bench(f"{SMCTL} is-active infer_expert_pcm_replay").strip(),
        "selinux": bench("getenforce").strip(),
        "xrecorder": bool(bench(f"pgrep -f {shlex.quote(XRECORDER_PATTERN)} || true").strip()),
    }


def restore_formal_app(initial: dict[str, str | bool], mounted: bool) -> None:
    """Restore the original official service, security mode and recorder."""
    bench(
        "set -e; "
        f"if test \"$({SMCTL} is-active infer_expert_pcm_replay)\" = active; "
        f"then {SMCTL} unloadsdf infer_expert_pcm_replay; fi; "
        f"if test \"$({SMCTL} is-active infer_expert_fusion)\" = active; "
        f"then {SMCTL} unloadsdf infer_expert_fusion; fi",
        timeout=300,
    )
    for _ in range(30):
        service = bench(f"{SMCTL} is-active infer_expert_pcm_replay").strip()
        app_procs = bench("pgrep -x infer_expert_pc || true; "
                          "pgrep -x infer_expert_fu || true; "
                          "pgrep -x infer_expert_liv || true").strip()
        if service == "inactive" and not app_procs:
            break
        time.sleep(2)
    else:
        raise ProbeError("candidate replay service or App process did not stop")
    if mounted:
        bench("set -e; umount /app/livc || umount -l /app/livc; "
              "if findmnt -M /app/livc -n >/dev/null 2>&1; then exit 43; fi",
              timeout=180)
    bench(f"{SMCTL} loadsdf /app/livc/sdf/infer_expert_fusion.sdf", timeout=180)
    if initial["xrecorder"]:
        # The candidate controller stops this exact, validated competing
        # recorder before opening the replay camera port.
        bench(
            f"if ! pgrep -f {shlex.quote(XRECORDER_PATTERN)} >/dev/null; then "
            "nohup sh -c 'cd /sota/lixplay_xrecorder && "
            "export LD_LIBRARY_PATH=/usr/lib/:/app/ad3/lib/:/framework/lib/:/sota/lixplay_xrecorder/lib/:$LD_LIBRARY_PATH && "
            "exec ./bin/x_recorder --enable_mosaic=false --use_generic_topic_sensor=true --use_lidar_pack_mode=true' "
            ">/tmp/x_recorder_probe_restore.log 2>&1 </dev/null & fi; true",
            timeout=60,
        )
    bench(f"/usr/sbin/setenforce {0 if initial['selinux'] == 'Permissive' else 1}", timeout=30)


def restore_candidate_config(backup: str, initial_selinux: str) -> None:
    """Copy the saved candidate config while its SELinux write gate is open."""
    original_mode = 0 if initial_selinux == "Permissive" else 1
    bench(
        "set -e; /usr/sbin/setenforce 0; "
        f"trap '/usr/sbin/setenforce {original_mode}' EXIT; "
        f"cp -p {shlex.quote(backup)} {shlex.quote(CANDIDATE_CFG)}",
        timeout=60,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--sweep", default=str(SWEEP),
                        help="controlled sweep script; stage a tested Skill copy for isolated validation")
    parser.add_argument("--adapter-dir", default=None,
                        help="isolated SHA-pinned controlled replay adapter directory")
    parser.add_argument("--max-attempts", type=int, default=None,
                        help="must be 1; retry with a new immutable run ID")
    parser.add_argument("--config-source", required=True, help="engineer-shipped common_param.yaml to base the probe config on")
    args = parser.parse_args()
    run_id = args.run_id
    run_dir = RUN_ROOT / "runs" / run_id
    evidence = Path(args.evidence).resolve()
    if any(evidence.iterdir()):
        raise ProbeError(f"evidence not empty: {evidence}")

    spec = importlib.util.spec_from_file_location("sw0058_base", str(BASE_SRC))
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)

    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    if job.get("run_id") != run_id:
        raise ProbeError(f"job run_id does not match requested run: {run_id}")
    require_bench_claim(run_id)
    max_attempts = (args.max_attempts if args.max_attempts is not None
                    else (job.get("execution") or {}).get("max_attempts", 1))
    if max_attempts != 1:
        raise ProbeError("only one attempt is allowed; retry requires a new immutable run ID")
    summary = {
        "schema_version": 1, "run_id": run_id, "started_at": now(),
        "requested_target": "workstation_predictions", "evaluation_requested": False,
        "activation_method": "temporary_bind_mount", "state": "RUNNING",
        "max_attempts": max_attempts,
    }
    write_json(run_dir / "operation-summary.json", summary)

    cfg_source = Path(args.config_source).resolve()
    gate_cfg = run_dir / "configured-inputs/common_param.probe.yaml"
    cfg_receipt = prepare_probe_config(cfg_source, gate_cfg, job)
    summary["configuration"] = cfg_receipt
    summary["config_source_sha256"] = sha256_local(cfg_source)

    mounted = False
    activation_attempted = False
    security_changed = False
    initial_service: dict[str, str | bool] | None = None
    primary_error: BaseException | None = None
    restore_error: BaseException | None = None
    released = False

    try:
        # The official package can lack bench-replay-mode entirely. Capture
        # its actual service state before any mutation, and restore that state.
        bench("if findmnt -M /app/livc -n >/dev/null 2>&1; then exit 42; fi")
        initial_service = formal_status()
        if initial_service["fusion"] != "active" or initial_service["replay"] != "inactive":
            raise ProbeError(f"unexpected official service state: {initial_service}")
        initial_hashes = sha256_remote([
            "/app/livc/lib/liburoad_m100.so", "/app/livc/lib/libxcamera.so",
            "/app/livc/lib/libxlidar.so", "/app/livc/etc/uroad/config/common_param.yaml"])
        if set(initial_hashes) != {"/app/livc/lib/liburoad_m100.so", "/app/livc/lib/libxcamera.so",
                                   "/app/livc/lib/libxlidar.so", "/app/livc/etc/uroad/config/common_param.yaml"}:
            raise ProbeError("could not capture all four formal app hashes")
        write_json(run_dir / "initial-bench-state.json", {"captured_at": now(), "services": initial_service, "hashes": initial_hashes})

        # The evaluated candidate owns the control scripts. The official App
        # was flashed from its vendor package and does not contain them.
        control_now = sha256_remote([f"{CANDIDATE}/{rel}" for rel in CONTROL_HASHES])
        for rel, expected in CONTROL_HASHES.items():
            if control_now.get(f"{CANDIDATE}/{rel}") != expected:
                raise ProbeError(f"control file hash mismatch: {rel}")

        # Verify the candidate's three SOs are the validated ones.
        cand_hashes = sha256_remote([f"{CANDIDATE}/lib/{name}" for name in CANDIDATE_SOS])
        for name, expected in CANDIDATE_SOS.items():
            if cand_hashes.get(f"{CANDIDATE}/lib/{name}") != expected:
                raise ProbeError(f"candidate {name} hash mismatch")

        # The candidate already contains the validated control files.
        # SELinux Enforcing denies root's rshell context from creating the
        # per-run directory under /app/livc-test. Keep the original mode for
        # restoration even if staging fails before the App is touched.
        security_changed = True
        bench("/usr/sbin/setenforce 0; test \"$(getenforce)\" = Permissive", timeout=30)
        config_dir = f"/app/livc-test/{run_id}/configs"
        config_dir_quoted = shlex.quote(config_dir)
        bench(
            "set -e; "
            f"test -d {shlex.quote(CANDIDATE)}; "
            f"install -d -m 0755 {config_dir_quoted}; "
            f"install -d -o infer_expert -g infer_expert {shlex.quote(str(job['formal_app']['prediction_root']))} "
            f"{shlex.quote(str(job['formal_app']['scene_root']))}")

        # Stage the generated gate config for later installation into the mounted /app/livc.
        scp_to(gate_cfg, config_dir)
        config_hash = base.sha256_file(gate_cfg)
        candidate_hashes = {
            f"/app/livc/lib/liburoad_m100.so": CANDIDATE_SOS["liburoad_m100.so"],
            f"/app/livc/lib/libxcamera.so": CANDIDATE_SOS["libxcamera.so"],
            f"/app/livc/lib/libxlidar.so": CANDIDATE_SOS["libxlidar.so"],
            f"/app/livc/etc/uroad/config/common_param.yaml": config_hash,
        }
        gate_hashes_file = run_dir / "candidate-gate-inputs.sha256"
        hash_file_strings(gate_hashes_file, candidate_hashes)
        scp_to(gate_hashes_file, config_dir)
        bench(
            "set -e; "
            + "; ".join(f"printf '%s  %s\\n' {digest} {shlex.quote(CANDIDATE + '/' + rel)} | sha256sum -c -"
                        for rel, digest in CONTROL_HASHES.items())
            + "; "
            + "; ".join(f"printf '%s  %s\\n' {digest} {shlex.quote(CANDIDATE + '/lib/' + name)} | sha256sum -c -"
                        for name, digest in CANDIDATE_SOS.items()))

        # Activate. Write the eval config into the candidate's REAL path BEFORE the
        # bind mount: overlayfs denies writes to sibling dirs while a subdir of the
        # overlay is bind-mounted (the validated recipe writes configs pre-mount).
        activation_attempted = True
        # Stop the official service through its manager. A blanket pkill could
        # race the manager's automatic restart while its SDF remains loaded.
        bench(f"{SMCTL} unloadsdf infer_expert_fusion", timeout=180)
        bench(
            "set -e; "
            f"cp -p {shlex.quote(CANDIDATE_CFG)} {shlex.quote(config_dir + '/common_param.factory.yaml')}; "
            f"cp {shlex.quote(config_dir + '/common_param.probe.yaml')} {shlex.quote(CANDIDATE_CFG)}; "
            f"chown root:root {shlex.quote(CANDIDATE_CFG)}; chmod 0644 {shlex.quote(CANDIDATE_CFG)}; "
            f"printf '%s  %s\\n' {config_hash} {shlex.quote(CANDIDATE_CFG)} | sha256sum -c -")
        bench(
            "set -e; if findmnt -M /app/livc -n >/dev/null 2>&1; then exit 42; fi; "
            f"mount --bind {shlex.quote(CANDIDATE)} /app/livc; findmnt -M /app/livc -n >/dev/null; "
            f"test \"$(stat -c %d:%i {shlex.quote(CANDIDATE)})\" = \"$(stat -c %d:%i /app/livc)\"")
        mounted = True
        # After the mount, /app/livc is the candidate; its config is already patched.
        bench(f"printf '%s  %s\\n' {config_hash} /app/livc/etc/uroad/config/common_param.yaml | sha256sum -c -")

        # Start the candidate app DETACHED: `bench-replay-mode start` keeps the ssh
        # channel open (graph/App inherits stdout). Run via nohup to a log file and
        # poll the real endpoints below instead of waiting on start returning.
        bench("nohup sh -c '/usr/sbin/setenforce 0; /app/livc/bin/bench-replay-mode start' "
              ">/tmp/brms_candidate_start.log 2>&1 </dev/null & echo detached_start_pid=$!",
              timeout=60)

        # Health: our controlled-replay driver needs camera 50053 + lidar 50054 (063-validated).
        cam_ok = wait_tcp_open(50053, 180)
        lidar_ok = wait_tcp_open(50054, 180)
        summary["endpoint_startup"] = {"camera_50053": cam_ok, "lidar_50054": lidar_ok}
        status_after = bench("/app/livc/bin/bench-replay-mode status", timeout=120)
        write_json(run_dir / "candidate-startup.json",
                   {"verified_at": now(), "camera_50053": cam_ok, "lidar_50054": lidar_ok, "status_text": status_after})
        if not (cam_ok and lidar_ok):
            raise ProbeError(f"candidate endpoints did not become ready: camera_50053={cam_ok} lidar_50054={lidar_ok}")

        # Run our controlled-replay PROBE (1 representative scene) via the skill driver.
        # Stream the sweep's output directly to probe.log (no stdout PIPE): a child
        # holding the pipe would otherwise keep the driver blocked after the sweep
        # exits. The result is read from evidence/batch-result.json below.
        probe_log_path = run_dir / "probe.log"
        probe_command = [
            sys.executable, str(Path(args.sweep).resolve()),
            "--from-job", str(Path(args.job).resolve()),
            "--evidence", str(evidence), "--probe",
            "--max-attempts", str(max_attempts),
        ]
        if args.adapter_dir:
            probe_command.extend(["--adapter-dir", str(Path(args.adapter_dir).resolve())])
        with probe_log_path.open("wb") as probe_stream:
            probe = subprocess.run(
                probe_command,
                stdout=probe_stream, stderr=subprocess.STDOUT, timeout=600)
        result_file = evidence / "batch-result.json"
        if probe.returncode != 0 or not result_file.is_file():
            raise ProbeError(f"controlled-sweep probe failed rc={probe.returncode}; see {probe_log_path}")
        batch = json.loads(result_file.read_text(encoding="utf-8"))
        completed = batch.get("completed") or []
        if len(completed) != 1:
            raise ProbeError(f"probe did not complete exactly one scene: {batch.get('expected')}")
        item = completed[0]
        counts = item.get("counts") or {}
        summary["probe"] = {
            "scene": item.get("scene"), "state": item.get("state"),
            "pred_traj": counts.get("pred_traj"), "traj_xy": counts.get("traj_xy"),
            "pred_img": counts.get("pred_img"),
        }
        summary["replay_completed"] = True
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except BaseException as exc:
        primary_error = exc
        summary.setdefault("replay_completed", False)
        summary["error"] = str(exc)
        print(f"PROBE_ERROR: {exc}", file=sys.stderr)
    finally:
        if activation_attempted:
            try:
                config_dir = f"/app/livc-test/{run_id}/configs"
                if initial_service is None:
                    raise ProbeError("initial official service state was not captured")
                restore_formal_app(initial_service, mounted)
                if mounted:
                    restore_candidate_config(
                        config_dir + "/common_param.factory.yaml",
                        str(initial_service["selinux"]),
                    )
                poll_deadline = time.monotonic() + 150
                status: dict[str, str | bool] = {}
                while time.monotonic() < poll_deadline:
                    try:
                        status = formal_status()
                        if (status == initial_service
                                and (not initial_service["xrecorder"] or wait_tcp_open(50051, 2))):
                            break
                    except RuntimeError:
                        pass
                    time.sleep(3)
                else:
                    raise ProbeError(f"restore start timed out: {status!r}")
                checks = "; ".join(
                    f"printf '%s  %s\\n' {digest} {shlex.quote(path)} | sha256sum -c -"
                    for path, digest in initial_hashes.items())
                bench("set -e; if findmnt -M /app/livc -n >/dev/null 2>&1; then exit 44; fi; "
                      f"{checks}", timeout=300)
                evidence_file = run_dir / "restore-verification.json"
                write_json(evidence_file, {"schema_version": 1, "run_id": run_id, "checked_at": now(),
                                           "formal_app_restored": True, "restore_ok": True,
                                           "initial_service": initial_service, "restored_service": status})
                release = subprocess.run(
                    [str(RUN_ROOT / "bin" / "bench-eval"), "release-bench", "--run-id", run_id,
                     "--reason", "RESTORE_VERIFIED", "--evidence", str(evidence_file)],
                    capture_output=True, text=True, timeout=120)
                if release.returncode == 0:
                    released = True
                summary["bench_lock_released"] = released
            except BaseException as exc:
                restore_error = exc
                summary["restore_error"] = str(exc)
        else:
            try:
                if security_changed:
                    if initial_service is None:
                        raise ProbeError("initial official service state was not captured")
                    bench(f"/usr/sbin/setenforce {0 if initial_service['selinux'] == 'Permissive' else 1}")
                    if formal_status() != initial_service:
                        raise ProbeError("official service state changed during pre-activation staging")
                release = subprocess.run(
                    [str(RUN_ROOT / "bin" / "bench-eval"), "release-bench", "--run-id", run_id,
                     "--reason", "NO_ACTIVATION"], capture_output=True, text=True, timeout=120)
                released = release.returncode == 0
                summary["bench_lock_released"] = released
            except BaseException as exc:
                restore_error = exc
                summary["restore_error"] = str(exc)

        summary["finished_at"] = now()
        summary["state"] = ("PROBE_COMPLETED_RESTORED"
                            if primary_error is None and restore_error is None else "FAILED")
        write_json(run_dir / "operation-summary.json", summary)

    if restore_error is not None:
        print(f"RESTORE_ERROR: {restore_error}", file=sys.stderr)
        return 3
    if primary_error is not None:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"GATE_PROBE_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(2)
