#!/usr/bin/env python3
"""Small launcher guard; the installed Runner release itself is unchanged."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys

from environment_store import EnvironmentError, locked, read
from candidate_tasks import resolve


def option(argv, name, default=None):
    if name in argv:
        i=argv.index(name)
        if i+1>=len(argv):raise EnvironmentError('missing '+name)
        return argv[i+1]
    for value in argv:
        if value.startswith(name+'='):return value.split('=',1)[1]
    return default


def pending_bound(runs):
    terminal={'COMPLETED_TO_REQUESTED_GOAL','CANCELLED','COMPLETED','FAILED_COORDINATION'}
    for path in Path(runs).glob('*/job.json'):
        job=read(path)
        if 'candidate_binding' not in job:continue
        state=read(path.parent/'state.json').get('state','')
        if state not in terminal and not state.startswith(('FAILED','RESTORE_FAILED')):
            return True
    return False


def dispatch(argv, runner_root, store, delegate=None):
    runner_root,store=Path(runner_root).resolve(),Path(store).resolve()
    runtime=(runner_root/'current/automation').resolve()
    if argv[:1]==['candidate-env']:
        return subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('candidate_env.py')),
                               '--root',str(store),'--runner-root',str(runner_root),*argv[1:]]).returncode
    sys.path.insert(0,str(runtime))
    from queue_worker import worker_status
    from runner_state import queue_snapshot
    if delegate is None:
        delegate=lambda:subprocess.run([sys.executable,'-B',str(runtime/'bench_eval.py'),*argv]).returncode
    if argv[:1]==['submit']:
        path=option(argv,'--job')
        if path and 'candidate_binding' in read(path):
            job=read(path);_,meta=resolve(job,store)
            if option(argv,'--submitted-by')!=meta['binding']['actor']:
                raise EnvironmentError('candidate owner differs from submitted-by')
            # Serialize this decision with background-worker start to avoid a race.
            with locked(store):
                if worker_status(runner_root/'locks')['status']=='RUNNING':
                    raise EnvironmentError('stop the legacy background worker before submitting frozen candidates')
                return delegate()
    if argv[:2]==['worker','start']:
        runs=Path(option(argv,'--runs-dir',str(runner_root/'runs')))
        with locked(store):
            if pending_bound(runs):
                raise EnvironmentError('frozen candidates require foreground worker once; background start blocked')
            return delegate()
    if argv[:2]==['worker','once']:
        runs=Path(option(argv,'--runs-dir',str(runner_root/'runs')))
        locks=Path(option(argv,'--locks-dir',str(runner_root/'locks')))
        queued=queue_snapshot(runs,locks)['queued']
        if queued and 'candidate_binding' in read(runs/queued[0]['run_id']/'job.json'):
            if option(argv,'--executor','coordination-smoke')!='target-workflow':
                raise EnvironmentError('frozen candidate tasks require executor target-workflow')
            from candidate_worker import process_next
            profile=option(argv,'--profile')
            result=process_next(store,runs,locks,runtime,Path(profile) if profile else None)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 2 if result.get('status') in ('BLOCKED','FAILED','UNSAFE') else 0
    return delegate()


def main():
    try:
        return dispatch(sys.argv[1:],os.environ.get('BENCH_RUNNER_ROOT','/home/lixiang/bench-eval'),
                        os.environ.get('BENCH_CANDIDATE_ROOT','/mnt/data/bench-eval/environments'))
    except Exception as exc:
        print(json.dumps({'status':'BLOCKED','error':str(exc)},ensure_ascii=False))
        return 2


if __name__=='__main__':raise SystemExit(main())
