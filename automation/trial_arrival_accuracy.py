#!/usr/bin/env python3
"""Trial one front-axle bump using an explicitly supplied speedbump core.

This adapter preserves source recordings and keeps jerk contact detection
independent of the prediction under test. It is not the missing ZIP pipeline.
"""

from __future__ import annotations

import argparse
import ast
import __future__
import csv
import json
import math
import types
from pathlib import Path

import numpy as np

from probe_arrival_inputs import identity, latest_success_at_time, read_jerk, read_timeline


def load_core(path: Path, arrays_only: bool = False):
    # Compile source directly: do not write __pycache__ in another project.
    module = types.ModuleType("bench_arrival_trial_core")
    module.__file__ = str(path.resolve())
    source = path.read_text(encoding="utf-8")
    if arrays_only:
        # The verified array functions do not read MDF. Keep their bodies and
        # constants unchanged; omit only the unused MDF dependency and main.
        tree = ast.parse(source, filename=str(path))
        tree.body = [n for n in tree.body if not (
            isinstance(n, ast.Import) and any(a.name == "asammdf" for a in n.names)
            or isinstance(n, ast.If) and "__name__" in ast.dump(n.test))]
        exec(compile(tree, str(path), "exec", flags=__future__.annotations.compiler_flag), module.__dict__)
        if not hasattr(np, "trapezoid"):
            class NumpyCompatibility:
                def __getattr__(self, name):
                    return np.trapz if name == "trapezoid" else getattr(np, name)
            module.np = NumpyCompatibility()
    else:
        exec(compile(source, str(path), "exec"), module.__dict__)
    for name in ("find_jerk_impacts", "find_model_detections", "assign_detections", "calc_error"):
        if not callable(getattr(module, name, None)):
            raise ValueError(f"core is missing {name}")
    return module


def summarize(rows: list[dict]) -> dict:
    gated = [r for r in rows if r["gate_eligible"]]
    detected = [r for r in gated if r["error_mm"] is not None and math.isfinite(r["error_mm"])]
    used = [r for r in detected if abs(r["error_mm"]) <= 500]
    values = np.array([r["error_mm"] for r in used], dtype=float)
    return {
        "expected": len(rows), "gate_eligible": len(gated), "detected": len(detected),
        "out_of_range": len(detected) - len(used), "accuracy_samples": len(used),
        "detection_rate": len(detected) / len(gated) if gated else None,
        "p90_abs_mm": float(np.percentile(np.abs(values), 90, method="linear")) if len(values) else None,
        "p95_abs_mm": float(np.percentile(np.abs(values), 95, method="linear")) if len(values) else None,
        "signed_mean_mm": float(values.mean()) if len(values) else None,
        "sample_std_mm": float(values.std(ddof=1)) if len(values) > 1 else None,
        "sample_variance_mm2": float(values.var(ddof=1)) if len(values) > 1 else None,
        "early": sum(r["status"] == "early" for r in used),
        "late": sum(r["status"] == "late" for r in used),
    }


