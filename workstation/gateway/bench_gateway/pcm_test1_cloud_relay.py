"""Loopback-only Test 1 LiPai transport over a restricted SSH forward."""

import argparse
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

from . import pcm_test1_cloud_storage as storage

ROUTE = "/pcm-test1-cloud-v1"
MAX_FRAME = 256 * 1024
MAX_RESPONSE = 96 * 1024**2
MAX_ARCHIVE = storage.FILES["predictions.tar.gz"]
CHUNK = 1024**2


def key(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("relay key path is not absolute")
    raw = storage.regular_bytes(path, 128).strip()
    if not re.fullmatch(rb"[a-f0-9]{64}", raw):
        raise ValueError("invalid relay key")
    return bytes.fromhex(raw.decode("ascii"))


def mac(secret, value):
    return hmac.new(secret, value, hashlib.sha256).hexdigest()


def call(cfg, request):
    action = request["action"]
    data = {k: request[k] for k in ("action", "remote_prefix")}
    if action == "collect":
        data["cloud_directory"] = request["cloud_directory"]
    archive = None
    size = 0
    if action == "upload":
        local = Path(request["local_directory"])
        archive = local / "predictions.tar.gz"
        storage.no_links(archive)
        size = archive.stat().st_size
        if not 0 < size <= MAX_ARCHIVE:
            raise ValueError("invalid relay archive size")
        with archive.open("rb") as source:
            data["archive_sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
        data["native_request"] = storage.object_json(storage.regular_bytes(local / "request.json", MAX_FRAME))
        data["archive_size"] = size
    secret = key(cfg["cloud_relay_key_file"])
    nonce = secrets.token_hex(16)
    payload = {"request": data, "issued_at": int(time.time()), "nonce": nonce}
    header = storage.canonical({"payload": payload, "hmac": mac(secret, storage.canonical(payload))}) + b"\n"
    if len(header) > MAX_FRAME:
        raise ValueError("relay request header too large")
    connection = http.client.HTTPConnection("127.0.0.1", cfg["cloud_relay_port"], timeout=300)
    try:
        connection.putrequest("POST", ROUTE)
        connection.putheader("Content-Length", str(len(header) + size))
        connection.endheaders()
        connection.send(header)
        if archive is not None:
            with archive.open("rb") as source:
                remaining = size
                while remaining:
                    chunk = source.read(min(remaining, CHUNK))
                    if not chunk:
                        raise ValueError("archive changed during relay upload")
                    connection.send(chunk)
                    remaining -= len(chunk)
        response = connection.getresponse()
        length = response.getheader("Content-Length", "")
        if response.status != 200 or not length.isdecimal() or not 0 < int(length) <= MAX_RESPONSE:
            raise ValueError("relay did not return a result")
        raw = response.read(int(length))
        if (len(raw) != int(length)
                or not hmac.compare_digest(response.getheader("X-PCM-Relay-Signature", ""),
                                           mac(secret, nonce.encode() + b"\0" + raw))):
            raise ValueError("relay response did not verify")
        return storage.object_json(raw)
    finally:
        connection.close()


class Relay:
    def __init__(self, secret, root, owner, profile_sha, command, signer):
        self.secret, self.root, self.owner, self.profile_sha = secret, root, owner, profile_sha
        self.command, self.signer = command, signer

    def request(self, stream, size):
        header = stream.readline(MAX_FRAME + 1)
        if not header.endswith(b"\n") or len(header) > MAX_FRAME or len(header) > size:
            raise ValueError("invalid relay frame")
        envelope = storage.object_json(header)
        payload = envelope["payload"]
        if (set(envelope) != {"payload", "hmac"} or set(payload) != {"request", "nonce", "issued_at"}
                or not hmac.compare_digest(envelope["hmac"], mac(self.secret, storage.canonical(payload)))
                or not re.fullmatch(r"[0-9a-f]{32}", payload["nonce"])
                or type(payload["issued_at"]) is not int or abs(time.time() - payload["issued_at"]) > 300):
            raise ValueError("relay request authentication failed")
        request = payload["request"]
        action, prefix = request["action"], request["remote_prefix"]
        remaining = size - len(header)
        names = {"action", "remote_prefix"}
        if action == "heartbeat":
            if set(request) != names or prefix != storage.PREFIX or remaining:
                raise ValueError("invalid relay heartbeat")
        else:
            suffix = prefix.removeprefix(storage.PREFIX)
            owner_prefix = "u_" + hashlib.sha256(self.owner.encode()).hexdigest()[:24] + "/"
            if not prefix.startswith(storage.PREFIX) or not suffix.startswith(owner_prefix) or not storage.TASK.fullmatch(suffix):
                raise ValueError("invalid relay owner prefix")
            if action == "status" and (set(request) != names or remaining):
                raise ValueError("invalid relay status")
            if action == "collect" and (set(request) != names | {"cloud_directory"} or remaining
                    or request["cloud_directory"] != storage.RUN_ROOT + "/" + suffix.rstrip("/")):
                raise ValueError("invalid relay result path")
            if action not in {"upload", "status", "collect"}:
                raise ValueError("unsupported relay operation")
        if action == "upload":
            result = self.upload(stream, request, remaining, names, suffix)
        else:
            result = self.transport(request)
        return payload["nonce"], result

    def upload(self, stream, request, remaining, names, suffix):
        native = request["native_request"]
        if (set(request) != names | {"archive_size", "archive_sha256", "native_request"}
                or type(request["archive_size"]) is not int or request["archive_size"] != remaining
                or not 0 < remaining <= MAX_ARCHIVE or not re.fullmatch(r"[a-f0-9]{64}", request["archive_sha256"])
                or native.get("owner") != self.owner or native.get("profile_sha256") != self.profile_sha
                or native.get("run_id") != suffix.rstrip("/").split("/")[-1]
                or not isinstance(native.get("scenes"), list) or not 1 <= len(native["scenes"]) <= 4):
            raise ValueError("relay upload does not match the approved scope")
        marker = self.root / ("upload-" + hashlib.sha256(suffix.encode()).hexdigest() + ".started")
        with tempfile.TemporaryDirectory(dir=self.root, prefix="incoming-") as folder:
            root = Path(folder)
            archive = root / "predictions.tar.gz"
            digest = hashlib.sha256()
            with archive.open("xb") as output:
                while remaining:
                    chunk = stream.read(min(remaining, CHUNK))
                    if not chunk:
                        raise ValueError("relay archive ended prematurely")
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            if not hmac.compare_digest(digest.hexdigest(), request["archive_sha256"]):
                raise ValueError("relay archive digest differs")
            native_path = root / "request.json"
            native_path.write_bytes(storage.canonical(native) + b"\n")
            subprocess.run([*self.signer, str(native_path), str(root / "request.sig")],
                           check=True, capture_output=True, timeout=20)
            with marker.open("xb") as evidence:
                evidence.write(storage.canonical({"request": native, "archive_sha256": digest.hexdigest()}) + b"\n")
                evidence.flush()
                os.fsync(evidence.fileno())
            return self.transport({"action": "upload", "remote_prefix": request["remote_prefix"],
                                   "local_directory": str(root), "files": list(storage.FILES)})

    def transport(self, request):
        result = subprocess.run(self.command, input=storage.canonical(request), capture_output=True, timeout=270)
        value = storage.object_json(result.stdout)
        if result.returncode or value.get("ok") is not True or not isinstance(value.get("data"), dict):
            raise ValueError("dedicated LiPai storage did not accept relay operation")
        return value["data"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            self.connection.settimeout(45)
            raw_size = self.headers.get("Content-Length", "")
            if self.path != ROUTE or not raw_size.isdecimal() or not 0 < int(raw_size) <= MAX_FRAME + MAX_ARCHIVE:
                raise ValueError("unsupported relay route")
            relay = self.server.relay
            nonce, result = relay.request(self.rfile, int(raw_size))
            body = storage.canonical(result)
            if not 0 < len(body) <= MAX_RESPONSE:
                raise ValueError("relay result too large")
            signature = mac(relay.secret, nonce.encode() + b"\0" + body)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-PCM-Relay-Signature", signature)
            self.end_headers()
            self.wfile.write(body)
        except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
            self.close_connection = True
            try:
                self.send_error(503, "Relay operation not confirmed")
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--relay-key", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--profile-sha", required=True)
    parser.add_argument("--sdk-python", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--signer", required=True, type=Path)
    parser.add_argument("--signing-key", required=True, type=Path)
    args = parser.parse_args()
    if (not 1024 <= args.port <= 65535 or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", args.owner)
            or not re.fullmatch(r"[0-9a-f]{64}", args.profile_sha) or not args.state.is_absolute()
            or os.environ.get("PCM_OFFLINE_TEST") == "1"):
        raise ValueError("invalid live Test 1 relay configuration")
    args.state.mkdir(parents=True, exist_ok=True)
    storage.no_links(args.state)
    for path in (args.sdk_python, args.binding, args.node, args.signer, args.signing_key):
        if not path.is_absolute() or not path.is_file():
            raise ValueError("missing local relay prerequisite")
    sender = args.signer.parent.parent / "bench_gateway/pcm_test1_cloud_storage.py"
    if not sender.is_file():
        raise ValueError("reviewed sender beside the configured signer is missing")
    command = [str(args.sdk_python), "-B", str(sender), "--binding", str(args.binding)]
    signer = [str(args.node), str(args.signer), "sign", str(args.signing_key)]
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.relay = Relay(key(args.relay_key), args.state, args.owner, args.profile_sha, command, signer)
        server.serve_forever()


if __name__ == "__main__":
    main()

