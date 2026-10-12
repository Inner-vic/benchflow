import sys, argparse, json
sys.path.insert(0, '/tmp')
import run_stream_batch as batch
valid = []
for path in batch.BASE.joinpath('adapters').iterdir():
    if not path.is_dir():
        continue
    try:
        batch.source_contract(path.name)
        valid.append(path)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        pass
if not valid:
    raise RuntimeError('No valid source run for read-only audit')
source = max(valid, key=lambda path: path.stat().st_mtime).name
args = argparse.Namespace(source_run=source, run_id='skill-audit-20261009-310', task_id=None,
                          actor='colleague-audit', scenes='all', resume_delivery=False)
p = batch.plan(args)
result = batch.offline_validate(p)
job, profile, adapter = batch.derive(p)
assert job['coordination']['allowed_actors'] == ['colleague-audit']
print(json.dumps({'state': 'PLAN_READY', 'source_run': source, 'actor': p['actor'],
                  'scene_count': p['scene_count'], 'submitted': False, **result}, indent=2))
