#!/usr/bin/env python3
"""Hash-verified bench -> workstation -> LPAI prediction transfer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ssh_transport import command as ssh_transport_command
from ssh_transport import environment as ssh_environment
from ssh_transport import normalize_auth_mode
from workflow_receipts import content_identity, write_receipt


WORKSTATION_PRODUCT_RECEIPT_SUFFIX = ".bench-eval-product.json"
SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
SAFE_ALIAS = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_REMOTE_EXECUTABLE = re.compile(r"^(?:/[A-Za-z0-9._/-]+|[A-Za-z0-9._-]+)$")
REMOTE_INVENTORY = r"""import hashlib,json,os,stat,sys
root=os.path.realpath(sys.argv[1]); out=[]
if not os.path.isdir(root): raise SystemExit(3)
for base,dirs,files in os.walk(root):
 dirs.sort(); files.sort()
 for name in files:
  p=os.path.join(base,name); rel=os.path.relpath(p,root).replace(os.sep,'/')
  if os.path.islink(p) or not stat.S_ISREG(os.lstat(p).st_mode): raise SystemExit(4)
  h=hashlib.sha256()
  with open(p,'rb') as f:
   for b in iter(lambda:f.read(1048576),b''): h.update(b)
  out.append({'path':rel,'size':os.path.getsize(p),'sha256':h.hexdigest()})
