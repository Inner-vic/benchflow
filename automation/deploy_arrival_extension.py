#!/usr/bin/env python3
"""Deploy only the isolated arrival extension, leaving Runner and bench intact."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

REMOTE='/home/bench-user/bench-eval/extensions/arrival-accuracy'
INSTALL=r'''
import hashlib,json,os,subprocess,sys,tarfile,tempfile
from pathlib import Path
base=Path(sys.argv[1]); archive=Path(sys.argv[2]); digest=sys.argv[3]
if hashlib.sha256(archive.read_bytes()).hexdigest()!=digest: raise SystemExit('archive checksum mismatch')
base.mkdir(parents=True,exist_ok=True)
version=base/'versions'/digest
version.parent.mkdir(exist_ok=True)
if not version.exists():
 with tempfile.TemporaryDirectory(prefix='.stage-',dir=str(base)) as temp:
  stage=Path(temp)
  with tarfile.open(archive,'r:gz') as tar:
   for member in tar.getmembers():
    p=Path(member.name)
    if p.is_absolute() or '..' in p.parts or not member.isfile(): raise SystemExit('unsafe package entry')
   tar.extractall(stage)
  manifest=json.loads((stage/'manifest.json').read_text())
  for name,want in manifest['files'].items():
   if hashlib.sha256((stage/name).read_bytes()).hexdigest()!=want: raise SystemExit('file checksum mismatch')
  subprocess.run(['python3','-B',str(stage/'run.py'),'--enabled','--check-runtime'],check=True)
  stage.rename(version)
subprocess.run(['python3','-B',str(version/'run.py'),'--enabled','--check-runtime'],check=True)
active={'path':str(version),'sha256':digest}
(base/'active.json.tmp').write_text(json.dumps(active)+'\n')
os.replace(str(base/'active.json.tmp'),str(base/'active.json'))
launcher="import json,runpy\nfrom pathlib import Path\nb=Path(__file__).resolve().parent\np=Path(json.loads((b/'active.json').read_text())['path'])\nif p.parent != b/'versions': raise SystemExit('invalid extension version path')\nrunpy.run_path(str(p/'run.py'),run_name='__main__')\n"
(base/'run.py.tmp').write_text(launcher)
os.replace(str(base/'run.py.tmp'),str(base/'run.py'))
archive.unlink()
print(json.dumps({'state':'DEPLOYED','entry':str(base/'run.py'),'version':str(version),'sha256':digest}))
'''


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',type=Path,required=True)
    p.add_argument('--ssh',default='ssh');p.add_argument('--scp',default='scp')
    a=p.parse_args()
    digest=hashlib.sha256(a.package.read_bytes()).hexdigest()
    remote='/tmp/arrival-extension-'+digest+'.tar.gz'
    subprocess.run([a.scp,'-o','BatchMode=yes',str(a.package),'ubuntu-workstation:'+remote],check=True)
    command='python3 -c '+shlex.quote(INSTALL)+' '+shlex.quote(REMOTE)+' '+shlex.quote(remote)+' '+digest
    subprocess.run([a.ssh,'-o','BatchMode=yes','ubuntu-workstation',command],check=True)


if __name__=='__main__':
    main()
