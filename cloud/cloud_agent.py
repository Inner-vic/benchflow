"""Single fixed evaluator receiver; standard OpenSSL verifies owner-PC requests."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid


def write(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temp, path)


def verify_request(root, runtime):
    raw = (root/'request.json').read_bytes()
    if len(raw) > 256*1024 or (root/'request.sig').stat().st_size != 64:
        raise ValueError('bounded signed request required')
    with tempfile.TemporaryDirectory(prefix='signature-', dir=runtime/'scratch') as folder:
        payload = Path(folder)/'request.bin'
        payload.write_bytes(b'bench-eval-request-v1\0'+raw)
        process = subprocess.run(['/usr/bin/openssl','pkeyutl','-verify','-pubin','-inkey',str(runtime/'public.pem'),
                                  '-rawin','-in',str(payload),'-sigfile',str(root/'request.sig')],
                                 capture_output=True, timeout=10)
        if process.returncode:
            raise ValueError('request signature rejected')
    return json.loads(raw)


def main():
    runtime = Path(__file__).resolve().parent
    profile = json.loads((runtime/'profile.json').read_text())
    (runtime/'scratch').mkdir(exist_ok=True)
    guard = socket.socket(socket.AF_UNIX)
    try:
        guard.bind('\0bench-eval-offline-dispatcher')
    except OSError:
        return  # A receiver is already alive; no stale PID/file lock cleanup.
    root = Path(os.environ['BENCH_EVAL_RUN_ROOT']).resolve(strict=True)
    if profile.get('gt_policy')=='task-dataset-v1':
        if not profile.get('gt_mount_roots'):raise ValueError('No authorized dataset mounts')
        for raw in profile['gt_mount_roots']:
            if not Path(raw).is_dir():raise ValueError('Dataset mount unavailable')
    elif str(Path(os.environ['BENCH_EVAL_GT_ROOT']).resolve(strict=True)) != profile['gt_root']:
        raise ValueError('GT environment differs from saved legacy profile')
    generation = uuid.uuid4().hex
    heartbeat = {'generation':generation,'pid':os.getpid(),'instance':profile['instance'],
                 'profile_sha256':profile['profile_sha256'],'state':'READY'}
    from cloud_evaluation import run
    while True:
        write(root/'worker-status.json', {**heartbeat,'checked_at':time.time()})
        directories = list(root.glob('u_*/cloud-eval-*'))
        for attempt in sorted(directories):
            if not (attempt/'request.sig').is_file() or (attempt/'state.json').is_file():
                continue
            if attempt.is_symlink() or root not in attempt.resolve().parents:
                continue
            try:
                request = verify_request(attempt, runtime)
                if request['profile_sha256'] != profile['profile_sha256'] or request['owner_key'] != attempt.parent.name:
                    raise ValueError('profile or owner folder differs')
                if request['tool_hashes'] != profile['tool_hashes']:
                    raise ValueError('request cannot replace the fixed evaluator')
                heartbeat['state'] = 'EVALUATING'
                heartbeat['run_id'] = request['run_id']
                write(root/'worker-status.json', {**heartbeat,'checked_at':time.time()})
                # Keep only liveness fresh while this single receiver blocks in native work.
                import threading
                stop = threading.Event()
                def pulse():
                    while not stop.wait(10):
                        write(root/'worker-status.json', {**heartbeat,'checked_at':time.time()})
                thread = threading.Thread(target=pulse, daemon=True)
                thread.start()
                try:
                    run(attempt, runtime_root=runtime/'evaluator', support_root=runtime,runtime_profile=profile)
                finally:
                    stop.set()
                    thread.join(timeout=12)
            except Exception as error:
                if not (attempt/'state.json').exists():
                    write(attempt/'state.json', {'state':'REJECTED','error':str(error)[:400]})
            finally:
                heartbeat['state'] = 'READY'
                heartbeat.pop('run_id',None)
        time.sleep(2)


if __name__ == '__main__':
    main()
