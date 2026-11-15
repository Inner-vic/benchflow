import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from bench_gateway.core import GatewayError
from bench_gateway.remote_agent import runtime_installer
from bench_gateway.runtime_bundle import bundle, load_contract


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gateway_runtime_installer_test", PROJECT / "vendor/runtime/direct_package_install.py")
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class RuntimeBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(PROJECT / "vendor/runtime", self.root / "vendor/runtime")

    def test_bundle_requires_pinned_content_and_matching_configured_release(self):
        contract, contents = bundle(self.root)
        self.assertEqual({"direct_package_install.py", "ssh_transport.py"}, set(contents))
        self.assertEqual(contract, load_contract(
            self.root, {"workstation_runtime_release": contract["release"]}))
        with self.assertRaises(GatewayError):
            load_contract(self.root, {"workstation_runtime_release": contract["release"] + "-old"})

    def test_changed_installer_is_rejected_before_any_transport(self):
        path = self.root / "vendor/runtime/direct_package_install.py"
        path.write_bytes(path.read_bytes() + b"\n# changed deployment content\n")
        with self.assertRaises(GatewayError):
            bundle(self.root)

    def test_manifest_cannot_add_executable_outside_fixed_inventory(self):
        path = self.root / "vendor/runtime/manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["files"]["../outside.py"] = "0" * 64
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(GatewayError):
            bundle(self.root)

    def test_remote_contract_rejects_arbitrary_release_path(self):
        contract, _ = bundle(self.root)
        contract["release"] = "/tmp/unreviewed-installer"
        with self.assertRaisesRegex(ValueError, "runtime directory"):
            runtime_installer(contract)

    def test_remote_contract_rejects_extra_script(self):
        contract, _ = bundle(self.root)
        contract["files"]["another.py"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "runtime inventory"):
            runtime_installer(contract)

    def archive(self, member="livc/lib/libxcamera.so", link=None):
        path = self.root / "asset.bin"
        with tarfile.open(path, "w:gz") as archive:
            item = tarfile.TarInfo(member)
            if link is None:
                item.size = 7
                archive.addfile(item, io.BytesIO(b"payload"))
            else:
                item.type = tarfile.SYMTYPE
                item.linkname = link
                archive.addfile(item)
        return path

    def arguments(self, package, expected=None):
        return argparse.Namespace(execute=True, run_id="gateway-inspection-test", package=package,
                                  expected_sha256=expected or hashlib.sha256(package.read_bytes()).hexdigest(),
                                  replace=[], root=self.root / "runner")

    def test_opaque_package_name_passes_content_check_before_material_resolution(self):
        package = self.archive()
        # Stop before controller creates run folders or contacts the bench. Reaching
        # material resolution proves the opaque filename and archive were accepted.
        with patch.object(INSTALLER, "resolve_m100", side_effect=RuntimeError("materials reached")), \
                patch.object(INSTALLER, "checked_run") as commands, \
                patch.object(sys, "path", list(sys.path)):
            with self.assertRaisesRegex(RuntimeError, "materials reached"):
                INSTALLER.controller(self.arguments(package))
            commands.assert_not_called()
        self.assertFalse((self.root / "runner").exists())

    def test_changed_package_digest_is_rejected_before_material_resolution(self):
        package = self.archive()
        with patch.object(INSTALLER, "resolve_m100") as materials, \
                patch.object(INSTALLER, "checked_run") as commands, \
                patch.object(sys, "path", list(sys.path)):
            with self.assertRaisesRegex(INSTALLER.InstallError, "SHA-256 differs"):
                INSTALLER.controller(self.arguments(package, "0" * 64))
            materials.assert_not_called()
            commands.assert_not_called()

    def test_unsafe_or_invalid_archives_fail_before_any_run_or_bench_mutation(self):
        for member, link in (("livc/../../outside", None),
                             ("livc/lib/escape", "../../outside"),
                             ("other-root/lib.so", None),
                             ("invalid", "not-an-archive")):
            with self.subTest(member=member):
                package = self.archive(member, link)
                if member == "invalid":
                    package.write_bytes(b"not gzip or tar")
                with patch.object(INSTALLER, "resolve_m100") as materials, \
                        patch.object(INSTALLER, "checked_run") as commands, \
                        patch.object(sys, "path", list(sys.path)):
                    with self.assertRaises(INSTALLER.InstallError):
                        INSTALLER.controller(self.arguments(package))
                    materials.assert_not_called()
                    commands.assert_not_called()
                self.assertFalse((self.root / "runner").exists())


if __name__ == "__main__":
    unittest.main()
