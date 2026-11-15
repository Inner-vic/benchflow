"""Shared-template qualification with an isolated filesystem and mocked inspect.

No Docker command, workstation connection, or actual production file is used.
Virtual links make the Linux link policy testable without Windows link privileges.
"""
import os
from pathlib import Path
import re
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

from bench_gateway import candidate_remote as remote


class CandidateTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.aliases = {}
        self.raw_links = {}
        aliases = self.aliases
        base_class = type(Path())

        class VirtualPath(base_class):
            def resolve(inner, strict=False):
                value = inner
                for unused in range(20):
                    replacement = None
                    for source in sorted(aliases, key=lambda p: len(str(p)), reverse=True):
                        if value == source or source in value.parents:
                            replacement = aliases[source] / value.relative_to(source)
                            break
                    if replacement is None:
                        return VirtualPath(base_class.resolve(value, strict=strict))
                    value = VirtualPath(replacement)
                raise RuntimeError("virtual link cycle")

            def is_symlink(inner):
                return inner in aliases or base_class.is_symlink(inner)

            def lstat(inner):
                if inner in aliases:
                    return types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_size=10)
                return base_class.lstat(inner)

        self.virtual = VirtualPath

        def mapped(value):
            raw = str(value)
            if raw.startswith("/"):
                return VirtualPath(self.root / raw.lstrip("/"))
            return VirtualPath(raw)

        self.path = mapped
        self.addCleanup(patch.stopall)
        patch.object(remote, "Path", side_effect=mapped).start()
        # Windows fstat/lstat device IDs differ; the worker runs on Linux where
        # a pinned descriptor and its path expose the same filesystem identity.
        original_open, original_fstat = os.open, os.fstat
        opened = {}
        def linux_open(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            opened[descriptor] = mapped(path)
            return descriptor
        patch("os.open", side_effect=linux_open).start()
        patch("os.fstat", side_effect=lambda fd: opened[fd].lstat() if fd in opened else original_fstat(fd)).start()
        original_readlink = os.readlink
        patch("os.readlink", side_effect=lambda p: self.raw_links.get(mapped(p))
              if mapped(p) in self.raw_links else original_readlink(p)).start()
        patch.object(remote, "control_bundle", return_value=[]).start()
        self.image_id = "sha256:" + "a" * 64
        self.inspect_calls = []

        def subprocess_run(argv, **kwargs):
            self.inspect_calls.append(argv)
            if argv[0] == "docker":
                self.assertEqual(argv[1:4], ["image", "inspect", "vehicle-ppo:v1.0"])
                return types.SimpleNamespace(returncode=0, stdout=self.image_id + "\n")
            self.assertEqual(argv[1:], ["-I", "-S", "--version"])
            return types.SimpleNamespace(returncode=0, stdout="Python 3.8.10\n")

        patch("subprocess.run", side_effect=subprocess_run).start()
        self.m = types.SimpleNamespace(BASE=mapped("/home/lixiang/bench-eval"), ROOT=mapped("/mnt/data/bench-eval"), SAFE=re.compile(r"^[a-z-]+$"))
        self.source = "reviewed-source"
        for name in remote.TEMPLATE_FILES:
            self.write(self.m.BASE / "adapters" / self.source / name, name.encode())
        for name in ("state.json", "restore-verification.json", "workstation-predictions.json"):
            self.write(self.m.ROOT / "runs" / self.source / name, name.encode())
        self.write(self.m.ROOT / "cache/predictions" / self.source / "predictions.bench-eval-product.json", b"prediction receipt")
        manifest = mapped("/mnt/data/shared/manifest.json")
        self.write(manifest, b"prepared data")
        self.m.source_contract = lambda source: ({"replay_input": {"prepared_manifest": str(manifest)}}, {}, "", None, None)
        sweep = mapped("/home/lixiang/bench-eval/adapters/gt-toggle-off-stream-20260929-001/v18/controlled-replay")
        for name in ("run_controlled_sweep.py", "adapter-manifest.json", "native_stream_replay.py"):
            self.write(sweep / name, name.encode())
        self.adapter_manifest = sweep / "adapter-manifest.json"
        self.recorder = mapped("/mnt/data/bench-eval/adapters/controlled-replay-native-stream-20260929-006/recorder")
        self.write(self.recorder / "lixplayCLI", b"executable")
        self.logical = mapped("/home/lixiang/bench-eval/cache/tools/RecorderHMI-v0.2.4")
        self.real = mapped("/mnt/data/bench-eval/cache/tools/RecorderHMI-v0.2.4")
        self.link(self.logical, self.real, str(self.real), directory=True)
        for label in ("_internal", "config"):
            (self.real / label).mkdir(parents=True, exist_ok=True)
            self.link(self.recorder / label, self.logical / label, str(self.logical / label))
        self.library = self.real / "_internal/PySide6/libexample.so"
        self.write(self.library, b"reviewed library")
        self.library_link = self.real / "_internal/libexample.so"
        self.link(self.library_link, self.library, "PySide6/libexample.so")
        self.config_file = self.real / "config/recorder.yaml"
        self.write(self.config_file, b"replay: true")
        python = mapped("/home/lixiang/bench-eval/venvs/xcamera-replay/bin/python")
        self.write(mapped("/usr/bin/python3.8"), b"python executable")
        self.link(mapped("/usr/bin/python3"), mapped("/usr/bin/python3.8"), "python3.8")
        self.link(python.parent / "python3", mapped("/usr/bin/python3"), "/usr/bin/python3")
        self.link(python, python.parent / "python3", "python3")
        self.write(python.parent.parent / "pyvenv.cfg", b"version = 3.8.10")

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def link(self, source, destination, text, directory=False):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.mkdir(exist_ok=True) if directory else source.touch()
        self.aliases[source] = destination
        self.raw_links[source] = text

    def qualify(self):
        return remote.qualify(self.m, self.source)

    def test_complete_content_identity_and_cached_second_scan(self):
        original_open = os.open
        opens = []

        def counted(path, flags, *args, **kwargs):
            opens.append(str(path))
            return original_open(path, flags, *args, **kwargs)

        with patch("os.open", side_effect=counted):
            first = self.qualify()
            self.assertEqual(opens.count(str(self.library)), 1)
            opens.clear()
            second = self.qualify()
            self.assertEqual(opens.count(str(self.library)), 0)
            self.assertEqual(opens.count(str(self.config_file)), 1)
        self.assertEqual(first, second)
        self.assertIn(str(self.adapter_manifest), first["snapshot"])
        self.assertEqual(first["recorder_dependencies"][0]["links"], 1)
        self.assertEqual(first["recorder_dependencies"][0]["files"], 1)
        self.assertEqual(first["recorder_image"]["id"], self.image_id)
        self.assertEqual(first["python_runtime"]["version"], "Python 3.8.10")

    def test_library_change_invalidates_registered_template(self):
        before = self.qualify()
        self.library.write_bytes(b"different library bytes")
        after = self.qualify()
        self.assertNotEqual(before["template_id"], after["template_id"])
        with self.assertRaisesRegex(ValueError, "template changed"):
            remote.load_template(self.m, before)

    def test_adapter_manifest_config_and_docker_changes_change_identity(self):
        previous = self.qualify()
        for file in (self.adapter_manifest, self.config_file):
            file.write_bytes(file.read_bytes() + b"changed")
            current = self.qualify()
            self.assertNotEqual(previous["template_id"], current["template_id"])
            previous = current
        self.image_id = "sha256:" + "b" * 64
        self.assertNotEqual(previous["template_id"], self.qualify()["template_id"])

    def test_internal_link_escape_rejected_without_reading_destination(self):
        outside = self.path("/mnt/data/private/secret")
        self.write(outside, b"never read")
        self.aliases[self.library_link] = outside
        self.raw_links[self.library_link] = "../../../../private/secret"
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.qualify()

    def test_outer_link_change_and_directory_link_rejected(self):
        self.raw_links[self.recorder / "config"] = "/different/config"
        with self.assertRaisesRegex(ValueError, "dependency link changed"):
            self.qualify()
        self.raw_links[self.recorder / "config"] = str(self.logical / "config")
        self.link(self.real / "_internal/hidden", self.real / "_internal/PySide6", "PySide6", directory=True)
        with self.assertRaisesRegex(ValueError, "directory symlinks"):
            self.qualify()

    def test_unknown_python_link_and_unavailable_image_are_rejected(self):
        python = self.path("/home/lixiang/bench-eval/venvs/xcamera-replay/bin/python")
        self.raw_links[python] = "other-python"
        with self.assertRaisesRegex(ValueError, "Python link changed"):
            self.qualify()
        self.raw_links[python] = "python3"
        self.image_id = "not-an-image-id"
        with self.assertRaisesRegex(ValueError, "Docker image identity"):
            self.qualify()


class ControlExecutableTests(unittest.TestCase):
    def test_missing_execute_bit_is_not_accepted(self):
        fake = unittest.mock.MagicMock()
        fake.resolve.return_value = fake
        fake.__truediv__.return_value = fake
        fake.name = "bench-replay-mode"
        fake.is_file.return_value = True
        fake.is_symlink.return_value = False
        fake.stat.return_value = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o440)
        with patch.object(remote, "Path", return_value=fake), patch.object(remote, "sha", return_value="a" * 64), \
                patch.object(remote, "CONTROL_HASHES", {"bin/bench-replay-mode": "a" * 64}):
            with self.assertRaisesRegex(ValueError, "executable permission"):
                remote.control_bundle()


if __name__ == "__main__":
    unittest.main()
