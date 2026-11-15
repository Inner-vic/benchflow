import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from bench_gateway import material_inspector as inspector


MOCK_SYMBOLS = {'status': 'LIMITED_PROVIDER_SYMBOLS_NO_REQUIREMENTS', 'required_symbol_count': 0,
                'missing_symbols': [], 'providers': {}, 'so_executed': False, 'full_abi_compatibility': 'NOT_CHECKED'}


def elf(machine=183, bits=2, endian=1, kind=3):
    header = bytearray(64)
    header[:7] = b"\x7fELF" + bytes((bits, endian, 1))
    order = "little" if endian == 1 else "big"
    header[16:18] = kind.to_bytes(2, order)
    header[18:20] = machine.to_bytes(2, order)
    return bytes(header)


class MaterialInspectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        symbol_patch = patch.object(inspector.abi_symbols, 'check_libraries', return_value=dict(MOCK_SYMBOLS))
        self.symbol_check = symbol_patch.start()
        self.addCleanup(symbol_patch.stop)
        self.root = Path(self.temp.name) / "owner"
        self.root.mkdir()
        self.request = {"owner_root": str(self.root)}
        for key, name in (("camera_so", "libxcamera.so"), ("lidar_so", "libxlidar.so"), ("m100_so", "liburoad_m100.so")):
            path = self.root / name
            path.write_bytes(elf())
            self.request[key] = self.asset(path, target="lib/" + name)
        self.app = self.root / "package.app.tar.gz"
        self.make_archive()

    def asset(self, path, **extra):
        return {"remote_path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **extra}

    def make_archive(self, extra=(), omit=(), original_elf=None):
        header = elf() if original_elf is None else original_elf
        with tarfile.open(self.app, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            root = tarfile.TarInfo("./")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            for name in ("libxcamera.so", "libxlidar.so", "liburoad_m100.so"):
                if name not in omit:
                    item = tarfile.TarInfo("./APP/lib/" + name)
                    item.size = len(header)
                    archive.addfile(item, io.BytesIO(header))
            for item, data in extra:
                archive.addfile(item, io.BytesIO(data) if data is not None else None)
        self.request["package"] = self.asset(self.app)

    @staticmethod
    def item(name, data=b"x"):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        return info, data

    @staticmethod
    def link(name, target, hard=False):
        info = tarfile.TarInfo(name)
        info.type = tarfile.LNKTYPE if hard else tarfile.SYMTYPE
        info.linkname = target
        return info, None

    def codes(self, result):
        return {x["code"] for x in result["issues"]}

    def test_valid_materials_with_root_directory_are_readonly_static_pass(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        result = inspector.inspect(self.request)
        self.assertEqual("PASS", result["status"], result)
        self.assertEqual("static-only", result["evidence"]["scope"])
        self.assertFalse(result["evidence"]["mutations_performed"])
        self.assertTrue(any("完整动态符号、ABI" in item for item in result["evidence"]["not_checked"]))
        self.assertEqual("APP/lib/libxcamera.so", result["evidence"]["assets"]["camera_so"]["resolved_target"])
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()})

    def test_missing_package_and_m100_still_checks_provided_so(self):
        del self.request["package"]
        del self.request["m100_so"]
        camera = self.root / "libxcamera.so"
        camera.write_bytes(b"not a shared library")
        self.request["camera_so"] = self.asset(camera, target="lib/libxcamera.so")
        result = inspector.inspect(self.request)
        self.assertTrue({"PACKAGE_REQUIRED", "M100_BUILD_REQUIRED", "INVALID_ELF"} <= self.codes(result))
        self.assertIn("lidar_so", result["evidence"]["assets"])

    def test_other_owner_asset_is_not_read(self):
        outside = self.root.parent / "private.so"
        outside.write_bytes(elf())
        self.request["camera_so"] = self.asset(outside, target="lib/libxcamera.so")
        result = inspector.inspect(self.request)
        self.assertIn("OWNER_PATH", self.codes(result))
        self.assertNotIn("camera_so", result["evidence"]["assets"])
        self.assertNotIn(str(outside), json.dumps(result))

    def test_registry_hash_change_stops_archive_use(self):
        self.request["package"]["sha256"] = "0" * 64
        result = inspector.inspect(self.request)
        self.assertIn("HASH_MISMATCH", self.codes(result))
        self.assertNotIn("archive", result["evidence"])

    def test_traversal_special_file_and_duplicate_members_are_blocked(self):
        device = tarfile.TarInfo("APP/device")
        device.type = tarfile.CHRTYPE
        for extra, code in (([self.item("../escape")], "UNSAFE_PATH"),
                            ([(device, None)], "ARCHIVE_SPECIAL_FILE"),
                            ([self.item("APP/lib/libxcamera.so", elf())], "ARCHIVE_DUPLICATE_PATH")):
            with self.subTest(code=code):
                self.make_archive(extra)
                self.assertIn(code, self.codes(inspector.inspect(self.request)))

    def test_safe_relative_symlink_and_hardlink_targets_are_inspected(self):
        for hard in (False, True):
            with self.subTest(hard=hard):
                target = "APP/actual/camera.so" if hard else "../actual/camera.so"
                self.make_archive([self.item("APP/actual/camera.so", elf()), self.link("APP/lib/libxcamera.so", target, hard)], omit=("libxcamera.so",))
                result = inspector.inspect(self.request)
                self.assertEqual("PASS", result["status"], result)
                self.assertEqual("APP/actual/camera.so", result["evidence"]["assets"]["camera_so"]["original_file"])

    def test_symlink_parent_escape_and_loop_are_blocked(self):
        cases = [([self.link("a", "."), self.link("a/b", "../escape")], "ARCHIVE_LINK_ESCAPE"),
                 ([self.link("a", "b"), self.link("b", "a")], "ARCHIVE_LINK_LOOP"),
                 ([self.link("a", "/outside")], "ARCHIVE_LINK_ESCAPE")]
        for links, code in cases:
            with self.subTest(code=code):
                self.make_archive(links)
                self.assertIn(code, self.codes(inspector.inspect(self.request)))

    def test_ambiguous_suffix_is_not_resolved_by_first_match(self):
        self.make_archive([self.item("other/lib/libxcamera.so", elf())])
        result = inspector.inspect(self.request)
        self.assertIn("TARGET_AMBIGUOUS", self.codes(result))
        self.assertNotIn("resolved_target", result["evidence"]["assets"]["camera_so"])

    def test_missing_target_and_architecture_mismatch_are_actionable(self):
        self.request["lidar_so"]["target"] = "missing/libxlidar.so"
        camera = self.root / "libxcamera.so"
        camera.write_bytes(elf(machine=62))
        self.request["camera_so"] = self.asset(camera, target="lib/libxcamera.so")
        result = inspector.inspect(self.request)
        self.assertTrue({"TARGET_MISSING", "ELF_TARGET_MISMATCH"} <= self.codes(result))
        issue = next(i for i in result["issues"] if i["code"] == "ELF_TARGET_MISMATCH")
        self.assertIn("AArch64 ELF64 小端", issue["next_action"])

    def test_matching_x86_package_and_replacements_still_fail_fixed_bench_target(self):
        header = elf(machine=62)
        self.make_archive(original_elf=header)
        for role in ("camera_so", "lidar_so", "m100_so"):
            path = Path(self.request[role]["remote_path"])
            path.write_bytes(header)
            self.request[role] = self.asset(path, target=self.request[role]["target"])
        result = inspector.inspect(self.request)
        self.assertEqual("NEEDS_INPUT", result["status"])
        rejected = {item["field"] for item in result["issues"] if item["code"] == "ELF_TARGET_MISMATCH"}
        self.assertEqual({"camera_so", "lidar_so", "m100_so"}, rejected)
        self.assertEqual(183, result["evidence"]["target_elf"]["machine"])

    def test_original_target_requires_aarch64_elf64_little_endian(self):
        for header in (elf(machine=62), elf(bits=1), elf(endian=2)):
            with self.subTest(header=header[:20]):
                self.make_archive(original_elf=header)
                result = inspector.inspect(self.request)
                self.assertEqual("NEEDS_INPUT", result["status"])
                self.assertIn("ELF_TARGET_MISMATCH", self.codes(result))
                self.assertFalse(any(c["check"] == "target_architecture" and c["status"] == "PASS"
                                     for c in result["checks"]))

    def test_replacement_target_rejects_32bit_and_big_endian_before_runtime(self):
        path = self.root / "libxcamera.so"
        for header in (elf(bits=1), elf(endian=2)):
            with self.subTest(header=header[:20]):
                path.write_bytes(header)
                self.request["camera_so"] = self.asset(path, target="lib/libxcamera.so")
                result = inspector.inspect(self.request)
                self.assertIn("ELF_TARGET_MISMATCH", self.codes(result))
                self.assertTrue(any("完整动态符号、ABI" in item for item in result["evidence"]["not_checked"]))

    def test_missing_limited_provider_symbol_blocks_with_provider_and_exact_symbol(self):
        missing = '_ZN6XLidar8callbackEv@LIDAR_2'
        self.symbol_check.return_value = {**MOCK_SYMBOLS, 'status': 'LIMITED_PROVIDER_SYMBOLS_MISSING',
            'providers': {'libxlidar.so': {'missing_symbols': [missing]}},
            'missing_symbols': [{'provider': 'libxlidar.so', 'symbol': missing}]}
        result = inspector.inspect(self.request)
        self.assertEqual('NEEDS_INPUT', result['status'])
        issue = next(item for item in result['issues'] if item['code'] == 'PROVIDER_SYMBOLS_MISSING')
        self.assertEqual('lidar_so', issue['field'])
        self.assertIn('libxlidar.so', issue['message'])
        self.assertIn(missing, issue['message'])

    def test_unavailable_or_unsupported_symbol_tool_is_never_reported_as_match(self):
        for code in ('SYMBOL_TOOL_UNAVAILABLE', 'SYMBOL_TABLE_UNSUPPORTED', 'SYMBOL_OUTPUT_LIMIT', 'SYMBOL_CHECK_TIMEOUT'):
            with self.subTest(code=code):
                self.symbol_check.side_effect = inspector.abi_symbols.SymbolCheckError(code, '未完成限定检查', '请管理员核验')
                result = inspector.inspect(self.request)
                self.assertEqual('NEEDS_INPUT', result['status'])
                self.assertIn(code, self.codes(result))
                self.assertEqual(code, result['evidence']['limited_provider_symbols']['status'])
                self.assertFalse(any(check['check'] == 'LIMITED_PROVIDER_SYMBOLS_MATCH' for check in result['checks']))

    def test_non_shared_object_is_rejected(self):
        camera = self.root / "libxcamera.so"
        camera.write_bytes(elf(kind=1))
        self.request["camera_so"] = self.asset(camera, target="lib/libxcamera.so")
        self.assertIn("NOT_SHARED_LIBRARY", self.codes(inspector.inspect(self.request)))

    def test_member_expanded_file_and_metadata_limits_stop_work(self):
        cases = [({"max_members": 1}, "ARCHIVE_MEMBER_LIMIT"),
                 ({"max_archive_expanded_bytes": 1000}, "ARCHIVE_SIZE_LIMIT"),
                 ({"max_file_bytes": 1}, "FILE_SIZE_LIMIT"),
                 ({"max_member_bytes": 10}, "ARCHIVE_MEMBER_SIZE_LIMIT")]
        for limits, code in cases:
            with self.subTest(code=code):
                self.request["limits"] = limits
                self.assertIn(code, self.codes(inspector.inspect(self.request)))
        metadata = tarfile.TarInfo("paxheader")
        metadata.type = tarfile.XHDTYPE
        metadata.size = 32
        self.make_archive([(metadata, b" " * 32)])
        self.request["limits"] = {"max_metadata_bytes": 16}
        self.assertIn("ARCHIVE_METADATA_LIMIT", self.codes(inspector.inspect(self.request)))

    def test_deadline_stops_inspection_without_retry(self):
        with patch.object(inspector.time, "monotonic", side_effect=[0] + [121] * 20):
            result = inspector.inspect(self.request)
        self.assertIn("TIME_LIMIT", self.codes(result))
        self.assertEqual({}, result["evidence"]["assets"])

    def test_corrupt_gzip_crc_is_not_static_pass(self):
        data = bytearray(self.app.read_bytes())
        data[-8] ^= 255
        self.app.write_bytes(data)
        self.request["package"] = self.asset(self.app)
        result = inspector.inspect(self.request)
        self.assertIn("ARCHIVE_INVALID", self.codes(result))

    def test_invalid_request_and_missing_file_return_structured_issues(self):
        for value in (None, {}, {"owner_root": str(self.root / "missing")}):
            with self.subTest(request=value):
                result = inspector.inspect(value)
                self.assertEqual("NEEDS_INPUT", result["status"])
                self.assertTrue(result["issues"])
        (self.root / "libxcamera.so").unlink()
        self.assertIn("FILE_UNAVAILABLE", self.codes(inspector.inspect(self.request)))

    def test_module_is_standalone_for_remote_python_stdin(self):
        encoded = base64.b64encode(json.dumps(self.request).encode()).decode()
        # Header-only synthetic fixtures need a mocked symbol reader. The
        # deployed caller injects the real, fixed administrator-owned module.
        module_source = Path(inspector.abi_symbols.__file__).read_text(encoding='utf-8')
        prelude = ("# -*- coding: utf-8 -*-\nimport sys,types\nm=types.ModuleType('bench_gateway_abi_symbols')\n"
                   "sys.modules['bench_gateway_abi_symbols']=m\nexec(" + repr(module_source) + ",m.__dict__)\n"
                   "m.check_libraries=lambda *args,**kwargs: " + repr(MOCK_SYMBOLS) + "\n")
        completed = subprocess.run([sys.executable, "-", encoded],
                                   input=prelude + Path(inspector.__file__).read_text(encoding="utf-8"),
                                   text=True, encoding="utf-8", capture_output=True, timeout=10,
                                   env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("PASS", json.loads(completed.stdout)["status"])


if __name__ == "__main__":
    unittest.main()
