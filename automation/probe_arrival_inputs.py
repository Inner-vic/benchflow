#!/usr/bin/env python3
"""Read-only feasibility probe for bench distance CSV and exported XCP jerk JSON.

This is an optional offline diagnostic. It does not select jerk contacts or claim
an arrival-accuracy result. Latest-estimate export is an explicit offline policy.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


TIME_COLUMN = "payload_ctrl_ts_ms"
WHEELS = ("fl", "fr")


def ms_to_ns(value: str) -> int:
    ns = Decimal(value) * 1_000_000
    if not ns.is_finite() or ns != ns.to_integral_value():
        raise ValueError("timestamp must be finite with at most nanosecond precision")
    return int(ns)


def identity(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path.resolve()), "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def read_timeline(path: Path, slot: int) -> tuple[dict, list[dict]]:
    columns = ["odom_ts_ms", "traj_ts_ms", TIME_COLUMN, "publish_seq", "dds_write_ok", "speed_kph"]
    columns += [f"{wheel}_{field}_{slot}" for wheel in WHEELS
                for field in ("dist_pre_mm", "dist_out_mm", "feat", "start_dist")]
    columns = list(dict.fromkeys(columns + ["fl_start_dist_0", "fr_start_dist_0"]))
    samples, invalid = [], []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        missing = sorted(set(columns) - set(header))
        if missing:
            raise ValueError(f"missing timeline columns: {missing}")
        if len(header) != len(set(header)):
            raise ValueError("duplicate timeline column names")
        for row in reader:
            line = reader.line_num
            if None in row or any(v in (None, "") for v in row.values()):
                invalid.append({"line": line, "reason": "incomplete_or_extra_columns"})
                continue
            try:
                sample = {key: float(row[key]) for key in columns}
                if not all(math.isfinite(v) for v in sample.values()):
                    raise ValueError("nonfinite signal")
                sample["timestamp_ns"] = ms_to_ns(row[TIME_COLUMN])
                sample["odom_timestamp_ns"] = ms_to_ns(row["odom_ts_ms"])
                sample["trajectory_timestamp_ns"] = ms_to_ns(row["traj_ts_ms"])
                sample["line"] = line
            except (ValueError, InvalidOperation, OverflowError) as exc:
                invalid.append({"line": line, "reason": str(exc)})
                continue
            samples.append(sample)
    if not samples:
        raise ValueError("timeline has no valid samples")
    groups = defaultdict(list)
    for sample in samples:
        groups[sample["timestamp_ns"]].append(sample)
    signature = ["speed_kph", "dds_write_ok", "fl_start_dist_0", "fr_start_dist_0"] + [f"{w}_{f}_{slot}" for w in WHEELS
                  for f in ("dist_out_mm", "feat", "start_dist")]
    conflicts = [ts for ts, rows in groups.items()
                 if len({tuple(r[k] for k in signature) for r in rows}) > 1]
    joint_groups = defaultdict(list)
    for sample in samples:
        joint_groups[(sample["odom_timestamp_ns"], sample["trajectory_timestamp_ns"])].append(sample)
    joint_conflicts = sum(len({tuple(r[k] for k in signature) for r in rows}) > 1
                          for rows in joint_groups.values())
    timestamps = [s["timestamp_ns"] for s in samples]
    wheel_info = {}
    for wheel in WHEELS:
        distance = [s[f"{wheel}_dist_out_mm_{slot}"] for s in samples]
        positive = [v for v in distance if v > 0]
        wheel_info[wheel] = {
            "positive_distance_rows": len(positive),
            "positive_distance_range_mm": [min(positive), max(positive)] if positive else None,
            "features": sorted({s[f"{wheel}_feat_{slot}"] for s in samples}),
            "startdist_values": sorted({s[f"{wheel}_start_dist_{slot}"] for s in samples}),
            "positive_to_zero_transitions_unvalidated": sum(a > 0 and b == 0 for a, b in zip(distance, distance[1:])),
        }
    return {
        **identity(path), "columns": header, "valid_rows": len(samples),
        "invalid_rows": invalid, "timestamp_column": TIME_COLUMN,
        "time_range_ns": [min(timestamps), max(timestamps)],
        "unique_timestamps": len(groups), "duplicate_timestamp_rows": len(samples) - len(groups),
        "conflicting_timestamp_group_count": len(conflicts),
        "conflicting_timestamp_examples_ns": conflicts[:10],
        "odom_trajectory_group_count": len(joint_groups),
        "conflicting_odom_trajectory_group_count": joint_conflicts,
        "odom_payload_equal_rows": sum(s["odom_timestamp_ns"] == s["timestamp_ns"] for s in samples),
        "front_pre_output_different_rows": sum(any(s[f"{w}_dist_pre_mm_{slot}"] != s[f"{w}_dist_out_mm_{slot}"] for w in WHEELS) for s in samples),
        "backwards_timestamp_steps": sum(b < a for a, b in zip(timestamps, timestamps[1:])),
        "dds_write_failed_rows": sum(s["dds_write_ok"] != 1 for s in samples),
        "startdist_gate_columns": ["fl_start_dist_0", "fr_start_dist_0"],
        "dual_startdist_eligible_rows": sum(s["fl_start_dist_0"] == s["fr_start_dist_0"] == 1 for s in samples),
        "wheels": wheel_info,
    }, samples


def latest_success_at_time(samples: list[dict], slot: int) -> list[dict]:
    """Offline latest-estimate policy, not reconstruction of real-time latency.

    Choose one entire successful message per payload control timestamp. Fail on
    regressions or conflicting values with the same ego and trajectory snapshot.
    Never average distances or choose each wheel/gate independently.
    """
    selected, snapshots = {}, {}
    previous_time, previous_sequence = None, -1
    fields = ["speed_kph", "fl_start_dist_0", "fr_start_dist_0"]
    fields += [f"{w}_{f}_{slot}" for w in WHEELS for f in ("dist_pre_mm", "dist_out_mm", "feat")]
    for sample in samples:
        timestamp = sample["timestamp_ns"]
        sequence = sample["publish_seq"]
        if sequence != int(sequence) or sequence <= previous_sequence:
            raise ValueError("latest-success policy requires strictly increasing integer publish_seq")
        if previous_time is not None and timestamp < previous_time:
            raise ValueError("latest-success policy requires nondecreasing payload timestamps")
        previous_time, previous_sequence = timestamp, sequence
        if sample["dds_write_ok"] != 1:
            continue
        key = (timestamp, sample["odom_timestamp_ns"], sample["trajectory_timestamp_ns"])
        signature = tuple(sample[field] for field in fields)
        if key in snapshots and snapshots[key] != signature:
            raise ValueError("different values share payload, odom and trajectory timestamps; explicit investigation required")
        snapshots[key] = signature
        if sample["trajectory_timestamp_ns"] > sample["odom_timestamp_ns"]:
            raise ValueError("trajectory is newer than the ego time; explicit alignment required")
        selected[timestamp] = sample
    return list(selected.values())


def read_jerk(path: Path, wheel: str) -> tuple[dict, list[dict]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("XCP JSON must contain an object")
    expected = f"s_InDatCalcFAS_WhlJerk{wheel.upper()}_mps3"
    if payload.get("signal_name") != expected:
        raise ValueError(f"expected signal_name {expected}")
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("XCP frames must be a nonempty array")
    samples = []
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValueError(f"invalid XCP frame at {i}")
        ts = frame.get("ts_ns")
        value = frame.get("physical")
        if type(ts) is not int or type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"invalid XCP timestamp/physical at frame {i}")
        samples.append({"timestamp_ns": ts, "jerk_mps3": value})
    if payload.get("frame_count", len(samples)) != len(samples):
        raise ValueError("XCP frame_count differs from actual frame count")
    timestamps = [s["timestamp_ns"] for s in samples]
    return {
        **identity(path), "signal": expected, "samples": len(samples),
        "time_range_ns": [min(timestamps), max(timestamps)],
        "backwards_timestamp_steps": sum(b < a for a, b in zip(timestamps, timestamps[1:])),
        "duplicate_timestamp_rows": len(timestamps) - len(set(timestamps)),
        "jerk_range_mps3": [min(s["jerk_mps3"] for s in samples), max(s["jerk_mps3"] for s in samples)],
    }, samples


def probe_scene(timeline: Path, truth: dict[str, Path], slot: int, export: Path | None = None,
                sample_policy: str = "all-publishes") -> dict:
    timeline_info, distance_samples = read_timeline(timeline, slot)
    result = {"state": "PARSEABLE_REQUIRES_ALIGNMENT_AND_CONTACT_RULE", "slot": slot,
              "distance_family": "bench_pcm_output", "accuracy_available": False,
              "timeline": timeline_info, "jerk": {}, "overlap_ns": {}}
    jerk_samples = {}
    for wheel in WHEELS:
        info, samples = read_jerk(truth[wheel], wheel)
        result["jerk"][wheel] = info
        jerk_samples[wheel] = samples
        start = max(info["time_range_ns"][0], timeline_info["time_range_ns"][0])
        end = min(info["time_range_ns"][1], timeline_info["time_range_ns"][1])
        result["overlap_ns"][wheel] = max(0, end - start)
    result["metric_blockers"] = [
        "Canonical jerk/contact detector and event matching are not supplied by the ZIP.",
        "Confirm CSV output timebase against XCP clock; overlapping ranges alone do not establish synchronization.",
        "Map PCM output to the metric explicitly; it is not automatically FAS FinalSusObjDist or PCM EndDist method 0.",
    ]
    if timeline_info["conflicting_timestamp_group_count"]:
        result["metric_blockers"].append("Different output values share timestamps; no silent deduplication or invented sample times.")
    if not all(result["overlap_ns"].values()):
        result["metric_blockers"].append("Distance and jerk have no temporal overlap for one or both wheels.")
    if timeline_info["backwards_timestamp_steps"] or any(v["backwards_timestamp_steps"] for v in result["jerk"].values()):
        result["metric_blockers"].append("Input timebase contains backwards steps.")
    if export:
        exported_samples = latest_success_at_time(distance_samples, slot) if sample_policy == "latest-success-at-time" else distance_samples
        result["export"] = {"sample_policy": sample_policy, "distance_messages": len(exported_samples),
                            "distance_rows": len(exported_samples) * len(WHEELS),
                            "time_column": TIME_COLUMN, "distance_columns": [f"{w}_dist_out_mm_{slot}" for w in WHEELS]}
        export.mkdir(parents=True, exist_ok=False)
        with (export / "distance_front.csv").open("w", encoding="utf-8", newline="") as stream:
            fields = ["source_line", "timestamp_ns", "odom_timestamp_ns", "trajectory_timestamp_ns", "publish_seq", "dds_write_ok", "speed_kph", "wheel", "slot", "distance_mm", "pre_distance_mm", "feature", "startdist_fl", "startdist_fr"]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for s in exported_samples:
                for w in WHEELS:
                    writer.writerow(dict(zip(fields, [s["line"], s["timestamp_ns"], s["odom_timestamp_ns"], s["trajectory_timestamp_ns"], s["publish_seq"], s["dds_write_ok"], s["speed_kph"], w.upper(), slot, s[f"{w}_dist_out_mm_{slot}"], s[f"{w}_dist_pre_mm_{slot}"], s[f"{w}_feat_{slot}"], s["fl_start_dist_0"], s["fr_start_dist_0"]])))
        for wheel, samples in jerk_samples.items():
            with (export / f"jerk_{wheel}.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["timestamp_ns", "jerk_mps3"])
                writer.writeheader()
                writer.writerows(samples)
    return result


def probe_task(index_path: Path, truth_root: Path, slot: int) -> dict:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    scenes = []
    for entry in index["scenes"]:
        name = entry["scene"]
        if Path(name).name != name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError("invalid scene name")
        paths = {w: truth_root / name / "original/xcp/BZCU" / f"s_InDatCalcFAS_WhlJerk{w.upper()}_mps3.json" for w in WHEELS}
        try:
            result = probe_scene(Path(entry["task_copy"]) / "sus_preview_timeline.csv", paths, slot)
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
            result = {"state": "INPUT_ERROR", "accuracy_available": False, "error": str(exc)}
        scenes.append({"scene": name, **result})
    return {"schema_version": 1, "operation": "probe-arrival-inputs", "run_id": index.get("run_id"),
            "product_identity": index.get("product_identity"), "index": identity(index_path),
            "scene_count": len(scenes), "states": dict(Counter(s["state"] for s in scenes)),
            "accuracy_available": False, "scenes": scenes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--timeline", type=Path)
    source.add_argument("--task-index", type=Path)
    parser.add_argument("--jerk-fl", type=Path)
    parser.add_argument("--jerk-fr", type=Path)
    parser.add_argument("--truth-root", type=Path)
    parser.add_argument("--slot", type=int, choices=range(10), default=0, help="Object slot; not a PCM comparison method")
    parser.add_argument("--output", type=Path, help="New JSON file; default stdout")
    parser.add_argument("--export", type=Path, help="New directory for single-scene normalized CSVs; default preserves duplicate timestamps")
    parser.add_argument("--sample-policy", choices=("all-publishes", "latest-success-at-time"), default="all-publishes",
                        help="Export all messages or last successful full message per payload timestamp (offline latest estimate)")
    args = parser.parse_args(argv)
    if args.task_index and (not args.truth_root or args.export or args.jerk_fl or args.jerk_fr):
        parser.error("--task-index requires --truth-root; export and individual jerk paths apply to --timeline only")
    if args.timeline and (not args.jerk_fl or not args.jerk_fr or args.truth_root):
        parser.error("--timeline requires --jerk-fl and --jerk-fr, without --truth-root")
    if args.sample_policy != "all-publishes" and not args.export:
        parser.error("--sample-policy requires single-scene --export")
    if args.output and args.output.exists():
        parser.error("output already exists; choose a new path")
    try:
        if args.task_index:
            result = probe_task(args.task_index, args.truth_root, args.slot)
        else:
            result = {"schema_version": 1, "operation": "probe-arrival-inputs", **probe_scene(args.timeline, {"fl": args.jerk_fl, "fr": args.jerk_fr}, args.slot, args.export, args.sample_policy)}
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        else:
            print(encoded, end="")
        return 2 if "INPUT_ERROR" in result.get("states", {}) else 0
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        parser.exit(2, f"arrival input probe failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
