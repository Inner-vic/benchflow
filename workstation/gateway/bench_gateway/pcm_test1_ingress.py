"""Authenticated PCM handoff over a dedicated, fixed SSH command."""
from contextlib import ExitStack
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

from .core import GatewayError, Store, digest, owner_key, within

REQUEST_SCHEMA = "pcm-benchflow-ingress-v1"
RECEIPT_SCHEMA = "pcm-benchflow-receipt-v1"
HEADER_LIMIT = 65536
REPORT_LIMIT = 64 * 1024**2
ROLE_LIMITS = dict.fromkeys(("camera", "lidar", "m100"), 128 * 1024**2)
ROLE_LIMITS["yaml"] = 2 * 1024**2
YAML_TARGETS = {name: "etc/uroad/config/" + name for name in ("common_param.yaml", "postprocess_param.yaml")}
BINDING = ("app_id", "task_id", "submission_sha256", "owner", "chat_id", "card_message_id", "revision")
SHA = r"[a-f0-9]{64}"
PRINCIPAL = r"pcm_test1_[a-f0-9]{32}"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sign(payload, key):
    return {"payload": payload, "hmac_sha256": hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()}


def _matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _reject_duplicates(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON field")
    return value


def policy(cfg):
    value = cfg.get("pcm_test1_ingress")
    if not isinstance(value, dict) or value.get("enabled") is not True:
        raise GatewayError("PCM Test 1 ingress disabled")
    if not _matches(r"cli_[a-zA-Z0-9]+", value.get("app_id")) or value["app_id"] == cfg.get("feishu_app_id"):
        raise GatewayError("PCM Test 1 ingress App identity invalid")
    prepare_only = value.get("prepare_only", False)
    if type(prepare_only) is not bool or (prepare_only and not _matches(r"[a-f0-9]{16}", value.get("prepare_only_task_id"))):
        raise GatewayError("PCM Test 1 prepare-only policy invalid")
    return value


def read_key(path):
    path = Path(path)
    mode = path.lstat().st_mode
    if not path.is_absolute() or not stat.S_ISREG(mode) or mode & 0o077 or path.stat().st_uid != os.geteuid():
        raise GatewayError("PCM Test 1 ingress key file is not private")
    with path.open("rb") as source:
        key = source.read(257).strip()
    if not 32 <= len(key) <= 256:
        raise GatewayError("PCM Test 1 ingress key length invalid")
    return key


def request(stream, key, app_id):
    line = stream.readline(HEADER_LIMIT + 1)
    if len(line) > HEADER_LIMIT or not line.endswith(b"\n"):
        raise GatewayError("PCM request header invalid")
    envelope = json.loads(line.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "hmac_sha256"}:
        raise GatewayError("PCM request envelope invalid")
    payload = envelope["payload"]
    if (not isinstance(payload, dict) or not _matches(SHA, envelope["hmac_sha256"])
            or not hmac.compare_digest(sign(payload, key)["hmac_sha256"], envelope["hmac_sha256"])):
        raise GatewayError("PCM request signature invalid")
    operation = payload.get("operation")
    extra = {"submit": {"inputs", "attachments"}, "status": set(), "artifact": {"remote_task_id", "report_sha256"},
             "release": {"approval_sha256", "freeze_sha256"}}
    if operation == "submit" and "preflight_only" in payload:
        if payload["preflight_only"] is not True:
            raise GatewayError("PCM preflight flag invalid")
        extra["submit"].add("preflight_only")
    if (not isinstance(operation, str) or operation not in extra
            or set(payload) != {"schema", "operation", "nonce", *BINDING, *extra[operation]}
            or payload["schema"] != REQUEST_SCHEMA or payload["app_id"] != app_id
            or not _matches(r"[a-f0-9]{32}", payload["nonce"])
            or not _matches(r"[a-f0-9]{16}", payload["task_id"])
            or not _matches(SHA, payload["submission_sha256"])
            or not _matches(r"ou_[A-Za-z0-9_-]{3,100}", payload["owner"])
            or not _matches(r"oc_[A-Za-z0-9_-]{3,100}", payload["chat_id"])
            or not _matches(r"om_[A-Za-z0-9_-]{3,100}", payload["card_message_id"])
            or type(payload["revision"]) is not int or not 0 < payload["revision"] < 2**63):
        raise GatewayError("PCM request binding invalid")
    if operation == "submit":
        _check_inputs(payload["inputs"])
        _check_attachments(payload["attachments"])
    elif operation == "release" and any(not _matches(SHA, payload[key]) for key in extra[operation]):
        raise GatewayError("PCM release proof invalid")
    elif operation == "artifact" and (not _matches(r"pcm-[a-f0-9]{32}", payload["remote_task_id"])
                                      or not _matches(SHA, payload["report_sha256"])):
        raise GatewayError("PCM artifact binding invalid")
    return payload


def _check_inputs(values):
    required = {"app_source", "dataset", "goal", "replay_mode"}
    if (not isinstance(values, dict) or not required <= values.keys() or values.keys() - required - {"build_reference", "scene_scope"}
            or any(not isinstance(v, str) or len(v) > 2000 or any(ord(c) < 32 for c in v) for v in values.values())
            or not values["app_source"].startswith("https://")
            or not values["dataset"].startswith("/") or values["dataset"].startswith("//")
            or values["goal"] not in ("replay", "replay_and_evaluate")
            or values["replay_mode"] not in ("stream", "frame_by_frame")):
        raise GatewayError("PCM task inputs invalid")


def _check_attachments(attachments):
    from .attachments import role_for
    if not isinstance(attachments, dict) or attachments.keys() - ROLE_LIMITS.keys():
        raise GatewayError("PCM material roles invalid")
    for role, item in attachments.items():
        if (not isinstance(item, dict) or set(item) != {"name", "size", "sha256"}
                or not _matches(r"[^/\\\x00-\x1f\x7f]{1,255}", item["name"])
                or type(item["size"]) is not int or not 0 < item["size"] <= ROLE_LIMITS[role]
                or not _matches(SHA, item["sha256"])
                or (role == "yaml" and item["name"] not in YAML_TARGETS)
                or (role != "yaml" and not item["name"].lower().endswith(".so"))):
            raise GatewayError("PCM material metadata invalid")
        known = role_for(item["name"])
        if known and known != role:
            raise GatewayError("PCM material filename and role differ")


def _material_file(role):
    return role + (".yaml" if role == "yaml" else ".so")


def _receive_material(stream, path, role, item):
    with path.open("xb") as output:
        remaining = item["size"]
        sha = hashlib.sha256()
        while remaining:
            chunk = stream.read(min(remaining, 1024**2))
            if not chunk:
                raise GatewayError("PCM material stream incomplete")
            output.write(chunk)
            sha.update(chunk)
            remaining -= len(chunk)
        output.flush()
        os.fsync(output.fileno())
    if not hmac.compare_digest(sha.hexdigest(), item["sha256"]):
        raise GatewayError("PCM material hash mismatch")
    if role == "yaml":
        if not path.read_bytes().decode("utf-8-sig").strip():
            raise GatewayError("PCM YAML is empty")
    else:
        with path.open("rb") as source:
            header = source.read(64)
        if (len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01"
                or header[16:20] != b"\x03\x00\xb7\x00"):
            raise GatewayError("PCM material is not a Linux AArch64 shared object")


def schema(store):
    with store.connect() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS pcm_test1_tasks(
            remote_task_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, task_id TEXT NOT NULL,
            principal TEXT NOT NULL UNIQUE, binding TEXT NOT NULL, submission_digest TEXT NOT NULL,
            materials TEXT NOT NULL, inputs TEXT NOT NULL, cloud_profile_sha256 TEXT,
            state TEXT NOT NULL, job_id TEXT UNIQUE, created REAL NOT NULL,
            UNIQUE(app_id,task_id))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS pcm_test1_holds(
            remote_task_id TEXT PRIMARY KEY REFERENCES pcm_test1_tasks(remote_task_id),
            payload TEXT, freeze_sha256 TEXT,
            CHECK ((payload IS NULL) = (freeze_sha256 IS NULL)))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS pcm_test1_releases(
            remote_task_id TEXT PRIMARY KEY REFERENCES pcm_test1_tasks(remote_task_id),
            approval_sha256 TEXT NOT NULL, freeze_sha256 TEXT NOT NULL, material_sha256 TEXT NOT NULL)""")


def _binding(payload):
    return {name: payload[name] for name in BINDING}


def _task_id(payload):
    return hashlib.sha256(canonical([payload["app_id"], payload["task_id"]])).hexdigest()[:32]


def _spool(store):
    root = store.root / "pcm-test1-intake"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise GatewayError("PCM intake spool redirected")
    return root


def _check_same(row, payload):
    if json.loads(row["binding"]) != _binding(payload):
        raise GatewayError("PCM request conflicts with the registered task")


def _hardware_open(cfg):
    from .maintenance import notice
    return cfg.get("live_enabled") is True and cfg.get("mode") != "simulate" and not notice(cfg)


def submit(cfg, store, payload, stream, *, approval=None):
    active = policy(cfg)
    if active.get("accept_submissions") is not True or not _hardware_open(cfg):
        raise GatewayError("PCM hardware dispatch is not configured")
    preflight_only = payload.get("preflight_only") is True
    if (preflight_only != (active.get("prepare_only") is True) or preflight_only and approval is not None
            or (preflight_only and payload["task_id"] != active["prepare_only_task_id"])):
        raise GatewayError("PCM submission does not match the prepare-only policy")
    identity = _task_id(payload)
    remote_task_id = "pcm-" + identity
    profile_sha = active.get("cloud_profile_sha256") if payload["inputs"]["goal"] == "replay_and_evaluate" else None
    if payload["inputs"]["goal"] == "replay_and_evaluate":
        if preflight_only:
            _cloud_profile(cfg, profile_sha)
        else:
            cloud_scope(cfg, {"owner": "pcm_test1_" + identity,
                              "payload": {"pcm_cloud_profile_sha256": profile_sha}})
    final = _spool(store) / identity
    principal = "pcm_test1_" + identity
    stable = {name: payload[name] for name in (*BINDING, "inputs", "attachments")}
    if preflight_only:
        stable["preflight_only"] = True
    request_digest = hashlib.sha256(canonical(stable)).hexdigest()
    with tempfile.TemporaryDirectory(prefix="receive-", dir=final.parent) as temporary:
        temporary = Path(temporary)
        for role, item in sorted(payload["attachments"].items()):
            _receive_material(stream, temporary / _material_file(role), role, item)
        if stream.read(1):
            raise GatewayError("PCM request has extra material bytes")
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM pcm_test1_tasks WHERE app_id=? AND task_id=?",
                                          (payload["app_id"], payload["task_id"])).fetchone()
            if existing:
                _check_same(existing, payload)
                if existing["submission_digest"] != request_digest:
                    raise GatewayError("PCM task material changed after registration")
                previous = connection.execute("SELECT approval_sha256,freeze_sha256,material_sha256 FROM pcm_test1_releases WHERE remote_task_id=?",
                                              (remote_task_id,)).fetchone()
                if (tuple(previous) if previous else None) != approval:
                    raise GatewayError("PCM task release proof changed after registration")
                return
            # A crash between the rename and transaction commit leaves evidence, never another queue entry.
            if final.exists():
                raise GatewayError("PCM intake outcome requires reconciliation")
            final.mkdir(mode=0o700)
            for role in sorted(payload["attachments"]):
                name = _material_file(role)
                (temporary / name).rename(final / name)
            connection.execute("""INSERT INTO pcm_test1_tasks VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                               (remote_task_id, payload["app_id"], payload["task_id"], principal,
                                canonical(_binding(payload)).decode(), request_digest,
                                canonical(payload["attachments"]).decode(), canonical(payload["inputs"]).decode(),
                                profile_sha, "REGISTERED", time.time()))
            if preflight_only:
                connection.execute("INSERT INTO pcm_test1_holds(remote_task_id) VALUES(?)", (remote_task_id,))
            if approval is not None:
                connection.execute("INSERT INTO pcm_test1_releases VALUES(?,?,?,?)", (remote_task_id, *approval))


