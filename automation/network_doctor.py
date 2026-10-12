#!/usr/bin/env python3
"""Read-only connectivity diagnostics for a shared bench workstation."""

from __future__ import annotations

import json
import ipaddress
import os
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any


def _run(command: list[str], timeout: float = 3.0) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "available": True,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except FileNotFoundError:
        return {"available": False, "returncode": None, "stdout": "", "stderr": "command not found"}
    except subprocess.TimeoutExpired:
        return {"available": True, "returncode": None, "stdout": "", "stderr": "timeout"}


def _interface_state(name: str | None) -> dict[str, Any]:
    if not name:
        return {"name": None, "exists": False, "operstate": None, "carrier": None}
    root = Path("/sys/class/net") / name
    result: dict[str, Any] = {"name": name, "exists": root.is_dir(), "operstate": None, "carrier": None}
    if not root.is_dir():
        return result
    try:
        result["operstate"] = (root / "operstate").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        result["carrier"] = int((root / "carrier").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    return result


def _parse_route(output: str) -> dict[str, Any]:
    first = output.splitlines()[0] if output.strip() else ""
    tokens = shlex.split(first)
    result: dict[str, Any] = {"raw": first, "interface": None, "source": None, "linkdown": "linkdown" in tokens}
    for marker, field in (("dev", "interface"), ("src", "source")):
        if marker in tokens and tokens.index(marker) + 1 < len(tokens):
            result[field] = tokens[tokens.index(marker) + 1]
    return result


def controller_ip_from_environment() -> str | None:
    # SSH_CONNECTION is: client_ip client_port server_ip server_port.
    value = os.environ.get("SSH_CONNECTION", "").split()
    return value[0] if len(value) == 4 else None


def collect_snapshot(profile: dict[str, Any], controller_ip: str | None, timeout: float = 2.0) -> dict[str, Any]:
    """Collect facts only; never changes routes, services, files or remote state."""
    management = profile.get("management", {})
    bench = profile.get("bench", {})
    bench_address = str(bench.get("address") or "")
    parent = str(bench.get("parent_interface") or "")
    vlan = str(bench.get("vlan_interface") or "")

    controller_route = _run(["ip", "route", "get", controller_ip], timeout) if controller_ip else None
    bench_route = _run(["ip", "route", "get", bench_address], timeout) if bench_address else None
    neighbor = _run(["ip", "neigh", "show", "to", bench_address], timeout) if bench_address else None
    sshd = _run(["systemctl", "is-active", "ssh"], timeout)
    listening = _run(["ss", "-H", "-lnt", "sport", "=", ":22"], timeout)

    tcp_reachable: bool | None = None
    tcp_error: str | None = None
    if bench_address:
        try:
            with socket.create_connection((bench_address, int(bench.get("ssh_port", 22))), timeout=timeout):
                tcp_reachable = True
        except OSError as exc:
            tcp_reachable = False
            tcp_error = type(exc).__name__

    lock_path = Path(str(bench.get("exclusive_lock") or "")).expanduser()
    lock: dict[str, Any] | None = None
    if str(lock_path) not in ("", ".") and lock_path.is_file():
        try:
            value = json.loads(lock_path.read_text(encoding="utf-8"))
            lock = value if isinstance(value, dict) else {"invalid": True}
        except (OSError, json.JSONDecodeError):
            lock = {"invalid": True}

    return {
        "controller": {
            "ip": controller_ip,
            "route": _parse_route(controller_route["stdout"]) if controller_route and controller_route["returncode"] == 0 else None,
            "route_probe": controller_route,
        },
        "workstation": {
            "expected_management_interface": management.get("expected_interface"),
            "sshd_active": (
                True if sshd["returncode"] == 0 and sshd["stdout"] == "active"
                else False if sshd["stdout"] in ("inactive", "failed", "deactivating")
                else None
            ),
            "ssh_listening": bool(listening["stdout"]) if listening["returncode"] == 0 else None,
        },
        "bench": {
            "address": bench_address or None,
            "parent": _interface_state(parent),
            "vlan": _interface_state(vlan),
            "route": _parse_route(bench_route["stdout"]) if bench_route and bench_route["returncode"] == 0 else None,
            "route_probe": bench_route,
            "neighbor": neighbor["stdout"] if neighbor and neighbor["returncode"] == 0 else None,
            "tcp22_reachable": tcp_reachable,
            "tcp22_error": tcp_error,
            "lock": lock,
        },
    }


def _issue(code: str, scope: str, severity: str, message: str, action: str) -> dict[str, str]:
    return {"code": code, "scope": scope, "severity": severity, "message": message, "action": action}


def diagnose(snapshot: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Convert probe facts into stable, user-facing diagnostic codes."""
    issues: list[dict[str, str]] = []
    controller = snapshot.get("controller", {})
    workstation = snapshot.get("workstation", {})
    bench = snapshot.get("bench", {})
    management = profile.get("management", {})

    # SSH authenticates controllers; subnet admission is optional for restricted deployments.
    enforce_subnets = management.get("enforce_controller_subnets", False)
    allowed_values = (management.get("allowed_controller_subnets") or []) if enforce_subnets else []
    unresolved_subnets = [value for value in allowed_values if str(value).strip().upper().startswith("TODO:")]
    allowed_networks = []
    for value in allowed_values:
        if value in unresolved_subnets:
            continue
        try:
            allowed_networks.append(ipaddress.ip_network(str(value), strict=False))
        except ValueError:
            issues.append(_issue(
                "DEVICE_PROFILE_INVALID", "pc_to_workstation", "blocker",
                f"设备 Profile 中的管理网段无效：{value}",
                "由工作站/网络管理员填写合法且已批准的 CIDR。",
            ))
    if enforce_subnets and (unresolved_subnets or not allowed_networks):
        issues.append(_issue(
            "DEVICE_PROFILE_INCOMPLETE", "pc_to_workstation", "blocker",
            "设备 Profile 尚未填写经批准的个人电脑管理网段。",
            "向网络管理员确认允许访问工作站的 CIDR，并替换 allowed_controller_subnets 中的 TODO。",
        ))

    if workstation.get("sshd_active") is False or workstation.get("ssh_listening") is False:
        issues.append(_issue(
            "WORKSTATION_SSH_SERVICE_DOWN", "pc_to_workstation", "blocker",
            "工作站 SSH 服务未运行或 22 端口未监听。",
            "通过工作站本地控制台检查 systemctl status ssh 和 ss -lnt；不要修改台架。",
        ))

    controller_ip = controller.get("ip")
    route = controller.get("route")
    if not controller_ip:
        issues.append(_issue(
            "CONTROLLER_IP_UNKNOWN", "pc_to_workstation", "warning",
            "当前不是 SSH 会话，无法自动识别个人电脑地址。",
            "通过 SSH 执行 doctor，或显式传入 --controller-ip。",
        ))
    elif not route:
        issues.append(_issue(
            "CONTROLLER_RETURN_ROUTE_MISSING", "pc_to_workstation", "blocker",
            f"工作站没有到个人电脑 {controller_ip} 的可用返回路由。",
            "联系工作站网络管理员配置获批的管理网段路由；不要为每位用户长期维护临时 /32 路由。",
        ))
    else:
        if allowed_networks:
            try:
                address = ipaddress.ip_address(str(controller_ip))
            except ValueError:
                address = None
            if address is None or not any(address in network for network in allowed_networks):
                issues.append(_issue(
                    "CONTROLLER_SUBNET_NOT_APPROVED", "pc_to_workstation", "blocker",
                    f"个人电脑 {controller_ip} 不在设备 Profile 的获批管理网段内。",
                    "核实当前网络；如需新增网段，由网络管理员审批并更新设备 Profile。",
                ))
        expected = management.get("expected_interface")
        actual = route.get("interface")
        tunnel_prefixes = tuple(management.get("tunnel_prefixes") or ("tun", "utun", "wg", "ppp"))
        if actual and (actual.startswith(tunnel_prefixes) or (expected and actual != expected)):
            issues.append(_issue(
                "CONTROLLER_RETURN_ROUTE_TUNNEL", "pc_to_workstation", "warning",
                f"工作站到个人电脑 {controller_ip} 的返回流量走 {actual}，预期走 {expected or '管理网卡'}；SSH 可能超时。",
                "当前 SSH 已连接；允许继续。若连接不稳定，检查 VPN 分流或返回路由。",
            ))
        expected_source = management.get("workstation_address")
        if expected_source and route.get("source") and route.get("source") != expected_source:
            issues.append(_issue(
                "CONTROLLER_RETURN_SOURCE_MISMATCH", "pc_to_workstation", "warning",
                f"返回个人电脑的源地址是 {route.get('source')}，预期是工作站管理地址 {expected_source}。",
                "检查策略路由、VPN 分流和管理网卡地址；由网络管理员处理。",
            ))

    lock = bench.get("lock")
    if lock:
        if lock.get("invalid"):
            issues.append(_issue(
                "BENCH_LOCK_INVALID", "coordination", "blocker",
                "台架锁文件损坏，无法确认当前占用者。",
                "由管理员核实台架进程和恢复状态后人工处理，禁止直接删除锁。",
            ))
        else:
            issues.append(_issue(
                "BENCH_OCCUPIED", "coordination", "blocker",
                f"台架已被任务 {lock.get('run_id')}（{lock.get('submitted_by')}）占用。",
                "等待占用任务完成并验证恢复；不要绕过全局锁。",
            ))

    parent = bench.get("parent") or {}
    vlan = bench.get("vlan") or {}
    physical_down = parent.get("exists") is False or parent.get("carrier") == 0 or parent.get("operstate") in ("down", "lowerlayerdown")
    if physical_down:
        issues.append(_issue(
            "BENCH_PHYSICAL_LINK_DOWN", "workstation_to_bench", "blocker",
            f"工作站连接台架的 {parent.get('name') or '物理网卡'} 无物理载波；当前未执行刷包、回灌或任何台架修改。",
            "检查网线、台架/交换机电源、交换端口，以及共享链路是否被切换给其他使用者。",
        ))
    elif vlan.get("exists") is False or vlan.get("operstate") in ("down", "lowerlayerdown"):
        issues.append(_issue(
            "BENCH_VLAN_LINK_DOWN", "workstation_to_bench", "blocker",
            f"台架 VLAN 接口 {vlan.get('name') or '未配置'} 不可用。",
            "检查设备 Profile、VLAN 配置和父接口状态。",
        ))

    bench_route = bench.get("route")
    if not bench_route:
        issues.append(_issue(
            "BENCH_ROUTE_MISSING", "workstation_to_bench", "blocker",
            f"工作站没有到台架 {bench.get('address') or '目标地址'} 的路由。",
            "核对设备 Profile、VLAN 地址和路由；不要继续执行评测。",
        ))
    elif bench_route.get("linkdown"):
        issues.append(_issue(
            "BENCH_ROUTE_LINKDOWN", "workstation_to_bench", "blocker",
            "台架路由存在，但内核标记为 linkdown。",
            "先恢复物理/VLAN 链路，再重新执行 doctor。",
        ))

    neighbor = str(bench.get("neighbor") or "").upper()
    if not physical_down and (not neighbor or any(state in neighbor for state in ("FAILED", "INCOMPLETE"))):
        issues.append(_issue(
            "BENCH_NEIGHBOR_UNRESOLVED", "workstation_to_bench", "blocker",
            "工作站无法解析台架二层邻居。",
            "检查台架是否开机、IP/VLAN 是否一致，以及链路是否接到了正确端口。",
        ))

    if bench.get("tcp22_reachable") is False and not physical_down:
        issues.append(_issue(
            "BENCH_SSH_TIMEOUT", "workstation_to_bench", "blocker",
            "台架 SSH 端口不可达。",
            "先确认链路、地址和台架 sshd；不要把超时误判为密码错误。",
        ))

    blockers = [item for item in issues if item["severity"] == "blocker"]
    warnings = [item for item in issues if item["severity"] == "warning"]
    advisory_codes = {"CONTROLLER_RETURN_ROUTE_TUNNEL", "CONTROLLER_RETURN_SOURCE_MISMATCH"}
    actionable_warnings = [item for item in warnings if item["code"] not in advisory_codes]
    return {
        "schema_version": 1,
        "status": "BLOCKED" if blockers else ("DEGRADED" if actionable_warnings else "READY"),
        "safe_to_start_bench_work": not blockers,
        "mutations_performed": False,
        "issues": issues,
        "summary": {
            "blocker_count": len(blockers),
            "warning_count": len(warnings),
            "scopes": sorted({item["scope"] for item in issues}),
        },
        "snapshot": snapshot,
    }
