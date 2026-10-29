#!/usr/bin/env python3
"""Auditable batch arrival calculation on independent jerk candidates.

Keeps the recovered core's detection/assignment/error rules. Additional quality
flags expose uncertain front-axle pairing and prediction-cycle consumption;
they do not silently change the primary statistical population.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from functools import lru_cache
from pathlib import Path

import numpy as np

from probe_arrival_inputs import identity, latest_success_at_time, read_jerk, read_timeline
from trial_arrival_accuracy import load_core, summarize


def pair_contacts(left: list[dict], right: list[dict], window: float) -> list[dict]:
    """Order-preserving maximum-cardinality/minimum-time-gap pairing.

    Uses only independently detected jerk peaks; unmatched candidates retain a
    missing-contact row for the opposite wheel. Does not synthesize onset times.
    """
    li = sorted(range(len(left)), key=lambda i: left[i]["peak_time"])
    ri = sorted(range(len(right)), key=lambda i: right[i]["peak_time"])

    @lru_cache(None)
    def solve(i, j):
        if i == len(li) or j == len(ri):
            return 0, 0., ()
        choices = [solve(i + 1, j), solve(i, j + 1)]
        gap = abs(left[li[i]]["peak_time"] - right[ri[j]]["peak_time"])
        if gap <= window:
            n, cost, pairs = solve(i + 1, j + 1)
            choices.insert(0, (n + 1, cost + gap, ((li[i], ri[j]),) + pairs))
        return min(choices, key=lambda v: (-v[0], v[1]))

    pairs = solve(0, 0)[2]
    used_left = {a for a, _ in pairs}
    used_right = {b for _, b in pairs}
    groups = [{"fl": a, "fr": b} for a, b in pairs]
    groups += [{"fl": a, "fr": None} for a in li if a not in used_left]
    groups += [{"fl": None, "fr": b} for b in ri if b not in used_right]
    groups.sort(key=lambda g: min((left if w == "fl" else right)[g[w]]["peak_time"] for w in ("fl", "fr") if g[w] is not None))
    for number, group in enumerate(groups, 1):
        group["event"] = number
        group["paired"] = group["fl"] is not None and group["fr"] is not None
        group["pair_gap_ms"] = abs(left[group["fl"]]["peak_time"] - right[group["fr"]]["peak_time"]) * 1000 if group["paired"] else None
        group["ambiguous"] = any(
            sum(abs((left if w == "fl" else right)[group[w]]["peak_time"] - other["peak_time"]) <= window
                for other in (right if w == "fl" else left)) > 1
            for w in ("fl", "fr") if group[w] is not None
        )
    return groups


def metrics(rows: list[dict]) -> dict:
    result = summarize(rows)
    result.update(
        contact_available=sum(r["contact_available"] for r in rows),
        contact_missing=sum(not r["contact_available"] for r in rows),
        gate_failed=sum(r["gate_eligible"] is False for r in rows),
        gate_unknown=sum(r["gate_eligible"] is None for r in rows),
        unmatched_predictions_all=sum(r["status"] == "missed" for r in rows),
        unmatched_predictions_gated=sum(r["status"] == "missed" and r["gate_eligible"] is True for r in rows),
        raw_detected_all=sum(r["error_mm"] is not None for r in rows),
        raw_out_of_range_all=sum(r["error_mm"] is not None and abs(r["error_mm"]) > 500 for r in rows),
        flagged_rows=sum(bool(r["flags"]) for r in rows),
    )
    return result


def independent_error(error, status, zero, at, times, distance, speed):
    if status == "late":
        i = max(0, int(np.searchsorted(times, at, side="right")) - 1)
        j = min(i + 1, len(times) - 1)
        fraction = (at-times[i])/(times[j]-times[i]) if j != i else 0
        expected = float(distance[i] + fraction*(distance[j]-distance[i]))
    elif zero is not None and zero <= at:
        nodes = [zero] + [float(t) for t in times if zero < t < at] + [at]
        velocity = np.interp(nodes, times, speed)/3.6
        expected = -sum((b-a)*(va+vb)/2 for a,b,va,vb in zip(nodes,nodes[1:],velocity,velocity[1:]))*1000
    else:
        raise ValueError("early result has no resolvable true zero")
    if not math.isfinite(error) or not math.isclose(error, expected, abs_tol=1e-7):
        raise ValueError("independent arithmetic reconciliation failed")
    return expected


def analyse_scene(scene_path: Path, scene_id: str, core, *, timeline_path=None, jerk_paths=None) -> tuple[dict, dict]:
    info, raw = read_timeline(timeline_path or scene_path/"sus_preview_timeline.csv", 0)
    samples = latest_success_at_time(raw, 0)
    if len(samples) < 2:
        raise ValueError("fewer than two successful prediction times")
    truth_info, truth = {}, {}
    for w in ("fl", "fr"):
        truth_info[w], truth[w] = read_jerk(jerk_paths[w] if jerk_paths else scene_path/f"s_InDatCalcFAS_WhlJerk{w.upper()}_mps3.json", w)
        if truth_info[w]["backwards_timestamp_steps"] or truth_info[w]["duplicate_timestamp_rows"]:
            raise ValueError("jerk timebase is not strictly increasing")
    origin = min(samples[0]["timestamp_ns"], *(v[0]["timestamp_ns"] for v in truth.values()))
    times = np.array([(s["timestamp_ns"]-origin)/1e9 for s in samples])
    speed = np.array([s["speed_kph"] for s in samples])
    contacts, detections, assignments, waveforms = {}, {}, {}, {}
    data_quality = [{"scene_id": scene_id, "scene": scene_path.name, "kind": "malformed_csv_row",
                     "wheel": "", "timestamp_ns": None, "source_line": r["line"], "reason": r["reason"]} for r in info["invalid_rows"]]
    for w in truth:
        tj = np.array([(s["timestamp_ns"]-origin)/1e9 for s in truth[w]])
        jerk = np.array([s["jerk_mps3"] for s in truth[w]])
        distance = np.array([s[f"{w}_dist_out_mm_0"] for s in samples])
        feature = np.array([s[f"{w}_feat_0"] for s in samples])
        contacts[w] = core.find_jerk_impacts(tj, jerk, times, speed)
        detections[w] = core.find_model_detections(times, distance, times, feature, None, None, None, None, times, speed)
        assignments[w] = core.assign_detections(contacts[w], detections[w])
        waveforms[w] = {"jerk_time_s": tj.tolist(), "jerk_mps3": jerk.tolist(),
                        "threshold_mps3": core.jerk_threshold_arr(tj, times, speed).tolist(),
                        "distance_mm": distance.tolist(), "feature": feature.tolist()}
        for i in range(1, len(samples)):
            limit = max(core.ZERO_DROP_GLITCH_MIN_MM, speed[i]/3.6*1000*(times[i]-times[i-1])*core.ZERO_DROP_GLITCH_SPEED_FACTOR)
            if distance[i] < core.DIST_ZERO_THRESHOLD and distance[i-1] > limit:
                data_quality.append({"scene_id": scene_id, "scene": scene_path.name,
                                     "kind": "large_distance_reset", "wheel": w.upper(),
                                     "timestamp_ns": samples[i]["timestamp_ns"], "source_line": samples[i]["line"],
                                     "reason": f"{distance[i-1]:.3f} -> {distance[i]:.3f} mm; preceding feature={feature[i-1]:g}; core glitch limit={limit:.3f} mm"})
    groups = pair_contacts(contacts["fl"], contacts["fr"], core.FRONT_FRONT_PAIR_WIN)
    contact_to_event = {(w,g[w]):g["event"] for g in groups for w in truth if g[w] is not None}
    detail = []
    for group in groups:
        for w in truth:
            ci = group[w]
            row = {"sample_id": f"{scene_id}-e{group['event']:03d}-{w.upper()}", "scene_id": scene_id,
                   "scene": scene_path.name, "event": group["event"], "wheel": w.upper(),
                   "contact_available": ci is not None, "paired_front_contacts": group["paired"],
                   "pair_ambiguous": group["ambiguous"], "pair_peak_gap_ms": group["pair_gap_ms"],
                   "gate_eligible": None, "startdist_fl": None, "startdist_fr": None,
                   "contact_time_s": None, "contact_timestamp_ns": None, "speed_kph": None,
                   "jerk_peak_mps3": None, "jerk_peak_ratio": None, "jerk_oscillation_peaks": None,
                   "feature_at_contact": None, "distance_at_contact_mm": None,
                   "previous_hold_distance_mm": None, "control_bracket_ms": None,
                   "left_timestamp_ns": None, "right_timestamp_ns": None,
                   "left_distance_mm": None, "right_distance_mm": None,
                   "detection_cycle_id": None, "detection_time_s": None,
                   "zero_time_s": None, "predicted_zero_timestamp_ns": None, "time_error_ms": None,
                   "status": "contact_unavailable", "error_mm": None, "raw_core_error_mm": None,
                   "independent_error_mm": None, "flags": [], "related_samples": []}
            if not group["paired"]:
                row["flags"].append("unpaired_jerk_candidate")
            if group["ambiguous"]:
                row["flags"].append("ambiguous_front_pair")
            if ci is None:
                row["flags"].append("missing_jerk_contact")
                row["exclusion_reason"] = "jerk contact unavailable; not a prediction miss"
                row["used_for_accuracy"] = False
                detail.append(row)
                continue
            contact = contacts[w][ci]
            at = contact["onset_time"]
            row.update(contact_time_s=at, contact_timestamp_ns=origin+int(round(at*1e9)),
                       speed_kph=contact["speed"], jerk_peak_mps3=contact["peak_mag"],
                       jerk_oscillation_peaks=contact["n_peaks"],
                       jerk_peak_ratio=contact["peak_mag"]/float(core.jerk_threshold_arr(np.array([at]),times,speed)[0]))
            if not times[0] <= at <= times[-1]:
                row["status"] = "outside_prediction_coverage"
                row["flags"].append("contact_outside_prediction_coverage")
                row["exclusion_reason"] = row["status"]
                row["used_for_accuracy"] = False
                detail.append(row)
                continue
            i = int(np.searchsorted(times, at, side="right"))-1
            j = min(i+1, len(times)-1)
            distance = np.array(waveforms[w]["distance_mm"])
            row.update(startdist_fl=samples[i]["fl_start_dist_0"], startdist_fr=samples[i]["fr_start_dist_0"],
                       gate_eligible=samples[i]["fl_start_dist_0"] == samples[i]["fr_start_dist_0"] == 1,
                       feature_at_contact=samples[i][f"{w}_feat_0"],
                       distance_at_contact_mm=float(np.interp(at,times,distance)),
                       previous_hold_distance_mm=float(distance[i]), control_bracket_ms=(times[j]-times[i])*1000,
                       left_timestamp_ns=samples[i]["timestamp_ns"],right_timestamp_ns=samples[j]["timestamp_ns"],
                       left_distance_mm=float(distance[i]), right_distance_mm=float(distance[j]))
            if row["control_bracket_ms"] > 50:
                row["flags"].append("interpolation_gap_gt_50ms")
            if row["feature_at_contact"] not in (0,1):
                row["flags"].append("non_bump_feature_at_contact")
            matched = assignments[w][ci]
            if matched is None:
                row["status"] = "missed"
                for di, det in enumerate(detections[w]):
                    z = det.get("zero_time")
                    if z is not None and z-core.FRONT_IMPACT_PRE_S <= at <= z+core.FRONT_IMPACT_POST_S:
                        owner = next((oi for oi,d in enumerate(assignments[w]) if d is det),None)
                        if owner is not None and owner != ci:
                            row["flags"].append("nearby_cycle_assigned_elsewhere")
                            row["related_samples"].append(f"{scene_id}-e{contact_to_event[(w,owner)]:03d}-{w.upper()}")
            else:
                di = next(i for i,d in enumerate(detections[w]) if d is matched)
                row.update(detection_cycle_id=f"{scene_id}-{w.upper()}-p{di+1:03d}", detection_time_s=matched["detect_time"])
                error,status,zero,terr = core.calc_error(matched,contact,times,distance,times,speed)
                row.update(raw_core_error_mm=float(error),status=status,zero_time_s=zero,
                           predicted_zero_timestamp_ns=origin+int(round(zero*1e9)) if zero is not None else None,
                           time_error_ms=terr*1000 if terr is not None else None)
                try:
                    row["independent_error_mm"] = independent_error(error,status,zero,at,times,distance,speed)
                    row["error_mm"] = float(error)
                except ValueError as exc:
                    row["status"] = "invalid_calculation"
                    row["flags"].append(str(exc))
                z = matched.get("zero_time")
                if z is not None and not z-core.FRONT_IMPACT_PRE_S <= at <= z+core.FRONT_IMPACT_POST_S:
                    row["flags"].append("match_far_from_cycle_zero")
                if zero is None:
                    row["flags"].append("no_true_zero_in_recording")
                if row["error_mm"] is not None and abs(row["error_mm"]) > 500:
                    row["flags"].append("absolute_error_gt_500mm")
            row["flags"] = list(dict.fromkeys(row["flags"]))
            row["used_for_accuracy"] = bool(row["gate_eligible"] and row["error_mm"] is not None and abs(row["error_mm"]) <= 500)
            reasons = []
            if not row["gate_eligible"]:
                reasons.append("dual StartDist failed")
            if row["error_mm"] is None:
                reasons.append(row["status"])
            elif abs(row["error_mm"]) > 500:
                reasons.append("absolute error exceeds 500 mm")
            row["exclusion_reason"] = "; ".join(reasons)
            detail.append(row)
    cycle_rows = []
    for w, cycles in detections.items():
        for di,det in enumerate(cycles):
            owners = [ci for ci,d in enumerate(assignments[w]) if d is det]
            cycle_rows.append({"scene_id":scene_id,"scene":scene_path.name,"wheel":w.upper(),
                               "cycle_id":f"{scene_id}-{w.upper()}-p{di+1:03d}",
                               "detection_time_s":det["detect_time"],"detection_distance_mm":det["detect_dist"],
                               "coarse_zero_time_s":det["zero_time"],
                               "assigned_sample":f"{scene_id}-e{contact_to_event[(w,owners[0])]:03d}-{w.upper()}" if owners else None})
    result = {"scene_id":scene_id,"scene":scene_path.name,"state":"CALCULATED",
              "origin_timestamp_ns":origin,"input":{"timeline":info,"jerk":truth_info},
              "jerk_contacts":contacts,"front_groups":groups,"detail":detail,
              "prediction_cycles":cycle_rows,"data_quality":data_quality,
              "metrics":{"combined":metrics(detail),**{w:metrics([r for r in detail if r["wheel"]==w]) for w in ("FL","FR")}}}
    plot = {"scene_id":scene_id,"scene":scene_path.name,"origin_timestamp_ns":origin,
            "control_time_s":times.tolist(),"speed_kph":speed.tolist(),
            "startdist_fl":[s["fl_start_dist_0"] for s in samples],"startdist_fr":[s["fr_start_dist_0"] for s in samples],
            "wheels":waveforms,"detail":detail}
    return result, plot


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None):
    fields = fields or list(rows[0]) if rows else fields or ["note"]
    with path.open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k:("; ".join(row[k]) if isinstance(row.get(k),list) else row.get(k)) for k in fields})


def run_batch(input_root: Path, core_path: Path, output: Path, source_receipt: Path | None = None,
              *, scene_sources=None, arrays_only=False) -> dict:
    if output.exists():
        raise FileExistsError("choose a new batch output directory")
    core=load_core(core_path, arrays_only=arrays_only)
    dirs=[Path(s["timeline"]).parent for s in scene_sources] if scene_sources is not None else sorted(p for p in input_root.iterdir() if p.is_dir())
    if not dirs:
        raise ValueError("no scene input directories")
    source = json.loads(source_receipt.read_text(encoding="utf-8")) if source_receipt else {}
    if source_receipt:
        expected = {str(Path(f["local"]).resolve()):f["sha256"] for f in source["files"]}
        actual = ({str(Path(p).resolve()) for s in scene_sources for p in [s["timeline"], *s["jerk"].values()]}
                  if scene_sources is not None else {str(p.resolve()) for scene in dirs for p in scene.iterdir() if p.is_file()})
        if actual != set(expected):
            raise ValueError("input files do not match source receipt")
        for path,digest in expected.items():
            if identity(Path(path))["sha256"] != digest:
                raise ValueError("input hash mismatch: "+path)
    output.mkdir(parents=True,exist_ok=False)
    scenes=[]
    for number,path in enumerate(dirs,1):
        source_scene = scene_sources[number-1] if scene_sources is not None else None
        sid=source_scene["scene_id"] if source_scene else f"s{number:03d}"
        try:
            result,plot=analyse_scene(path,sid,core, timeline_path=Path(source_scene["timeline"]) if source_scene else None,
                                      jerk_paths={w:Path(p) for w,p in source_scene["jerk"].items()} if source_scene else None)
            (output/f"{sid}-waveforms.json").write_text(json.dumps(plot,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
        except (OSError,ValueError,KeyError,TypeError) as exc:
            result={"scene_id":sid,"scene":path.name,"state":"INPUT_OR_CALCULATION_ERROR","error":str(exc),"detail":[],"prediction_cycles":[],"data_quality":[]}
        scenes.append(result)
    rows=[r for s in scenes for r in s["detail"]]
    summary_rows=[{"scene_id":s["scene_id"],"scene":s["scene"],"state":s["state"],"group_count":len(s.get("front_groups",[])),**s.get("metrics",{}).get("combined",{}),
                   "FL_p95_abs_mm":s.get("metrics",{}).get("FL",{}).get("p95_abs_mm"),
                   "FR_p95_abs_mm":s.get("metrics",{}).get("FR",{}).get("p95_abs_mm")} for s in scenes]
    subsets={"all-detail":rows,"missed-predictions":[r for r in rows if r["status"]=="missed"],
             "gate-failed":[r for r in rows if r["gate_eligible"] is False],
             "contact-unavailable":[r for r in rows if not r["contact_available"]],
             "out-of-range":[r for r in rows if r["error_mm"] is not None and abs(r["error_mm"])>500],
             "anomalous-samples":[r for r in rows if r["flags"]]}
    for name,subset in subsets.items():
        write_csv(output/f"{name}.csv",subset,list(rows[0]) if rows else None)
    write_csv(output/"scene-summary.csv",summary_rows)
    write_csv(output/"data-quality.csv",[r for s in scenes for r in s["data_quality"]])
    cycles=[r for s in scenes for r in s["prediction_cycles"]]
    write_csv(output/"prediction-cycles.csv",cycles)
    write_csv(output/"unmatched-prediction-cycles.csv",[r for r in cycles if not r["assigned_sample"]],list(cycles[0]) if cycles else None)
    def speed_bin(r,lo,hi):
        return r["speed_kph"] is not None and lo <= r["speed_kph"] and (r["speed_kph"] < hi or hi==40 and r["speed_kph"]==40)
    report={"schema_version":1,"state":"CALCULATED" if all(s["state"]=="CALCULATED" for s in scenes) else "PARTIAL_BATCH",
            "run_id":source.get("run_id"),"product_identity":source.get("product_identity"),
            "source_receipt":identity(source_receipt) if source_receipt else None,"input_root":str(input_root.resolve()),
            "core":identity(core_path),"adapter":identity(Path(__file__)),
            "scene_count":len(scenes),"scenes":scenes,"detail":rows,"scene_summary":summary_rows,
            "overall":{"combined":metrics(rows),**{w:metrics([r for r in rows if r["wheel"]==w]) for w in ("FL","FR")}},
            "speed_bins":{f"{lo}-{hi}_kph":metrics([r for r in rows if speed_bin(r,lo,hi)]) for lo,hi in ((0,10),(10,20),(20,30),(30,40))},
            "paired_unambiguous_sensitivity":metrics([r for r in rows if r["paired_front_contacts"] and not r["pair_ambiguous"]]),
            "flag_counts":dict(Counter(f for r in rows for f in r["flags"])),
            "rules":{"fields":"payload_ctrl_ts_ms + fl/fr_dist_out_mm_0","slot":0,
                     "duplicates":"last successful complete message by publish_seq",
                     "contacts":"unchanged recovered core; jerk+speed only, no prediction-guided onset or feature rejection",
                     "pairing":"independent jerk peaks; ordered maximum pairs then minimum absolute gap; fixed core 1-second window; missing opposite contact retained",
                     "prediction_assignment":"unchanged core.assign_detections on ALL per-wheel jerk candidates, before StartDist gating",
                     "error":"unchanged core.calc_error, independently reconciled; late distance interpolated linearly; early speed integral",
                     "gate":"previous hold; FL StartDist[0] == FR StartDist[0] == 1 at each actual contact",
                     "accuracy":"gate eligible, finite reconciled error, abs(error)<=500mm; quality flags do not silently filter primary metrics",
                     "quantile":"pooled absolute errors, linear interpolation; never mean of scene P95",
                     "signed_statistics":"signed mean, sample SD/variance ddof=1","additional_compensation":False,
                     "missing":"missing jerk contact is NOT a prediction miss; unknown gate is NOT gate failure",
                     "diagnostic_windows_only":{"cycle_zero_before_s":core.FRONT_IMPACT_PRE_S,"cycle_zero_after_s":core.FRONT_IMPACT_POST_S,"interpolation_gap_ms":50},
                     "parameters":{k:(v.tolist() if isinstance(v,np.ndarray) else v) for k,v in vars(core).items() if k in ("_THRESH_SPD_KPH","_THRESH_GS","MIN_JERK_PEAKS","EVENT_MERGE_GAP_S","MERGE_GAP_DIST_MM","ONSET_MIN_RATIO","ONSET_MIN_SPAN_MM","ONSET_GAP_AFTER_MM","STARTUP_GUARD_S","_DIST_TRULY_ZERO_MM","DIST_ZERO_THRESHOLD","FRONT_FRONT_PAIR_WIN")}},
            "limitations":["Automatic jerk candidates are not independently verified speed-bump truth; noise and unpaired contacts are preserved.",
                           "Greedy core assignment may consume a cycle at an earlier candidate; flagged misses are not proven model misses.",
                           "No calibrated XCP physical timing offset or end-to-end real-time latency measurement.",
                           "Recovered core is not the missing ZIP per-signal pipelines."]}
    (output/"batch-result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ("input-root","core","output"):
        parser.add_argument("--"+key,type=Path,required=True)
    parser.add_argument("--source-receipt",type=Path,help="Verify every input against the downloaded product receipt")
    args=parser.parse_args()
    result=run_batch(args.input_root,args.core,args.output,args.source_receipt)
    print(json.dumps({"state":result["state"],"scene_count":result["scene_count"],"overall":result["overall"],"flag_counts":result["flag_counts"]},indent=2))
    return 0 if result["state"]=="CALCULATED" else 2


if __name__=="__main__":
    raise SystemExit(main())
