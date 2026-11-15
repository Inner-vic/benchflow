"""Read-only syntax, relative-document-link and source-tree boundary checks.

No imports of business modules, network access, dependency installs or hardware.
This is a static check, not a secret scanner or deployment validation.
"""
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT=Path(__file__).resolve().parents[1]

FORBIDDEN_EXTENSIONS=re.compile(r'\.(?:sqlite\d*|db|exe|so(?:\.\d+)*|pem|key|zip|tar(?:\.gz)?|7z|o|obj|pdb|dll|a|lib|bin|log|tmp|npy|npz|pcap|bag)(?:-wal|-shm)?$',re.I)
PRIVATE_HEADER=re.compile(rb'(?m)^-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----')
TOKEN=re.compile(rb'(?:glpat-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|sk-proj-[A-Za-z0-9_-]{24,})')

def boundary_errors(relative,raw):
    """Report only paths/rules, never the content of suspected credentials."""
    parts=Path(relative).parts;name=parts[-1];errors=[]
    fixed_vendor_parser=relative=='workstation/gateway/vendor/offline_eval/scripts/runtime/parse_eval_report.py'
    if any(p in ('state','__pycache__','CMakeFiles') or p=='runtime' and not fixed_vendor_parser for p in parts):
        errors.append('runtime/state/generated cache requires separate management')
    if FORBIDDEN_EXTENSIONS.search(name) or name in ('gateway.json','credentials.json','.env','a.out','CMakeCache.txt','.ninja_deps','.ninja_log'):
        errors.append('private configuration, binary or generated output is excluded')
    if b'\0' in raw or raw.startswith((b'\x7fELF',b'MZ',b'PK\x03\x04')):
        errors.append('binary content is excluded')
    if PRIVATE_HEADER.search(raw) or TOKEN.search(raw):
        errors.append('possible embedded private key or access token; inspect locally')
    if len(raw)>2*1024**2:
        errors.append('file exceeds source-only 2MiB review boundary')
    return errors

def main():
    result=subprocess.run(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT,capture_output=True,check=True)
    files=sorted({ROOT/p for p in result.stdout.decode('utf-8').split('\0') if p})
    errors=[];python_count=markdown_count=link_count=json_count=0
    for path in files:
        if not path.is_file():continue
        relative=path.relative_to(ROOT).as_posix()
        raw=path.read_bytes()
        errors.extend(relative+': '+message for message in boundary_errors(relative,raw))
        if path.suffix=='.json':
            json_count+=1
            try:json.loads(raw.decode('utf-8-sig'))
            except (ValueError,UnicodeError):errors.append(relative+': invalid JSON/UTF-8')
        if path.suffix=='.py':
            python_count+=1
            try:ast.parse(path.read_text(encoding='utf-8-sig'),filename=relative)
            except (SyntaxError,UnicodeError) as error:errors.append(relative+': '+str(error))
        if path.suffix=='.md':
            markdown_count+=1
            for raw in re.findall(r'\]\(([^)]+)\)',path.read_text(encoding='utf-8-sig')):
                target=raw.strip('<>').split('#')[0]
                if not target or '://' in target or target.startswith(('mailto:','/')):continue
                link_count+=1
                if not (path.parent/unquote(target)).exists():errors.append(relative+': missing relative link '+target)
    fixed=ROOT/'workstation/gateway/vendor/offline_eval'
    provenance=fixed/'provenance.json'
    if provenance.is_file():
        for name,record in json.loads(provenance.read_text(encoding='utf-8'))['files'].items():
            p=fixed/name
            if (Path(name).is_absolute() or '..' in Path(name).parts or not p.is_file()
                    or hashlib.sha256(p.read_bytes()).hexdigest()!=record['sha256']
                    or p.stat().st_size!=record['size_bytes']):
                errors.append('fixed vendor source missing or modified: '+name)
    print(f'Python AST {python_count}; JSON {json_count}; Markdown {markdown_count}; relative links {link_count}; errors {len(errors)}')
    for error in errors:print(error)
    return bool(errors)

if __name__=='__main__':sys.exit(main())
