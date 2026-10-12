#!/usr/bin/env python3
"""安全的离线台架评测编排骨架。

仅实现 --simulate：它不发起 SSH，不访问远端路径，也不执行配置中的命令。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


STEPS = [
    ("PREFLIGHT_OK", "预检：确认配置、场景和运行标识"),
    ("DEPLOYED", "部署 PCM 包到台架"),
    ("APP_READY", "启动并检查台架 App"),
    ("REPLAYING", "回灌录制场景"),
    ("PREDICTION_READY", "检查台架预测结果"),
    ("UPLOADED", "回传预测结果到开发空间"),
    ("EVALUATED", "用真值运行评测"),
    ("RESTORED", "恢复并核验原台架 App（仅当激活阶段改变过台架状态）"),
    ("REPORTED", "生成评测报告")
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def todo_fields(value: Any, prefix: str = "") -> list[str]:
    missing: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            missing.extend(todo_fields(child, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            missing.extend(todo_fields(child, f"{prefix}[{index}]"))
    elif isinstance(value, str) and value.strip().startswith("TODO:"):
        missing.append(prefix)
    return missing


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="离线台架评测自动化骨架")
    parser.add_argument("--config", required=True, type=Path, help="JSON 配置文件")
    parser.add_argument("--simulate", action="store_true", help="本机模拟运行（当前唯一允许的模式）")
    args = parser.parse_args()

    if not args.simulate:
        parser.error("当前版本只允许 --simulate；尚未实现远端执行。")
    if not args.config.is_file():
        parser.error(f"找不到配置文件：{args.config}")

    config = json.loads(args.config.read_text(encoding="utf-8"))
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    runs_dir = Path(__file__).resolve().parent / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    missing = todo_fields(config)
    manifest = {
        "run_id": run_id,
        "mode": "simulate",
        "created_at": utc_now(),
        "config": config,
        "unresolved_fields": missing,
        "safety_note": "本次为本机模拟；未连接工作站、台架或开发空间。"
    }
    write_json(run_dir / "manifest.json", manifest)

    events_path = run_dir / "events.jsonl"
    lines: list[str] = []
    for state, detail in STEPS:
        event = {
            "at": utc_now(),
            "state": state,
            "detail": detail,
            "mode": "simulate",
            "outcome": "SIMULATED_NOT_EXECUTED",
        }
        lines.append(json.dumps(event, ensure_ascii=False))
        print(f"[{state}] {detail}（模拟）")
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    job = config.get("job", {})
    report = [
        f"# 离线台架评测模拟报告：{run_id}",
        "",
        "- 模式：本机模拟；没有执行远端命令或传输数据。",
        f"- 任务：{job.get('name', '未命名')}",
        f"- PCM 包：{job.get('pcm_package', '未填写')}",
        f"- 基线：{job.get('baseline', '未填写')}",
        f"- 场景：{', '.join(job.get('scenes', [])) or '未填写'}",
        "",
        "## 结果",
        "",
        "控制流已模拟到 REPORTED；所有阶段结果均为 `SIMULATED_NOT_EXECUTED`，不是实际评测结论。",
        "真实任务一旦执行 ACTIVATE，必须在 REPORT 前验证 RESTORE；仅不可变计划明确选择 formal_update 时，允许改为验证正式激活并保留新版本。",
        "",
        "## 待确认信息",
        ""
    ]
    report.extend([f"- `{field}`" for field in missing] or ["- 无 TODO 字段；仍需先完成受控联调。"])
    (run_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\n模拟完成：{run_dir}")
    print(f"待确认字段：{len(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
