#!/usr/bin/env python3
"""Compound deploy/replay lifecycle, called only under the Runner bench lock."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from environment_store import EnvironmentError, identity, read, sha, write
from candidate_tasks import resolve


def command(argv, timeout=3600):
    p = subprocess.run([str(x) for x in argv], text=True, capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=timeout,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if p.returncode:
        raise EnvironmentError("command failed: " + str(argv[0]) + " " + (p.stderr or p.stdout)[-3000:])
    return p.stdout.strip()


def remote_service(installer, action):
    if action == "cleanup-stage":
        path = Path(installer)
        stage = path.parent.parent
        if (stage.parent != Path('/map/bench-eval/install-stage') or stage.is_symlink()
                or path.parent.is_symlink() or identity(stage.name) != read(stage/'incoming/input.json')['run_id']):
            raise EnvironmentError("unsafe per-run staging cleanup path")
        shutil.rmtree(stage)
        return {"state": "STAGE_REMOVED", "run_id": stage.name}
    spec = importlib.util.spec_from_file_location("frozen_installer", installer)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    service = mod.BenchService()
    if action == "restore-service":
        service.enforcing()
        if service.status()["fusion"] != "active": service.start()
    status = service.status()
    if action != "status" and status != mod.EXPECTED_STATUS:
        raise EnvironmentError("formal service restoration differs")
    return status


def run_step(snapshot, run_dir, step):
    snapshot, run_dir = Path(snapshot).resolve(), Path(run_dir).resolve()
    job = read(run_dir / "job.json")
    store = snapshot.parent.parent
    frozen_profile, meta = resolve(job, store)
    run_id = identity(job["run_id"])
    if run_dir.name != run_id or snapshot != frozen_profile.parent:
        raise EnvironmentError("run/snapshot identity differs")
    runtime = Path(meta["runtime"]["root"])
    sys.path.insert(0, str(runtime))
    from runner_state import read_json
    # Resolve the actual Runner project's locks directory, not a caller-provided arbitrary lock.
    lock = read_json(run_dir.parent.parent / "locks/bench.lock")
    if lock.get("run_id") != run_id:
        raise EnvironmentError("Runner bench lock is not owned by this run")
    config = snapshot / "tools/ssh_config"
    ssh = ["ssh", "-F", str(config), "-o", "BatchMode=yes", "bench-avm"]
    stage = "/map/bench-eval/install-stage/" + run_id
    remote_installer = stage + "/incoming/direct_package_install.py"
    remote_helper = stage + "/incoming/candidate_steps.py"
    package = Path(meta["binding"]["environment_path"]) / "candidate.app.tar.gz"
    deployment_path = run_dir / "candidate-deployment.json"
    lifecycle_path = run_dir / "candidate-lifecycle.json"
    lifecycle = read(lifecycle_path) if lifecycle_path.exists() else {}

    def bench(*argv): return command(ssh + [shlex.join([str(x) for x in argv])])

    def status(action="status"):
        return json.loads(bench("python3", "-B", remote_helper, "--remote-installer",
                                remote_installer, "--remote-action", action))

    def delegate(name):
        from bench_transaction import CommandRunner
        source = read(snapshot / "source-profile.json")
        selected = source["execution"]["command_sets"][job["execution"]["command_set_id"]]
        result = CommandRunner().run(name, selected["commands"][name], source["execution"],
            {"run_id": run_id, "run_dir": str(run_dir),
             "scene": str((job.get("replay_input") or {}).get("scene", ""))})
        write(run_dir / "candidate-delegate" / (name + ".json"), result)
        if not result.get("ok"): raise EnvironmentError("reviewed adapter failed at " + name)
        return result

    if step == "precheck":
        # Capacity gate is read-only; no bench staging or activation before capture_baseline.
        result = json.loads(command(["python3", "-B", snapshot / "tools/capacity_gate.py",
                                     "--plan", snapshot / "tools/capacity-plan.json"]))
        if result.get("state") != "READY": raise EnvironmentError("capacity gate is not READY")
        write(run_dir / "candidate-capacity.json", result)
    elif step == "capture_baseline":
        # The package and control files are transferred once under this same lock.
        bench("test", "!", "-e", stage)
        bench("install", "-d", "-m", "0700", stage + "/incoming")
        spec = {"run_id": run_id, "package_sha256": sha(package), "replacements": [],
                "clean_formal": True, "force_overwrite": False,
                "preserve_from_formal": meta["preserve_from_formal"]}
        write(run_dir / "candidate-install-spec.json", spec)
        files = [(package, "package.app.tar.gz"),
                 (snapshot / "tools/direct_package_install.py", "direct_package_install.py"),
                 (run_dir / "candidate-install-spec.json", "input.json")]
        # The remote helper needs its sibling imports, but never receives credentials.
        for name in ("candidate_steps.py", "candidate_tasks.py", "environment_store.py"):
            files.append((snapshot / "tools" / name, name))
        for source, name in files:
            command(["rsync", "--archive", "-e", shlex.join(ssh[:-1]), str(source),
                     "bench-avm:" + stage + "/incoming/" + name])
            actual = bench("sha256sum", stage + "/incoming/" + name).split()[0]
            if actual != sha(source): raise EnvironmentError("transferred input hash differs")
        write(run_dir / "candidate-original-service.json", status())
        lifecycle["staged"] = True
        write(lifecycle_path, lifecycle)
    elif step == "stop_active":
        prepared = json.loads(bench("python3", remote_installer, "--worker-prepare", stage,
                                    stage + "/incoming/input.json"))
        write(run_dir / "candidate-prepare.json", prepared)
        if prepared.get("state") != "READY_TO_INSTALL": raise EnvironmentError("candidate is not prepared")
        lifecycle["activation_attempted"] = True
        write(lifecycle_path, lifecycle)
        receipt = json.loads(bench("python3", remote_installer, "--worker-activate", stage,
                                   stage + "/incoming/input.json"))
        write(deployment_path, {**receipt, "package_sha256": sha(package)})
        if receipt.get("state") != "FORMAL_ACTIVATED_VERIFIED":
            raise EnvironmentError("candidate installation failed; restore receipt retained")
        # Existing adapters expect the newly installed formal APP as their baseline.
        delegate("precheck")
        delegate("capture_baseline")
        lifecycle["delegate_started"] = True
        write(lifecycle_path, lifecycle)  # Before a potentially partial stop.
        delegate("stop_active")
    elif step in ("start_candidate", "health_candidate", "replay"):
        delegate(step)
    elif step in ("restore", "verify_restore"):
        deployed = read(deployment_path) if deployment_path.exists() else {}
        if lifecycle.get("delegate_started"):
            delegate(step)
        else:
            # A failure before original adapter stop still requires verified restoration.
            if not lifecycle.get("staged"):
                raise EnvironmentError("no staged recovery controller; retain bench lock")
            if lifecycle.get("activation_attempted") and not deployed:
                raise EnvironmentError("activation outcome missing; retain bench lock")
            if deployed and deployed.get("safe_to_release") is not True:
                raise EnvironmentError("installation recovery is unverified; retain bench lock")
            status("restore-service" if step == "restore" else "verify-service")
        if step == "verify_restore":
            status("verify-service")
            # Retain stage/rollback material for explicit receipt-based cleanup after delivery.
            write(run_dir / "candidate-stage-retention.json", {"stage": stage,
                  "reason": "retained for receipt-based cleanup after product delivery"})
    else: raise EnvironmentError("unsupported lifecycle step")
    return {"step": step, "state": "SUCCEEDED", "run_id": run_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot")
    parser.add_argument("--run-dir")
    parser.add_argument("--step")
    parser.add_argument("--remote-installer")
    parser.add_argument("--remote-action", choices=["status", "restore-service", "verify-service", "cleanup-stage"])
    args = parser.parse_args()
    try:
        result = remote_service(args.remote_installer, args.remote_action) if args.remote_action else \
                 run_step(args.snapshot, args.run_dir, args.step)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__": raise SystemExit(main())


def cleanup_after_delivery(snapshot, run_dir):
    """Only the worker calls this after successful promotion/product verification."""
    snapshot, run_dir = Path(snapshot), Path(run_dir)
    job = read(run_dir/'job.json')
    resolve(job, snapshot.parent.parent)
    result = read(run_dir/'candidate-execution-result.json')
    if result.get('candidate_promotion') != 'VERIFIED':
        raise EnvironmentError('cleanup requires verified delivery')
    run_id = identity(job['run_id'])
    stage = '/map/bench-eval/install-stage/'+run_id
    output = command(['ssh','-F',str(snapshot/'tools/ssh_config'),'-o','BatchMode=yes','bench-avm',
        shlex.join(['python3','-B',stage+'/incoming/candidate_steps.py','--remote-installer',
                    stage+'/incoming/direct_package_install.py','--remote-action','cleanup-stage'])])
    return json.loads(output)
