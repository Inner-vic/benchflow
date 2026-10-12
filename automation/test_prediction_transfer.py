from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prediction_transfer import (
    EndpointRunner,
    TransferError,
    backfill_workstation_product_receipt,
    collect_predictions_to_workstation,
    inventory_digest,
    local_inventory,
    publish_predictions_to_lpai,
    resolve_collection,
    resolve_publication,
    transfer_predictions,
    verify_workstation_product,
    workstation_product_receipt_path,
    write_workstation_product_receipt,
)


def make_case(root: Path):
    source_root = root / "bench"
    source = source_root / "eval_pred_result" / "scene-a"
    (source / "0").mkdir(parents=True)
    (source / "0" / "pred.bin").write_bytes(b"prediction-0")
    (source / "1").mkdir()
    (source / "1" / "pred.bin").write_bytes(b"prediction-1")
    cache_root = root / "workstation-cache"
    destination_root = root / "lpai"
    destination_root.mkdir()
    job = {
        "run_id": "transfer-test",
        "predictions": {
            "transfer_set_id": "local-smoke",
            "source_path": str(source),
            "destination_path": str(destination_root / "transfer-test"),
        },
    }
    profile = {
        "prediction_transfer": {
            "sets": {
                "local-smoke": {
                    "source": {"transport": "local", "allowed_root": str(source_root)},
                    "workstation_cache_root": str(cache_root),
                    "destination": {"transport": "local", "allowed_root": str(destination_root)},
                }
            }
        }
    }
    return source, job, profile


def create_product(path: Path, *, producer_run_id: str = "producer-run") -> dict:
    path.mkdir(parents=True)
    (path / "pred.bin").write_bytes(b"prediction")
    product = path.resolve()
    return write_workstation_product_receipt(
        product,
        local_inventory(product),
        producer_run_id=producer_run_id,
        source="test-fixture",
    )


class FailSecondHopOnce(EndpointRunner):
    def __init__(self, transfer):
        super().__init__(transfer)
        self.failed = False

    def copy_from_local(self, source, endpoint, destination):
        if not self.failed:
            self.failed = True
            raise TransferError("injected LPAI outage")
        return super().copy_from_local(source, endpoint, destination)


class CorruptHopOne(EndpointRunner):
    def copy_to_local(self, endpoint, source, destination):
        super().copy_to_local(endpoint, source, destination)
        next(destination.rglob("*.bin")).write_bytes(b"corrupt")