print(json.dumps(out,separators=(',',':')))
"""
REMOTE_FINALIZE = r"""import os,sys
src=os.path.realpath(sys.argv[1]); dst=os.path.realpath(sys.argv[2])
if os.path.exists(dst): raise SystemExit(5)
os.rename(src,dst)
"""
REMOTE_EXISTS = "import os,sys; raise SystemExit(0 if os.path.exists(sys.argv[1]) else 1)"
REMOTE_PREPARE = "import os,sys; os.makedirs(os.path.dirname(os.path.realpath(sys.argv[1])),exist_ok=True)"
REMOTE_TAR_RECEIVER = r"""import os,subprocess,sys
dst=os.path.realpath(sys.argv[1]); os.mkdir(dst)
raise SystemExit(subprocess.run([sys.argv[2],'-C',dst,'-xf','-']).returncode)
"""


class TransferError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransferError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TransferError(f"JSON must contain an object: {path}")
    return value


def ensure_below(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    allowed = root.expanduser().resolve()
    try:
        relative = resolved.relative_to(allowed)
    except ValueError as exc:
        raise TransferError(f"{label} must stay below {allowed}") from exc
    if not relative.parts:
        raise TransferError(f"{label} must not equal its allowed root")
    return resolved


def ensure_remote_path(path: str, root: str, label: str) -> str:
    if not SAFE_REMOTE_PATH.fullmatch(path) or not SAFE_REMOTE_PATH.fullmatch(root):
        raise TransferError(f"{label} must be an absolute safe ASCII path")
    normalized = posixpath.normpath(path)
    allowed = posixpath.normpath(root)
    if normalized == allowed or not normalized.startswith(allowed.rstrip("/") + "/"):
        raise TransferError(f"{label} must stay below {allowed}")
    return normalized


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_inventory(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        raise TransferError(f"prediction directory does not exist: {root}")
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise TransferError(f"symbolic links are forbidden in predictions: {path}")
        if path.is_file():
            result.append({
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": hash_file(path),
            })
        elif not path.is_dir():
            raise TransferError(f"non-regular prediction entry is forbidden: {path}")
    if not result:
        raise TransferError(f"prediction directory contains no files: {root}")
    return result


def inventory_digest(files: list[dict[str, Any]]) -> str:
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EndpointRunner:
    def __init__(self, transfer: dict[str, Any]):
        self.ssh_binary = str(transfer.get("ssh_binary") or "ssh")
        self.rsync_binary = str(transfer.get("rsync_binary") or "rsync")
        self.tar_binary = str(transfer.get("tar_binary") or "tar")
        args = transfer.get("ssh_args", ["-o", "BatchMode=yes"])
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise TransferError("prediction_transfer.ssh_args must be a string array")
        self.ssh_args = args

    def _auth_mode(self, endpoint: dict[str, Any]) -> str:
        try:
            return normalize_auth_mode(endpoint.get("ssh_auth_mode"))
        except ValueError as exc:
            raise TransferError(str(exc)) from exc

    def _rsync_ssh(self, endpoint: dict[str, Any]) -> str:
        return shlex.join(
            ssh_transport_command(self.ssh_binary, self.ssh_args, self._auth_mode(endpoint))
        )

    @staticmethod
    def _python(endpoint: dict[str, Any]) -> str:
        executable = endpoint.get("python_binary", "python3")
        if not isinstance(executable, str) or not SAFE_REMOTE_EXECUTABLE.fullmatch(executable):
            raise TransferError("endpoint python_binary must be a safe executable name or absolute path")
        return executable

    @staticmethod
    def _remote_executable(endpoint: dict[str, Any], field: str, default: str) -> str:
        executable = endpoint.get(field, default)
        if not isinstance(executable, str) or not SAFE_REMOTE_EXECUTABLE.fullmatch(executable):
            raise TransferError(f"endpoint {field} must be a safe executable name or absolute path")
        return executable

    def _ssh_command(
        self, alias: str, argv: list[str], endpoint: dict[str, Any]
    ) -> list[str]:
        if not SAFE_ALIAS.fullmatch(alias):
            raise TransferError(f"unsafe SSH alias: {alias}")
        return [
            *ssh_transport_command(self.ssh_binary, self.ssh_args, self._auth_mode(endpoint)),
            alias, "--", shlex.join(argv),
        ]

    def _ssh(
        self, alias: str, argv: list[str], endpoint: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._ssh_command(alias, argv, endpoint),
            stdin=subprocess.DEVNULL, text=True, capture_output=True, shell=False,
            env=ssh_environment(self._auth_mode(endpoint)),
        )

    def inventory(self, endpoint: dict[str, Any], path: str) -> list[dict[str, Any]]:
        if endpoint["transport"] == "local":
            return local_inventory(Path(path))
        completed = self._ssh(
            endpoint["host_alias"], [self._python(endpoint), "-c", REMOTE_INVENTORY, path], endpoint
        )
        if completed.returncode != 0:
            raise TransferError(f"remote inventory failed on {endpoint['host_alias']}: {completed.stderr.strip()}")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise TransferError("remote inventory returned invalid JSON") from exc
        if not isinstance(value, list) or not value:
            raise TransferError("remote prediction inventory is empty")
        # os.walk reports files in a parent directory before its child
        # directories. local_inventory sorts complete relative paths instead.
        # Canonicalize before byte-for-byte comparison and receipt hashing.
        return sorted(value, key=lambda item: item["path"])

    def exists(self, endpoint: dict[str, Any], path: str) -> bool:
        if endpoint["transport"] == "local":
            return Path(path).exists()
        completed = self._ssh(
            endpoint["host_alias"], [self._python(endpoint), "-c", REMOTE_EXISTS, path], endpoint
        )
        if completed.returncode not in (0, 1):
            raise TransferError(f"remote destination existence check failed: {completed.stderr.strip()}")
        return completed.returncode == 0

    def prepare_parent(self, endpoint: dict[str, Any], path: str) -> None:
        if endpoint["transport"] == "local":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            return
        completed = self._ssh(
            endpoint["host_alias"], [self._python(endpoint), "-c", REMOTE_PREPARE, path], endpoint
        )
        if completed.returncode != 0:
            raise TransferError(f"remote destination parent creation failed: {completed.stderr.strip()}")

    def copy_to_local(self, endpoint: dict[str, Any], source: str, destination: Path) -> None:
        if endpoint["transport"] == "local":
            shutil.copytree(Path(source), destination)
            return
        completed = subprocess.run(
            [self.rsync_binary, "-a", "--partial", "--protect-args", "-e", self._rsync_ssh(endpoint), "--",
             f"{endpoint['host_alias']}:{source.rstrip('/')}/", str(destination) + os.sep],
            stdin=subprocess.DEVNULL, text=True, capture_output=True, shell=False,
            env=ssh_environment(self._auth_mode(endpoint)),
        )
        if completed.returncode != 0:
            raise TransferError(f"bench to workstation rsync failed: {completed.stderr.strip()}")

    def copy_from_local(self, source: Path, endpoint: dict[str, Any], destination: str) -> None:
        if endpoint["transport"] == "local":
            shutil.copytree(source, Path(destination))
            return
        copy_protocol = endpoint.get("copy_protocol", "rsync")
        if copy_protocol == "tar":
            remote_tar = self._remote_executable(endpoint, "tar_binary", "tar")
            source_process = subprocess.Popen(
                [self.tar_binary, "-C", str(source), "-cf", "-", "."],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            assert source_process.stdout is not None
            completed = subprocess.run(
                self._ssh_command(
                    endpoint["host_alias"],
                    [self._python(endpoint), "-c", REMOTE_TAR_RECEIVER, destination, remote_tar],
                    endpoint,
                ),
                stdin=source_process.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=ssh_environment(self._auth_mode(endpoint)),
            )
            source_process.stdout.close()
            source_stderr = source_process.stderr.read() if source_process.stderr is not None else b""
            source_returncode = source_process.wait()
            if source_returncode != 0 or completed.returncode != 0:
                details = (source_stderr + completed.stderr).decode("utf-8", errors="replace").strip()
                raise TransferError(f"workstation to LPAI tar stream failed: {details}")
            return
        if copy_protocol != "rsync":
            raise TransferError(f"unsupported destination copy_protocol: {copy_protocol}")
        completed = subprocess.run(
            [self.rsync_binary, "-a", "--partial", "--protect-args", "-e", self._rsync_ssh(endpoint), "--",
             str(source) + os.sep, f"{endpoint['host_alias']}:{destination.rstrip('/')}/"],
            stdin=subprocess.DEVNULL, text=True, capture_output=True, shell=False,
            env=ssh_environment(self._auth_mode(endpoint)),
        )
        if completed.returncode != 0:
            raise TransferError(f"workstation to LPAI rsync failed: {completed.stderr.strip()}")

    def finalize(self, endpoint: dict[str, Any], partial: str, final: str) -> None:
        if endpoint["transport"] == "local":
            os.replace(partial, final)
            return
        completed = self._ssh(
            endpoint["host_alias"],
            [self._python(endpoint), "-c", REMOTE_FINALIZE, partial, final],
            endpoint,
        )
        if completed.returncode != 0:
            raise TransferError(f"remote destination finalization failed: {completed.stderr.strip()}")


def endpoint_path(endpoint: dict[str, Any], value: str, label: str) -> str:
    transport = endpoint.get("transport")
    if transport not in ("local", "ssh"):
        raise TransferError(f"{label} endpoint transport must be local or ssh")
    root = endpoint.get("allowed_root")
    if not isinstance(root, str):
        raise TransferError(f"{label} endpoint requires allowed_root")
    if transport == "ssh":
        if not isinstance(endpoint.get("host_alias"), str):
            raise TransferError(f"{label} SSH endpoint requires host_alias")
        return ensure_remote_path(value, root, label)
    return str(ensure_below(Path(value), Path(root), label))


def _resolve_transfer_set(
    job: dict[str, Any], profile: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    request = job.get("predictions")
    if not isinstance(request, dict):
        raise TransferError("job is missing predictions")
    set_id = request.get("transfer_set_id")
    transfer = profile.get("prediction_transfer")
    sets = transfer.get("sets") if isinstance(transfer, dict) else None
    selected = sets.get(set_id) if isinstance(sets, dict) else None
    if not isinstance(selected, dict):
        raise TransferError(f"unknown predictions.transfer_set_id: {set_id}")
    return request, transfer, selected


def _workstation_source(job: dict[str, Any]) -> str | None:
    workflow = job.get("workflow")
    sources = workflow.get("sources") if isinstance(workflow, dict) else None
    source = sources.get("workstation_predictions") if isinstance(sources, dict) else None
    return str(source) if source not in (None, "") else None


def _resolve_cache(
    job: dict[str, Any], selected: dict[str, Any], workstation_source: str | None,
) -> Path:
    cache_root = Path(str(selected.get("workstation_cache_root") or ""))
    if not cache_root.is_absolute():
        raise TransferError("workstation_cache_root must be an absolute path")
    if workstation_source is None:
        return ensure_below(
            cache_root / str(job.get("run_id")) / "predictions",
            cache_root,
            "workstation cache",
        )
    return ensure_below(
        Path(workstation_source), cache_root, "workstation prediction source",
    )


def resolve_collection(
    job: dict[str, Any], profile: dict[str, Any], run_dir: Path,
) -> dict[str, Any]:
    request, transfer, selected = _resolve_transfer_set(job, profile)
    workstation_source = _workstation_source(job)
    source_endpoint = selected.get("source")
    if workstation_source is None:
        if not isinstance(source_endpoint, dict):
            raise TransferError("collection transfer set requires a source endpoint")
        source = endpoint_path(
            source_endpoint,
            str(request.get("source_path") or ""),
            "prediction source",
        )
    else:
        source = None
    return {
        "transfer": transfer,
        "source_endpoint": source_endpoint,
        "source": source,
        "cache": _resolve_cache(job, selected, workstation_source),
        "evidence_dir": run_dir.expanduser().resolve(),
        "workstation_source_supplied": workstation_source is not None,
    }


def resolve_publication(
    job: dict[str, Any], profile: dict[str, Any], run_dir: Path,
) -> dict[str, Any]:
    request, transfer, selected = _resolve_transfer_set(job, profile)
    destination_endpoint = selected.get("destination")
    if not isinstance(destination_endpoint, dict):
        raise TransferError("publication transfer set requires a destination endpoint")
    workstation_source = _workstation_source(job)
    return {
        "transfer": transfer,
        "destination_endpoint": destination_endpoint,
        "cache": _resolve_cache(job, selected, workstation_source),
        "destination": endpoint_path(
            destination_endpoint,
            str(request.get("destination_path") or ""),
            "LPAI destination",
        ),
        "evidence_dir": run_dir.expanduser().resolve(),
        "workstation_source_supplied": workstation_source is not None,
    }


def resolve_transfer(job: dict[str, Any], profile: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Resolve the legacy two-hop route without changing its required inputs."""
    collection = resolve_collection(job, profile, run_dir)
    publication = resolve_publication(job, profile, run_dir)
    return {**collection, **publication}


