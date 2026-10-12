#!/usr/bin/env python3
"""Build deterministic Codex and Claude Code archives for the one bench Skill."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable


def skill_version(skill_dir: Path) -> str:
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    header = text.split("---", 2)[1] if text.startswith("---") else ""
    match = re.search(r'^\s+version:\s*["\']?(\d+\.\d+\.\d+)["\']?\s*$', header, re.MULTILINE)
    if not match:
        raise ValueError(f"missing semantic version in Skill metadata: {skill_dir}")
    return match.group(1)


SKILLS = {"bench-eval-execution": skill_version(Path(__file__).parent / "bench-eval-execution")}
CLIENTS = ("codex", "claude-code")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def claude_skill_md(content: bytes) -> bytes:
    text = content.decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md must begin with YAML frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("SKILL.md frontmatter is not closed") from exc
    if any(line.startswith("disable-model-invocation:") for line in lines[1:closing]):
        raise ValueError("shared source must not contain client-specific Claude frontmatter")
    lines.insert(closing, "disable-model-invocation: true")
    return ("\n".join(lines) + "\n").encode("utf-8")


def source_files(skill_dir: Path) -> Iterable[Path]:
    for path in sorted(skill_dir.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(skill_dir)
        if (any(part.startswith(".") or part.startswith("tmp") or part == "__pycache__"
                for part in relative.parts)
                or path.suffix in {".pyc", ".pyo"}):
            continue
        if path.is_file():
            yield path


def add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    archive.addfile(info, io.BytesIO(data))


def build_archive(skill_dir: Path, version: str, client: str, output_dir: Path) -> Path:
    output = output_dir / f"{skill_dir.name}-{version}.{client}.skill.tar.gz"
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in source_files(skill_dir):
                    relative = path.relative_to(skill_dir).as_posix()
                    data = path.read_bytes()
                    if client == "claude-code" and relative == "SKILL.md":
                        data = claude_skill_md(data)
                    mode = 0o755 if relative.startswith("scripts/") and path.suffix in {".py", ".ps1"} else 0o644
                    add_bytes(archive, f"{skill_dir.name}/{relative}", data, mode)
    return output


def bundle_install_text(client: str, bundle_version: str) -> bytes:
    client_name = "Codex" if client == "codex" else "Claude Code"
    hidden_root = ".codex" if client == "codex" else ".claude"
    triggers = "$bench-eval-execution" if client == "codex" else "/bench-eval-execution"
    destination = "$CODEX_HOME\\skills" if client == "codex" else "%USERPROFILE%\\.claude\\skills"
    text = f"""# bench-eval-execution {bundle_version} 部署说明（{client_name}）

当用户说“安装这个包”时，AI 请直接完成本地部署：

1. 读取 `PACKAGE_MANIFEST.json`，确认当前客户端为 `{client}`；不匹配则停止并请用户换包。
2. 解压后将 `{hidden_root}/skills/` 下的 `bench-eval-execution` 安装到 `{destination}`；Codex 使用实际 `CODEX_HOME`，不得假定在用户目录。
3. 如已存在旧版，先在同一 Skill 根目录暂存新版本、备份旧目录，再整体替换；不要混合新旧文件。
4. 确认唯一的 `SKILL.md` 存在，并移除旧版 `bench-workstation-access`，然后报告安装路径并提示重载 {client_name}。显式触发：`{triggers}`。

安装只授权上述本地 Skill 文件变更；不得在安装期间运行 SSH 接入、台架、LPAI 或 production 操作。
如用户同时要求“安装并自检”，使用 `bench-eval-execution` 内的 `scripts/access/connect_workstation_ssh.ps1` 快路径；不自动新建或登记密钥。
"""
    return text.encode("utf-8")


def add_zip_bytes(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def build_direct_bundle(source_root: Path, bundle_version: str, client: str, output_dir: Path) -> Path:
    client_label = "codex" if client == "codex" else "claude-code"
    hidden_root = ".codex" if client == "codex" else ".claude"
    output = output_dir / f"bench-eval-execution-{bundle_version}-{client_label}.zip"
    manifest = {
        "contract": "bench-eval-skill-bundle/v1",
        "bundle_version": bundle_version,
        "client": client,
        "destination_root": "$CODEX_HOME\\skills" if client == "codex" else "%USERPROFILE%\\.claude\\skills",
        "explicit_invocation_only": True,
        "skills": [{"name": name, "version": skill_version(source_root / name)} for name in SKILLS],
        "install_does_not_authorize_runtime_operations": True,
    }
    with zipfile.ZipFile(output, "w") as archive:
        add_zip_bytes(archive, "部署说明.md", bundle_install_text(client, bundle_version))
        add_zip_bytes(archive, "PACKAGE_MANIFEST.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        for skill_name in sorted(SKILLS):
            skill_dir = source_root / skill_name
            for path in source_files(skill_dir):
                relative = path.relative_to(skill_dir).as_posix()
                data = path.read_bytes()
                if client == "claude-code" and relative == "SKILL.md":
                    data = claude_skill_md(data)
                add_zip_bytes(archive, f"{hidden_root}/skills/{skill_name}/{relative}", data)
    return output


def build_universal_bundle(source_root: Path, bundle_version: str, output_dir: Path) -> Path:
    """A single client-neutral Skill folder; installation belongs to the client."""
    output = output_dir / f"台架评测执行-{bundle_version}.zip"
    with zipfile.ZipFile(output, "w") as archive:
        for name in SKILLS:
            for path in source_files(source_root / name):
                relative = path.relative_to(source_root / name).as_posix()
                add_zip_bytes(archive, f"{name}/{relative}", path.read_bytes())
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bundle-version", help="Override the release ZIP version (defaults to Skill metadata)")
    parser.add_argument("--legacy-client-packages", action="store_true", help="Explicitly include legacy client-specific archives")
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    built: list[str] = []
    for name in SKILLS:
        skill_dir = source_root / name
        if not (skill_dir / "SKILL.md").is_file():
            raise SystemExit(f"missing Skill source: {skill_dir}")
        version = skill_version(skill_dir)
        if args.legacy_client_packages:
            for client in CLIENTS:
                built.append(str(build_archive(skill_dir, version, client, output_dir)))
    if args.bundle_version and args.legacy_client_packages:
        for client in CLIENTS:
            built.append(str(build_direct_bundle(source_root, args.bundle_version, client, output_dir)))
    bundle_version = args.bundle_version or skill_version(source_root / "bench-eval-execution")
    built.append(str(build_universal_bundle(source_root, bundle_version, output_dir)))
    print(json.dumps({"status": "BUILT", "archives": built}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
