"""Pinned shared evaluator contract; verification never downloads or deploys files."""
import json
from pathlib import Path


def contract():
    path = Path(__file__).resolve().parents[1] / 'assets/shared-workstation.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema_version') != 1 or value.get('download_or_deploy') is not False:
        raise ValueError('invalid shared workstation contract')
    return value


def validate_command(name, command, pin=None):
    pin = pin or contract()
    actions = {'prepare_config': 'prepare', 'precheck': 'precheck',
               'evaluate': 'evaluate', 'collect_report': 'collect'}
    argv = command['argv']
    if argv[:4] != ['python3', '-B', pin['worker']['path'], actions[name]]:
        raise ValueError('commands must use the pinned shared workstation worker')
    if argv.count('--package') != 1:
        raise ValueError('exactly one pinned --package is required')
    i = argv.index('--package')
    if i + 1 >= len(argv) or argv[i + 1] != pin['evaluator']['root']:
        raise ValueError('evaluator path differs from the pinned shared package')


GUARD = r'''
import hashlib,json,os,sys
from pathlib import Path

def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as stream:
  for block in iter(lambda:stream.read(1048576),b''):h.update(block)
 return h.hexdigest()

def checked(root,name):
 path=(root/name).resolve()
 if root not in path.parents:raise ValueError('package path escapes pinned root')
 return path

try:
 pin=json.loads(sys.argv[1]); mode=sys.argv[2]
 root=Path(pin['evaluator']['root'])
 if root.resolve()!=root:raise ValueError('pinned package root was redirected')
 manifest_path=checked(root,pin['evaluator']['manifest_file'])
 if sha(manifest_path)!=pin['evaluator']['manifest_sha256']:
  raise ValueError('package manifest differs from Skill pin')
 manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
 if len(manifest['files'])!=pin['evaluator']['file_count']:
  raise ValueError('package file count differs from Skill pin')
 expected_names=set()
 for item in manifest['files']:
  if item['path'] in expected_names:raise ValueError('duplicate package manifest entry')
  expected_names.add(item['path'])
  path=checked(root,item['path'])
  if path.stat().st_size!=item['size'] or sha(path)!=item['sha256']:
   raise ValueError('package file differs from pinned manifest: '+item['path'])
 for name,digest in pin['evaluator']['key_files'].items():
  if sha(checked(root,name))!=digest:raise ValueError('pinned key file changed: '+name)
 worker=Path(pin['worker']['path'])
 if worker.resolve()!=worker or sha(worker)!=pin['worker']['sha256']:
  raise ValueError('shared worker differs from Skill pin')
 if mode=='probe':
  runs=Path(pin['runs_root'])
  print(json.dumps({'worker_exists':True,'worker_verified':True,'package_verified':True,
    'package_version':pin['evaluator']['version'],
    'runs_writable':runs.is_dir() and os.access(str(runs),os.W_OK)}))
 elif mode=='execute':
  argv=sys.argv[3:]
  if argv[:3]!=['python3','-B',str(worker)]:raise ValueError('unreviewed worker command')
  os.execvp(argv[0],argv)
 else:raise ValueError('unknown verification mode')
except Exception as exc:
 print('SHARED_RUNTIME_PIN_MISMATCH: '+str(exc),file=sys.stderr)
 sys.exit(42)
'''


def guarded_argv(argv=None, *, pin=None):
    return ['python3', '-B', '-c', GUARD,
            json.dumps(pin or contract(), separators=(',', ':')),
            'execute' if argv is not None else 'probe', *(argv or [])]
