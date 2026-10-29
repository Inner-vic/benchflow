#!/usr/bin/env python3
"""Build an isolated, hash-pinned workstation postprocessor; no pip installs."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent
MODULES=['arrival_workstation.py','render_arrival_workbook.py','arrival_input_lineage.py',
         'batch_arrival_accuracy.py','trial_arrival_accuracy.py','probe_arrival_inputs.py',
         'prediction_transfer.py','workflow_receipts.py','ssh_transport.py']

LAUNCHER='''#!/usr/bin/env python3
import hashlib,json,runpy,sys
from pathlib import Path
root=Path(__file__).resolve().parent
manifest=json.loads((root/'manifest.json').read_text())
for name,digest in manifest['files'].items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:
  raise SystemExit('Extension checksum mismatch: '+name)
sys.dont_write_bytecode=True
sys.path.insert(0,str(root))
runpy.run_path(str(root/'arrival_workstation.py'),run_name='__main__')
'''


def build(output, dependency_root):
    files={name:(ROOT/name).read_bytes() for name in MODULES}
    files['vendor/arrival-core/speedbump_accuracy.py']=(ROOT/'vendor/arrival-core/speedbump_accuracy.py').read_bytes()
    files['vendor/arrival-core/PROVENANCE.json']=(ROOT/'vendor/arrival-core/PROVENANCE.json').read_bytes()
    for name in ['openpyxl','et_xmlfile','openpyxl-3.1.5.dist-info','et_xmlfile-2.0.0.dist-info']:
        folder=dependency_root/name
        if not folder.is_dir():
            raise ValueError('missing bundled library: '+str(folder))
        for p in sorted(folder.rglob('*')):
            if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc':
                files['vendor/'+str(p.relative_to(dependency_root)).replace('\\','/')]=p.read_bytes()
    files['run.py']=LAUNCHER.encode()
    files['manifest.json']=json.dumps({'schema_version':1,'purpose':'optional arrival accuracy postprocessing',
         'files':{n:hashlib.sha256(b).hexdigest() for n,b in sorted(files.items())}},indent=2).encode()
    output.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(output,'w:gz') as archive:
        for name,blob in sorted(files.items()):
            info=tarfile.TarInfo(name); info.size=len(blob); info.mode=0o644
            archive.addfile(info,io.BytesIO(blob))
    return {'state':'BUILT','path':str(output.resolve()),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'files':len(files)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--dependency-root',required=True,type=Path)
    a=p.parse_args(); print(json.dumps(build(a.output,a.dependency_root),indent=2))
