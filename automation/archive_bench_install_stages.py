#!/usr/bin/env python3
"""Archive selected inactive bench install stages before removing their bench copies.

Run on the workstation. Each stage is copied to the data disk and compared with
rsync's checksum mode before its original is removed under the bench lock.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone


ROOT = Path('/home/bench-user/bench-eval')
STAGE_ROOT = Path('/app/.bench-eval-install')
ARCHIVE_ROOT = Path('/mnt/data/bench-eval/bench-stage-archive')
SSH_CONFIG = ROOT / 'config/ssh_config'
LOCK = ROOT / 'locks/bench.lock'
SSH_ARGS = [
    'ssh', '-F', str(SSH_CONFIG), '-o', 'BatchMode=no',
    '-o', 'PubkeyAuthentication=no', '-o', 'PasswordAuthentication=no',
    '-o', 'KbdInteractiveAuthentication=yes',
    '-o', 'PreferredAuthentications=keyboard-interactive',
    '-o', 'NumberOfPasswordPrompts=1',
]
ENV = dict(os.environ, SSH_ASKPASS='/bin/true', SSH_ASKPASS_REQUIRE='force', DISPLAY='bench-eval')


def run(argv, *, capture=False):
    result = subprocess.run(argv, env=ENV, text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, check=True)
    return result.stdout.strip() if capture else ''


def bench(command):
    return run([*SSH_ARGS, 'bench-avm', command], capture=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive-id', required=True)
    parser.add_argument('stage_ids', nargs='+')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}', args.archive_id) or any(
        not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}', name) for name in args.stage_ids
    ) or len(set(args.stage_ids)) != len(args.stage_ids):
        parser.error('invalid archive or stage ID')

    archive = ARCHIVE_ROOT / args.archive_id
    archive.mkdir(parents=True, exist_ok=False)
    report = {'archive': str(archive), 'stage_ids': args.stage_ids,
              'created_at': datetime.now(timezone.utc).isoformat(), 'verified': [], 'removed': []}
    lock_claimed = False
    selinux_permissive = False
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'run_id': args.archive_id, 'purpose': 'archive-install-stages',
                       'pid': os.getpid()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        lock_claimed = True
        if bench('getenforce') != 'Enforcing':
            raise RuntimeError('bench SELinux must start Enforcing')
        rsync_ssh = shlex.join(SSH_ARGS)
        for name in args.stage_ids:
            source = STAGE_ROOT / name
            if bench(f'test -d {shlex.quote(str(source))} && test ! -L {shlex.quote(str(source))}'):
                raise RuntimeError(f'invalid bench stage: {name}')
            target = archive / name
            target.mkdir()
            base = ['rsync', '-a', '--no-o', '--no-g', '-e', rsync_ssh]
            run([*base, f'bench-avm:{source}/', f'{target}/'])
            delta = run([*base, '--checksum', '--dry-run', '--delete', '--itemize-changes',
                         f'bench-avm:{source}/', f'{target}/'], capture=True)
            if delta:
                raise RuntimeError(f'archive differs from bench stage {name}: {delta[:1000]}')
            report['verified'].append(name)
            (archive / 'archive-report.json').write_text(json.dumps(report, indent=2) + '\n')
        bench('setenforce 0')
        selinux_permissive = True
        for name in args.stage_ids:
            source = STAGE_ROOT / name
            bench(f'rm -rf -- {shlex.quote(str(source))} && test ! -e {shlex.quote(str(source))}')
            report['removed'].append(name)
            (archive / 'archive-report.json').write_text(json.dumps(report, indent=2) + '\n')
        bench('setenforce 1')
        selinux_permissive = False
        if bench('getenforce') != 'Enforcing':
            raise RuntimeError('SELinux restoration unverified')
        report['bench_df'] = bench('df -h /app && df -i /app')
        report['completed_at'] = datetime.now(timezone.utc).isoformat()
        (archive / 'archive-report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))
    finally:
        if selinux_permissive:
            bench('setenforce 1')
        if lock_claimed and bench('getenforce') == 'Enforcing':
            if json.loads(LOCK.read_text()).get('run_id') != args.archive_id:
                raise RuntimeError('bench lock ownership changed')
            LOCK.unlink()


if __name__ == '__main__':
    sys.exit(main())
