import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

from bench_gateway import yaml_worker as worker


class YamlWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "owner"
        self.root.mkdir()
        self.package = self.root / "bundle.app.tar.gz"
        self.original = b"camera:\n  enabled: true\n  frequency: 20\n  scale: 1.5\nitems:\n- name: first\n  enabled: false\n"
        self.request = {"owner_root": str(self.root), "preparation_id": "prep-123", "plan": {"operations": [self.operation()]}}
        self.archive()

    @staticmethod
    def operation(**extra):
        value = {"operation": "set", "target": "config.yaml", "path": ["camera", "enabled"], "value_json": "false", "source": ""}
        value.update(extra)
        return value

    def archive(self, entries=None):
        if entries is None:
            entries = [("livc/config/config.yaml", self.original)]
        with tarfile.open(self.package, "w:gz", format=tarfile.PAX_FORMAT) as output:
            root = tarfile.TarInfo("./")
            root.type = tarfile.DIRTYPE
            output.addfile(root)
            for name, data in entries:
                member = tarfile.TarInfo(name) if isinstance(name, str) else name
                if data is not None:
                    member.size = len(data)
                output.addfile(member, io.BytesIO(data) if data is not None else None)
        self.request["package"] = {"remote_path": str(self.package), "sha256": hashlib.sha256(self.package.read_bytes()).hexdigest()}

    def rejected(self, code=None, request=None):
        result = worker.run(request or self.request)
        self.assertEqual("NEEDS_INPUT", result["status"], result)
        self.assertEqual([], result["outputs"])
        self.assertNotIn("receipt_path", result)
        if code:
            self.assertIn(code, [entry["code"] for entry in result["issues"]], result)
        return result

    def test_scalar_set_creates_private_verified_candidate_diff_and_receipt(self):
        before = self.package.read_bytes()
        result = worker.run(self.request)
        self.assertEqual("PREPARED", result["status"], result)
        output = result["outputs"][0]
        candidate = Path(output["path"])
        self.assertFalse(yaml.safe_load(candidate.read_bytes())["camera"]["enabled"])
        self.assertEqual(hashlib.sha256(candidate.read_bytes()).hexdigest(), output["sha256"])
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), output["before_sha256"])
        self.assertIn("-  enabled: true", Path(output["diff_path"]).read_text())
        receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["outputs"], receipt["outputs"])
        self.assertEqual("config/config.yaml", output["target"])
        self.assertEqual(before, self.package.read_bytes())
        self.assertFalse(result["package_modified"])
        self.assertFalse(result["bench_modified"])
        self.assertFalse((self.root / "catalog.json").exists())
        self.assertIn((self.root / "preparations" / "prep-123").resolve(), candidate.parents)

    def test_comment_only_replacement_has_equal_semantic_identity(self):
        source=self.root/'comments.yaml';source.write_bytes(b'# comment\n'+self.original)
        self.request['plan']['operations']=[self.operation(operation='replace',path=[],source=str(source),value_json='')]
        out=worker.run(self.request)['outputs'][0]
        self.assertNotEqual(out['sha256'],out['before_sha256'])
        self.assertEqual(out['semantic_sha256'],out['before_semantic_sha256'])

    def test_fixed_replay_fields_do_not_create_a_false_effective_change(self):
        from bench_gateway.candidate_adapter import COMMON,configuration_overrides
        self.archive([('livc/'+COMMON,b'replay:\n  enable: false\nuser:\n  count: 1\n')])
        self.request['plan']['operations']=[self.operation(target=COMMON,path=['replay','enable'],value_json='true')]
        self.request['semantic_overrides']={COMMON:configuration_overrides('<run_id>')}
        out=worker.run(self.request)['outputs'][0]
        self.assertEqual(out['semantic_sha256'],out['before_semantic_sha256'])
        self.request['plan']['operations'].append(self.operation(target=COMMON,path=['user','count'],value_json='2'))
        out=worker.run(self.request)['outputs'][0]
        self.assertNotEqual(out['semantic_sha256'],out['before_semantic_sha256'])

    def test_list_index_and_multiple_updates_share_one_candidate(self):
        self.request["plan"]["operations"].append(self.operation(path=["items", "0", "name"], value_json='"updated"'))
        result = worker.run(self.request)
        self.assertEqual("PREPARED", result["status"], result)
        self.assertEqual(1, len(result["outputs"]))
        value = yaml.safe_load(Path(result["outputs"][0]["path"]).read_bytes())
        self.assertEqual("updated", value["items"][0]["name"])

    def test_replace_preserves_supplied_bytes_then_optional_set_applies(self):
        source = self.root / "replacement.yaml"
        data = b"# caller comment\ncamera:\n  enabled: false\nnew_section: 42\n"
        source.write_bytes(data)
        self.request["plan"]["operations"] = [self.operation(operation="replace", path=[], value_json="", source=str(source))]
        result = worker.run(self.request)
        self.assertEqual("PREPARED", result["status"], result)
        self.assertEqual(data, Path(result["outputs"][0]["path"]).read_bytes())
        self.request["plan"]["operations"].append(self.operation(path=["new_section"], value_json="43"))
        result2 = worker.run(self.request)
        self.assertEqual("PREPARED", result2["status"], result2)
        self.assertNotEqual(result["receipt_path"], result2["receipt_path"])
        self.assertEqual(43, yaml.safe_load(Path(result2["outputs"][0]["path"]).read_bytes())["new_section"])

    def test_type_mismatch_and_new_fields_do_not_write(self):
        for operation, code in [
            (self.operation(value_json="1"), "VALUE_TYPE_MISMATCH"),
            (self.operation(path=["camera", "frequency"], value_json="1.5"), "VALUE_TYPE_MISMATCH"),
            (self.operation(path=["camera", "new"], value_json="true"), "FIELD_MISSING"),
            (self.operation(path=["camera"], value_json='{"enabled":false,"frequency":1,"scale":1.5,"extra":1}'), "FIELD_STRUCTURE"),
            (self.operation(path=["items", "01", "name"], value_json='"x"'), "FIELD_MISSING"),
            (self.operation(path=["items", "-1", "name"], value_json='"x"'), "FIELD_MISSING"),
        ]:
            with self.subTest(operation=operation):
                self.request["plan"]["operations"] = [operation]
                self.rejected(code)
                self.assertFalse((self.root / "preparations").exists())

    def test_last_invalid_operation_prevents_all_candidate_writes(self):
        self.request["plan"]["operations"].append(self.operation(path=["missing"], value_json="1"))
        self.rejected("FIELD_MISSING")
        self.assertFalse((self.root / "preparations").exists())

    def test_ambiguous_basename_requires_unique_path(self):
        self.archive([("livc/config/config.yaml", self.original), ("livc/other/config.yaml", self.original)])
        self.rejected("TARGET_AMBIGUOUS")
        self.request["plan"]["operations"][0]["target"] = "config/config.yaml"
        self.assertEqual("PREPARED", worker.run(self.request)["status"])

    def test_only_livc_package_root_is_allowed_and_output_is_app_relative(self):
        self.request["plan"]["operations"][0]["target"] = "livc/config/config.yaml"
        result = worker.run(self.request)
        self.assertEqual("PREPARED", result["status"], result)
        self.assertEqual("config/config.yaml", result["outputs"][0]["target"])
        self.assertEqual("config/config.yaml", result["changes"][0]["target"])
        self.archive([("other/config/config.yaml", self.original)])
        self.rejected("ARCHIVE_ROOT")

    def test_hash_mismatch_and_other_owner_are_blocked(self):
        self.request["package"]["sha256"] = "0" * 64
        self.rejected("HASH_MISMATCH")
        self.request["package"]["remote_path"] = str(self.root.parent / "other-package.tar.gz")
        Path(self.request["package"]["remote_path"]).write_bytes(b"other owner's bytes")
        result = self.rejected("OWNER_PATH")
        self.assertNotIn("other owner's", json.dumps(result))

    def test_replacement_must_be_own_regular_file(self):
        source = self.root.parent / "private.yaml"
        source.write_text("secret: hidden", encoding="utf-8")
        self.request["plan"]["operations"] = [self.operation(operation="replace", path=[], source=str(source))]
        result = self.rejected("OWNER_PATH")
        self.assertNotIn("hidden", json.dumps(result))

    def test_yaml_alias_tags_duplicates_and_multidoc_are_rejected(self):
        examples = [
            (b"camera: &x {enabled: true}\ncopy: *x\n", "YAML_ALIAS"),
            (b"camera: !!python/object/apply:os.system ['echo unsafe']\n", "YAML_TAG"),
            (b"camera:\n  enabled: true\n  enabled: false\n", "YAML_DUPLICATE_KEY"),
            (b"camera: {enabled: true}\n---\nother: 1\n", "YAML_MULTIDOC"),
            (b"camera: {enabled: true}\ndate: 2026-10-22\n", "YAML_VALUE_TYPE"),
            (b"camera: {enabled: true}\nnumber: .nan\n", "YAML_VALUE_TYPE"),
            (b"camera: {enabled: true}\ntrue: 1\n", "YAML_KEY_TYPE"),
        ]
        for data, code in examples:
            with self.subTest(code=code):
                self.archive([("livc/config.yaml", data)])
                self.rejected(code)

    def test_replacement_receives_same_strict_yaml_validation(self):
        source = self.root / "bad.yaml"
        source.write_bytes(b"camera: {enabled: false, enabled: true}\n")
        self.request["plan"]["operations"] = [self.operation(operation="replace", path=[], source=str(source))]
        self.rejected("YAML_DUPLICATE_KEY")

    def test_archive_traversal_duplicate_and_special_members_are_rejected(self):
        device = tarfile.TarInfo("livc/device")
        device.type = tarfile.CHRTYPE
        for extra, code in [
            (("../escape", b"x"), "UNSAFE_PATH"),
            (("livc/config/config.yaml", self.original), "ARCHIVE_DUPLICATE_PATH"),
            ((device, None), "ARCHIVE_SPECIAL_FILE"),
        ]:
            with self.subTest(code=code):
                self.archive([("livc/config/config.yaml", self.original), extra])
                self.rejected(code)

    def test_linked_yaml_and_linked_parent_are_rejected(self):
        link = tarfile.TarInfo("livc/config/config.yaml")
        link.type = tarfile.SYMTYPE
        link.linkname = "../real.yaml"
        self.archive([("livc/real.yaml", self.original), (link, None)])
        self.rejected("TARGET_LINK")
        link.name = "livc/config"
        link.linkname = "real"
        self.archive([(link, None), ("livc/config/config.yaml", self.original)])
        self.rejected("TARGET_LINK")

    def test_unrelated_safe_so_link_is_allowed_but_escape_is_rejected(self):
        link = tarfile.TarInfo("livc/lib/libfoo.so")
        link.type = tarfile.SYMTYPE
        link.linkname = "libfoo.so.1"
        self.archive([("livc/config/config.yaml", self.original), ("livc/lib/libfoo.so.1", b"SO"), (link, None)])
        self.assertEqual("PREPARED", worker.run(self.request)["status"])
        link.linkname = "../../../outside"
        self.archive([("livc/config/config.yaml", self.original), (link, None)])
        self.rejected("ARCHIVE_LINK")

    def test_existing_output_link_is_rejected(self):
        destination = self.root.parent / "outside"
        destination.mkdir()
        try:
            (self.root / "preparations").symlink_to(destination, target_is_directory=True)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        self.rejected("PATH_LINK")
        self.assertEqual([], list(destination.iterdir()))

    def test_hardlinked_source_is_rejected(self):
        source = self.root / "source.yaml"
        source.write_bytes(self.original)
        import os
        try:
            os.link(str(source), str(self.root / "second.yaml"))
        except OSError:
            self.skipTest("hardlink unavailable")
        self.request["plan"]["operations"] = [self.operation(operation="replace", path=[], source=str(source))]
        self.rejected("NOT_REGULAR_FILE")

    def test_limits_for_bytes_members_nodes_depth_and_duration(self):
        for limits, code in [
            ({"max_package_bytes": 1}, "FILE_SIZE_LIMIT"),
            ({"max_yaml_bytes": 10}, "YAML_SIZE_LIMIT"),
            ({"max_members": 1}, "ARCHIVE_MEMBER_LIMIT"),
            ({"max_nodes": 2}, "YAML_COMPLEXITY"),
            ({"max_depth": 1}, "YAML_COMPLEXITY"),
            ({"duration_seconds": 0.00000001}, "TIME_LIMIT"),
            ({"max_yaml_bytes": LIMIT_TOO_HIGH}, "INVALID_LIMITS"),
            ({"max_yaml_bytes": 1.5}, "INVALID_LIMITS"),
        ]:
            with self.subTest(limits=limits):
                request = copy.deepcopy(self.request)
                request["limits"] = limits
                self.rejected(code, request)

    def test_invalid_json_and_nonfinite_numbers_are_rejected(self):
        for value in ("NaN", "Infinity", "1e9999", '{"x":1,"x":2}', "not json"):
            with self.subTest(value=value):
                self.request["plan"]["operations"][0]["value_json"] = value
                self.rejected()

    def test_dependency_failure_is_actionable(self):
        real_import = __import__
        def missing_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("missing")
            return real_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=missing_yaml):
            self.rejected("YAML_DEPENDENCY_MISSING")

    def test_malformed_archive_and_invalid_request_are_safe_failures(self):
        self.package.write_bytes(b"not a tar.gz")
        self.request["package"]["sha256"] = hashlib.sha256(self.package.read_bytes()).hexdigest()
        self.rejected()
        self.assertEqual("NEEDS_INPUT", worker.run(None)["status"])

    def test_source_runs_as_standalone_stdin(self):
        encoded = base64.b64encode(json.dumps(self.request).encode()).decode()
        completed = subprocess.run([sys.executable, "-", encoded], input=Path(worker.__file__).read_text(encoding="utf-8"), text=True, capture_output=True, timeout=30)
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("PREPARED", result["status"], result)
        self.assertTrue(Path(result["receipt_path"]).is_file())


LIMIT_TOO_HIGH = worker.LIMITS["max_yaml_bytes"] + 1


if __name__ == "__main__":
    unittest.main()