class _MaterialStream:
    """Read the retained uploads in the same role order as the normal ingress."""
    def __init__(self, files):
        self.files = iter(files)
        self.current = next(self.files, None)

    def read(self, size):
        while self.current is not None:
            chunk = self.current.read(size)
            if chunk:
                return chunk
            self.current = next(self.files, None)
        return b""


def _release_material_sha(plan):
    assets = plan["assets"]
    package, dataset = (assets[role]["data"]["sha256"] for role in ("package", "dataset"))
    dependencies = sorted((item["data"]["target"], item["data"]["sha256"]) for item in assets["dependencies"])
    if (len(plan["scenes"]) != 4 or len(dependencies) < 3
            or not all(_matches(SHA, value) for value in (package, dataset, *(item[1] for item in dependencies)))):
        raise GatewayError("PCM released material identity is incomplete")
    identity = {"package": package, "dataset": dataset, "dependencies": dependencies,
                "scenes": plan["scenes"], "gt": plan.get("evaluation_gt_binding"),
                "evaluate_after": plan["evaluate_after"]}
    return hashlib.sha256(canonical(identity)).hexdigest()


def release_held(cfg, private_store, shared_cfg, shared_store, payload):
    active = policy(cfg)
    shared_policy = policy(shared_cfg)
    admission = cfg.get("pcm_test1_preflight_admission", {})
    if (active.get("prepare_only") is not True or active.get("prepare_only_task_id") != payload["task_id"]
            or admission.get("task_id") != payload["task_id"] or admission.get("scene_scope") != "1-4"
            or not _matches(SHA, admission.get("release_sha256")) or admission["release_sha256"] != payload["freeze_sha256"]
            or private_store.root == shared_store.root or shared_policy["app_id"] != active["app_id"]
            or shared_policy.get("accept_submissions") is not False or shared_policy.get("prepare_only") is True):
        raise GatewayError("PCM preflight release is not configured for this card")
    with private_store.connect() as connection:
        row = connection.execute("""SELECT task.*,hold.payload AS frozen,hold.freeze_sha256
            FROM pcm_test1_tasks task JOIN pcm_test1_holds hold USING(remote_task_id)
            WHERE app_id=? AND task_id=?""", (payload["app_id"], payload["task_id"])).fetchone()
    if (not row or row["state"] != "READY_FOR_GATE" or row["job_id"] is not None or not row["frozen"]
            or row["freeze_sha256"] != payload["freeze_sha256"]
            or hashlib.sha256(row["frozen"].encode()).hexdigest() != row["freeze_sha256"]):
        raise GatewayError("PCM release has no verified frozen preflight")
    _check_same(row, payload)
    plan = json.loads(row["frozen"])
    inputs, materials = json.loads(row["inputs"]), json.loads(row["materials"])
    _check_inputs(inputs)
    _check_attachments(materials)
    original = {**_binding(payload), "inputs": inputs, "attachments": materials}
    if (hashlib.sha256(canonical({**original, "preflight_only": True})).hexdigest() != row["submission_digest"]
            or plan.get("action") != "replay" or len(plan.get("scenes", [])) != 4
            or plan["baseline"]["data"].get("gate_verified") is not False
            or inputs.get("dataset") != admission.get("dataset") or inputs.get("scene_scope") != admission["scene_scope"]
            or (inputs["goal"] == "replay_and_evaluate" and
                shared_policy.get("cloud_profile_sha256") != row["cloud_profile_sha256"])):
        raise GatewayError("PCM release does not match the frozen materials and scope")
    material_sha = _release_material_sha(plan)
    if inputs["goal"] == "replay_and_evaluate":
        from .cloud_provider import ready
        evaluation = {"owner": row["principal"], "payload": {"pcm_cloud_profile_sha256": row["cloud_profile_sha256"]}}
        ready(cloud_scope(shared_cfg, evaluation), _cloud_profile(shared_cfg, row["cloud_profile_sha256"]))
    source = private_store.root / "pcm-test1-intake" / _task_id(payload)
    if source.resolve() != source:
        raise GatewayError("PCM held material path redirected")
    with ExitStack() as stack:
        files = []
        for role, item in sorted(materials.items()):
            path = source / _material_file(role)
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"]:
                raise GatewayError("PCM held material changed")
            files.append(stack.enter_context(path.open("rb")))
        approved_cfg = {**shared_cfg, "pcm_test1_ingress": {**shared_policy, "accept_submissions": True}}
        submit(approved_cfg, shared_store, original, _MaterialStream(files),
               approval=(payload["approval_sha256"], payload["freeze_sha256"], material_sha))


