#!/usr/bin/env python3
"""Opt-in, plan-first arrival postprocessor for verified existing local products.

Separate from the LPAI trajectory/vision evaluation target. No SSH, replay,
upload, service operation, or original-plugin fallback is performed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from arrival_report_payload import prepare
from arrival_input_lineage import verify_upstream
from probe_arrival_inputs import read_timeline, latest_success_at_time, read_jerk


ROOT = Path(__file__).resolve().parent
ENGINE = "bench_csv_trial_v1"
CORE_SHA256 = "07e8ee9c2f907a63f357d70067065d11d098f493323928bdd8bb22ef1ca57d8f"
INPUT_NAMES = {"sus_preview_timeline.csv", "s_InDatCalcFAS_WhlJerkFL_mps3.json", "s_InDatCalcFAS_WhlJerkFR_mps3.json"}


class ArrivalExecutionError(RuntimeError):
    """Execution started and its independent attempt has failed."""


def computation_identity(core):
    return {"core": sha(core), **{name: sha(ROOT/name) for name in (
        "batch_arrival_accuracy.py", "trial_arrival_accuracy.py", "probe_arrival_inputs.py")}}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def plan(config_path: Path) -> dict:
    config_path = config_path.resolve()
    config = load(config_path)
    if set(config) - {"enabled", "engine", "source", "core", "output", "node", "artifact_runtime_root", "reuse_result"}:
        raise ValueError("unknown configuration keys; rule overrides are not supported")
    if type(config.get("enabled", False)) is not bool:
        raise ValueError("enabled must be a boolean")
    base = config_path.parent
    def path(value):
        p = Path(value)
        return (base / p).resolve() if not p.is_absolute() else p.resolve()
    result = {"schema_version": 1, "operation": "arrival_accuracy", "enabled": config.get("enabled", False),
              "state": "NOT_REQUESTED", "mutations_performed": False, "blockers": [],
              "engine": config.get("engine"), "required_formats": ["analysis_plan.json", "arrival-accuracy.xlsx"],
              "native_plugin_equivalence": False, "dit": "unavailable",
              "rules": {"version": ENGINE, "time": "payload_ctrl_ts_ms", "distance": "fl/fr_dist_out_mm_0",
                        "sample_policy": "latest successful whole message at control time",
                        "contact": "independent jerk + speed; no prediction-guided truth adjustment",
                        "prediction_assignment": "unchanged recovered core; all candidates before gate",
                        "gate": "FL StartDist[0] == FR StartDist[0] == 1 at contact, previous hold",
                        "accuracy": "gated finite error, abs(error) <= 500 mm",
                        "quantile": "pooled absolute error, linear/PERCENTILE.INC",
                        "statistics": "signed mean, sample SD and variance ddof=1",
                        "extra_compensation": False},
              "unavailable": ["native original/startdist pipelines", "MDF remark DIT label", "geometry width/height", "calibrated XCP clock offset", "post-compensation comparison"],
              "adaptations": ["bench CSV + decoded XCP JSON input mapping", "jerk-only ordered front-pair grouping; missing opposite wheel is not a prediction miss", "independent contact option and original core greedy assignment are explicitly pinned"]}
    if not result["enabled"]:
        return result
    if config.get("engine") != ENGINE:
        result["blockers"].append("Only the explicitly identified bench_csv_trial_v1 adapter is available; native plugin mode cannot run without its missing pipelines.")
    source = config["source"]
    if set(source) != {"input_root", "receipt", "receipt_base", "run_id", "product_identity", "truth_root", "upstream"}:
        raise ValueError("source must identify input files, truth_root and existing upstream delivery receipts")
    input_root, receipt_path, receipt_base = (path(source[k]) for k in ("input_root", "receipt", "receipt_base"))
    receipt = load(receipt_path)
    for k in ("run_id", "product_identity"):
        if not source[k] or receipt.get(k) != source[k]:
            raise ValueError(f"source receipt {k} mismatch")
    scenes = sorted(p for p in input_root.iterdir() if p.is_dir())
    if not scenes:
        raise ValueError("no existing prediction scenes")
    expected = {p.resolve() for s in scenes for p in (s/n for n in INPUT_NAMES)}
    actual_paths = list(input_root.rglob("*"))
    if any(p.is_symlink() for p in actual_paths) or {p.resolve() for p in actual_paths if p.is_file()} != expected:
        raise ValueError("unexpected files or links in arrival input scenes")
    supplied = {}
    for item in receipt["files"]:
        p = Path(item["local"])
        p = p.resolve() if p.is_absolute() else (receipt_base/p).resolve()
        if p in supplied:
            raise ValueError("duplicate source receipt file")
        supplied[p] = item
    if set(supplied) != expected:
        raise ValueError("input scene/file set differs from source receipt")
    files, signals = [], []
    for p, item in supplied.items():
        digest = sha(p)
        if digest != item["sha256"]:
            raise ValueError(f"source file hash mismatch: {p}")
        files.append({"local": str(p), "source": item.get("source"), "sha256": digest, "bytes": p.stat().st_size})
    upstream = verify_upstream(source, scenes, supplied, path, load, sha)
    for s in scenes:
        timeline, raw = read_timeline(s/"sus_preview_timeline.csv", 0)
        selected = latest_success_at_time(raw, 0)
        if len(selected) < 2:
            raise ValueError(f"fewer than two successful control times: {s.name}")
        jerk = []
        for wheel in ("FL", "FR"):
            name = f"s_InDatCalcFAS_WhlJerk{wheel}_mps3"
            info, samples = read_jerk(s/(name+".json"), wheel)
            if info["backwards_timestamp_steps"] or info["duplicate_timestamp_rows"]:
                raise ValueError(f"jerk times are not strictly increasing: {s.name} {wheel}")
            overlap = min(selected[-1]["timestamp_ns"], samples[-1]["timestamp_ns"])-max(selected[0]["timestamp_ns"], samples[0]["timestamp_ns"])
            if overlap <= 0:
                raise ValueError(f"jerk and control distance have no overlap: {s.name} {wheel}")
            jerk.append({"name": name, "samples": info["samples"], "unit": "m/s^3", "overlap_ns": overlap})
        signals.append({"scene": s.name, "timeline_columns": timeline["columns"], "successful_control_times": len(selected),
                        "invalid_rows": timeline["invalid_rows"], "jerk": jerk, "startdist": "shared dual-wheel gate enabled"})
    core = path(config["core"])
    if sha(core) != CORE_SHA256:
        raise ValueError("core differs from the audited version; do not silently change rules")
    implementation = computation_identity(core)
    output = path(config["output"])
    if output.exists():
        raise ValueError("output already exists; choose a new independent attempt directory")
    # Never let an output directory be an ancestor of, or inside, the raw inputs.
    if output == input_root or output in input_root.parents or input_root in output.parents:
        raise ValueError("output must be separate from source input directory")
    node, runtime = path(config["node"]), path(config["artifact_runtime_root"])
    if not node.is_file() or not (runtime/"node_modules/@oai/artifact-tool").exists():
        raise ValueError("XLSX dependency missing; HTML/CSV will not replace the required workbook")
    reuse_spec = config.get("reuse_result")
    reuse = path(reuse_spec["path"]) if reuse_spec else None
    if reuse:
        if sha(reuse) != reuse_spec["sha256"]:
            raise ValueError("cached result file hash mismatch")
        cached = load(reuse)
        if cached.get("state") != "CALCULATED" or cached.get("run_id") != source["run_id"] or cached.get("product_identity") != source["product_identity"]:
            raise ValueError("cached result identity/state mismatch")
        if cached.get("core", {}).get("sha256") != CORE_SHA256 or cached.get("adapter", {}).get("sha256") != sha(ROOT/"batch_arrival_accuracy.py"):
            raise ValueError("cached result code identity mismatch")
        if cached.get("source_receipt", {}).get("sha256") != sha(receipt_path):
            raise ValueError("cached result source receipt mismatch")
        context = cached.get("execution_context", {})
        if context.get("computation_identity") != implementation or context.get("upstream") != upstream:
            raise ValueError("cached result lacks matching upstream/helper-code provenance; recompute from the verified inputs")
        if sorted(s["scene"] for s in cached["scenes"]) != [s.name for s in scenes]:
            raise ValueError("cached result scene set mismatch")
        # Validate exact per-scene hashes as well as the receipt association.
        for s in cached["scenes"]:
            identities = [("sus_preview_timeline.csv", s["input"]["timeline"])] + [
                (f"s_InDatCalcFAS_WhlJerk{w.upper()}_mps3.json", item) for w,item in s["input"]["jerk"].items()]
            if any(item["sha256"] != supplied[(input_root/s["scene"]/name).resolve()]["sha256"] for name,item in identities):
                raise ValueError("cached result input hash mismatch")
    result.update(state="BLOCKED" if result["blockers"] else "PLAN_READY", run_id=source["run_id"],
                  product_identity=source["product_identity"], scene_count=len(scenes),
                  scenes=[s.name for s in scenes], files=files, signals=signals,
                  selected_modes=[ENGINE], native_modes=[], startdist="shared dual-wheel gate enabled",
                  source_receipt={"path": str(receipt_path), "sha256": sha(receipt_path)},
                  input_root=str(input_root), upstream=upstream, computation_identity=implementation,
                  core={"path": str(core), "sha256": CORE_SHA256}, output=str(output),
                  node=str(node), artifact_runtime_root=str(runtime), receipt_base=str(receipt_base),
                  reuse_result={"path": str(reuse), "sha256": sha(reuse)} if reuse else None,
                  config_sha256=sha(config_path))
    return result


def execute(p: dict) -> dict:
    if p["state"] != "PLAN_READY":
        return p
    references = p["files"] + [p["source_receipt"], p["core"]] + list(p["upstream"]["evidence"].values())
    if p["reuse_result"]:
        references.append(p["reuse_result"])
    for item in references:
        filename = item.get("local") or item.get("path")
        if sha(filename) != item["sha256"]:
            raise ValueError(f"input/evidence changed after planning: {filename}")
    if computation_identity(p["core"]["path"]) != p["computation_identity"]:
        raise ValueError("calculation implementation changed after planning")
    output = Path(p["output"])
    output.mkdir(parents=True, exist_ok=False)
    def write(name, value):
        (output/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    write("analysis_plan.json", p)
    attempt = {"state": "RUNNING", "run_id": p["run_id"], "engine": ENGINE, "analysis_plan_sha256": sha(output/"analysis_plan.json")}
    write("attempt.json", attempt)
    try:
        if p["reuse_result"]:
            result = load(p["reuse_result"]["path"])
            write("batch-result.json", result)
        else:
            subprocess.run([sys.executable, str(ROOT/"batch_arrival_accuracy.py"), "--input-root", p["input_root"],
                            "--source-receipt", p["source_receipt"]["path"], "--core", p["core"]["path"], "--output", str(output/"calculation")],
                           cwd=p["receipt_base"], check=True, capture_output=True, text=True, encoding="utf-8")
            result = load(output/"calculation/batch-result.json")
            result["execution_context"] = {"computation_identity": p["computation_identity"], "upstream": p["upstream"]}
            write("batch-result.json", result)
        write("report-payload.json", prepare(result))
        command = [p["node"], str(ROOT/"render_arrival_workbook.mjs"), "--payload", str(output/"report-payload.json"),
                   "--output", str(output/"arrival-accuracy.xlsx"), "--runtime-root", p["artifact_runtime_root"], "--evidence", str(output/"workbook-checks")]
        completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
        (output/"workbook.stdout.log").write_text(completed.stdout, encoding="utf-8")
        artifacts = {name: {"sha256": sha(output/name), "bytes": (output/name).stat().st_size} for name in ("analysis_plan.json", "arrival-accuracy.xlsx", "batch-result.json")}
        attempt.update(state="SUCCEEDED", artifacts=artifacts, native_plugin_equivalence=False)
        write("attempt.json", attempt)
        return {"state": "SUCCEEDED", "output": str(output), "artifacts": artifacts, "native_plugin_equivalence": False}
    except Exception as exc:
        attempt.update(state="FAILED", error=str(exc))
        if isinstance(exc, subprocess.CalledProcessError):
            attempt["stderr"] = (exc.stderr or "")[-12000:]
        write("attempt.json", attempt)
        raise ArrivalExecutionError(str(exc)) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="Without this flag only read-only planning is performed")
    args = parser.parse_args(argv)
    try:
        p = plan(args.config)
        response = execute(p) if args.execute else p
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 2 if response["state"] == "BLOCKED" else 0
    except ArrivalExecutionError as exc:
        print(json.dumps({"state": "FAILED", "error": str(exc)}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
