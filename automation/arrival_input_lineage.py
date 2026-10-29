"""Bind arrival inputs to existing project prediction/delivery receipts."""
from pathlib import Path, PurePosixPath

from prediction_transfer import inventory_digest
from workflow_receipts import content_identity


def verify_upstream(source, scenes, supplied, resolve, load, sha, scene_sources=None):
    """Verify receipt consistency and selected files; never contact the bench."""
    names = {"task_index", "producer_receipt", "transfer_receipt", "run_state", "restore_receipt"}
    specs = source.get("upstream")
    if not isinstance(specs, dict) or set(specs) != names:
        raise ValueError("source.upstream must bind the existing task, producer, collection, run and restore receipts")
    documents, evidence = {}, {}
    for key, spec in specs.items():
        p = resolve(spec["path"])
        digest = sha(p)
        if digest != spec["sha256"]:
            raise ValueError(f"upstream receipt hash mismatch: {key}")
        documents[key] = load(p)
        evidence[key] = {"path": str(p), "sha256": digest}
    index, producer, transfer, state, restore = (documents[k] for k in ("task_index", "producer_receipt", "transfer_receipt", "run_state", "restore_receipt"))
    run, product = source["run_id"], source["product_identity"]
    if (index.get("state") != "PASS" or producer.get("state") not in {"SUCCEEDED", "REUSED_VERIFIED"}
            or producer.get("product_type") != "workstation_predictions"
            or transfer.get("state") != "WORKSTATION_PREDICTIONS_COLLECTED"
            or state.get("state") != "COMPLETED_TO_REQUESTED_GOAL" or restore.get("restore_ok") is not True):
        raise ValueError("upstream replay/delivery/restoration is not complete")
    if (any(d.get("run_id") != run for d in (index, transfer, state, restore))
            or producer.get("producer_run_id") != run or transfer.get("producer_run_id") != run):
        raise ValueError("upstream producer Run ID mismatch")
    files = producer.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("upstream producer inventory is missing")
    inventory = inventory_digest(files)
    identity = content_identity({"product_type": "workstation_predictions", "files": files})
    if (identity != product or any(d.get("product_identity") != product for d in (index, producer, transfer))
            or any(d.get("inventory_sha256") != inventory for d in (index, producer, transfer))
            or any(d.get("file_count") != len(files) for d in (index, transfer))):
        raise ValueError("upstream prediction product identity/inventory mismatch")
    if (producer.get("workstation_product") != index.get("canonical_product")
            or transfer.get("workstation_product") != index.get("canonical_product")
            or transfer.get("product_receipt") != index.get("producer_receipt")):
        raise ValueError("upstream prediction product path mismatch")
    by_path = {}
    for item in files:
        p = PurePosixPath(item["path"])
        if p.is_absolute() or ".." in p.parts or len(p.parts) < 2 or str(p) in by_path:
            raise ValueError("unsafe/duplicate upstream prediction path")
        by_path[str(p)] = item
    entries = index.get("scenes", [])
    scene_map = {s["scene"]: s for s in entries}
    if len(scene_map) != len(entries) or index.get("scene_count") != len(entries):
        raise ValueError("upstream task scene inventory mismatch")
    if any(s.name not in scene_map for s in scenes):
        raise ValueError("selected scene is not in the delivered task")
    truth_root = PurePosixPath(source["truth_root"])
    if not truth_root.is_absolute() or ".." in truth_root.parts:
        raise ValueError("source.truth_root must be the explicit absolute workstation truth root")
    for scene in scenes:
        direct = next((s for s in (scene_sources or []) if s["scene"] == scene.name), None)
        timeline_path = Path(direct["timeline"]) if direct else scene/"sus_preview_timeline.csv"
        entry = scene_map[scene.name]
        if entry.get("run_id") != run:
            raise ValueError("task scene Run ID mismatch")
        rel = scene.name+"/sus_preview_timeline.csv"
        item = by_path.get(rel)
        actual = supplied[timeline_path.resolve()]
        if item is None or item["sha256"] != actual["sha256"] or item["size"] != timeline_path.stat().st_size:
            raise ValueError(f"arrival timeline is not the delivered prediction: {scene.name}")
        allowed = {str(PurePosixPath(entry["task_copy"])/"sus_preview_timeline.csv"), str(PurePosixPath(index["canonical_product"])/rel)}
        if actual.get("source") not in allowed:
            raise ValueError("timeline download source does not match the task/product path")
        for wheel in ("FL", "FR"):
            name = f"s_InDatCalcFAS_WhlJerk{wheel}_mps3.json"
            jerk_path = Path(direct["jerk"][wheel.lower()]) if direct else scene/name
            actual = supplied[jerk_path.resolve()]
            expected = str(truth_root/scene.name/"original/xcp/BZCU"/name)
            if actual.get("source") != expected:
                raise ValueError("jerk source does not match the selected scene and truth root")
    return {"state": "VERIFIED", "evidence": evidence, "producer_run_id": run,
            "product_identity": identity, "inventory_sha256": inventory, "producer_file_count": len(files),
            "selected_scenes": [s.name for s in scenes], "selected_timeline_files_verified": len(scenes),
            "full_product_rehashed_locally": False, "truth_root": str(truth_root),
            "dependency": "delivered prediction timeline + same-scene XCP jerk/speed; no trajectory/vision evaluation report required"}
