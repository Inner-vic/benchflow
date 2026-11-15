"""Single software-build worker, separate from every hardware queue and Runner."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
import urllib.error

from .config import load
from .core import owner_key
from .pcm_dual_build import live_drafts
from .pcm_build_jobs import Jobs, BuildFailure, Uncertain, ReadPending
from .pcm_build_engine import Engine
from .pcm_build_remote import Remote
from .pcm_build_cards import deliver


def enabled(cfg):
    try:
        d = json.loads((Path(cfg['project_root'])/'runtime/pcm-dual-validation.json').read_text())
        return d.get('enabled') is True and d.get('jenkins_enabled') is True
    except (OSError, ValueError): return False


class Worker:
    def __init__(self, cfg, *, store=None, remote_factory=None, client=None, verify_recipe=None, output_root=None):
        self.cfg = cfg;self.store = store or live_drafts(cfg['state_root']);self.jobs = Jobs(self.store)
        self.remote_factory = remote_factory or (lambda owner: Remote(cfg, owner))
        if client is None:
            from .task_cards import _client
            client = _client(cfg)
        self.client = client
        if verify_recipe is None:
            scripts = Path(cfg['project_root'])/'extensions/pcm-dual-validation/scripts'
            sys.path.insert(0, str(scripts))
            from source_build_discovery import verify_recipe
        self.verify_recipe = verify_recipe
        self.output_root = Path(output_root) if output_root is not None else Path('/mnt/data/bench-eval/users')

    def notifications(self):
        for job in self.jobs.deliveries():
            try: deliver(self.store, self.client, self.jobs, job)
            except Exception:
                # Keep only the durable outbox bit. No execution state changes.
                pass

    def once(self):
        self.notifications()
        # Maintenance/disable stops claiming new software jobs without cancelling
        # an already submitted Jenkins build; known in-flight jobs remain queryable.
        if not enabled(self.cfg): return False
        from .maintenance import notice
        job = self.jobs.next(allow_new=not bool(notice(self.cfg)))
        if not job: return False
        try:
            root = self.output_root/owner_key(job['owner'])/'builds'/job['id']
            if os.name != 'nt' or self.output_root != Path('/mnt/data/bench-eval/users'):
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = root/'confirmed-review.json'
                raw = json.dumps(job['review'], ensure_ascii=False, indent=2)
                if path.exists():
                    if path.read_text() != raw: raise Uncertain('工作站构建快照已被改变。')
                else:
                    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w') as f: f.write(raw)
            Engine(self.jobs, self.remote_factory(job['owner']), self.verify_recipe).advance(job)
        except BuildFailure as error:
            self.jobs.update(job['id'], state='FAILED', progress='构建已停止', error=str(error))
        except Uncertain as error:
            self.jobs.update(job['id'], state='NEEDS_REVIEW', progress='构建状态待核验', error=str(error))
        except ReadPending as error:
            if self.jobs.get(job['id'])['progress'] != str(error):
                self.jobs.update(job['id'], progress=str(error))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            # All mutations have their own durable ambiguity handling. Here only
            # known/read-only stages are retried, never a blind Build request.
            current = self.jobs.get(job['id'])
            if current['progress'] != '远端状态查询暂未完成，稍后继续':
                self.jobs.update(job['id'], progress='远端状态查询暂未完成，稍后继续')
        except Exception:
            self.jobs.update(job['id'], state='NEEDS_REVIEW', progress='构建状态待核验',
                             error='后台处理未完成，请管理员核对已保存的派发收据；不会自动重新发包。')
        self.notifications()
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args();cfg = load(args.root)
    state = Path(cfg['state_root']).resolve()
    if os.name == 'nt' or Path('/mnt/data/bench-eval') not in state.parents:
        raise ValueError('Build worker must run on the Linux business workstation')
    # This is ONLY a process guard for this software worker. It never opens,
    # inspects or changes the production hardware bench.lock.
    import fcntl
    guard = state/'pcm-software-worker.guard'
    fd = os.open(guard, os.O_CREAT|os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
    worker = Worker(cfg);stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop);signal.signal(signal.SIGINT, stop)
    print('pcm-build-worker ready; software queue only', flush=True)
    while not stopping:
        worker.once()
        if args.once: break
        for _ in range(10):
            if stopping: break
            time.sleep(1)
    os.close(fd)


if __name__ == '__main__': main()
