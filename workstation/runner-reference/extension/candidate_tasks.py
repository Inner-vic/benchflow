"""Freeze execution inputs and select a per-job profile for the existing Runner."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from environment_store import (EnvironmentError, copy_file, identity, inventory, locked,
                               read, relative, seal, sha, slot_path, verify, write)


def canonical(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def remap(value, mapping):
    if isinstance(value, str):
        # Longest first: an explicitly declared directory can contain several inputs.
        for old in sorted(mapping, key=len, reverse=True):
            if value == old or value.startswith(old + "/"):
                return mapping[old] + value[len(old):]
        return value
    if isinstance(value, dict): return {k: remap(v, mapping) for k, v in value.items()}
    if isinstance(value, list): return [remap(v, mapping) for v in value]
    return value


def runtime_identity(runtime):
    runtime = Path(runtime).resolve(strict=True)
    files = sorted(list(runtime.glob("*.py")) + list(runtime.glob("*.json")))
    return {"root": str(runtime), "files": {p.name: sha(p) for p in files}}


def verify_runtime(record):
    root = Path(record["root"])
    if runtime_identity(root) != record:
        raise EnvironmentError("pinned Runner runtime changed")
    return root


def bind(root, actor, slot, version, expected, job_path, profile_path, spec_path, runtime):
    """Spec declares reviewed adapter dependencies and deployment control files."""
    actor, slot, version = identity(actor), identity(slot), identity(version)
    job, profile, spec = read(job_path), read(profile_path), read(spec_path)
    run_id = identity(job["run_id"])
    if job.get("mode") != "target_workflow" or job.get("runner_protocol") != "1.1":
        raise EnvironmentError("only protocol 1.1 target-workflow is supported")
    if job.get('workflow', {}).get('targets') != ['workstation_predictions']:
        raise EnvironmentError('candidate validation collects workstation predictions only; publish separately as the owner')
    if "candidate_binding" in job:
        raise EnvironmentError("job already bound")
    key = job.get("execution", {}).get("command_set_id")
    source_set = profile["execution"]["command_sets"][key]
    if source_set["strategy"] != "isolated_candidate":
        raise EnvironmentError("reviewed isolated_candidate lifecycle is required")
    if spec.get("reviewed") is not True:
        raise EnvironmentError("adapter dependencies/deployment spec must be reviewed")
    with locked(root):
        env = slot_path(root, actor, slot) / "versions" / version
        env_meta = verify(env, expected)
        if (env_meta["actor"], env_meta["slot"], env_meta["version"]) != (actor, slot, version):
            raise EnvironmentError("environment ownership mismatch")
        path = Path(root) / "tasks" / run_id
        if path.exists(): raise EnvironmentError("immutable task already exists")
        temp = path.parent / (".partial-" + uuid.uuid4().hex)
        temp.mkdir(parents=True)
        deployment = spec["deployment"]
        if deployment.get('preserve_from_formal'):
            raise EnvironmentError('freeze required control files as overlays; do not inherit another formal environment')
        mapping = {str(Path(deployment["ssh_config"]).resolve()): str(path / "tools/ssh_config"),
                   "/home/lixiang/bench-eval/current/automation": str(Path(runtime).resolve()),
                   "/home/lixiang/bench-eval/current": str(Path(runtime).resolve().parent)}
        for old, target in spec.get("files", {}).items():
            old = str(Path(old).resolve(strict=True))
            target = "attachments/" + relative(target)
            source = Path(old)
            if source.is_dir():
                for file in source.rglob("*"):
                    if "__pycache__" in file.parts: continue
                    if file.is_symlink(): raise EnvironmentError("adapter dependency symlink")
                    if file.is_file(): copy_file(file, temp / target / file.relative_to(source))
            else: copy_file(source, temp / target)
            mapping[old] = str(path / target)
        # Relocate explicit absolute dependency references in reviewed text scripts.
        for file in (temp / "attachments").rglob("*") if (temp / "attachments").exists() else []:
            if file.is_file() and file.suffix in (".py", ".sh", ".json", ".yaml", ".yml"):
                text = file.read_text(encoding="utf-8")
                for old in sorted(mapping, key=len, reverse=True): text = text.replace(old, mapping[old])
                file.write_text(text, encoding="utf-8")
        source_profile = remap(profile, mapping)
        job = remap(job, mapping)
        evaluation_library = (job.get('artifacts') or {}).get('evaluation_library')
        if evaluation_library:
            library = env / 'libraries/liburoad_m100.so'
            if not library.is_file() or evaluation_library.get('sha256') != sha(library):
                raise EnvironmentError('job M100 identity differs from assembled environment')
            evaluation_library['path'] = str(library)
        for record, field in ((job.get("configuration_input") or {}, "path"),
                              (job.get("replay_input") or {}, "prepared_manifest")):
            if record.get(field) and not str(record[field]).startswith(str(path / "attachments") + "/"):
                raise EnvironmentError("configuration/prepared manifest must be in frozen files")
        # Require every explicitly invoked adapter script to be snapshotted.
        for command in source_profile["execution"]["command_sets"][key]["commands"].values():
            if command.get("transport") != "local":
                raise EnvironmentError("first version requires reviewed local adapters; they own bench SSH")
            for token in command["argv"]:
                if token.endswith((".py", ".sh")) and not token.startswith(str(path / "attachments") + "/"):
                    raise EnvironmentError("adapter script is not in declared frozen files: " + token)
        if deployment.get("bench_alias") != "bench-avm":
            raise EnvironmentError("only the reviewed bench-avm target is supported")
        copy_file(deployment["ssh_config"], temp / "tools/ssh_config")
        copy_file(deployment["installer"], temp / "tools/direct_package_install.py")
        copy_file(deployment["capacity_gate"], temp / "tools/capacity_gate.py")
        capacity = read(deployment["capacity_plan"])
        capacity["package"] = str(env / "candidate.app.tar.gz")
        capacity["bench"]["ssh_config"] = str(path / "tools/ssh_config")
        capacity["bench"]["ssh_alias"] = "bench-avm"
        write(temp / "tools/capacity-plan.json", capacity)
        for name in ("candidate_steps.py", "environment_store.py", "candidate_tasks.py"):
            copy_file(Path(__file__).parent / name, temp / "tools" / name)
        write(temp / "source-profile.json", source_profile)
        write(temp / "job.json", job)
        binding = {"actor": actor, "slot": slot, "version": version,
                   "environment_path": str(env), "environment_sha256": expected}
        wrapper = copy.deepcopy(source_profile)
        commands = {}
        for step in ("precheck", "capture_baseline", "stop_active", "start_candidate",
                     "health_candidate", "replay", "restore", "verify_restore"):
            commands[step] = {"transport": "local", "argv": ["python3", "-B",
                str(path / "tools/candidate_steps.py"), "--snapshot", str(path),
                "--run-dir", "{run_dir}", "--step", step],
                "timeout_seconds": 3600 if step in ("precheck", "stop_active", "replay") else 600}
        wrapper["execution"]["command_sets"] = {key: {"strategy": "isolated_candidate", "commands": commands}}
        write(temp / "profile.json", wrapper)
        fingerprint = seal(temp, {"schema_version": 1, "run_id": run_id, "binding": binding,
            "job_payload_sha256": canonical(job), "runtime": runtime_identity(runtime),
            "preserve_from_formal": [relative(p) for p in deployment.get("preserve_from_formal", [])]})
        os.replace(temp, path)
        bound = {**job, "candidate_binding": {"snapshot": str(path), "sha256": fingerprint}}
        # Reference exists before submission, protecting even waiting tasks from slot deletion.
        write(Path(root) / "references" / (run_id + ".json"), {**binding, "run_id": run_id,
              "task_sha256": fingerprint, "state": "BOUND"})
        write(Path(root) / "jobs" / (run_id + ".json"), bound)
    return {"state": "BOUND_UNVERIFIED", "run_id": run_id, "job": str(Path(root) / "jobs" / (run_id + ".json")),
            "snapshot": str(path), "sha256": fingerprint}


def resolve(job, store):
    binding = job["candidate_binding"]
    path = Path(binding["snapshot"])
    if path != Path(store).resolve() / "tasks" / identity(job["run_id"]):
        raise EnvironmentError("task snapshot is outside the configured registry")
    meta = verify(path, binding["sha256"])
    payload = {k: v for k, v in job.items() if k != "candidate_binding"}
    if meta["run_id"] != job["run_id"] or canonical(payload) != meta["job_payload_sha256"]:
        raise EnvironmentError("job differs from frozen snapshot")
    b = meta["binding"]
    env = verify(b["environment_path"], b["environment_sha256"])
    if any(env[k] != b[k] for k in ("actor", "slot", "version")):
        raise EnvironmentError("environment binding changed")
    verify_runtime(meta["runtime"])
    return path / "profile.json", meta


def submit(root, run_id, cli):
    path = Path(root) / "jobs" / (identity(run_id) + ".json")
    job = read(path)
    _, meta = resolve(job, root)
    result = subprocess.run([str(cli), "submit", "--job", str(path), "--submitted-by",
                            meta["binding"]["actor"]], text=True, capture_output=True,
                            env={**os.environ, 'BENCH_CANDIDATE_ROOT':str(Path(root).resolve())})
    if result.returncode: raise EnvironmentError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def promote(root, runs_dir, run_id):
    run_dir = Path(runs_dir) / identity(run_id)
    job = read(run_dir / "job.json")
    _, meta = resolve(job, root)
    state = read(run_dir / "state.json")
    result = read(run_dir / "candidate-execution-result.json")
    if (state.get("state") != "COMPLETED_TO_REQUESTED_GOAL"
            or result.get("status") != "PROCESSED"
            or result.get("state") != "COMPLETED_TO_REQUESTED_GOAL"
            or result.get("run_id") != run_id
            or (result.get("transaction") or {}).get("restore_verified") is not True):
        raise EnvironmentError("successful bench execution and restore receipts are required")
    restoration = read(run_dir / "restore-verification.json")
    if restoration.get("restore_ok") is not True or restoration.get("run_id") != run_id:
        raise EnvironmentError("Runner restore-verification receipt is required")
    # Independently validate product identity, inventory and every collected file.
    runtime = verify_runtime(meta["runtime"])
    sys.path.insert(0, str(runtime))
    from prediction_transfer import verify_workstation_product, local_inventory, resolve_collection
    product = result.get("workstation_predictions") or {}
    cache = product.get("workstation_product")
    if not cache: raise EnvironmentError("workstation product receipt is required")
    collection = resolve_collection(job, read(Path(job["candidate_binding"]["snapshot"]) / "profile.json"), run_dir)
    if Path(cache) != collection["cache"]:
        raise EnvironmentError("product path differs from frozen collection plan")
    receipt = verify_workstation_product(Path(cache), local_inventory(Path(cache)))
    if receipt["producer_run_id"] != run_id or not receipt["files"]:
        raise EnvironmentError("current run must produce a nonempty verified product")
    evidence = read(collection["evidence_dir"] / "workstation-predictions.json")
    deployed = read(run_dir / "candidate-deployment.json")
    if (deployed.get("state") != "FORMAL_ACTIVATED_VERIFIED"
            or deployed.get("package_sha256") != sha(Path(meta["binding"]["environment_path"]) / "candidate.app.tar.gz")
            or result.get("frozen_task_sha256") != job["candidate_binding"]["sha256"]):
        raise EnvironmentError("deployment/result is not bound to this frozen task")
    if (evidence.get("file_count") != len(receipt.get("files", []))
            or any(evidence.get(k) != receipt.get(k) for k in ("inventory_sha256", "product_identity"))):
        raise EnvironmentError("workstation product evidence differs")
    b = meta["binding"]
    with locked(root):
        path = slot_path(root, b["actor"], b["slot"]) / "slot.json"
        slot = read(path)
        slot["verified"] = {"version": b["version"], "environment_sha256": b["environment_sha256"],
                            "run_id": run_id, "task_sha256": job["candidate_binding"]["sha256"],
                            "requested_targets": result.get("requested_targets"),
                            "product_identity": receipt["product_identity"]}
        write(path, slot)
        reference = Path(root) / 'references' / (run_id + '.json')
        record = read(reference)
        write(reference, {**record, 'state':'VERIFIED', 'run_dir':str(run_dir)})
    return slot
