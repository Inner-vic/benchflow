#!/usr/bin/env python3
"""Read-only diagnosis for a personal computer's shared-workstation SSH entry."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "bench-workstation-access/v1"
DEFAULT_RUNNER_ENTRY = "~/bench-eval/bin/bench-eval"
RUNNER_ENTRY_RE = re.compile(r"^(?:~|/)[A-Za-z0-9._/+~-]+$")


def issue(code: str, message: str, action: str, *, scope: str = "pc_to_workstation") -> dict[str, str]:
    return {"code": code, "severity": "blocker", "scope": scope, "message": message, "action": action}


def has_exact_host_alias(path: Path, alias: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if len(parts) > 1 and parts[0].lower() == "host" and alias in parts[1:]:
            return True
    return False


def parse_ssh_g(output: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in output.splitlines():
        key, _, value = line.partition(" ")
        if key and value:
            result.setdefault(key.lower(), []).append(value.strip())
    return result


def validate_runner_entry(value: str) -> str:
    if not RUNNER_ENTRY_RE.fullmatch(value) or ".." in value.split("/"):
        raise ValueError("runner entry must be an absolute or ~/ path without shell syntax or '..'")
    return value


def runner_capabilities_command(entry: str) -> str:
    validate_runner_entry(entry)
    prefix = "python3 " if entry.endswith(".py") else ""
    return f"{prefix}{entry} capabilities"


def classify_ssh_failure(stderr: str) -> dict[str, str]:
    value = stderr.lower()
    if "password has expired" in value or "password change required" in value or "account has expired" in value:
        return issue("WORKSTATION_ACCOUNT_PASSWORD_EXPIRED", "工作站账号要求先修改密码，SSH 命令无法执行。", "由账号本人在交互终端更新密码，或请工作站管理员恢复账号访问；随后重跑只读接入检查。不要更换 SSH 密钥或修改 known_hosts。")
    if "bench-eval" in value and ("no such file" in value or "not found" in value):
        return issue("RUNNER_MISSING", "SSH 已连通，但指定的 Runner 入口不存在。", "no-activate 验收时传入新 release 的绝对入口；正式使用时由管理员维护稳定入口。", scope="runner_handshake")
    if "remote host identification has changed" in value:
        return issue("SSH_HOST_KEY_CHANGED", "工作站主机密钥与本机记录不一致。", "停止连接；通过管理员或带外渠道核对新指纹，禁止直接删除 known_hosts。")
    if "host key verification failed" in value:
        return issue("SSH_HOST_KEY_UNVERIFIED", "工作站主机指纹尚未受信任或校验失败。", "从管理员处获取指纹并带外核对后，再由用户本人接受。")
    if "permission denied" in value:
        return issue("SSH_AUTH_FAILED", "工作站拒绝了个人公钥认证。", "确认使用本人私钥，并请管理员核对对应公钥是否登记。")
    if "could not resolve hostname" in value or "name or service not known" in value:
        return issue("SSH_HOST_UNRESOLVED", "SSH 别名解析出的主机名不可解析。", "检查 SSH 配置中的 HostName、公司网络和 DNS。")
    if "connection refused" in value:
        return issue("SSH_CONNECTION_REFUSED", "已到达目标主机，但 SSH 端口拒绝连接。", "请工作站管理员通过本地控制台检查 sshd 和监听端口。")
    if "timed out" in value or "operation timed out" in value:
        return issue("SSH_TIMEOUT", "个人电脑到工作站 SSH 超时；这不是密码错误。", "检查到工作站的网络/VPN、防火墙及返回路由是否可达；VPN 网卡本身不构成拒绝条件。")
    return issue("SSH_FAILED", "工作站 SSH 只读检查失败。", "保留 stderr，按网络、主机指纹和公钥三层排查。")


def make_result(status: str, issues: list[dict[str, str]], facts: dict[str, Any], *, mode: str) -> dict[str, Any]:
    ssh_connected = facts.get("ssh_returncode") == 0
    runner_blocked = any(item.get("scope") == "runner_handshake" for item in issues)
    return {
        "contract": CONTRACT_VERSION,
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "status": status,
        "safe_to_submit": status == "READY" and not issues,
        "mutations_performed": False,
        "checks": {
            "pc_to_workstation": "READY" if ssh_connected or status == "READY" else status,
            "runner_handshake": "READY" if status == "READY" else ("BLOCKED" if runner_blocked else "NOT_VERIFIED"),
            "agent_forwarding": "NOT_CHECKED",
        },
        "issues": issues,
        "facts": facts,
    }


def inspect(alias: str, config: Path, expected_protocol: str, connect_timeout: int, config_only: bool, runner_entry: str = DEFAULT_RUNNER_ENTRY) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    mode = "config_only" if config_only else "connected"
    facts: dict[str, Any] = {"alias": alias, "ssh_config": str(config), "runner_entry": runner_entry}
    try:
        remote_command = runner_capabilities_command(runner_entry)
    except ValueError as exc:
        issues.append(issue("RUNNER_ENTRY_INVALID", str(exc), "传入受信任 release 下的绝对 Runner 路径。", scope="runner_handshake"))
        return make_result("BLOCKED", issues, facts, mode=mode)
    facts["remote_command"] = remote_command

    ssh = shutil.which("ssh")
    if not ssh:
        issues.append(issue("OPENSSH_CLIENT_MISSING", "本机没有找到 ssh 命令。", "安装系统 OpenSSH Client。"))
        return make_result("BLOCKED", issues, facts, mode=mode)
    facts["ssh_binary"] = ssh

    if not config.is_file() or not has_exact_host_alias(config, alias):
        issues.append(issue("WORKSTATION_SSH_CONFIG_MISSING", f"{config} 中没有精确的 Host {alias} 配置。", "显式调用 bench-workstation-access 的 onboard 模式；不要把密码或私钥写入配置。"))
        return make_result("BLOCKED", issues, facts, mode=mode)

    expanded = subprocess.run([ssh, "-F", str(config), "-G", alias], text=True, capture_output=True, check=False)
    if expanded.returncode != 0:
        issues.append(issue("SSH_CONFIG_INVALID", "OpenSSH 无法解析当前 SSH 配置。", "运行 ssh -F <config> -G <alias> 查看错误。"))
        facts["ssh_config_stderr"] = expanded.stderr.strip()
        return make_result("BLOCKED", issues, facts, mode=mode)

    settings = parse_ssh_g(expanded.stdout)
    facts.update({"hostname": (settings.get("hostname") or [None])[0], "user": (settings.get("user") or [None])[0], "port": (settings.get("port") or [None])[0]})
    identities = [Path(value).expanduser() for value in settings.get("identityfile", [])]
    existing = [path for path in identities if path.is_file()]
    facts["configured_identity_count"] = len(identities)
    facts["existing_identity_count"] = len(existing)
    if not existing:
        issues.append(issue("SSH_IDENTITY_MISSING", "SSH 别名没有指向存在的个人私钥文件。", "生成本人独立 ed25519 密钥，只登记 .pub 公钥。"))
        return make_result("BLOCKED", issues, facts, mode=mode)

    if config_only:
        return make_result("LOCAL_CONFIG_READY", issues, facts, mode=mode)

    completed = subprocess.run([ssh, "-F", str(config), "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}", alias, remote_command], text=True, capture_output=True, check=False)
    facts["ssh_returncode"] = completed.returncode
    if completed.returncode != 0:
        issues.append(classify_ssh_failure(completed.stderr))
        facts["ssh_stderr"] = completed.stderr.strip()
        return make_result("BLOCKED", issues, facts, mode=mode)

    try:
        capabilities = json.loads(completed.stdout)
    except json.JSONDecodeError:
        issues.append(issue("RUNNER_INVALID_RESPONSE", "SSH 已连通，但 Runner 没有返回合法 JSON。", "检查所选 Runner 入口及 release 完整性。", scope="runner_handshake"))
        facts["runner_stdout"] = completed.stdout.strip()
        return make_result("BLOCKED", issues, facts, mode=mode)

    facts["runner"] = capabilities
    accepted = capabilities.get("accepted_runner_protocols")
    if not isinstance(accepted, list) or expected_protocol not in accepted:
        issues.append(issue("RUNNER_PROTOCOL_MISMATCH", f"Job 需要协议 {expected_protocol}，Runner 接受 {accepted!r}。", "同步配套 Skill/Runner；不要用临时命令绕过协议检查。", scope="runner_handshake"))
        return make_result("BLOCKED", issues, facts, mode=mode)
    return make_result("READY", issues, facts, mode=mode)


def write_output(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only check of the shared workstation SSH entry")
    parser.add_argument("--alias", default="ubuntu-workstation")
    parser.add_argument("--ssh-config", type=Path, default=Path.home() / ".ssh" / "config")
    parser.add_argument("--expected-protocol", default="1.1")
    parser.add_argument("--runner-entry", default=DEFAULT_RUNNER_ENTRY, help="Stable launcher or no-activate release bench_eval.py path")
    parser.add_argument("--connect-timeout", type=int, default=5)
    parser.add_argument("--config-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    value = inspect(args.alias, args.ssh_config.expanduser().resolve(), args.expected_protocol, args.connect_timeout, args.config_only, args.runner_entry)
    if args.output:
        write_output(args.output.expanduser().resolve(), value)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 2 if value["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
