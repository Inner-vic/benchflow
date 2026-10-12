#!/usr/bin/env python3
"""Package the one generic bench Skill for Codex and Claude Code."""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "skills/bench-eval-execution"
INSTALLER = ROOT / "skills/install_local_skill.ps1"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version() -> str:
    match = re.search(r'^  version: "(\d+\.\d+\.\d+)"$',
                      (SOURCE / "SKILL.md").read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise RuntimeError("Skill version is missing")
    return match.group(1)


def files(client: str) -> dict[str, bytes]:
    names = [Path("SKILL.md"), Path(".gitignore"), Path("agents/openai.yaml")]
    names.extend(path.relative_to(SOURCE) for path in sorted((SOURCE / "scripts").rglob("*"))
                 if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    data = {name.as_posix(): (SOURCE / name).read_bytes() for name in names}
    if client == "claude-code":
        skill = data["SKILL.md"].decode("utf-8")
        index = skill.index("\n---\n", 4)
        data["SKILL.md"] = (skill[:index] + "\ndisable-model-invocation: true"
                            + skill[index:]).encode("utf-8")
    return data


def write_client(out: Path, release: str, client: str) -> list[Path]:
    data = files(client)
    prefix = "bench-eval-execution/"
    tar_path = out / f"bench-eval-execution-{release}.{client}.skill.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        for relative, body in data.items():
            item = tarfile.TarInfo(prefix + relative)
            item.size = len(body)
            item.mode = 0o644
            archive.addfile(item, io.BytesIO(body))
    zip_path = out / f"bench-eval-execution-{release}-{client}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, body in data.items():
            archive.writestr(prefix + relative, body)
    with tarfile.open(tar_path) as archive:
        assert {item.name.removeprefix(prefix): archive.extractfile(item).read()
                for item in archive.getmembers()} == data
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.testzip() is None
        assert {name.removeprefix(prefix): archive.read(name)
                for name in archive.namelist()} == data
    return [tar_path, zip_path]


def main() -> None:
    release = version()
    out = ROOT / "delivery" / f"bench-eval-execution-{release}"
    out.mkdir(parents=True, exist_ok=False)
    archives = write_client(out, release, "codex")
    archives += write_client(out, release, "claude-code")
    shutil.copy2(INSTALLER, out / "install_local_skill.ps1")
    sums = "".join(f"{digest(path.read_bytes())}  {path.name}\n"
                   for path in [*archives, out / "install_local_skill.ps1"])
    (out / "SHA256SUMS").write_text(sums, encoding="utf-8")
    (out / "交付说明.md").write_text(
        f"# bench-eval-execution {release}\n\n"
        "唯一通用台架评测 Skill。工作站预测验收后，按任务建立稳定的场景入口；"
        "随附 index_workstation_task.py 从最终 PASS 证明逐文件核验产品及收据，"
        "建立任务索引与导航链接。重试编号保留在不可变 run ID 中，"
        "下游 Job 继续使用原权威产品路径。\n\n"
        "本交付只更新控制面和任务目录工具；没有切换工作站 production Runner、"
        "移动历史产品或启动回灌。\n",
        encoding="utf-8",
    )
    print(f"PACKAGED {release}: {out}")


if __name__ == "__main__":
    main()
