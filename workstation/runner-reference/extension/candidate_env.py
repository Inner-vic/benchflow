#!/usr/bin/env python3
"""Manage personal workstation candidates; bench execution requires worker-once."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from environment_store import create_slot, freeze, prepare, read, set_limit, status, update
from candidate_tasks import bind, promote, submit


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', action='version', version='candidate-environments 1.0.0')
    p.add_argument("--root", type=Path, default=Path('/mnt/data/bench-eval/environments'))
    p.add_argument("--runner-root", type=Path, default=Path('/home/lixiang/bench-eval'))
    sub = p.add_subparsers(dest="action", required=True)
    for action in ('status','set-limit','create-slot','prepare','update','freeze','bind'):
        parser = sub.add_parser(action)
        parser.add_argument('--actor', required=True)
        if action not in ('status','set-limit'): parser.add_argument('--slot', default='default')
        if action=='set-limit':parser.add_argument('--maximum',required=True,type=int)
        if action=='prepare':
            parser.add_argument('--package',required=True,type=Path)
            parser.add_argument('--replacements',type=Path)
            parser.add_argument('--replace-draft',action='store_true')
        if action=='update':
            parser.add_argument('--target',required=True)
            parser.add_argument('--file',required=True,type=Path)
        if action in ('freeze','bind'):parser.add_argument('--version',required=True)
        if action=='freeze':parser.add_argument('--evidence',required=True,type=Path)
        if action=='bind':
            parser.add_argument('--expected-sha256',required=True)
            for name in ('job','profile','spec'):parser.add_argument('--'+name,required=True,type=Path)
    for action in ('submit','promote'):
        parser=sub.add_parser(action);parser.add_argument('--run-id',required=True)
    parser=sub.add_parser('worker-once')
    parser.add_argument('--legacy-profile',type=Path)
    a=p.parse_args();root=a.root.resolve();rr=a.runner_root.resolve()
    # Pin the resolved production release; do not switch Runner current.
    runtime=(rr/'current/automation').resolve()
    try:
        if a.action=='status':r=status(root,a.actor)
        elif a.action=='set-limit':r=set_limit(root,a.actor,a.maximum)
        elif a.action=='create-slot':r=create_slot(root,a.actor,a.slot)
        elif a.action=='prepare':r=prepare(root,a.actor,a.slot,a.package,
            read(a.replacements) if a.replacements else {},a.replace_draft)
        elif a.action=='update':r=update(root,a.actor,a.slot,a.target,a.file)
        elif a.action=='freeze':r=freeze(root,a.actor,a.slot,a.version,a.evidence)
        elif a.action=='bind':r=bind(root,a.actor,a.slot,a.version,a.expected_sha256,
            a.job,a.profile,a.spec,runtime)
        elif a.action=='submit':r=submit(root,a.run_id,rr/'bin/bench-eval')
        elif a.action=='promote':r=promote(root,rr/'runs',a.run_id)
        else:
            from candidate_worker import process_next
            r=process_next(root,rr/'runs',rr/'locks',runtime,a.legacy_profile)
        print(json.dumps(r,ensure_ascii=False,indent=2))
        return 2 if r.get('status') in ('BLOCKED','FAILED','UNSAFE') else 0
    except Exception as exc:
        print(json.dumps({'status':'BLOCKED','error':str(exc)},ensure_ascii=False))
        return 2


if __name__=='__main__':raise SystemExit(main())
