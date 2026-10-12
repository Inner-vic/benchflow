from __future__ import annotations

import importlib.util
import tarfile
import tempfile
import unittest
import hashlib
import json
import zipfile
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("build_skill_packages.py")
SPEC = importlib.util.spec_from_file_location("build_skill_packages", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SkillPackageTests(unittest.TestCase):
    def test_universal_bundle_contains_one_unmodified_skill_folder(self):
        source_root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp:
            path = MODULE.build_universal_bundle(source_root, "3.10.0", Path(temp))
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                self.assertEqual({"bench-eval-execution"}, {name.split("/")[0] for name in names})
                self.assertFalse(any(".codex/" in name or ".claude/" in name for name in names))
                for file in MODULE.source_files(source_root / "bench-eval-execution"):
                    name = "bench-eval-execution/" + file.relative_to(source_root / "bench-eval-execution").as_posix()
                    self.assertEqual(file.read_bytes(), archive.read(name))

    def test_client_archives_have_distinct_invocation_metadata(self) -> None:
        source_root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            codex = MODULE.build_archive(source_root / "bench-eval-execution", "3.4.0", "codex", output)
            claude = MODULE.build_archive(source_root / "bench-eval-execution", "3.4.0", "claude-code", output)
            with tarfile.open(codex, "r:gz") as archive:
                codex_skill = archive.extractfile("bench-eval-execution/SKILL.md").read().decode("utf-8")
                openai = archive.extractfile("bench-eval-execution/agents/openai.yaml").read().decode("utf-8")
            with tarfile.open(claude, "r:gz") as archive:
                claude_skill = archive.extractfile("bench-eval-execution/SKILL.md").read().decode("utf-8")
            self.assertNotIn("disable-model-invocation", codex_skill)
            self.assertIn("allow_implicit_invocation: false", openai)
            self.assertIn("disable-model-invocation: true", claude_skill)

    def test_archives_exclude_python_caches(self) -> None:
        source_root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp:
            archive_path = MODULE.build_archive(source_root / "bench-eval-execution", "1.8.2", "codex", Path(temp))
            with tarfile.open(archive_path, "r:gz") as archive:
                names = archive.getnames()
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))

    def test_client_payloads_differ_only_in_skill_frontmatter(self) -> None:
        source_root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp:
            for skill, version in MODULE.SKILLS.items():
                codex = MODULE.build_archive(source_root / skill, version, "codex", Path(temp))
                claude = MODULE.build_archive(source_root / skill, version, "claude-code", Path(temp))
                with tarfile.open(codex, "r:gz") as c, tarfile.open(claude, "r:gz") as a:
                    cfiles = {m.name: c.extractfile(m).read() for m in c.getmembers() if m.isfile()}
                    afiles = {m.name: a.extractfile(m).read() for m in a.getmembers() if m.isfile()}
                self.assertEqual(set(cfiles), set(afiles))
                for name in cfiles:
                    if not name.endswith("/SKILL.md"):
                        self.assertEqual(hashlib.sha256(cfiles[name]).digest(), hashlib.sha256(afiles[name]).digest(), name)

    def test_direct_bundles_are_self_describing_and_client_specific(self) -> None:
        source_root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            for client, hidden_root in (("codex", ".codex"), ("claude-code", ".claude")):
                bundle = MODULE.build_direct_bundle(source_root, "1.8.3", client, output)
                with zipfile.ZipFile(bundle) as archive:
                    names = set(archive.namelist())
                    manifest = json.loads(archive.read("PACKAGE_MANIFEST.json"))
                    instructions = archive.read("部署说明.md").decode("utf-8")
                    execution_skill = archive.read(f"{hidden_root}/skills/bench-eval-execution/SKILL.md").decode("utf-8")
                self.assertEqual(client, manifest["client"])
                self.assertEqual([{"name": name, "version": version} for name, version in MODULE.SKILLS.items()], manifest["skills"])
                self.assertIn(f"{hidden_root}/skills/bench-eval-execution/SKILL.md", names)
                self.assertFalse(any("bench-workstation-access/SKILL.md" in name for name in names))
                self.assertIn("安装这个包", instructions)
                self.assertIn("PACKAGE_MANIFEST.json", instructions)
                self.assertIn("安装并自检", instructions)
                if client == "codex":
                    self.assertNotIn("disable-model-invocation", execution_skill)
                    self.assertIn(f"{hidden_root}/skills/bench-eval-execution/agents/openai.yaml", names)
                else:
                    self.assertIn("disable-model-invocation: true", execution_skill)


if __name__ == "__main__":
    unittest.main()