class PredictionTransferTests(unittest.TestCase):
    def test_remote_inventory_uses_canonical_relative_path_order(self) -> None:
        runner = EndpointRunner({})
        files = [
            {"path": "scene/timeline.csv", "size": 1, "sha256": "a"},
            {"path": "scene/0/pred.npy", "size": 1, "sha256": "b"},
        ]
        with patch.object(runner, "_ssh", return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(files), stderr="",
        )):
            observed = runner.inventory({"transport": "ssh", "host_alias": "bench"}, "/app/output")
        self.assertEqual(["scene/0/pred.npy", "scene/timeline.csv"], [item["path"] for item in observed])

    def test_remote_python_can_be_configured_per_endpoint(self) -> None:
        runner = EndpointRunner({})
        self.assertEqual("python3", runner._python({}))
        self.assertEqual(
            "/platform/conda/bin/python3.12",
            runner._python({"python_binary": "/platform/conda/bin/python3.12"}),
        )
        with self.assertRaisesRegex(TransferError, "safe executable"):
            runner._python({"python_binary": "python3; touch /tmp/unsafe"})
        self.assertEqual(
            "/usr/bin/tar",
            runner._remote_executable({"tar_binary": "/usr/bin/tar"}, "tar_binary", "tar"),
        )
        with self.assertRaisesRegex(TransferError, "safe executable"):
            runner._remote_executable({"tar_binary": "tar --overwrite"}, "tar_binary", "tar")

    def test_two_hops_are_hash_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, job, profile = make_case(root)
            result = transfer_predictions(job, profile, root / "evidence", execute=True)
            self.assertEqual("PREDICTIONS_TRANSFERRED", result["state"])
            self.assertEqual(2, result["file_count"])
            self.assertFalse(result["windows_data_plane"])
            self.assertTrue((root / "lpai" / "transfer-test" / "0" / "pred.bin").is_file())
            self.assertTrue((root / "evidence" / "hop1-bench-workstation.json").is_file())
            self.assertTrue(
                workstation_product_receipt_path(
                    root / "workstation-cache" / "transfer-test" / "predictions"
                ).is_file()
            )

    def test_second_hop_retry_reuses_verified_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, job, profile = make_case(root)
            runner = FailSecondHopOnce(profile["prediction_transfer"])
            with self.assertRaises(TransferError):
                transfer_predictions(job, profile, root / "evidence", execute=True, runner=runner)
            for path in sorted(source.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                else:
                    path.rmdir()
            source.rmdir()
            result = transfer_predictions(job, profile, root / "evidence", execute=True, runner=runner)
            self.assertTrue(result["hop1_reused"])
            self.assertEqual("PREDICTIONS_TRANSFERRED", result["state"])

    def test_hash_mismatch_preserves_partial_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, job, profile = make_case(root)
            runner = CorruptHopOne(profile["prediction_transfer"])
            with self.assertRaisesRegex(TransferError, "hop-1 SHA-256"):
                transfer_predictions(job, profile, root / "evidence", execute=True, runner=runner)
            self.assertTrue((root / "workstation-cache" / "transfer-test" / "predictions.partial").is_dir())
            self.assertFalse((root / "lpai" / "transfer-test").exists())

    def test_verified_partial_can_complete_collection_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, job, profile = make_case(root)
            (source / "timeline.csv").write_text("time,value\n", encoding="utf-8")
            partial = root / "workstation-cache" / "transfer-test" / "predictions.partial"
            partial.parent.mkdir(parents=True)
            shutil.copytree(source, partial)
            result = collect_predictions_to_workstation(job, profile, root / "evidence", execute=True)
            self.assertEqual("WORKSTATION_PREDICTIONS_REUSED", result["state"])
            self.assertTrue(partial.is_dir())
            self.assertEqual(local_inventory(source), local_inventory(Path(result["workstation_product"])))

    def test_collection_can_complete_without_publishing_to_lpai(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, job, profile = make_case(root)
            result = collect_predictions_to_workstation(
                job, profile, root / "evidence", execute=True,
            )
            self.assertEqual("WORKSTATION_PREDICTIONS_COLLECTED", result["state"])
            self.assertTrue(
                (root / "workstation-cache" / "transfer-test" / "predictions" / "0" / "pred.bin").is_file()
            )
            self.assertTrue(Path(result["product_receipt"]).is_file())
            self.assertFalse((root / "lpai" / "transfer-test").exists())
            self.assertTrue(
                (root / "evidence" / "receipts" / "collect-workstation.json").is_file()
            )

    def test_collection_resolution_does_not_require_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, job, profile = make_case(root)
            del profile["prediction_transfer"]["sets"]["local-smoke"]["destination"]
            job["predictions"].pop("destination_path")
            resolved = resolve_collection(job, profile, root / "evidence")
            result = collect_predictions_to_workstation(
                job, profile, root / "evidence", execute=True,
            )
            self.assertEqual(
                (root / "bench" / "eval_pred_result" / "scene-a").resolve(),
                Path(resolved["source"]).resolve(),
            )
            self.assertEqual("WORKSTATION_PREDICTIONS_COLLECTED", result["state"])

    def test_publication_can_use_verified_workstation_source_without_bench(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, job, profile = make_case(root)
            collected = collect_predictions_to_workstation(
                job, profile, root / "collect-evidence", execute=True,
            )
            job["workflow"] = {
                "targets": ["lpai_predictions"],
                "sources": {"workstation_predictions": collected["workstation_product"]},
            }
            result = publish_predictions_to_lpai(
                job, profile, root / "publish-evidence", execute=True,
            )
            self.assertEqual("LPAI_PREDICTIONS_PUBLISHED", result["state"])
            self.assertEqual(collected["product_identity"], result["product_identity"])
            self.assertTrue((root / "lpai" / "transfer-test" / "1" / "pred.bin").is_file())
            self.assertTrue(
                (root / "publish-evidence" / "receipts" / "publish-lpai.json").is_file()
            )

    def test_publication_resolution_does_not_require_bench_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, job, profile = make_case(root)
            selected = profile["prediction_transfer"]["sets"]["local-smoke"]
            del selected["source"]
            job["predictions"].pop("source_path")
            shared = root / "workstation-cache" / "shared-product"
            create_product(shared)
            job["workflow"] = {
                "targets": ["lpai_predictions"],
                "sources": {"workstation_predictions": str(shared)},
            }
            resolved = resolve_publication(job, profile, root / "evidence")
            result = publish_predictions_to_lpai(
                job, profile, root / "evidence", execute=True,
            )
            self.assertEqual(shared.resolve(), resolved["cache"])
            self.assertEqual("LPAI_PREDICTIONS_PUBLISHED", result["state"])

    def test_upload_only_may_omit_bench_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, job, profile = make_case(root)
            shared = root / "workstation-cache" / "shared-product"
            create_product(shared)
            job["predictions"].pop("source_path")
            job["workflow"] = {
                "targets": ["lpai_predictions"],
                "sources": {"workstation_predictions": str(shared)},
            }
            result = publish_predictions_to_lpai(
                job, profile, root / "evidence", execute=True,
            )
            self.assertEqual("LPAI_PREDICTIONS_PUBLISHED", result["state"])

    def test_supplied_product_requires_a_producer_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, job, profile = make_case(root)
            shared = root / "workstation-cache" / "unverified"
            shared.mkdir(parents=True)
            (shared / "pred.bin").write_bytes(b"prediction")
            job["workflow"] = {
                "targets": ["lpai_predictions"],
                "sources": {"workstation_predictions": str(shared)},
            }
            with self.assertRaisesRegex(TransferError, "no producer receipt"):
                publish_predictions_to_lpai(
                    job, profile, root / "evidence", execute=True,
                )

    def test_supplied_product_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, job, profile = make_case(root)
            shared = root / "workstation-cache" / "shared-product"
            create_product(shared)
            (shared / "pred.bin").write_bytes(b"tampered")
            job["workflow"] = {
                "targets": ["lpai_predictions"],
                "sources": {"workstation_predictions": str(shared)},
            }
            with self.assertRaisesRegex(TransferError, "does not match"):
                publish_predictions_to_lpai(
                    job, profile, root / "evidence", execute=True,
                )

    def test_explicit_backfill_requires_matching_verified_hop1_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache_root = root / "workstation-cache"
            shared = cache_root / "legacy"
            shared.mkdir(parents=True)
            (shared / "pred.bin").write_bytes(b"prediction")
            files = local_inventory(shared)
            evidence = root / "hop1-bench-workstation.json"
            evidence.write_text(json.dumps({
                "state": "HOP1_VERIFIED",
                "files": files,
                "inventory_sha256": inventory_digest(files),
                "cache": str(shared.resolve()),
            }), encoding="utf-8")

            receipt = backfill_workstation_product_receipt(
                shared,
                cache_root=cache_root,
                producer_run_id="legacy-run",
                hop1_evidence=evidence,
            )

            self.assertEqual("explicit-legacy-hop1-backfill-v1", receipt["provenance"]["method"])
            self.assertEqual(receipt, verify_workstation_product(shared.resolve(), files))

    def test_backfill_rejects_arbitrary_directory_or_mismatched_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache_root = root / "workstation-cache"
            shared = cache_root / "legacy"
            shared.mkdir(parents=True)
            (shared / "pred.bin").write_bytes(b"prediction")
            evidence = root / "hop1-bench-workstation.json"
            evidence.write_text(json.dumps({
                "state": "HOP1_VERIFIED",
                "files": [],
                "inventory_sha256": "bad",
                "cache": str(shared.resolve()),
            }), encoding="utf-8")
            with self.assertRaisesRegex(TransferError, "does not match"):
                backfill_workstation_product_receipt(
                    shared,
                    cache_root=cache_root,
                    producer_run_id="legacy-run",
                    hop1_evidence=evidence,
                )
            self.assertFalse(workstation_product_receipt_path(shared.resolve()).exists())

    def test_plan_performs_no_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _source, job, profile = make_case(root)
            result = transfer_predictions(job, profile, root / "evidence", execute=False)
            self.assertEqual("TRANSFER_PLANNED", result["state"])
            self.assertFalse(result["mutations_performed"])

    def test_unknown_remote_copy_protocol_is_rejected(self) -> None:
        runner = EndpointRunner({})
        with self.assertRaisesRegex(TransferError, "unsupported destination copy_protocol"):
            runner.copy_from_local(
                Path("/not-read"),
                {"transport": "ssh", "host_alias": "lpai", "copy_protocol": "unknown"},
                "/lpai/not-written",
            )


if __name__ == "__main__":
    unittest.main()
