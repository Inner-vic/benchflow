#!/usr/bin/env python3
"""Default-off SSH bridge. Calculates on the workstation; never transfers raw inputs."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess

ENTRY='/home/bench-user/bench-eval/extensions/arrival-accuracy/run.py'
SAFE=re.compile(r'^[a-z0-9][a-z0-9._-]{0,95}$')


def command(args):
    remote=['python3','-B',ENTRY,'--enabled']
    if args.check_runtime:
        remote.append('--check-runtime')
    else:
        if not args.task_id or not SAFE.fullmatch(args.task_id):
            raise ValueError('valid --task-id is required')
        if not args.truth_root:
            raise ValueError('--truth-root is required')
        remote += ['--task-id',args.task_id]
    if args.truth_root:
        if not args.truth_root.startswith('/') or '\x00' in args.truth_root:
            raise ValueError('truth root must be an absolute workstation path')
        remote += ['--truth-root',args.truth_root]
    if args.attempt_id:
        if not SAFE.fullmatch(args.attempt_id):
            raise ValueError('invalid attempt ID')
        remote += ['--attempt-id',args.attempt_id]
    if args.execute:
        remote.append('--execute')
    return [args.ssh,'-o','BatchMode=yes','-o','UpdateHostKeys=no','ubuntu-workstation',' '.join(shlex.quote(x) for x in remote)]


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--enabled',action='store_true')
    p.add_argument('--execute',action='store_true')
    p.add_argument('--check-runtime',action='store_true')
    p.add_argument('--task-id');p.add_argument('--truth-root');p.add_argument('--attempt-id')
    p.add_argument('--ssh',default='ssh')
    a=p.parse_args(argv)
    if not a.enabled:
        print(json.dumps({'state':'NOT_REQUESTED','enabled':False,'ssh_called':False})); return 0
    try:
        return subprocess.run(command(a),check=False).returncode
    except (OSError,ValueError) as exc:
        print(json.dumps({'state':'BLOCKED','error':str(exc)})); return 2


if __name__=='__main__':
    raise SystemExit(main())
