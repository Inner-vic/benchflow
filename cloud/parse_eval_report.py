#!/usr/bin/env python3
"""Parse stable summary metrics from the uroad offline evaluation report."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


class ReportError(RuntimeError):
    pass


CORE_PATTERNS = {
    "candidate_frames": (r"候选预测帧:\s*(\d+)", int),
    "completed_frames": (r"至少完成一项任务:\s*(\d+)", int),
    "unmatched_timestamps": (r"未匹配真值时间戳:\s*(\d+)", int),
}

TASK_PATTERNS = {
    "trajectory": {
        "trajectory_z_mae_cm": (r"平均绝对误差:\s*([0-9.]+)\s*cm", float),
        "trajectory_acc_1cm_percent": (r"合格率:\s*([0-9.]+)%", float),
        "trajectory_pass_rate_percent": (r"通过率:\s*([0-9.]+)%", float),
        "trajectory_overall_occupied_iou_percent": (r"Overall\(occupied\)\s+([0-9.]+)", float),
    },
    "vision": {
        "vision_overall_map": (r"Overall mAP:\s*([0-9.]+)", float),
    },
}

SUPPORTED_TASKS = frozenset(TASK_PATTERNS)
DEFAULT_VALIDITY_POLICY = {
    "minimum_candidate_frames": 1,
    "minimum_completed_frames": 1,
    "required_tasks": [],
}


def parse_report(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ReportError(f"evaluation report is missing or empty: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    metrics: dict[str, Any] = {}
    missing = []
    for name, (pattern, converter) in CORE_PATTERNS.items():
        match = re.search(pattern, text)
        if match:
            metrics[name] = converter(match.group(1))
        else:
            missing.append(name)
    if missing:
        raise ReportError(f"evaluation report is missing required core metrics: {missing}")

    evaluated_tasks = []
    for task, patterns in TASK_PATTERNS.items():
        matches = {
            name: re.search(pattern, text)
            for name, (pattern, _converter) in patterns.items()
        }
        if not any(matches.values()):
            continue
        task_missing = [name for name, match in matches.items() if match is None]
        if task_missing:
            raise ReportError(f"evaluation report has incomplete {task} metrics: {task_missing}")
        evaluated_tasks.append(task)
        for name, match in matches.items():
            converter = patterns[name][1]
            metrics[name] = converter(match.group(1))

    if not evaluated_tasks:
        raise ReportError("evaluation report contains no supported task metrics")

    return {
        "state": "EVALUATION_REPORT_PARSED",
        "report": str(path.resolve()),
        "evaluated_tasks": evaluated_tasks,
        "metrics": metrics,
    }


def validate_result(parsed: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply result-validity gates that are distinct from metric acceptance thresholds.

    A successful evaluator process is not evidence of a useful evaluation.  These
    gates reject empty scene selections and missing task coverage before release
    thresholds are considered.
    """
    if policy is None:
        policy = {}
    if not isinstance(policy, dict):
        raise ReportError("evaluation validity policy must be an object")
    unsupported = sorted(set(policy) - set(DEFAULT_VALIDITY_POLICY))
    if unsupported:
        raise ReportError(f"unsupported evaluation validity rules: {unsupported}")

    effective = {**DEFAULT_VALIDITY_POLICY, **policy}
    for field in ("minimum_candidate_frames", "minimum_completed_frames"):
        value = effective[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReportError(f"evaluation validity rule {field} must be a non-negative integer")

    required_tasks = effective["required_tasks"]
    if (
        not isinstance(required_tasks, list)
        or any(not isinstance(task, str) for task in required_tasks)
        or len(required_tasks) != len(set(required_tasks))
    ):
        raise ReportError("evaluation validity rule required_tasks must be a unique string array")
    unknown_tasks = sorted(set(required_tasks) - SUPPORTED_TASKS)
    if unknown_tasks:
        raise ReportError(f"unsupported required evaluation tasks: {unknown_tasks}")

    metrics = parsed.get("metrics") or {}
    evaluated_tasks = set(parsed.get("evaluated_tasks") or [])
    checks = [
        {
            "gate": "minimum_candidate_frames",
            "actual": int(metrics.get("candidate_frames", 0)),
            "required": effective["minimum_candidate_frames"],
        },
        {
            "gate": "minimum_completed_frames",
            "actual": int(metrics.get("completed_frames", 0)),
            "required": effective["minimum_completed_frames"],
        },
    ]
    for item in checks:
        item["passed"] = item["actual"] >= item["required"]
    for task in required_tasks:
        checks.append({
            "gate": "required_task",
            "task": task,
            "actual": task in evaluated_tasks,
            "required": True,
            "passed": task in evaluated_tasks,
        })
    valid = all(item["passed"] for item in checks)
    return {
        "decision": "VALID" if valid else "INVALID",
        "checks": checks,
    }


def compare_metrics(metrics: dict[str, Any], policy: dict[str, Any] | None) -> dict[str, Any]:
    if not policy:
        return {"decision": "NOT_EVALUATED", "reason": "no explicit acceptance policy was supplied", "checks": []}
    checks = []
    for name, rule in policy.items():
        if name not in metrics or not isinstance(rule, dict):
            raise ReportError(f"invalid acceptance rule for metric: {name}")
        value = float(metrics[name])
        if "minimum" in rule:
            threshold = float(rule["minimum"])
            passed = value >= threshold
            operator = ">="
        elif "maximum" in rule:
            threshold = float(rule["maximum"])
            passed = value <= threshold
            operator = "<="
        else:
            raise ReportError(f"acceptance rule requires minimum or maximum: {name}")
        checks.append({"metric": name, "value": value, "operator": operator, "threshold": threshold, "passed": passed})
    return {"decision": "PASS" if all(item["passed"] for item in checks) else "FAIL", "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse an offline evaluation report")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--policy", type=Path, help="Optional JSON metric acceptance policy")
    parser.add_argument("--validity-policy", type=Path, help="Optional JSON result-validity policy")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = parse_report(args.report)
    policy = json.loads(args.policy.read_text(encoding="utf-8")) if args.policy else None
    validity_policy = json.loads(args.validity_policy.read_text(encoding="utf-8")) if args.validity_policy else None
    result["validity"] = validate_result(result, validity_policy)
    result["acceptance"] = compare_metrics(result["metrics"], policy)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["validity"]["decision"] == "VALID" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReportError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"state": "EVALUATION_REPORT_INVALID", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
