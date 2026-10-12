import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import direct_package_install as install


class FakeService:
    def __init__(self):
        self.state = dict(install.EXPECTED_STATUS)
        self.events = []

    def status(self):
        return dict(self.state)

    def stop(self):
        self.events.append("stop")
        self.state["fusion"] = "inactive"

    def start(self):
        self.events.append("start")
        self.state["fusion"] = "active"

    def writable(self):
        self.events.append("writable")
        self.state["selinux"] = "Permissive"

    def enforcing(self):
        self.events.append("enforcing")
        self.state["selinux"] = "Enforcing"

    def relabel(self, root, changes):
        self.events.append("relabel")


def make_package(path, *, link=False, escaping=False):
    with tarfile.open(path, "w:gz") as archive:
        directory = tarfile.TarInfo("livc/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for name, content in (("livc/lib/liburoad_m100.so", b"package-so"),
                              ("livc/etc/common.yaml", b"package: true\n"),
                              ("livc/sdf/infer_expert_fusion.sdf", b"fusion")):
            item = tarfile.TarInfo(name)
            item.mode = 0o644
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
        if link or escaping:
            member = tarfile.TarInfo("livc/lib/current.so")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside" if escaping else "liburoad_m100.so"
            archive.addfile(member)


class DirectInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        self.stage = self.root / "stage"
        (self.stage / "incoming").mkdir(parents=True)
        self.formal = self.root / "formal"
        (self.formal / "lib").mkdir(parents=True)
        (self.formal / "etc").mkdir()
        (self.formal / "lib/liburoad_m100.so").write_bytes(b"old-so")
        (self.formal / "etc/common.yaml").write_text("old: true\n")
        self.package = self.stage / "incoming/package.app.tar.gz"
        make_package(self.package)
        self.spec = {"run_id": "test-001", "package_sha256": install.sha256(self.package),
                     "replacements": []}

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_and_activate_with_yaml_and_m100_overrides(self):
        (self.stage / "incoming/replacement-001").write_bytes(b"provided-so")
        (self.stage / "incoming/replacement-002").write_text("updated: true\n")
        self.spec["replacements"] = [
            {"target": "lib/liburoad_m100.so", "staged_name": "replacement-001",
             "sha256": install.sha256(self.stage / "incoming/replacement-001")},
            {"target": "etc/common.yaml", "staged_name": "replacement-002",
             "sha256": install.sha256(self.stage / "incoming/replacement-002")},
        ]
        ready = install.prepare_worker(self.stage, self.spec)
        self.assertEqual(ready["state"], "READY_TO_INSTALL")
        service = FakeService()
        result = install.activate_worker(self.stage, self.spec, formal=self.formal, service=service)
        self.assertEqual(result["state"], "FORMAL_ACTIVATED_VERIFIED")
        self.assertEqual((self.formal / "lib/liburoad_m100.so").read_bytes(), b"provided-so")
        self.assertEqual((self.formal / "etc/common.yaml").read_text(), "updated: true\n")
        self.assertEqual(service.status(), install.EXPECTED_STATUS)
        self.assertTrue((self.stage / "backup/lib/liburoad_m100.so").is_file())

    def test_unsafe_archive_link_is_rejected_before_extract(self):
        make_package(self.package, link=True, escaping=True)
        self.spec["package_sha256"] = install.sha256(self.package)
        with self.assertRaisesRegex(install.InstallError, "escapes livc"):
            install.prepare_worker(self.stage, self.spec)
        self.assertFalse((self.stage / "unpacked").exists())

    def test_force_overwrite_includes_files_already_identical_to_formal(self):
        install.prepare_worker(self.stage, self.spec)
        first = install.activate_worker(self.stage, self.spec, formal=self.formal, service=FakeService())
        self.assertEqual(first["state"], "FORMAL_ACTIVATED_VERIFIED")
        candidate = self.stage / "unpacked/livc"
        self.assertEqual(install.changed_entries(candidate, self.formal), [])
        forced = install.changed_entries(candidate, self.formal, force_overwrite=True)
        self.assertIn("lib/liburoad_m100.so", forced)
        self.assertIn("etc/common.yaml", forced)

    def test_activate_after_app_staging_starts_permissive_and_restores_enforcing(self):
        install.prepare_worker(self.stage, self.spec)
        service = FakeService()
        service.state["selinux"] = "Permissive"
        result = install.activate_worker(self.stage, self.spec, formal=self.formal, service=service)
        self.assertEqual(result["state"], "FORMAL_ACTIVATED_VERIFIED")
        self.assertEqual(service.status(), install.EXPECTED_STATUS)

    def test_failed_partial_install_restores_formal_files(self):
        install.prepare_worker(self.stage, self.spec)
        real_copy = install.copy_entry
        calls = 0

        def fail_second_install(source, target, suffix):
            nonlocal calls
            if suffix.startswith(".installing"):
                calls += 1
                if calls == 2:
                    raise OSError("simulated full disk")
            return real_copy(source, target, suffix)

        service = FakeService()
        with patch.object(install, "copy_entry", side_effect=fail_second_install):
            result = install.activate_worker(self.stage, self.spec, formal=self.formal, service=service)
        self.assertEqual(result["state"], "OLD_FORMAL_RESTORED")
        self.assertTrue(result["safe_to_release"])
        self.assertEqual((self.formal / "lib/liburoad_m100.so").read_bytes(), b"old-so")
        self.assertEqual((self.formal / "etc/common.yaml").read_text(), "old: true\n")
        self.assertFalse((self.formal / "lib/current.so").exists())
        self.assertEqual(service.status(), install.EXPECTED_STATUS)

    def test_backup_failure_does_not_stop_service(self):
        install.prepare_worker(self.stage, self.spec)
        with patch.object(install.shutil, "copy2", side_effect=OSError("simulated full disk")):
            service = FakeService()
            result = install.activate_worker(self.stage, self.spec, formal=self.formal, service=service)
        self.assertEqual(result["state"], "NO_FORMAL_CHANGE")
        self.assertEqual(service.events, [])
        self.assertEqual((self.formal / "lib/liburoad_m100.so").read_bytes(), b"old-so")

    def test_clean_install_removes_files_absent_from_new_package(self):
        (self.formal / "lib/retired.so").write_bytes(b"retired")
        self.spec["clean_formal"] = True
        install.prepare_worker(self.stage, self.spec)
        service = FakeService()
        result = install.activate_worker(self.stage, self.spec, formal=self.formal, service=service)
        self.assertEqual(result["state"], "FORMAL_ACTIVATED_VERIFIED")
        self.assertEqual(result["mode"], "clean-formal")
        self.assertFalse((self.formal / "lib/retired.so").exists())
        self.assertTrue((self.stage / "backup-full/livc/lib/retired.so").exists())
        self.assertEqual(install.tree_signatures(self.formal),
                         install.tree_signatures(self.stage / "unpacked/livc"))
        self.assertEqual(service.status(), install.EXPECTED_STATUS)

    def test_clean_install_preserves_only_selected_runtime_controls(self):
        (self.formal / "bin").mkdir()
        (self.formal / "bin/formal-replay-wrapper.sh").write_text("wrapper")
        (self.formal / "lib/retired.so").write_bytes(b"retired")
        self.spec["clean_formal"] = True
        self.spec["preserve_from_formal"] = ["bin/formal-replay-wrapper.sh"]
        install.prepare_worker(self.stage, self.spec)
        result = install.activate_worker(self.stage, self.spec, formal=self.formal, service=FakeService())
        self.assertEqual(result["state"], "FORMAL_ACTIVATED_VERIFIED")
        self.assertEqual(result["preserved_controls"], ["bin/formal-replay-wrapper.sh"])
        self.assertEqual((self.formal / "bin/formal-replay-wrapper.sh").read_text(), "wrapper")
        self.assertFalse((self.formal / "lib/retired.so").exists())

    def test_clean_install_failure_restores_complete_old_tree(self):
        (self.formal / "lib/retired.so").write_bytes(b"retired")
        self.spec["clean_formal"] = True
        install.prepare_worker(self.stage, self.spec)
        old = install.tree_signatures(self.formal)
        real_copytree = install.shutil.copytree
        candidate = self.stage / "unpacked/livc"

        def fail_during_install(source, target, *args, **kwargs):
            if Path(source) == candidate:
                target.mkdir()
                (target / "partial").write_text("partial")
                raise OSError("simulated copy failure")
            return real_copytree(source, target, *args, **kwargs)

        service = FakeService()
        with patch.object(install.shutil, "copytree", side_effect=fail_during_install):
            result = install.activate_worker(self.stage, self.spec, formal=self.formal, service=service)
        self.assertEqual(result["state"], "OLD_FORMAL_RESTORED")
        self.assertTrue(result["safe_to_release"])
        self.assertEqual(install.tree_signatures(self.formal), old)
        self.assertEqual(service.status(), install.EXPECTED_STATUS)

    def test_clean_install_backup_failure_preserves_formal(self):
        self.spec["clean_formal"] = True
        install.prepare_worker(self.stage, self.spec)
        old = install.tree_signatures(self.formal)
        with patch.object(install.shutil, "copytree", side_effect=OSError("simulated backup failure")):
            service = FakeService()
            result = install.activate_worker(self.stage, self.spec, formal=self.formal, service=service)
        self.assertEqual(result["state"], "NO_FORMAL_CHANGE")
        self.assertTrue(result["safe_to_release"])
        self.assertEqual(install.tree_signatures(self.formal), old)
        self.assertEqual(service.status(), install.EXPECTED_STATUS)

if __name__ == "__main__":
    unittest.main()