def _cloud_profile(cfg, expected_sha):
    from .cloud_provider import canonical as profile_bytes
    from .cloud_truth import POLICY
    profile = cfg.get("pcm_test1_cloud_profile")
    if not isinstance(profile, dict):
        raise GatewayError("PCM Test 1 cloud profile missing")
    body = {k: v for k, v in profile.items() if k != "profile_sha256"}
    if (not _matches(SHA, expected_sha) or expected_sha != profile.get("profile_sha256")
            or hashlib.sha256(profile_bytes(body)).hexdigest() != expected_sha or profile.get("gt_policy") != POLICY):
        raise GatewayError("PCM Test 1 cloud profile does not match the frozen profile")
    return profile


def is_pcm(principal):
    return _matches(PRINCIPAL, principal)


def cloud_owner(job):
    if not is_pcm(job["owner"]):
        return job["owner"]
    owner = job["payload"].get("pcm_original_owner")
    if not _matches(r"ou_[A-Za-z0-9_-]{3,100}", owner):
        raise GatewayError("PCM evaluation owner binding missing")
    return owner


def cloud_scope(cfg, job):
    if not is_pcm(job["owner"]):
        return cfg
    # Job metadata is created locally from the authenticated request and carried to evaluation children.
    expected = job["payload"].get("pcm_cloud_profile_sha256")
    profile = _cloud_profile(cfg, expected)
    options = cfg.get("pcm_test1_cloud_transport")
    if isinstance(options, dict) and set(options) == {"cloud_relay_port", "cloud_relay_key_file"}:
        port, key_file = options["cloud_relay_port"], options["cloud_relay_key_file"]
        if type(port) is not int or not 1024 <= port <= 65535 or not isinstance(key_file, str) or not Path(key_file).is_absolute():
            raise GatewayError("PCM Test 1 cloud relay configuration invalid")
        return {**cfg, **options, "cloud_transport_kind": "pcm-test1-relay-v1", "cloud_browser_autostart": False,
                "cloud_evaluation_profiles": [{"effective_after": 0, "profile": profile}]}
    required = {"cloud_sdk_python", "cloud_storage_binding", "cloud_node_binary", "cloud_signing_root"}
    if (not isinstance(options, dict) or set(options) != required
            or any(not isinstance(value, str) or not Path(value).is_absolute() for value in options.values())):
        raise GatewayError("PCM Test 1 cloud transport is not configured")
    return {**cfg, **options, "cloud_transport_kind": "pcm-test1-v1", "cloud_browser_autostart": False,
            "cloud_signing_private_key": "request-private.pem",
            "cloud_evaluation_profiles": [{"effective_after": 0, "profile": profile}]}


