#!/usr/bin/env python3
"""Remove bench replay outputs after verified workstation collection.

Run on the workstation with one or more producer receipt paths after the batch
has finished. The collector has already verified the byte inventory; this
command uses its immutable receipt and does not hash the product again.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess


ROOT = Path('/home/bench-user/bench-eval')
LOCK = ROOT / 'locks/bench.lock'
SSH = ['ssh', '-F', str(ROOT / 'config/ssh_config'), '-o', 'BatchMode=no',
       '-o', 'PubkeyAuthentication=no', '-o', 'PasswordAuthentication=no',
       '-o', 'KbdInteractiveAuthentication=yes',
       '-o', 'PreferredAuthentications=keyboard-interactive',
       '-o', 'NumberOfPasswordPrompts=1', 'bench-avm']
ENV = dict(os.environ, SSH_ASKPASS='/bin/true', SSH_ASKPASS_REQUIRE='force', DISPLAY='bench-eval')
ID_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,95}$')


def bench(command):
    result = subprocess.run([*SSH, command], text=True, capture_output=True, env=ENV, check=False)
    if result.returncode:
        raise RuntimeError(f'bench command failed: {result.stderr[-900:]}')
    return result.stdout.strip()


def within(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def accepted_source(receipt_path):
    receipt = json.loads(receipt_path.read_text())
    product = Path(str(receipt.get('workstation_product') or ''))
    expected_receipt = product.with_name(product.name + '.bench-eval-product.json')
    run_id = str(receipt.get('producer_run_id') or '')
    source = PurePosixPath(str(receipt.get('source') or ''))
    if (receipt_path.resolve() != expected_receipt.resolve() or
            not product.is_dir() or product.is_symlink() or
            not within(product, Path('/mnt/data/bench-eval')) or
            receipt.get('state') not in ('SUCCEEDED', 'REUSED_VERIFIED') or
            receipt.get('product_type') != 'workstation_predictions' or
            not isinstance(receipt.get('files'), list) or not receipt['files'] or
            not re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('inventory_sha256') or '')) or
            not ID_RE.fullmatch(run_id)):
        raise RuntimeError(f'workstation product receipt is not accepted: {receipt_path}')
    # The current replay Profile uses a per-run output below /app/livc-test.
    if source.parts != ('/', 'app', 'livc-test', run_id, 'outputs', 'eval_pred_result'):
        raise RuntimeError(f'bench output is outside the per-run allowlist: {source}')
    return {'run_id': run_id, 'source': str(source), 'workstation_product': str(product),
            'product_receipt': str(receipt_path), 'inventory_sha256': receipt['inventory_sha256']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-id', required=True)
    parser.add_argument('product_receipts', nargs='+', type=Path)
    args = parser.parse_args()
    if not ID_RE.fullmatch(args.batch_id):
        parser.error('invalid batch ID')
    report_path = Path('/mnt/data/bench-eval/maintenance') / f'{args.batch_id}.json'
    if report_path.exists():
        raise RuntimeError(f'cleanup ID already used: {args.batch_id}')
    entries = [accepted_source(path) for path in args.product_receipts]
    if len({entry['source'] for entry in entries}) != len(entries):
        raise RuntimeError('duplicate bench output path')
    report = {'batch_id': args.batch_id, 'accepted': entries, 'removed': [],
              'started_at': datetime.now(timezone.utc).isoformat()}
    fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'run_id': args.batch_id, 'purpose': 'cleanup-collected-outputs', 'pid': os.getpid()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if bench('getenforce') != 'Enforcing' or bench('LD_LIBRARY_PATH=/framework/lib:/usr/lib /framework/bin/liware.tool.smctl is-active infer_expert_fusion') != 'active' or bench('LD_LIBRARY_PATH=/framework/lib:/usr/lib /framework/bin/liware.tool.smctl is-active infer_expert_pcm_replay') != 'inactive':
            raise RuntimeError('bench is not in its formal idle state')
        bench('setenforce 0')
        try:
            for entry in entries:
                quoted = shlex.quote(entry['source'])
                bench(f'test -d {quoted} && test ! -L {quoted} && rm -rf -- {quoted} && test ! -e {quoted}')
                report['removed'].append(entry['source'])
        finally:
            bench('setenforce 1')
        if bench('getenforce') != 'Enforcing' or bench('LD_LIBRARY_PATH=/framework/lib:/usr/lib /framework/bin/liware.tool.smctl is-active infer_expert_fusion') != 'active' or bench('LD_LIBRARY_PATH=/framework/lib:/usr/lib /framework/bin/liware.tool.smctl is-active infer_expert_pcm_replay') != 'inactive':
            raise RuntimeError('bench restoration unverified')
        report['completed_at'] = datetime.now(timezone.utc).isoformat()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        if json.loads(LOCK.read_text()).get('run_id') != args.batch_id:
            raise RuntimeError('bench lock ownership changed')
        LOCK.unlink()
        print(json.dumps(report))
    except BaseException:
        # Keep the lock if deletion or restoration was incomplete.
        raise


if __name__ == '__main__':
    main()
