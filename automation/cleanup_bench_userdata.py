#!/usr/bin/env python3
"""Remove approved, inactive legacy bench data under the shared bench lock.

This command intentionally accepts only the two known legacy directories. It
does not clean current replay outputs or the formal application.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path('/home/bench-user/bench-eval')
LOCK = ROOT / 'locks/bench.lock'
BENCH_SCRIPT = Path('/tmp/bench-eval-cleanup-userdata.py')
SOURCES = {'stage': Path('/userdata/bench-eval-stage'),
           'models': Path('/userdata/benchEval/models')}
SSH = ['ssh', '-F', str(ROOT / 'config/ssh_config'), '-o', 'BatchMode=no',
       '-o', 'PubkeyAuthentication=no', '-o', 'PasswordAuthentication=no',
       '-o', 'KbdInteractiveAuthentication=yes',
       '-o', 'PreferredAuthentications=keyboard-interactive',
       '-o', 'NumberOfPasswordPrompts=1', 'bench-avm']
ENV = dict(os.environ, SSH_ASKPASS='/bin/true', SSH_ASKPASS_REQUIRE='force', DISPLAY='bench-eval')


def run(argv, *, env=None):
    result = subprocess.run(argv, text=True, capture_output=True, env=env, check=False)
    if result.returncode:
        raise RuntimeError(f'{argv[0]} failed ({result.returncode}): {result.stderr[-1200:]}')
    return result.stdout.strip()


def worker(target_name):
    source = SOURCES[target_name]
    if not source.is_dir() or source.is_symlink() or source.resolve() != source:
        raise RuntimeError(f'legacy source is absent or unsafe: {source}')
    if str(source) in run(['lsof']):
        raise RuntimeError(f'open file under {source}')
    before = shutil.disk_usage('/userdata').free
    file_count = sum(len(files) for _, _, files in os.walk(source))
    shutil.rmtree(source)
    if source.exists() or source.is_symlink():
        raise RuntimeError(f'legacy source remains: {source}')
    return {'target': target_name, 'removed_path': str(source), 'file_count': file_count,
            'free_bytes_before': before, 'free_bytes_after': shutil.disk_usage('/userdata').free,
            'completed_at': datetime.now(timezone.utc).isoformat()}


def controller(run_id, target_name):
    report_path = Path('/mnt/data/bench-eval/maintenance') / f'{run_id}.json'
    if report_path.exists():
        raise RuntimeError(f'run ID already used: {run_id}')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'run_id': run_id, 'purpose': 'cleanup-legacy-userdata',
                   'target': target_name, 'pid': os.getpid()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if run([*SSH, 'getenforce'], env=ENV) != 'Enforcing':
            raise RuntimeError('bench must start Enforcing')
        before_fusion = run([*SSH, 'LD_LIBRARY_PATH=/framework/lib:/usr/lib /framework/bin/liware.tool.smctl is-active infer_expert_fusion'], env=ENV)
        before_replay = run([*SSH, 'LD_LIBRARY_PATH=/framework/lib:/usr/lib /framework/bin/liware.tool.smctl is-active infer_expert_pcm_replay'], env=ENV)
        if (before_fusion, before_replay) != ('active', 'inactive'):
            raise RuntimeError('bench service baseline differs')
        run([*SSH, 'setenforce 0'], env=ENV)
        try:
            payload = run([*SSH, f'python3 {BENCH_SCRIPT} --worker {target_name}'], env=ENV)
        finally:
            run([*SSH, 'setenforce 1'], env=ENV)
        if run([*SSH, 'getenforce'], env=ENV) != 'Enforcing':
            raise RuntimeError('SELinux restoration unverified')
        fusion = run([*SSH, 'LD_LIBRARY_PATH=/framework/lib:/usr/lib /framework/bin/liware.tool.smctl is-active infer_expert_fusion'], env=ENV)
        replay = run([*SSH, 'LD_LIBRARY_PATH=/framework/lib:/usr/lib /framework/bin/liware.tool.smctl is-active infer_expert_pcm_replay'], env=ENV)
        if (fusion, replay) != ('active', 'inactive'):
            raise RuntimeError('service restoration unverified')
        report = json.loads(payload)
        report['run_id'] = run_id
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        if json.loads(LOCK.read_text()).get('run_id') != run_id:
            raise RuntimeError('bench lock ownership changed')
        LOCK.unlink()
        return report
    except BaseException:
        # Retain the lock when deletion or restoration has not been verified.
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('target', choices=SOURCES)
    parser.add_argument('--run-id')
    args = parser.parse_args()
    try:
        if not args.worker and (not args.run_id or not args.run_id.startswith('bench-space-')):
            parser.error('controller needs a new bench-space run ID')
        print(json.dumps(worker(args.target) if args.worker else controller(args.run_id, args.target)))
    except BaseException as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