def _draft(cfg, store, row):
    from .preparation import TARGETS
    from .registry import Registry
    from .transfer import SSH

    values = json.loads(row["inputs"])
    materials = json.loads(row["materials"])
    if not values.get("scene_scope") or not set(TARGETS) <= {role + "_so" for role in materials}:
        raise GatewayError("PCM first-run preparation requires a scope and three uploaded SO roles")
    principal = row["principal"]
    store.allow(principal, "PCM 测试 1")
    registry = Registry(cfg, store)
    draft = {"package": values["app_source"], "dataset": values["dataset"], "scene_scope": values["scene_scope"],
             "goal": values["goal"], "replay_mode": values["replay_mode"], "m100_mode": "provided",
             "build_reference": values.get("build_reference", "")}
    for role, item in materials.items():
        source = _spool(store) / row["remote_task_id"][4:] / _material_file(role)
        if source.is_symlink() or source.stat().st_size != item["size"] or digest(source) != item["sha256"]:
            raise GatewayError("PCM material changed before preparation")
        if role == "yaml":
            upload = {"owner": principal, "kind": "dependency", "data": {"sha256": item["sha256"]}}
            remote = SSH(cfg).upload(store, upload, trusted_source=source)
            draft["yaml_request"] = f"用工作站文件 {remote} 替换 APP 内 {YAML_TARGETS[item['name']]}"
        else:
            field = role + "_so"
            asset = registry.import_file(principal, "dependency", field, source, TARGETS[field])
            if asset["data"].get("sha256") != item["sha256"]:
                raise GatewayError("PCM workstation material receipt changed")
            draft[field] = asset["id"]
    return draft