def _inventory_summary(files: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_count": len(files),
        "total_bytes": sum(int(item["size"]) for item in files),
        "inventory_sha256": inventory_digest(files),
    }


def workstation_product_receipt_path(product: Path) -> Path:
    return product.with_name(product.name + WORKSTATION_PRODUCT_RECEIPT_SUFFIX)


def write_workstation_product_receipt(
    product: Path,
    files: list[dict[str, Any]],
    *,
    producer_run_id: str,
    source: str,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    receipt = {
        "schema_version": 1,
        "product_type": "workstation_predictions",
        "state": "SUCCEEDED",
        "producer_run_id": producer_run_id,
        "workstation_product": str(product),
        "source": source,
        "inventory_sha256": inventory_digest(files),
        "product_identity": content_identity({
            "product_type": "workstation_predictions",
            "files": files,
        }),
        "files": files,
    }
    if provenance is not None:
        receipt["provenance"] = provenance
    path = workstation_product_receipt_path(product)
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pending.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(pending, path)
    return receipt


def verify_workstation_product(
    product: Path,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    path = workstation_product_receipt_path(product)
    if not path.is_file():
        raise TransferError(
            "supplied workstation prediction product has no producer receipt; "
            "explicitly backfill and verify it before reuse"
        )
    receipt = load_json(path)
    expected_identity = content_identity({
        "product_type": "workstation_predictions",
        "files": files,
    })
    if (
        receipt.get("schema_version") != 1
        or receipt.get("product_type") != "workstation_predictions"
        or receipt.get("state") not in {"SUCCEEDED", "REUSED_VERIFIED"}
        or receipt.get("workstation_product") != str(product)
        or not receipt.get("producer_run_id")
        or receipt.get("files") != files
        or receipt.get("inventory_sha256") != inventory_digest(files)
        or receipt.get("product_identity") != expected_identity
    ):
        raise TransferError(
            "supplied workstation prediction product does not match its producer receipt"
        )
    return receipt


def backfill_workstation_product_receipt(
    product: Path,
    *,
    cache_root: Path,
    producer_run_id: str,
    hop1_evidence: Path,
) -> dict[str, Any]:
    """Create provenance only from explicit, hash-verified legacy hop-1 evidence."""
    resolved_product = ensure_below(product, cache_root, "workstation prediction product")
    if not producer_run_id:
        raise TransferError("producer_run_id is required for workstation product backfill")
    evidence_path = hop1_evidence.expanduser().resolve()
    evidence = load_json(evidence_path)
    files = local_inventory(resolved_product)
    if (
        evidence.get("state") != "HOP1_VERIFIED"
        or evidence.get("cache") != str(resolved_product)
        or evidence.get("files") != files
        or evidence.get("inventory_sha256") != inventory_digest(files)
    ):
        raise TransferError(
            "legacy hop-1 evidence does not match the workstation prediction product"
        )
    receipt_path = workstation_product_receipt_path(resolved_product)
    if receipt_path.exists():
        return verify_workstation_product(resolved_product, files)
    return write_workstation_product_receipt(
        resolved_product,
        files,
        producer_run_id=producer_run_id,
        source="legacy-verified-hop1-evidence",
        provenance={
            "method": "explicit-legacy-hop1-backfill-v1",
            "hop1_evidence": str(evidence_path),
            "hop1_evidence_sha256": hash_file(evidence_path),
        },
    )


def collect_predictions_to_workstation(
    job: dict[str, Any], profile: dict[str, Any], run_dir: Path, *, execute: bool,
    runner: EndpointRunner | None = None,
) -> dict[str, Any]:
    plan = resolve_collection(job, profile, run_dir)
    public = {
        "state": "WORKSTATION_COLLECTION_PLANNED" if not execute else "COLLECTING_WORKSTATION_PREDICTIONS",
        "run_id": job.get("run_id"),
        "route": "bench_to_shared_workstation_product",
        "source": plan["source"],
        "workstation_product": str(plan["cache"]),
        "windows_data_plane": False,
        "mutations_performed": False,
    }
    if not execute:
        return public
    evidence = plan["evidence_dir"]
    evidence.mkdir(parents=True, exist_ok=True)
    cache = plan["cache"]
    if plan["workstation_source_supplied"]:
        source_files = local_inventory(cache)
        product_receipt = verify_workstation_product(cache, source_files)
        reused = True
    else:
        engine = runner or EndpointRunner(plan["transfer"])
        hop1_record = evidence / "hop1-bench-workstation.json"
        if hop1_record.is_file() and cache.is_dir():
            source_files = load_json(hop1_record).get("files")
            if not isinstance(source_files, list) or local_inventory(cache) != source_files:
                raise TransferError("existing workstation cache does not match persisted hop-1 evidence")
            if workstation_product_receipt_path(cache).is_file():
                product_receipt = verify_workstation_product(cache, source_files)
            else:
                product_receipt = write_workstation_product_receipt(
                    cache,
                    source_files,
                    producer_run_id=str(job.get("run_id") or ""),
                    source=str(plan["source"]),
                )
            reused = True
        else:
            if cache.exists():
                raise TransferError("workstation cache already exists; refusing to overwrite")
            source_files = engine.inventory(plan["source_endpoint"], plan["source"])
            partial = cache.with_name(cache.name + ".partial")
            partial.parent.mkdir(parents=True, exist_ok=True)
            if partial.exists():
                # A prior attempt may have copied every byte but failed only
                # because its source and destination inventories had different
                # ordering. Keep that partial untouched as failure evidence.
                if local_inventory(partial) != source_files:
                    raise TransferError("existing partial does not match current bench inventory")
                recovery = cache.with_name(cache.name + ".recovering")
                if recovery.exists():
                    raise TransferError("workstation recovery path already exists")
                shutil.copytree(partial, recovery)
                if local_inventory(recovery) != source_files:
                    raise TransferError("recovered copy differs from bench inventory")
                os.replace(str(recovery), str(cache))
                reused = True
            else:
                engine.copy_to_local(plan["source_endpoint"], plan["source"], partial)
                if local_inventory(partial) != source_files:
                    raise TransferError("hop-1 SHA-256 inventory mismatch; partial cache preserved")
                os.replace(str(partial), str(cache))
                reused = False
            hop1_record.write_text(json.dumps({
                "state": "HOP1_VERIFIED", "files": source_files,
                "inventory_sha256": inventory_digest(source_files), "cache": str(cache),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            product_receipt = write_workstation_product_receipt(
                cache,
                source_files,
                producer_run_id=str(job.get("run_id") or ""),
                source=str(plan["source"]),
            )

    summary = {
        **public,
        "state": "WORKSTATION_PREDICTIONS_REUSED" if reused else "WORKSTATION_PREDICTIONS_COLLECTED",
        "mutations_performed": not reused,
        **_inventory_summary(source_files),
        "product_identity": product_receipt["product_identity"],
        "product_receipt": str(workstation_product_receipt_path(cache)),
        "producer_run_id": product_receipt["producer_run_id"],
        "reused": reused,
    }
    (evidence / "workstation-predictions.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    identity = content_identity({
        "source": plan["source"] if not plan["workstation_source_supplied"] else str(cache),
        "source_files": source_files,
        "workstation_product": str(cache),
    })
    receipt_source = plan["source"] if not plan["workstation_source_supplied"] else str(cache)
    write_receipt(
        evidence,
        "COLLECT_WORKSTATION",
        "REUSED_VERIFIED" if reused else "SUCCEEDED",
        targets=["workstation_predictions"],
        input_identity=content_identity({"source": receipt_source, "files": source_files}),
        output_identity=inventory_digest(source_files),
        source=receipt_source,
        verification={"method": "sha256-inventory-v1", **_inventory_summary(source_files)},
        details={"workstation_product": str(cache), "product_identity": identity},
    )
    return summary


def publish_predictions_to_lpai(
    job: dict[str, Any], profile: dict[str, Any], run_dir: Path, *, execute: bool,
    runner: EndpointRunner | None = None,
) -> dict[str, Any]:
    plan = resolve_publication(job, profile, run_dir)
    public = {
        "state": "LPAI_PUBLICATION_PLANNED" if not execute else "PUBLISHING_LPAI_PREDICTIONS",
        "run_id": job.get("run_id"),
        "route": "shared_workstation_product_to_submitter_lpai",
        "workstation_product": str(plan["cache"]),
        "destination": plan["destination"],
        "windows_data_plane": False,
        "mutations_performed": False,
    }
    if not execute:
        return public

    evidence = plan["evidence_dir"]
    evidence.mkdir(parents=True, exist_ok=True)
    cache = plan["cache"]
    source_files = local_inventory(cache)
    product_receipt = verify_workstation_product(cache, source_files)
    engine = runner or EndpointRunner(plan["transfer"])
    destination = plan["destination"]
    endpoint = plan["destination_endpoint"]
    partial_destination = destination + ".partial"
    if engine.exists(endpoint, destination):
        if engine.inventory(endpoint, destination) != source_files:
            raise TransferError("existing LPAI destination has different content; refusing to overwrite")
        reused = True
    else:
        reused = False
        if engine.exists(endpoint, partial_destination):
            raise TransferError("LPAI partial destination already exists; refusing to overwrite")
        engine.prepare_parent(endpoint, partial_destination)
        engine.copy_from_local(cache, endpoint, partial_destination)
        destination_files = engine.inventory(endpoint, partial_destination)
        if destination_files != source_files:
            raise TransferError("hop-2 SHA-256 inventory mismatch; workstation cache is retained for retry")
        engine.finalize(endpoint, partial_destination, destination)

    summary = {
        **public,
        "state": "LPAI_PREDICTIONS_REUSED" if reused else "LPAI_PREDICTIONS_PUBLISHED",
        "mutations_performed": not reused,
        **_inventory_summary(source_files),
        "product_identity": product_receipt["product_identity"],
        "product_receipt": str(workstation_product_receipt_path(cache)),
        "producer_run_id": product_receipt["producer_run_id"],
        "destination_reused": reused,
    }
    (evidence / "lpai-predictions.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_receipt(
        evidence,
        "PUBLISH_LPAI",
        "REUSED_VERIFIED" if reused else "SUCCEEDED",
        targets=["lpai_predictions"],
        input_identity=content_identity({"workstation_product": str(cache), "files": source_files}),
        output_identity=inventory_digest(source_files),
        source=str(cache),
        verification={"method": "sha256-inventory-v1", **_inventory_summary(source_files)},
        details={"destination": destination},
    )
    return summary


def transfer_predictions(
    job: dict[str, Any], profile: dict[str, Any], run_dir: Path, *, execute: bool,
    runner: EndpointRunner | None = None,
) -> dict[str, Any]:
    plan = resolve_transfer(job, profile, run_dir)
    public = {
        "state": "TRANSFER_PLANNED" if not execute else "TRANSFERRING",
        "run_id": job.get("run_id"),
        "route": "bench_to_workstation_to_submitter_lpai",
        "source": plan["source"],
        "workstation_cache": str(plan["cache"]),
        "destination": plan["destination"],
        "windows_data_plane": False,
        "mutations_performed": False,
    }
    if not execute:
        return public

    collection = collect_predictions_to_workstation(
        job, profile, run_dir, execute=True, runner=runner,
    )
    publication = publish_predictions_to_lpai(
        job, profile, run_dir, execute=True, runner=runner,
    )
    result = {
        **public,
        "state": "PREDICTIONS_TRANSFERRED",
        "mutations_performed": bool(
            collection.get("mutations_performed") or publication.get("mutations_performed")
        ),
        "file_count": publication["file_count"],
        "total_bytes": publication["total_bytes"],
        "inventory_sha256": publication["inventory_sha256"],
        "hop1_reused": bool(collection.get("reused")),
        "destination_reused": publication["destination_reused"],
    }
    evidence = plan["evidence_dir"]
    (evidence / "prediction-transfer.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Transfer predictions through the workstation with per-file SHA-256")
    parser.add_argument("--job", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--backfill-workstation-product", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--producer-run-id")
    parser.add_argument("--hop1-evidence", type=Path)
    args = parser.parse_args()
    if args.backfill_workstation_product is not None:
        missing = [
            name
            for name, value in (
                ("--cache-root", args.cache_root),
                ("--producer-run-id", args.producer_run_id),
                ("--hop1-evidence", args.hop1_evidence),
            )
            if value in (None, "")
        ]
        if missing:
            parser.error(
                "workstation product backfill requires " + ", ".join(missing)
            )
        result = backfill_workstation_product_receipt(
            args.backfill_workstation_product,
            cache_root=args.cache_root,
            producer_run_id=args.producer_run_id,
            hop1_evidence=args.hop1_evidence,
        )
    else:
        if args.job is None or args.profile is None or args.run_dir is None:
            parser.error("transfer requires --job, --profile, and --run-dir")
        result = transfer_predictions(
            load_json(args.job), load_json(args.profile), args.run_dir,
            execute=args.execute,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TransferError as exc:
        print(json.dumps({"state": "TRANSFER_BLOCKED", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
