#!/usr/bin/env python3
"""Read-only validation of local SSH agent forwarding through the workstation."""

from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from diagnose_workstation_ssh import CONTRACT_VERSION, has_exact_host_alias


ENDPOINT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/=]+")


def ssh_settings(ssh: str, config: Path, alias: str) -> tuple[dict[str, list[str]], subprocess.CompletedProcess[str]]:
    completed = subprocess.run(
        [ssh, "-F", str(config), "-G", alias], text=True, capture_output=True, check=False
    )
    values: dict[str, list[str]] = {}
    if completed.returncode == 0:
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition(" ")
            if separator:
                values.setdefault(key.lower(), []).append(value.strip())
    return values, completed


def first(values: dict[str, list[str]], key: str, default: str = "") -> str:
    items = values.get(key, [])
    return items[0] if items else default


def trusted_host_records(ssh_keygen: str, settings: dict[str, list[str]], host: str, port: int) -> list[str]:
    """Return the exact LPAI host-key records already trusted by this PC."""
    lookup = host if port == 22 else f"[{host}]:{port}"
    records: list[str] = []
    for configured in settings.get("userknownhostsfile", []):
        for value in configured.split():
            path = Path(value).expanduser()
            if not path.is_file():
                continue
            found = subprocess.run(
                [ssh_keygen, "-F", lookup, "-f", str(path)], text=True, capture_output=True, check=False
            )
            if found.returncode == 0:
                records.extend(line for line in found.stdout.splitlines() if line and not line.startswith("#"))
    return records


def blocked(code: str, message: str, action: str, facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": CONTRACT_VERSION,
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "BLOCKED",
        "safe_to_submit": False,
        "mutations_performed": False,
        "checks": {"agent_forwarding": "BLOCKED"},
        "issues": [{"code": code, "severity": "blocker", "scope": "workstation_to_lpai", "message": message, "action": action}],
        "facts": facts,
    }


def ready(facts: dict[str, Any], lpai_checked: bool) -> dict[str, Any]:
    return {
        "contract": CONTRACT_VERSION,
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "READY",
        "safe_to_submit": True,
        "mutations_performed": False,
        "checks": {"agent_forwarding": "READY", "lpai_read_only_handshake": "READY" if lpai_checked else "NOT_REQUESTED"},
        "issues": [],
        "facts": facts,
    }


