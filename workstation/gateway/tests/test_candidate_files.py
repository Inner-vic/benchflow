import ast
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from bench_gateway import candidate_files as files
from bench_gateway import yaml_worker


class CandidateFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "owner"
        self.root.mkdir()
        self.owner = "ou_candidate_alice"
        self.preparation_id = "prep-binding-001"
        self.package = self.root / "package.tar.gz"
        self.original_yaml = b"camera:\n  enabled: true\n"
        self.archive()
        self.assets = {"package": self.asset(self.package, "package", "package-A"), "dependencies": []}
        for index, name in enumerate(("libxcamera.so", "libxlidar.so", "liburoad_m100.so")):
            path = self.root / name
            path.write_bytes(("replacement-%d" % index).encode())
            self.assets["dependencies"].append(self.asset(path, "dependency", name, target="lib/" + name))
        self.assets["dataset"] = {"id": "ds-1", "owner": self.owner, "kind": "dataset", "name": "data", "version": 1,
                                  "data": {"remote_path": str(self.root / "dataset"), "manifest": str(self.root / "manifest.json"), "sha256": "a" * 64, "scene_count": 18}}

    def archive(self, additional=None, root="livc"):
        with tarfile.open(self.package, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            item = tarfile.TarInfo("./")
            item.type = tarfile.DIRTYPE
            archive.addfile(item)
            entries = [("./" + root + "/config/main.yaml", self.original_yaml)]
            entries += [(root + "/lib/" + name, b"original-so") for name in ("libxcamera.so", "libxlidar.so", "liburoad_m100.so")]
            for name, data in entries + (additional or []):
                info = tarfile.TarInfo(name) if isinstance(name, str) else name
                if data is not None:
                    info.size = len(data)
                archive.addfile(info, io.BytesIO(data) if data is not None else None)

    def asset(self, path, kind, name, **data):
        return {"id": "asset-" + name, "owner": self.owner, "kind": kind, "name": name, "version": 1,
                "data": {"remote_path": str(path), "sha256": self.sha(path), **data}}

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def yaml(self):
        result = yaml_worker.run({"owner_root": str(self.root), "preparation_id": self.preparation_id,
                                  "package": self.assets["package"]["data"],
                                  "plan": {"operations": [{"operation": "set", "target": "main.yaml", "path": ["camera", "enabled"], "value_json": "false"}]}})
        self.assertEqual("PREPARED", result["status"], result)
        return result

    def freeze(self, yaml=None, assets=None):
        return files.freeze_inputs(str(self.root), self.preparation_id, assets or self.assets, yaml)

    def reject(self, code, callback):
        with self.assertRaises(files.InputValidationError) as raised:
            callback()
        self.assertEqual(code, raised.exception.code, str(raised.exception))

    @staticmethod
    def redigest(frozen):
        frozen["input_digest"] = hashlib.sha256(files.canonical({k: v for k, v in frozen.items() if k != "input_digest"})).hexdigest()

    @staticmethod
    def update_receipt(result, mutate):
        path = Path(result["receipt_path"])
        receipt = json.loads(path.read_text(encoding="utf-8"))
        mutate(receipt)
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        for key in ("schema_version", "scope", "preparation_id", "package_sha256", "changes", "outputs"):
            result[key] = copy.deepcopy(receipt[key])

    def test_freeze_and_revalidate_without_yaml_are_readonly(self):
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        frozen = self.freeze()
        self.assertEqual(self.owner, frozen["owner"])
        self.assertEqual(self.assets["package"]["data"]["sha256"], frozen["package"]["sha256"])
        self.assertEqual(3, len(frozen["replacements"]))
        self.assertIsNone(frozen["yaml_receipt"])
        self.assertEqual("ds-1", frozen["dataset_identity"]["id"])
        self.assertEqual("package-A", frozen["asset_identities"]["package"]["name"])
        self.assertIsNone(files.validate_inputs(str(self.root), frozen))
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(frozen, self.freeze())

    def test_yaml_freeze_binds_persisted_receipt_and_real_files(self):
        prepared = self.yaml()
        frozen = self.freeze({"status": "STATIC_PASSED", "yaml_preparation": prepared})
        item = next(item for item in frozen["replacements"] if item["kind"] == "yaml")
        self.assertEqual("config/main.yaml", item["target"])
        self.assertEqual(prepared["outputs"][0]["sha256"], item["sha256"])
        self.assertEqual(self.sha(Path(prepared["receipt_path"])), frozen["yaml_receipt"]["sha256"])
        self.assertIsNone(files.validate_inputs(str(self.root), frozen))

    def test_all_frozen_byte_changes_are_detected(self):
        prepared = self.yaml()
        frozen = self.freeze(prepared)
        paths = [self.package, Path(self.assets["dependencies"][0]["data"]["remote_path"]),
                 Path(prepared["outputs"][0]["path"]), Path(prepared["outputs"][0]["diff_path"]), Path(prepared["receipt_path"])]
        for path in paths:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                self.reject("HASH_MISMATCH", lambda: files.validate_inputs(str(self.root), frozen))
                path.write_bytes(original)

    def test_foreign_asset_and_private_dataset_owners_are_rejected(self):
        for role in ("dependency", "dataset"):
            with self.subTest(role=role):
                assets = copy.deepcopy(self.assets)
                asset = assets["dependencies"][0] if role == "dependency" else assets["dataset"]
                asset["owner"] = "ou_candidate_bob"
                self.reject("ASSET_OWNER", lambda: self.freeze(assets=assets))

    def test_declared_shared_catalog_dataset_is_frozen_for_later_authority_check(self):
        self.assets["dataset"].update(owner="shared", origin="dataset_catalog")
        self.assets["dataset"]["data"]["remote_path"] = "/not-read-by-candidate-files/shared"
        frozen = self.freeze()
        files.validate_inputs(str(self.root), frozen)
        self.assets["dataset"].pop("origin")
        self.reject("ASSET_OWNER", self.freeze)

    def test_other_owner_file_paths_and_hardlinks_are_rejected(self):
        outside = self.root.parent / "foreign.so"
        outside.write_bytes(b"secret")
        assets = copy.deepcopy(self.assets)
        assets["dependencies"][0]["data"].update(remote_path=str(outside), sha256=self.sha(outside))
        self.reject("OWNER_PATH", lambda: self.freeze(assets=assets))
        hardlink = self.root / "hardlink.so"
        try:
            os.link(str(outside), str(hardlink))
        except OSError:
            self.skipTest("hardlink unavailable")
        assets["dependencies"][0]["data"]["remote_path"] = str(hardlink)
        self.reject("NOT_REGULAR_FILE", lambda: self.freeze(assets=assets))

    def test_symlink_file_is_rejected(self):
        link = self.root / "alias.so"
        try:
            link.symlink_to(self.root / "libxcamera.so")
        except OSError:
            self.skipTest("symlink privilege unavailable")
        self.assets["dependencies"][0]["data"]["remote_path"] = str(link)
        self.reject("PATH_LINK", self.freeze)

    def test_dependency_target_conflicts_and_traversal_are_rejected(self):
        for value, code in [("lib/libxlidar.so", "TARGET_CONFLICT"), ("lib", "TARGET_CONFLICT"),
                            ("../escape", "UNSAFE_TARGET"), ("/app/x", "UNSAFE_TARGET"),
                            ("livc/lib/libxcamera.so", "UNSAFE_TARGET"), ("./lib/libxcamera.so", "UNSAFE_TARGET"),
                            ("config/main.yaml", "DEPENDENCY_TARGET")]:
            with self.subTest(value=value):
                assets = copy.deepcopy(self.assets)
                assets["dependencies"][0]["data"]["target"] = value
                self.reject(code, lambda: self.freeze(assets=assets))

    def test_target_must_exist_at_exact_app_relative_path(self):
        self.assets["dependencies"][0]["data"]["target"] = "libxcamera.so"
        self.reject("TARGET_MISSING", self.freeze)

    def test_yaml_submitted_result_and_persisted_receipt_must_agree(self):
        prepared = self.yaml()
        prepared["outputs"][0]["sha256"] = "0" * 64
        self.reject("RECEIPT_MISMATCH", lambda: self.freeze(prepared))

    def test_yaml_original_hash_must_match_package_bytes(self):
        prepared = self.yaml()
        self.update_receipt(prepared, lambda receipt: receipt["outputs"][0].update(before_sha256="0" * 64))
        self.reject("YAML_BASE_MISMATCH", lambda: self.freeze(prepared))

    def test_yaml_cannot_target_so_even_if_receipt_is_consistent(self):
        prepared = self.yaml()
        def mutate(receipt):
            receipt["outputs"][0]["target"] = "lib/libxcamera.so"
            receipt["changes"][0]["target"] = "lib/libxcamera.so"
        self.update_receipt(prepared, mutate)
        self.reject("YAML_TARGET", lambda: self.freeze(prepared))

    def test_receipt_must_be_in_this_preparation_batch(self):
        prepared = self.yaml()
        self.reject("RECEIPT_PATH", lambda: files.freeze_inputs(str(self.root), "prep-other", self.assets, prepared))
        moved = self.root / "receipt.json"
        moved.write_bytes(Path(prepared["receipt_path"]).read_bytes())
        prepared["receipt_path"] = str(moved)
        self.reject("RECEIPT_PATH", lambda: self.freeze(prepared))

    def test_yaml_output_must_live_alongside_receipt(self):
        prepared = self.yaml()
        moved = self.root / "candidate.yaml"
        moved.write_bytes(Path(prepared["outputs"][0]["path"]).read_bytes())
        self.update_receipt(prepared, lambda receipt: receipt["outputs"][0].update(path=str(moved)))
        self.reject("OUTPUT_PATH", lambda: self.freeze(prepared))

    def test_receipt_changes_inventory_and_duplicate_json_are_rejected(self):
        prepared = self.yaml()
        self.update_receipt(prepared, lambda receipt: receipt.update(changes=[]))
        self.reject("RECEIPT_INVENTORY", lambda: self.freeze(prepared))
        Path(prepared["receipt_path"]).write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
        self.reject("RECEIPT_INVALID", lambda: self.freeze(prepared))

    def test_missing_or_unfinished_yaml_preparation_is_not_accepted(self):
        prepared = self.yaml()
        prepared["status"] = "NEEDS_INPUT"
        self.reject("YAML_NOT_PREPARED", lambda: self.freeze(prepared))
        self.reject("YAML_NOT_PREPARED", lambda: self.freeze({"status": "NEEDS_INPUT", "yaml_preparation": prepared}))
        frozen = self.freeze({"status": "STATIC_PASSED", "yaml_preparation": {}})
        self.assertIsNone(frozen["yaml_receipt"])

    def test_dataset_and_asset_identity_changes_change_input_digest(self):
        original = self.freeze()
        self.assets["dataset"]["data"]["scene_count"] = 17
        changed = self.freeze()
        self.assertNotEqual(original["input_digest"], changed["input_digest"])
        self.assets["package"]["version"] = 2
        again = self.freeze()
        self.assertNotEqual(changed["input_digest"], again["input_digest"])
        original["dataset_identity"]["data"]["scene_count"] = 1
        self.reject("INPUT_DIGEST_MISMATCH", lambda: files.validate_inputs(str(self.root), original))

    def test_validate_checks_yaml_receipt_inventory_not_only_hash(self):
        prepared = self.yaml()
        frozen = self.freeze(prepared)
        yaml_item = next(item for item in frozen["replacements"] if item["kind"] == "yaml")
        copied = Path(yaml_item["path"]).with_name("other-candidate.yaml")
        copied.write_bytes(Path(yaml_item["path"]).read_bytes())
        yaml_item["path"] = str(copied)
        self.redigest(frozen)
        self.reject("RECEIPT_MISMATCH", lambda: files.validate_inputs(str(self.root), frozen))

    def test_validate_rejects_removed_yaml_receipt_even_with_recomputed_digest(self):
        frozen = self.freeze(self.yaml())
        frozen["yaml_receipt"] = None
        self.redigest(frozen)
        self.reject("RECEIPT_MISSING", lambda: files.validate_inputs(str(self.root), frozen))

    def test_validate_rechecks_yaml_original_when_receipt_hash_is_recomputed(self):
        prepared = self.yaml()
        frozen = self.freeze(prepared)
        self.update_receipt(prepared, lambda receipt: receipt["outputs"][0].update(before_sha256="0" * 64))
        frozen["yaml_receipt"]["sha256"] = self.sha(Path(prepared["receipt_path"]))
        self.redigest(frozen)
        self.reject("YAML_BASE_MISMATCH", lambda: files.validate_inputs(str(self.root), frozen))

    def test_archive_root_traversal_duplicates_and_special_files_are_rejected(self):
        special = tarfile.TarInfo("livc/device")
        special.type = tarfile.CHRTYPE
        for additional, root, code in [([], "other", "ARCHIVE_ROOT"), ([("../escape", b"x")], "livc", "UNSAFE_TARGET"),
                                       ([("livc/config/main.yaml", self.original_yaml)], "livc", "ARCHIVE_DUPLICATE_PATH"),
                                       ([(special, None)], "livc", "ARCHIVE_SPECIAL_FILE")]:
            with self.subTest(code=code):
                self.archive(additional, root)
                self.assets["package"]["data"]["sha256"] = self.sha(self.package)
                self.reject(code, self.freeze)

    def test_unrelated_safe_links_pass_but_target_parent_link_is_rejected(self):
        link = tarfile.TarInfo("livc/lib/unrelated.so")
        link.type = tarfile.SYMTYPE
        link.linkname = "libxcamera.so"
        self.archive([(link, None)])
        self.assets["package"]["data"]["sha256"] = self.sha(self.package)
        files.validate_inputs(str(self.root), self.freeze())
        link.name = "livc/lib"
        link.linkname = "somewhere"
        self.archive([(link, None)])
        self.assets["package"]["data"]["sha256"] = self.sha(self.package)
        self.reject("TARGET_LINK", self.freeze)

    def test_limits_are_bounded_and_invalid_preparation_id_is_rejected(self):
        for key, value, code in [("max_package_bytes", 1, "FILE_SIZE_LIMIT"), ("max_members", 1, "ARCHIVE_MEMBER_LIMIT"),
                                  ("max_archive_bytes", 10, "ARCHIVE_SIZE_LIMIT"), ("duration_seconds", 0.000001, "TIME_LIMIT")]:
            with self.subTest(key=key), patch.dict(files.LIMITS, {key: value}):
                self.reject(code, self.freeze)
        self.reject("PREPARATION_ID", lambda: files.freeze_inputs(str(self.root), "../escape", self.assets))

    def test_empty_package_and_bool_schema_alias_are_rejected(self):
        prepared = self.yaml()
        prepared["schema_version"] = True
        self.reject("RECEIPT_MISMATCH", lambda: self.freeze(prepared))
        with tarfile.open(self.package, "w:gz") as archive:
            directory = tarfile.TarInfo("./livc")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
        self.assets["package"]["data"]["sha256"] = self.sha(self.package)
        self.assets["dependencies"] = []
        self.reject("ARCHIVE_EMPTY", self.freeze)

    def test_standalone_standard_library_source_and_python38_syntax(self):
        source = Path(files.__file__).read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 8))
        namespace = {"__name__": "isolated_candidate_files"}
        exec(compile(source, "isolated_candidate_files", "exec"), namespace)
        frozen = namespace["freeze_inputs"](str(self.root), self.preparation_id, self.assets)
        self.assertIsNone(namespace["validate_inputs"](str(self.root), frozen))


if __name__ == "__main__":
    unittest.main()
