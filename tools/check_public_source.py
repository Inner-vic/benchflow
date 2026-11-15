"""Static source checks; does not import business modules or contact services."""
import ast
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode('utf-8').split('\0')
errors = []
counts = {'files': 0, 'python': 0, 'json': 0}
for name in filter(None, names):
    path = root / name
    if not path.is_file():
        errors.append(name + ': missing tracked file')
        continue
    counts['files'] += 1
    raw = path.read_bytes()
    if re.search(rb'(?m)^-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', raw):
        errors.append(name + ': private key header')
    if re.search(rb'\b(?:glpat-|gh[pousr]_|github_pat_|sk-proj-)[A-Za-z0-9_-]{20,}', raw):
        errors.append(name + ': possible access token')
    try:
        if path.suffix in {'.py', '.pyw'}:
            ast.parse(raw.decode('utf-8-sig'), filename=name)
            counts['python'] += 1
        elif path.suffix == '.json':
            json.loads(raw.decode('utf-8-sig'))
            counts['json'] += 1
    except (SyntaxError, UnicodeError, ValueError) as error:
        errors.append(name + ': ' + type(error).__name__)
print(json.dumps({'status': 'failed' if errors else 'passed', **counts, 'errors': errors}))
raise SystemExit(bool(errors))