def inspect(alias: str, config: Path, connect_timeout: int, lpai_host: str | None = None, lpai_user: str = "root", lpai_port: int = 22, lpai_alias: str | None = None, require_local_lpai_probe: bool = False) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "alias": alias,
        "ssh_config": str(config),
        "lpai_probe_requested": bool(lpai_host or lpai_alias),
        "lpai_local_probe_requested": bool(lpai_alias and require_local_lpai_probe),
        "lpai_auth_mode": "publickey_only",
    }
    ssh = shutil.which("ssh")
    ssh_add = shutil.which("ssh-add")
    ssh_keygen = shutil.which("ssh-keygen") if lpai_alias else None
    if not ssh or not ssh_add or (lpai_alias and not ssh_keygen):
        return blocked("OPENSSH_AGENT_TOOLS_MISSING", "本机缺少 ssh 或 ssh-add。", "安装系统 OpenSSH Client。", facts)
    if not config.is_file() or not has_exact_host_alias(config, alias):
        return blocked("WORKSTATION_SSH_CONFIG_MISSING", "工作站 SSH 别名尚未配置。", "先显式执行 access onboard。", facts)

    required_fingerprint: str | None = None
    if lpai_alias:
        if not ENDPOINT_RE.fullmatch(lpai_alias) or not has_exact_host_alias(config, lpai_alias):
            return blocked("LPAI_SSH_ALIAS_MISSING", "本机没有明确的个人 LPAI SSH alias。", "从个人开发空间复制 SSH 连接信息并配置独立 alias；不要使用他人的私钥。", facts)
        settings, expanded = ssh_settings(ssh, config, lpai_alias)
        if expanded.returncode != 0:
            return blocked("LPAI_SSH_CONFIG_INVALID", "OpenSSH 无法解析个人 LPAI alias。", "修复该 alias 的 HostName、User、Port 和 IdentityFile。", facts)
        lpai_host = first(settings, "hostname")
        lpai_user = first(settings, "user", "root")
        try:
            lpai_port = int(first(settings, "port", "22"))
        except ValueError:
            return blocked("LPAI_ENDPOINT_INVALID", "LPAI alias 的端口无效。", "从个人开发空间重新复制 SSH 连接信息。", facts)
        identity_candidates = [Path(value).expanduser() for value in settings.get("identityfile", [])]
        identity = next((path for path in identity_candidates if path.is_file()), None)
        if identity is None:
            facts["configured_identity_count"] = len(identity_candidates)
            return blocked("LPAI_PRIVATE_KEY_MISSING", "本机没有该个人开发空间对应的私钥。", "在 LPAI 平台下载/找回本人私钥，或在平台登记本机新公钥；LPAI 无密码兜底，禁止复制他人私钥。", facts)
        public_identity = Path(f"{identity}.pub")
        if not public_identity.is_file():
            return blocked("LPAI_PUBLIC_KEY_MISSING", "个人 LPAI 私钥存在，但缺少对应的 .pub 文件。", "在本人电脑前台从私钥导出公钥；只允许公钥离开个人电脑，禁止复制私钥。", facts)
        fingerprint = subprocess.run([ssh_keygen, "-lf", str(public_identity), "-E", "sha256"], text=True, capture_output=True, check=False)
        match = FINGERPRINT_RE.search(fingerprint.stdout)
        if fingerprint.returncode != 0 or not match:
            return blocked("LPAI_KEY_FINGERPRINT_UNAVAILABLE", "无法读取个人 LPAI 密钥的公钥指纹。", "核对 IdentityFile 格式和文件权限；不要输出私钥内容。", facts)
        required_fingerprint = match.group(0)
        host_records = trusted_host_records(ssh_keygen, settings, lpai_host, lpai_port)
        if not host_records:
            return blocked("LPAI_HOST_KEY_NOT_PINNED", "本机没有可复用的 LPAI 主机密钥记录。", "先在本人电脑通过可信渠道核对 LPAI 主机指纹并完成一次严格连接；禁止使用 StrictHostKeyChecking=no。", facts)
        facts.update({"lpai_alias": lpai_alias, "lpai_host": lpai_host, "lpai_user": lpai_user, "lpai_port": lpai_port, "lpai_identity_file": str(identity), "lpai_identity_fingerprint": required_fingerprint, "lpai_host_key_records": len(host_records)})

    identities = subprocess.run([ssh_add, "-l"], text=True, capture_output=True, check=False)
    facts["ssh_add_returncode"] = identities.returncode
    if identities.returncode != 0:
        text = f"{identities.stdout}\n{identities.stderr}".lower()
        if "permission denied" in text:
            return blocked("SSH_AGENT_ACCESS_DENIED", "当前 AI 进程无权访问该 Windows 用户的 SSH agent。", "让 AI 工具与拥有密钥的交互式 Windows 用户保持同一上下文，或在 agent 服务可用后重启 AI；不要新建密钥。", facts)
        if "could not open a connection" in text or "authentication agent" in text:
            return blocked("SSH_AGENT_NOT_RUNNING", "本地 SSH agent 未运行或不可访问。", "启动个人 SSH agent，再显式加载个人 LPAI 密钥。", facts)
        return blocked("SSH_AGENT_KEY_MISSING", "本地 SSH agent 没有可转发的身份。", "使用 ssh-add 加载本人 LPAI 私钥；不得复制私钥到工作站。", facts)
    if required_fingerprint and required_fingerprint not in identities.stdout:
        return blocked("LPAI_KEY_NOT_LOADED", "本地 agent 中没有该开发空间对应的私钥。", f"只加载指纹 {required_fingerprint} 对应的本人私钥，再重试。", facts)

    if lpai_alias and require_local_lpai_probe:
        direct = subprocess.run(
            [
                ssh, "-F", str(config), "-o", "BatchMode=yes",
                "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                "-o", "PreferredAuthentications=publickey", "-o", "IdentitiesOnly=yes",
                "-i", str(public_identity), "-o", f"ConnectTimeout={connect_timeout}", lpai_alias, "true",
            ],
            text=True, capture_output=True, check=False,
        )
        facts["lpai_direct_returncode"] = direct.returncode
        if direct.returncode != 0:
            text = f"{direct.stdout}\n{direct.stderr}".lower()
            if "host key" in text or "identification has changed" in text:
                code = "LPAI_LOCAL_HOST_KEY_BLOCKED"
                action = "通过可信渠道核对 LPAI 主机指纹；不要禁用主机检查。"
            elif any(marker in text for marker in ("timed out", "no route to host", "connection refused", "could not resolve hostname")):
                code = "LPAI_LOCAL_UNREACHABLE"
                action = "先到 LPAI 页面确认开发空间已启动，并核对当前主机和端口；若已启动仍不通，再检查本机 VPN/内网路由。工作站直传任务不依赖本机直连。"
            else:
                code = "LPAI_LOCAL_AUTH_FAILED"
                action = "核对平台公钥授权和 alias 绑定；不要改用密码。"
            return blocked(code, "本机尚不能用指定个人密钥连接 LPAI。", action, facts)

    forwarded = subprocess.run(
        [ssh, "-A", "-F", str(config), "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}", alias, 'test -n "$SSH_AUTH_SOCK" && ssh-add -l'],
        text=True,
        capture_output=True,
        check=False,
    )
    facts["forwarding_returncode"] = forwarded.returncode
    if forwarded.returncode != 0:
        return blocked("AGENT_FORWARDING_NOT_ACTIVE", "工作站前台会话中没有可用的 SSH_AUTH_SOCK。", "仅对需要个人 LPAI 的任务使用 ssh -A；不要永久共享或复制私钥。", facts)
    if required_fingerprint and required_fingerprint not in forwarded.stdout:
        return blocked("LPAI_KEY_NOT_FORWARDED", "工作站看到的转发 agent 不包含指定个人 LPAI 密钥。", "用当前用户的前台 ssh -A 会话重试，并核对本地 ssh-add -l 的指纹。", facts)

    if lpai_host:
        if not ENDPOINT_RE.fullmatch(lpai_host) or not ENDPOINT_RE.fullmatch(lpai_user) or not 1 <= lpai_port <= 65535:
            return blocked("LPAI_ENDPOINT_INVALID", "LPAI 只读探测参数无效。", "使用审核过的主机名、用户名和端口。", facts)
        target = f"{lpai_user}@{lpai_host}"
        if lpai_alias:
            public_key_data = Path(f"{identity}.pub").read_bytes()
            known_hosts_data = ("\n".join(host_records) + "\n").encode("utf-8")
            public_b64 = base64.b64encode(public_key_data).decode("ascii")
            known_b64 = base64.b64encode(known_hosts_data).decode("ascii")
            remote_probe = (
                "umask 077; d=$(mktemp -d) || exit 90; "
                "trap 'rm -rf -- \"$d\"' EXIT HUP INT TERM; "
                f"printf %s {shlex.quote(public_b64)} | base64 -d > \"$d/identity.pub\" || exit 91; "
                f"printf %s {shlex.quote(known_b64)} | base64 -d > \"$d/known_hosts\" || exit 92; "
                "ssh -o BatchMode=yes -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no "
                "-o PreferredAuthentications=publickey -o IdentitiesOnly=yes -o IdentityAgent=SSH_AUTH_SOCK "
                f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile=\"$d/known_hosts\" -i \"$d/identity.pub\" "
                f"-o ConnectTimeout={connect_timeout} -p {lpai_port} {target} true"
            )
            facts["lpai_identity_selection"] = "EXACT_FORWARDED_KEY_VIA_PUBLIC_KEY"
            facts["lpai_host_key_policy"] = "PINNED_FROM_LOCAL_TRUST_STORE"
        else:
            remote_probe = f"ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout={connect_timeout} -p {lpai_port} {target} true"
        lpai = subprocess.run(
            [ssh, "-A", "-F", str(config), "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}", alias, remote_probe],
            text=True,
            capture_output=True,
            check=False,
        )
        facts["lpai_returncode"] = lpai.returncode
        if lpai.returncode != 0:
            message = f"{lpai.stdout}\n{lpai.stderr}".lower()
            if "host key verification failed" in message or "identification has changed" in message:
                return blocked("LPAI_HOST_KEY_BLOCKED", "工作站侧无法验证 LPAI 主机密钥。", "停止并通过可信渠道核对 LPAI 主机指纹。", facts)
            if "permission denied" in message:
                return blocked("LPAI_AUTH_FAILED", "转发的个人身份未被 LPAI 接受。", "核对本地 agent 中的个人密钥和实例授权。", facts)
            if "connection refused" in message:
                return blocked("LPAI_INSTANCE_STOPPED_OR_PORT_CLOSED", "LPAI 网关可达，但开发空间 SSH 端口拒绝连接。", "检查该开发空间是否已启动，并重新从页面核对端口；不要重配密钥。", facts)
            if any(marker in message for marker in ("timed out", "no route to host", "could not resolve hostname")):
                return blocked("LPAI_INSTANCE_UNREACHABLE", "工作站当前无法到达 LPAI endpoint。", "先到 LPAI 页面确认开发空间已启动，并核对当前主机和端口；若已启动仍超时，再检查工作站路由/DNS。不要改换密钥。", facts)
            return blocked("LPAI_INSTANCE_UNREACHABLE", "工作站无法完成 LPAI 只读握手。", "先确认开发空间已启动且页面端口仍有效，再检查网络；不要改换密钥。", facts)
    return ready(facts, bool(lpai_host))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only SSH agent-forwarding validation")
    parser.add_argument("--alias", default="ubuntu-workstation")
    parser.add_argument("--ssh-config", type=Path, default=Path.home() / ".ssh" / "config")
    parser.add_argument("--connect-timeout", type=int, default=5)
    parser.add_argument("--lpai-host")
    parser.add_argument("--lpai-alias", help="本机已有且能直接连接的个人 LPAI SSH alias；优先于裸 host 参数")
    parser.add_argument("--require-local-lpai-probe", action="store_true", help="额外要求个人电脑直连 LPAI；工作站直传数据的正式门禁不需要此项")
    parser.add_argument("--lpai-user", default="root")
    parser.add_argument("--lpai-port", type=int, default=22)
    args = parser.parse_args()
    value = inspect(args.alias, args.ssh_config.expanduser().resolve(), args.connect_timeout, args.lpai_host, args.lpai_user, args.lpai_port, args.lpai_alias, args.require_local_lpai_probe)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
