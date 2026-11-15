"""Menu-only warm SSH channel; PC persists connection metadata, never requests."""
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from bridge_protocol import MAX_PACKET, decode, validate_response

LIMIT = 384 * 1024


def endpoint_path(root):
    return Path(root) / 'runtime/desktop-menu/endpoint.json'


def local_request(root, value, timeout=75):
    """None means definitely not dispatched. All ambiguous outcomes raise."""
    from multiprocessing.connection import Client
    try:
        record = json.loads(endpoint_path(root).read_text(encoding='utf-8'))
        if record['state'] != 'READY' or not 0 <= time.time()-record['updated'] < 90:
            return None
        address = record['address']
        if not address.startswith('\\\\.\\pipe\\bench-menu-'):
            return None
        if os.name == 'nt':
            import _winapi
            _winapi.WaitNamedPipe(address, 50)
        conn = Client(address, family='AF_PIPE', authkey=base64.b64decode(record['key'], validate=True))
    except Exception:
        # Authentication/connect failure occurs before any request is sent.
        return None
    try:
        # After this point a broken pipe/timeout cannot justify replay.
        conn.send_bytes(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        if not conn.poll(timeout):
            raise RuntimeError('Menu outcome is unconfirmed')
        answer = json.loads(conn.recv_bytes(LIMIT).decode('utf-8'))
        if answer.get('kind') == 'not_started':
            return None
        if answer.get('kind') == 'result':
            return validate_response(answer['response'])
        if value.get('kind') == 'ping' and answer.get('kind') == 'pong':
            return answer
        raise RuntimeError('Menu outcome is unconfirmed')
    finally:
        conn.close()


def forward(root, project, session, text):
    from bridge_protocol import packet
    value = json.loads(packet(project, session, text))
    if not text.startswith('@@BENCH_MENU@@'):
        return None
    return local_request(root, {'kind': 'menu', 'packet': value})


class Channel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.inbox = None
        self.lock = threading.Lock()
        self.ready = False

    def close(self):
        self.ready = False
        p, self.process = self.process, None
        if p is not None:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=3)
            for stream in (p.stdin, p.stdout, p.stderr):
                if stream:
                    stream.close()

    def connect(self):
        from desktop_gateway_agent import argv
        args = argv(self.cfg)
        args[-1] += ' --menu-stream'
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        self.process = p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, **options)
        self.inbox = inbox = queue.Queue(maxsize=2)
        def read():
            try:
                while True:
                    raw = p.stdout.readline(LIMIT+1)
                    if not raw or len(raw) > LIMIT:
                        break
                    inbox.put(json.loads(raw), timeout=1)
            except Exception:
                pass
            finally:
                try:
                    inbox.put(None, timeout=1)
                except queue.Full:
                    pass
        threading.Thread(target=read, daemon=True).start()
        answer = inbox.get(timeout=90)
        if answer != {'kind': 'ready', 'protocol': 'menu-stream-v1'}:
            raise ConnectionError('Menu channel unavailable')
        self.ready = True

    def exchange(self, value):
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        if len(raw) > MAX_PACKET:
            raise ValueError('Menu request too large')
        self.process.stdin.write(raw+b'\n')
        self.process.stdin.flush()
        answer = self.inbox.get(timeout=65)
        if not isinstance(answer, dict):
            raise ConnectionError('Menu channel closed')
        return answer

    def request(self, value):
        # No local queue of business requests or concurrent SSH dispatch.
        if not self.ready:
            return {'kind': 'not_started'}
        if not self.lock.acquire(timeout=2):
            return {'kind': 'not_started'}
        try:
            if not self.ready or self.process.poll() is not None:
                return {'kind': 'not_started'}
            if value.get('kind') != 'ping':
                packet = decode(json.dumps(value.get('packet')), self.cfg['cc_project'])
                if value.get('kind') != 'menu' or not packet['text'].startswith('@@BENCH_MENU@@'):
                    return {'kind': 'not_started'}
            answer = self.exchange(value)
            if answer.get('kind') in ('not_started', 'unknown'):
                self.close()
            return answer
        except Exception:
            self.close()
            return {'kind': 'unknown'}
        finally:
            self.lock.release()


def private_directory(path):
    """Restrict the random local pipe credential before writing it."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        os.chmod(path, 0o700)
        return
    import re
    options = {'creationflags': subprocess.CREATE_NO_WINDOW}
    result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                            text=True, timeout=15, check=True, **options)
    sid = re.search(r'S-1-5-\d+(?:-\d+)+', result.stdout)
    if not sid:
        raise RuntimeError('Local identity unavailable')
    subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r',
                    '*'+sid.group(0)+':(OI)(CI)F'], capture_output=True, timeout=15, check=True, **options)


def serve(cfg):
    from multiprocessing.connection import Listener
    root = Path(cfg['project_root'])
    target = endpoint_path(root)
    private_directory(target.parent)
    key = os.urandom(32)
    identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    address = '\\\\.\\pipe\\bench-menu-'+identity+'-'+uuid.uuid4().hex
    channel = Channel(cfg)
    def status():
        # No session, owner, message, material, response or stderr is saved.
        value = {'state': 'READY' if channel.ready else 'CONNECTING', 'updated': time.time(),
                 'pid': os.getpid(), 'address': address, 'key': base64.b64encode(key).decode()}
        from bench_gateway.config import save
        save(target, value)
    def keep_warm():
        while True:
            try:
                with channel.lock:
                    if not channel.ready:
                        channel.connect()
                    elif channel.exchange({'kind': 'ping'}) != {'kind': 'pong'}:
                        channel.close()
                    status()
            except Exception:
                with channel.lock:
                    channel.close()
                    try:
                        status()
                    except OSError:
                        pass
            time.sleep(5)
    with Listener(address, family='AF_PIPE', authkey=key) as listener:
        status()
        threading.Thread(target=keep_warm, daemon=True).start()
        def respond(conn):
            try:
                if not conn.poll(5):
                    return
                value = json.loads(conn.recv_bytes(MAX_PACKET).decode('utf-8'))
                answer = channel.request(value)
                conn.send_bytes(json.dumps(answer, ensure_ascii=False).encode('utf-8'))
            except Exception:
                pass  # The client treats disconnect after send as unknown.
            finally:
                conn.close()
        while True:
            try:
                conn = listener.accept()
            except Exception:
                continue
            threading.Thread(target=respond, args=(conn,), daemon=True).start()


def start(cfg):
    if os.name == 'nt' and cfg.get('desktop_menu_warm_transport'):
        def guarded():
            try:
                serve(cfg)
            except Exception:
                pass  # Missing endpoint keeps the original one-shot transport.
        threading.Thread(target=guarded, daemon=True).start()