def prepare_replay(cfg, store, row):
    from .bindings import bind_preparation, validate_candidate
    from .cloud_provider import task_gt_binding
    from .preparation import MaterialCheck, PreparationQueue, fingerprint
    from .tasks import Tasks

    principal = row["principal"]
    draft = _draft(cfg, store, row)
    if draft["goal"] == "replay_and_evaluate":
        from .workstation_evaluation import validate_descriptor
        validate_descriptor(cfg, cfg.get("default_evaluator"))
    task_api = Tasks(store)
    preparation = PreparationQueue(cfg, store)
    task = task_api.ensure(principal, "pcm-test1")
    task = task_api.update(principal, task["id"], draft)
    now = time.time()
    prep_id = "prep-" + row["remote_task_id"][4:20]
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""INSERT INTO preparations
            (id,owner,session,fingerprint,state,payload,created,updated,sent,request_id)
            VALUES(?,?,?,?, 'RUNNING',?,?,?,1,?)""",
                           (prep_id, principal, "pcm-test1", fingerprint(task["data"]),
                            json.dumps(task["data"], ensure_ascii=False), now, now, task["id"]))
        connection.execute("UPDATE task_requests SET prep_id=? WHERE owner=? AND id=?",
                           (prep_id, principal, task["id"]))
        prep_job = store.unpack(connection.execute("SELECT * FROM preparations WHERE id=?", (prep_id,)).fetchone())
    try:
        result = MaterialCheck(cfg, store).run(prep_job)
        passed = result.get("status") == "STATIC_PASSED"
        if not passed:
            preparation.finish(prep_job, result)
    except Exception:
        preparation.finish(prep_job, {"status": "NEEDS_INPUT", "issues": []})
        raise
    if not passed:
        raise GatewayError("PCM material preflight did not pass")
    # The legacy binding function consumes persisted static preparation evidence.
    # This adapter owns binding and result readback, so legacy background notifications stay muted.
    result["adapter_binding"] = {"state": "PCM_VALIDATING"}
    preparation.finish(prep_job, result)
    baseline = bind_preparation(cfg, store, principal, preparation_id=prep_id)
    assets, scenes = validate_candidate(cfg, store, principal, baseline)
    value = {"action": "replay", "assets": assets, "baseline": baseline, "scenes": scenes,
             "evaluate_after": draft["goal"] == "replay_and_evaluate"}
    if value["evaluate_after"]:
        profile = _cloud_profile(cfg, row["cloud_profile_sha256"])
        value["evaluation_gt_binding"] = task_gt_binding(cfg, assets["dataset"]["data"], row["created"], profile)
    return value


def recover(store):
    schema(store)
    with store.connect() as connection:
        connection.execute("UPDATE pcm_test1_tasks SET state='NEEDS_REVIEW' WHERE state='PREPARING' AND job_id IS NULL")


def prepare_one(cfg, store, prepare=prepare_replay):
    active = policy(cfg)
    if not _hardware_open(cfg):
        return None
    schema(store)
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if active.get("prepare_only") is True:
            row = connection.execute("""SELECT task.* FROM pcm_test1_tasks task
                JOIN pcm_test1_holds hold ON hold.remote_task_id=task.remote_task_id
                WHERE task.state='REGISTERED' AND task.app_id=? AND task.task_id=?""",
                                     (active["app_id"], active["prepare_only_task_id"])).fetchone()
        else:
            row = connection.execute("SELECT * FROM pcm_test1_tasks WHERE state='REGISTERED' ORDER BY created,remote_task_id LIMIT 1").fetchone()
        if row is None:
            return None
        release = connection.execute("SELECT material_sha256 FROM pcm_test1_releases WHERE remote_task_id=?",
                                     (row["remote_task_id"],)).fetchone()
        connection.execute("UPDATE pcm_test1_tasks SET state='PREPARING' WHERE remote_task_id=?", (row["remote_task_id"],))
    try:
        value = prepare(cfg, store, row)
        if not isinstance(value, dict) or value.get("action") != "replay" or not value.get("scenes"):
            raise GatewayError("PCM static preparation did not produce a replay")
        if release and _release_material_sha(value) != release["material_sha256"]:
            raise GatewayError("PCM released material changed during shared preparation")
        if not _hardware_open(cfg):
            raise GatewayError("PCM preparation finished during maintenance; no hardware job queued")
    except GatewayError:
        state = "FAILED"
    except Exception:
        state = "NEEDS_REVIEW"
    else:
        owner = json.loads(row["binding"])["owner"]
        value.update(pcm_original_owner=owner, pcm_cloud_profile_sha256=row["cloud_profile_sha256"])
        frozen = canonical(value)
        remote_task_id = row["remote_task_id"]
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # A durable hold always wins over the worker's current configuration.
            held = connection.execute("""UPDATE pcm_test1_holds SET payload=?,freeze_sha256=?
                WHERE remote_task_id=? AND payload IS NULL""",
                                      (frozen.decode(), hashlib.sha256(frozen).hexdigest(), remote_task_id)).rowcount
            if held:
                updated = connection.execute("""UPDATE pcm_test1_tasks SET state='READY_FOR_GATE'
                    WHERE remote_task_id=? AND state='PREPARING' AND job_id IS NULL""", (remote_task_id,)).rowcount
                if updated != 1:
                    raise GatewayError("PCM preparation state changed; no job queued")
                return None
            now = time.time()
            job_id = "gw-" + time.strftime("%Y%m%d-") + remote_task_id[-14:]
            updated = connection.execute("""UPDATE pcm_test1_tasks SET state='QUEUED',job_id=?
                WHERE remote_task_id=? AND state='PREPARING' AND job_id IS NULL
                AND NOT EXISTS(SELECT 1 FROM pcm_test1_holds WHERE remote_task_id=pcm_test1_tasks.remote_task_id)""",
                                         (job_id, remote_task_id)).rowcount
            if updated != 1:
                raise GatewayError("PCM preparation state changed; no job queued")
            connection.execute("""INSERT INTO jobs(id,owner,session,request_key,state,payload,result,created,updated,lane)
                VALUES(?,?,?,?, 'QUEUED',?,NULL,?,?,'hardware')""",
                               (job_id, row["principal"], "pcm-test1", remote_task_id, frozen.decode(), now, now))
        return job_id
    with store.connect() as connection:
        connection.execute("UPDATE pcm_test1_tasks SET state=? WHERE remote_task_id=? AND state='PREPARING'",
                           (state, row["remote_task_id"]))
    return None

def _result(store, row):
    if not row["job_id"]:
        state = row["state"]
        return ("PREPARING" if state in {"REGISTERED", "PREPARING"} else state), None, None
    job = store.job(row["principal"], row["job_id"])
    state = job["state"]
    if state == "SIMULATED":
        return "NEEDS_REVIEW", None, None
    if state not in {"SUCCEEDED", "PARTIAL_SUCCESS"}:
        return state, None, None
    saved = job.get("result") or {}
    if saved.get("run_id") != job["id"] or saved.get("state") != "PASS":
        return "NEEDS_REVIEW", None, None
    data = {"job_id": job["id"], "run_id": saved["run_id"], "state": state}
    if json.loads(row["inputs"])["goal"] == "replay":
        return (state, data, None) if state == "SUCCEEDED" else ("NEEDS_REVIEW", None, None)
    evaluation = saved.get("evaluation") or {}
    profile_sha = row["cloud_profile_sha256"]
    if (not saved.get("local_report") or evaluation.get("local_report") != saved["local_report"]
            or not saved.get("evaluation_job") or not profile_sha):
        return "NEEDS_REVIEW", None, None
    child = store.job(row["principal"], saved["evaluation_job"])
    if (child["payload"].get("source_job") != job["id"] or child["payload"].get("source_run") != saved["run_id"]
            or child["payload"].get("pcm_cloud_profile_sha256") != profile_sha or child["result"] != evaluation):
        return "NEEDS_REVIEW", None, None
    receipt = evaluation.get("cloud_receipt") or {}
    if not receipt and evaluation.get("receipt_path"):
        # Older result shapes record a local receipt path. Validate its subtree first.
        receipt_path = within(store.user_root(row["principal"]), evaluation["receipt_path"])
        if receipt_path.is_file() and receipt_path.stat().st_size <= 1024**2:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    path = Path(saved["local_report"])
    try:
        target = within(store.user_root(row["principal"]), path)
        if path.is_symlink() or path.suffix.lower() != ".txt" or not target.is_file():
            return "NEEDS_REVIEW", None, None
        size = target.stat().st_size
        sha = receipt.get("report_sha256")
        if (not _matches(SHA, sha) or not 0 < size <= REPORT_LIMIT or digest(target) != sha
                or receipt.get("source_job") != job["id"] or receipt.get("job_id") != child["id"]
                or receipt.get("source_task") != saved.get("task") or receipt.get("profile_sha256") != profile_sha):
            return "NEEDS_REVIEW", None, None
        native = receipt.get("native_receipt") or {}
        if (native.get("source_run") != saved["run_id"]
                or native.get("owner") != json.loads(row["binding"])["owner"] or native.get("report_sha256") != sha):
            return "NEEDS_REVIEW", None, None
    except (OSError, ValueError):
        return "NEEDS_REVIEW", None, None
    data.update(report_sha256=sha, report_size=size)
    return state, data, target


def serve(cfg, store, key, payload, source, destination, *, shared=None):
    schema(store)
    if payload["operation"] == "submit":
        submit(cfg, store, payload, source)
    elif source.read(1):
        raise GatewayError("PCM read request has extra bytes")
    if payload["operation"] == "release":
        if shared is None:
            raise GatewayError("PCM preflight release is unavailable")
        shared_cfg, shared_store = shared
        release_held(cfg, store, shared_cfg, shared_store, payload)
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM pcm_test1_tasks WHERE app_id=? AND task_id=?",
                                 (payload["app_id"], payload["task_id"])).fetchone()
    state, result, artifact = "NOT_FOUND", None, None
    if row:
        _check_same(row, payload)
        if shared is not None and row["state"] == "READY_FOR_GATE":
            shared_store = shared[1]
            with shared_store.connect() as connection:
                promoted = connection.execute("""SELECT task.* FROM pcm_test1_tasks task
                    JOIN pcm_test1_releases approval USING(remote_task_id) WHERE remote_task_id=?""",
                    (row["remote_task_id"],)).fetchone()
            if promoted:
                _check_same(promoted, payload)
                store, row = shared_store, promoted
        state, result, artifact = _result(store, row)
    raw = b""
    if payload["operation"] == "artifact" and row:
        if (artifact is None or payload["remote_task_id"] != row["remote_task_id"]
                or payload["report_sha256"] != result["report_sha256"]):
            raise GatewayError("PCM report does not match the signed pin")
        with artifact.open("rb") as report:
            raw = report.read(REPORT_LIMIT + 1)
        if len(raw) != result["report_size"] or hashlib.sha256(raw).hexdigest() != result["report_sha256"]:
            raise GatewayError("PCM report changed during readback")
    reply = {"schema": RECEIPT_SCHEMA, "operation": payload["operation"], "nonce": payload["nonce"], **_binding(payload),
             "remote_task_id": row["remote_task_id"] if row else None, "state": state, "result": result}
    destination.write(canonical(sign(reply, key)) + b"\n" + raw)
    destination.flush()


def main():
    from .config import load
    try:
        cfg = load()
        settings = policy(cfg)
        if os.environ.get("SSH_ORIGINAL_COMMAND") != REQUEST_SCHEMA:
            raise GatewayError("PCM Test 1 forced command required")
        key = read_key(settings["key_file"])
        payload = request(sys.stdin.buffer, key, settings["app_id"])
        serve(cfg, Store(cfg["state_root"]), key, payload, sys.stdin.buffer, sys.stdout.buffer)
    except (ValueError, KeyError, OSError, UnicodeError):
        print("PCM Test 1 request rejected; consult controlled gateway logs", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