def trial(timeline: Path, jerk_paths: dict[str, Path], core_path: Path) -> tuple[dict, dict]:
    core = load_core(core_path)
    timeline_info, raw = read_timeline(timeline, 0)
    samples = latest_success_at_time(raw, 0)
    if len(samples) < 2:
        raise ValueError("need at least two successful control timestamps")
    truth, truth_info = {}, {}
    for wheel in ("fl", "fr"):
        truth_info[wheel], truth[wheel] = read_jerk(jerk_paths[wheel], wheel)
        if truth_info[wheel]["backwards_timestamp_steps"] or truth_info[wheel]["duplicate_timestamp_rows"]:
            raise ValueError("jerk times must be strictly increasing")
    origin = min(samples[0]["timestamp_ns"], *(truth[w][0]["timestamp_ns"] for w in truth))
    times = np.array([(s["timestamp_ns"] - origin) / 1e9 for s in samples])
    speed = np.array([s["speed_kph"] for s in samples])
    contacts, predictions, waveforms = {}, {}, {}
    for wheel in truth:
        tj = np.array([(s["timestamp_ns"] - origin) / 1e9 for s in truth[wheel]])
        jerk = np.array([s["jerk_mps3"] for s in truth[wheel]])
        distance = np.array([s[f"{wheel}_dist_out_mm_0"] for s in samples])
        feature = np.array([s[f"{wheel}_feat_0"] for s in samples])
        # Deliberately omit t_feat/feat and t_d/dist: the tested prediction must
        # not shift the jerk truth or remove inconvenient jerk events.
        contacts[wheel] = core.find_jerk_impacts(tj, jerk, times, speed)
        predictions[wheel] = core.find_model_detections(
            times, distance, times, feature, None, None, None, None, times, speed,
        )
        waveforms[wheel] = {"jerk_time_s": tj.tolist(), "jerk_mps3": jerk.tolist(),
                            "jerk_threshold_mps3": core.jerk_threshold_arr(tj, times, speed).tolist(),
                            "distance_mm": distance.tolist(), "feature": feature.tolist()}
    if any(len(contacts[w]) != 1 for w in contacts):
        raise ValueError(f"this bounded trial requires one independently detected contact per front wheel; found { {w: len(v) for w,v in contacts.items()} }")
    if abs(contacts["fl"][0]["peak_time"] - contacts["fr"][0]["peak_time"]) > core.FRONT_FRONT_PAIR_WIN:
        raise ValueError("front contacts are outside the core front-axle pairing window")
    rows, independent_checks = [], []
    for wheel in contacts:
        contact = contacts[wheel][0]
        at = contact["onset_time"]
        if not times[0] <= at <= times[-1]:
            raise ValueError("jerk contact is outside prediction/speed coverage")
        matched = core.assign_detections(contacts[wheel], predictions[wheel])[0]
        distance = np.array(waveforms[wheel]["distance_mm"])
        i = int(np.searchsorted(times, at, side="right")) - 1
        j = min(i + 1, len(times) - 1)
        row = {"event": 1, "wheel": wheel.upper(), "contact_time_s": at,
               "contact_timestamp_ns": origin + int(round(at * 1e9)),
               "speed_kph": contact["speed"], "jerk_peak_mps3": contact["peak_mag"],
               "jerk_oscillation_peaks": contact["n_peaks"],
               "startdist_fl": samples[i]["fl_start_dist_0"], "startdist_fr": samples[i]["fr_start_dist_0"],
               "gate_eligible": samples[i]["fl_start_dist_0"] == samples[i]["fr_start_dist_0"] == 1,
               "feature_at_contact": samples[i][f"{wheel}_feat_0"],
               "control_bracket_ms": (times[j] - times[i]) * 1000,
               "previous_hold_distance_mm": float(distance[i]),
               "left_timestamp_ns": samples[i]["timestamp_ns"], "right_timestamp_ns": samples[j]["timestamp_ns"],
               "left_distance_mm": float(distance[i]), "right_distance_mm": float(distance[j])}
        if matched is None:
            row.update(status="missed", error_mm=None, zero_time_s=None, time_error_ms=None,
                       predicted_zero_timestamp_ns=None, detection_time_s=None)
        else:
            error, status, zero, time_error = core.calc_error(matched, contact, times, distance, times, speed)
            row.update(status=status, error_mm=float(error), zero_time_s=zero,
                       time_error_ms=time_error * 1000 if time_error is not None else None,
                       predicted_zero_timestamp_ns=origin + int(round(zero * 1e9)) if zero is not None else None,
                       detection_time_s=matched["detect_time"])
            # Independent arithmetic check, without invoking the core helpers.
            if status == "late":
                fraction = (at - times[i]) / (times[j] - times[i]) if j != i else 0
                independently_computed = float(distance[i] + fraction * (distance[j] - distance[i]))
            elif zero is not None and zero <= at:
                nodes = [zero] + [float(t) for t in times if zero < t < at] + [at]
                velocities = np.interp(nodes, times, speed) / 3.6
                independently_computed = -sum((b-a) * (va+vb) / 2 for a,b,va,vb in zip(nodes,nodes[1:],velocities,velocities[1:])) * 1000
            else:
                raise ValueError("core returned an early result without an independently checkable zero")
            if not math.isclose(error, independently_computed, abs_tol=1e-7):
                raise ValueError("independent distance-error reconciliation failed")
            independent_checks.append({"wheel": wheel.upper(), "core_error_mm": float(error),
                                       "independent_error_mm": independently_computed})
        row["used_for_accuracy"] = row["gate_eligible"] and row["error_mm"] is not None and abs(row["error_mm"]) <= 500
        row["exclusion_reason"] = ("dual StartDist failed" if not row["gate_eligible"] else
                                   "no matched prediction" if row["error_mm"] is None else
                                   "absolute error exceeds 500 mm" if abs(row["error_mm"]) > 500 else "")
        rows.append(row)
    result = {"schema_version": 1, "state": "EXPERIMENTAL_SINGLE_SCENE_RESULT", "scene": timeline.parent.name,
              "origin_timestamp_ns": origin, "input": {"timeline": timeline_info, "jerk": truth_info},
              "core": identity(core_path), "adapter": identity(Path(__file__)),
              "rules": {"timestamp": "payload_ctrl_ts_ms", "distance": "fl/fr_dist_out_mm_0",
                        "duplicate_policy": "last successful complete message by publish_seq at each control time",
                        "contact": "core.find_jerk_impacts(jerk, speed); prediction-guided onset and feature gates disabled",
                        "distance_at_contact": "linear interpolation, as implemented by recovered core",
                        "startdist_at_contact": "previous hold; both FL[0] and FR[0] must equal 1",
                        "compensation": "no additional compensation", "statistics_limit_mm": 500,
                        "quantile": "linear interpolation of absolute errors", "std_variance_ddof": 1,
                        "true_zero_threshold_mm": core._DIST_TRULY_ZERO_MM,
                        "cycle_completion_threshold_mm": core.DIST_ZERO_THRESHOLD,
                        "jerk_threshold_speed_kph": core._THRESH_SPD_KPH.tolist(),
                        "jerk_threshold_g_per_s": core._THRESH_GS.tolist(),
                        "minimum_oscillation_peaks": core.MIN_JERK_PEAKS,
                        "merge_gap_s": core.EVENT_MERGE_GAP_S,
                        "wide_gap_travel_mm": core.MERGE_GAP_DIST_MM,
                        "onset_minimum_threshold_ratio": core.ONSET_MIN_RATIO,
                        "onset_minimum_span_mm": core.ONSET_MIN_SPAN_MM,
                        "onset_gap_after_mm": core.ONSET_GAP_AFTER_MM,
                        "startup_guard_s": core.STARTUP_GUARD_S,
                        "minimum_event_peak_ratio": 1.5},
              "jerk_contacts": contacts,
              "prediction_cycles": {w: [{k:v for k,v in d.items() if k not in ("obj_length", "obj_height")} for d in ds] for w,ds in predictions.items()},
              "detail": rows, "metrics": {"combined": summarize(rows), **{w: summarize([r for r in rows if r["wheel"] == w]) for w in ("FL", "FR")}},
              "speed_bins": {f"{lo}-{hi}_kph": summarize([r for r in rows if lo <= r["speed_kph"] < hi]) for lo,hi in ((0,10),(10,20),(20,30),(30,40))},
              "independent_checks": independent_checks,
              "limitations": ["Recovered core is not either missing per-signal ZIP pipeline; equivalence is unverified.",
                              "One automatically identified bump / two front-wheel observations; quantiles are arithmetic trial results, not dataset performance.",
                              "No independent manually labeled contact list or calibrated XCP physical time offset was supplied.",
                              "PCM output is not FAS FinalSusObjDist; no claim about downstream ECU compensation."]}
    plot = {"origin_timestamp_ns": origin, "control_time_s": times.tolist(), "speed_kph": speed.tolist(),
            "startdist_fl": [s["fl_start_dist_0"] for s in samples], "startdist_fr": [s["fr_start_dist_0"] for s in samples],
            "wheels": waveforms, "detail": rows}
    return result, plot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("timeline", "jerk-fl", "jerk-fr", "core", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new output directory")
    result, plot = trial(args.timeline, {"fl": args.jerk_fl, "fr": args.jerk_fr}, args.core)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, payload in (("trial-result.json", result), ("plot-data.json", plot)):
        (args.output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    with (args.output / "arrival-detail.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(result["detail"][0]))
        writer.writeheader()
        writer.writerows(result["detail"])
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
