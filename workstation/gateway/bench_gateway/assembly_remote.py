"""Immutable workstation APP assemblies; no bench, subprocess, or package execution.

The caller must first verify the real full replay and its *current* precheck
with Runner's validate_and_retain.  This module independently checks the small
success receipts supplied by that caller.  A saved assembly contains the source
APP and exactly the three dependency replacements.  Task YAML and shared replay
controls never become reusable software defaults.
"""
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import tarfile
import time
import uuid


TARGETS = ('lib/liburoad_m100.so', 'lib/libxcamera.so', 'lib/libxlidar.so')
SAFE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$')
SHA = re.compile(r'^[0-9a-f]{64}$')
LIMITS = {'seconds': 240, 'package_bytes': 8 * 1024 ** 3,
          'expanded_bytes': 32 * 1024 ** 3, 'dependency_bytes': 2 * 1024 ** 3,
          'members': 100000, 'json_bytes': 4 * 1024 ** 2,
          'evidence_bytes': 16 * 1024 ** 2}
REQUIRED_EVIDENCE = ('runner_state', 'restore', 'product_receipt', 'task_index',
                     'smoke_state', 'smoke_restore', 'smoke_product_receipt', 'binding')


class AssemblyError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise AssemblyError('invalid SHA256 identity')
    return value


class Budget:
    def __init__(self):
        self.started = time.monotonic()

    def check(self):
        if time.monotonic() - self.started > LIMITS['seconds']:
            raise AssemblyError('assembly time budget exceeded')


def _path(value):
    if not isinstance(value, (str, Path)) or not str(value) or any(ord(c) < 32 for c in str(value)):
        raise AssemblyError('invalid workstation path')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or path.absolute() != path:
        raise AssemblyError('canonical absolute workstation path required')
    return path


def _no_links(path, allow_missing=False):
    for item in reversed((path, *path.parents)):
        try:
            observed = item.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise AssemblyError('workstation path is missing: ' + str(item)) from None
        if stat.S_ISLNK(observed.st_mode) or item.resolve() != item:
            raise AssemblyError('workstation path contains a link: ' + str(item))


def _root(value):
    root = _path(value)
    _no_links(root)
    if not root.is_dir():
        raise AssemblyError('owner root must be an existing directory')
    return root


def _owned(root, value, allow_missing=False):
    path = _path(value)
    if root not in path.parents:
        raise AssemblyError('software path is outside this owner directory')
    _no_links(path, allow_missing)
    return path


def _fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


class Source:
    """A pinned regular file; before/after checks fail on concurrent changes."""
    def __init__(self, path, budget, maximum, expected=None):
        self.path, self.budget = path, budget
        _no_links(path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise AssemblyError('source is not a bounded nonempty regular file: ' + str(path))
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(str(path), flags)
        self.stream = os.fdopen(descriptor, 'rb')
        self.before_path = _fingerprint(before)
        self.before = _fingerprint(os.fstat(self.stream.fileno()))
        try:
            # Windows path stat reports birth time as ctime, while fd stat may
            # report change time. Compare each source against its own baseline.
            if ((self.before != self.before_path) if os.name != 'nt'
                    else (self.before[:5] != self.before_path[:5])):
                raise AssemblyError('source changed while opening')
            digest = hashlib.sha256()
            for chunk in iter(lambda: self.stream.read(1024 * 1024), b''):
                budget.check()
                digest.update(chunk)
            self.sha256 = digest.hexdigest()
            self.size = before.st_size
            self.check()
            if expected is not None and self.sha256 != _sha(expected):
                raise AssemblyError('source SHA256 mismatch: ' + str(path))
            self.stream.seek(0)
        except BaseException:
            self.stream.close()
            raise

    def check(self):
        self.budget.check()
        _no_links(self.path)
        if (_fingerprint(os.fstat(self.stream.fileno())) != self.before
                or _fingerprint(self.path.lstat()) != self.before_path):
            raise AssemblyError('source changed during assembly: ' + str(self.path))

    def close(self):
        self.stream.close()


def _json_source(path, expected, budget, maximum=None):
    with contextlib.closing(Source(path, budget, maximum or LIMITS['json_bytes'], expected)) as source:
        raw = source.stream.read()
        source.check()
        try:
            value = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise AssemblyError('invalid JSON receipt: ' + str(path)) from None
        if not isinstance(value, dict):
            raise AssemblyError('receipt must be a JSON object')
        return value, raw


def _software_inputs(candidate):
    if not isinstance(candidate, dict) or not isinstance(candidate.get('package'), dict) or not isinstance(candidate.get('replacements'), list):
        raise AssemblyError('complete software binding required')
    package_sha = _sha(candidate['package'].get('sha256'))
    dependencies = []
    for item in candidate['replacements']:
        if not isinstance(item, dict):
            raise AssemblyError('invalid software binding replacement')
        if item.get('kind') == 'dependency':
            dependencies.append({'target': item.get('target'), 'sha256': _sha(item.get('sha256'))})
    if len(dependencies) != 3 or {item['target'] for item in dependencies} != set(TARGETS):
        raise AssemblyError('three unique software binding dependency roles required')
    return {'owner_root': candidate.get('owner_root'), 'package_sha256': package_sha,
            'package_source_sha256': _sha(candidate.get('package_source_sha256', package_sha)),
            'dependencies': sorted(dependencies, key=lambda item: item['target'])}


def _evidence(source_run, input_digest, evidence, budget, owner_root, candidate):
    if (not isinstance(evidence, dict) or evidence.get('state') != 'PASS'
            or evidence.get('run_id') != source_run
            or evidence.get('smoke_run') != source_run + '-smoke'):
        raise AssemblyError('real full replay and current precheck evidence required')
    task = _path(evidence.get('task'))
    _no_links(task)
    if not task.is_dir() or task.name != source_run:
        raise AssemblyError('retained task does not belong to the source run')
    supplied = evidence.get('files')
    if not isinstance(supplied, list) or not len(REQUIRED_EVIDENCE) <= len(supplied) <= 24:
        raise AssemblyError('complete bounded success receipt inventory required')
    records, objects, total = [], {}, 0
    for item in supplied:
        if not isinstance(item, dict) or not SAFE.fullmatch(str(item.get('role', ''))):
            raise AssemblyError('invalid evidence role')
        role = item['role']
        if role in objects:
            raise AssemblyError('duplicate evidence role')
        path = _path(item.get('path'))
        value, raw = _json_source(path, _sha(item.get('sha256')), budget)
        total += len(raw)
        if total > LIMITS['evidence_bytes']:
            raise AssemblyError('success evidence exceeds budget')
        objects[role] = value
        records.append({'role': role, 'path': str(path), 'sha256': item['sha256'],
                        'bytes': len(raw), 'snapshot': value})
    if not set(REQUIRED_EVIDENCE).issubset(objects):
        raise AssemblyError('required success evidence roles are missing')
    for prefix, run in (('', source_run), ('smoke_', source_run + '-smoke')):
        state = objects[prefix + ('state' if prefix else 'runner_state')]
        restored = objects[prefix + 'restore']
        product = objects[prefix + 'product_receipt']
        if (state.get('state') != 'COMPLETED_TO_REQUESTED_GOAL'
                or (state.get('run_id') is not None and state.get('run_id') != run)
                or restored.get('restore_ok') is not True
                or (restored.get('run_id') is not None and restored.get('run_id') != run)
                or product.get('state') != 'SUCCEEDED' or product.get('producer_run_id') != run):
            raise AssemblyError('Runner completion, restoration or producer receipt is not successful')
    index = objects['task_index']
    if (index.get('state') != 'PASS' or index.get('run_id') != source_run
            or _path(next(item['path'] for item in records if item['role'] == 'task_index')) != task / 'index.json'):
        raise AssemblyError('task index does not identify the successful source run')
    product = objects['product_receipt']
    for key in ('inventory_sha256', 'product_identity'):
        if not product.get(key) or index.get(key) != product.get(key):
            raise AssemblyError('retained task differs from the producer receipt')
    binding = objects['binding']
    if (binding.get('frozen', {}).get('input_digest') != input_digest
            or binding.get('binding_identity', {}).get('input_digest') != input_digest
            or binding.get('candidate', {}).get('owner_root') != str(owner_root)
            or binding.get('state') != 'READY_FOR_GATE'):
        raise AssemblyError('source run is not bound to these frozen inputs')
    if _software_inputs(binding.get('candidate')) != _software_inputs(candidate):
        raise AssemblyError('successful binding describes different software inputs')
    value = {'schema_version': 1, 'state': 'PASS', 'source_run': source_run,
             'input_digest': input_digest, 'task': str(task), 'smoke_run': source_run + '-smoke',
             'verified_by': 'caller-validate-and-retain;receipt-crosschecks-v1',
             'files': sorted(records, key=lambda item: item['role'])}
    if len(canonical(value)) > LIMITS['evidence_bytes']:
        raise AssemblyError('success evidence exceeds budget')
    return value


def _layout(member):
    name = member.name
    if (not name or len(name) > 4096 or '\\' in name or name.startswith('/')
            or any(ord(c) < 32 for c in name) or '..' in PurePosixPath(name).parts
            or len(canonical(member.pax_headers)) > 256 * 1024):
        raise AssemblyError('unsafe APP archive member')
    parts = [part for part in name.split('/') if part not in ('', '.')]
    if not parts and member.isdir():
        return None
    if not parts or parts[0] != 'livc' or (len(parts) == 1 and not member.isdir()):
        raise AssemblyError('APP must contain exactly one livc root')
    name = '/'.join(parts)
    if member.isdir():
        kind = 'directory'
    elif member.isfile() and not getattr(member, 'sparse', None):
        kind = 'file'
    elif member.issym():
        target = member.linkname
        normalized = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        if (not target or target.startswith('/') or '\\' in target
                or any(ord(c) < 32 for c in target)
                or normalized != 'livc' and not normalized.startswith('livc/')):
            raise AssemblyError('APP link escapes livc')
        kind = 'link'
    else:
        raise AssemblyError('unsupported APP archive member type')
    return name, kind


class _Reader:
    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget
        self.digest, self.bytes = hashlib.sha256(), 0

    def read(self, size=-1):
        self.budget.check()
        value = self.stream.read(size)
        self.digest.update(value)
        self.bytes += len(value)
        return value


def _inventory(archive, budget, replacements=None, output=None):
    inventory, found, expanded = {}, set(), 0
    for number, member in enumerate(archive):
        budget.check()
        if number >= LIMITS['members']:
            raise AssemblyError('APP member budget exceeded')
        layout = _layout(member)
        if layout is None:
            continue
        name, kind = layout
        if name in inventory:
            raise AssemblyError('duplicate APP archive member')
        record = {'kind': kind, 'mode': member.mode & 0o777}
        info = tarfile.TarInfo(name)
        info.mode, info.uid, info.gid, info.mtime = record['mode'], 0, 0, 0
        if kind == 'file':
            relative = name[len('livc/'):]
            source = (replacements or {}).get(relative)
            if source is not None:
                found.add(relative)
                source.check()
                source.stream.seek(0)
                stream, size = source.stream, source.size
            else:
                stream, size = archive.extractfile(member), member.size
            expanded += max(size, member.size)
            if expanded > LIMITS['expanded_bytes']:
                raise AssemblyError('APP expanded byte budget exceeded')
            reader = _Reader(stream, budget)
            info.size = size
            if output is not None:
                output.addfile(info, reader)
            else:
                while reader.read(1024 * 1024):
                    pass
            if reader.bytes != size:
                raise AssemblyError('truncated APP archive member')
            record.update(bytes=size, sha256=reader.digest.hexdigest())
        elif kind == 'directory':
            info.type = tarfile.DIRTYPE
            if output is not None:
                output.addfile(info)
        else:
            record['target'] = member.linkname
            info.type, info.linkname = tarfile.SYMTYPE, member.linkname
            if output is not None:
                output.addfile(info)
        inventory[name] = record
    for name in inventory:
        for parent in PurePosixPath(name).parents:
            if str(parent) in inventory and inventory[str(parent)]['kind'] != 'directory':
                raise AssemblyError('APP member descends through a non-directory')
    if replacements is not None and found != set(TARGETS):
        raise AssemblyError('APP must contain regular targets for all three dependencies')
    if not inventory or not any(item['kind'] == 'file' for item in inventory.values()):
        raise AssemblyError('APP is empty')
    return inventory


def _write(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o440)


def _copy_source(source, path):
    source.stream.seek(0)
    digest = hashlib.sha256()
    with path.open('xb') as stream:
        for chunk in iter(lambda: source.stream.read(1024 * 1024), b''):
            source.budget.check()
            stream.write(chunk)
            digest.update(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    if digest.hexdigest() != source.sha256:
        raise AssemblyError('dependency changed while archiving')
    source.check()
    path.chmod(0o440)


def _receipt(root, directory, manifest, manifest_sha, evidence_path, evidence_sha, proof):
    return {'schema_version': 1, 'kind': 'saved-assembly-v1', 'state': 'SAVED',
            'owner_root': str(root), 'source_run': proof['source_run'], 'input_digest': proof['input_digest'],
            'software_identity': directory.name, 'package_source_sha256': manifest['package_source_sha256'],
            'artifact': manifest['artifact'], 'dependencies': manifest['dependencies'],
            'configuration_policy': 'original-package-defaults',
            'manifest_path': str(directory / 'manifest.json'), 'manifest_sha256': manifest_sha,
            'evidence_path': str(evidence_path), 'evidence_sha256': evidence_sha,
            'requires_current_precheck': True, 'hardware_actions': 'NOT_RUN'}


def _verify_software(root, directory, budget, expected_manifest_sha=None):
    path = _owned(root, directory / 'manifest.json')
    with contextlib.closing(Source(path, budget, LIMITS['evidence_bytes'], expected_manifest_sha)) as source:
        manifest_sha = source.sha256
        try:
            manifest = json.loads(source.stream.read().decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise AssemblyError('invalid assembly manifest') from None
    if (manifest.get('schema_version') != 1 or manifest.get('kind') != 'saved-assembly-v1'
            or manifest.get('owner_root') != str(root) or manifest.get('software_identity') != directory.name
            or manifest.get('configuration_policy') != 'original-package-defaults'):
        raise AssemblyError('assembly manifest owner or identity mismatch')
    dependencies = manifest.get('dependencies')
    if not isinstance(dependencies, list) or [item.get('target') for item in dependencies] != list(TARGETS):
        raise AssemblyError('assembly dependencies are incomplete')
    identity = {'package_source_sha256': _sha(manifest.get('package_source_sha256')),
                'dependencies': [{'target': item['target'], 'sha256': _sha(item.get('sha256'))} for item in dependencies]}
    if hashlib.sha256(canonical(identity)).hexdigest() != directory.name:
        raise AssemblyError('assembly software identity mismatch')
    for item in dependencies:
        expected_path = directory / 'dependencies' / PurePosixPath(item['target']).name
        if item.get('path') != str(expected_path):
            raise AssemblyError('assembly dependency path mismatch')
        with contextlib.closing(Source(_owned(root, expected_path), budget, LIMITS['dependency_bytes'], item['sha256'])) as source:
            if item.get('bytes') != source.size:
                raise AssemblyError('assembly dependency byte count mismatch')
    artifact = manifest.get('artifact', {})
    expected_path = directory / 'assembled.app.tar.gz'
    if artifact.get('path') != str(expected_path):
        raise AssemblyError('assembled artifact path mismatch')
    with contextlib.closing(Source(_owned(root, expected_path), budget, LIMITS['package_bytes'], artifact.get('sha256'))) as source:
        if source.size != artifact.get('bytes'):
            raise AssemblyError('assembled artifact byte count mismatch')
        with tarfile.open(fileobj=source.stream, mode='r:gz') as archive:
            inventory = _inventory(archive, budget)
        source.check()
    if inventory != manifest.get('inventory'):
        raise AssemblyError('assembled APP inventory differs from manifest')
    for item in dependencies:
        target = inventory.get('livc/' + item['target'], {})
        if target.get('kind') != 'file' or target.get('sha256') != item['sha256']:
            raise AssemblyError('assembled APP dependency differs from sidecar')
    return manifest, manifest_sha


def verify(receipt, owner_root=None):
    """Revalidate this owner's complete artifact, three sidecars and saved proof.

    Returns the validated receipt unchanged.  It does not require the original
    APP/dependency paths, dataset, current draft, or a live bench.
    """
    if not isinstance(receipt, dict) or receipt.get('schema_version') != 1 or receipt.get('kind') != 'saved-assembly-v1' or receipt.get('state') != 'SAVED':
        raise AssemblyError('saved assembly receipt required')
    root = _root(owner_root if owner_root is not None else receipt.get('owner_root'))
    if receipt.get('owner_root') != str(root):
        raise AssemblyError('saved assembly belongs to another owner directory')
    software_identity = _sha(receipt.get('software_identity'))
    directory = root / 'assemblies' / software_identity
    if receipt.get('manifest_path') != str(directory / 'manifest.json'):
        raise AssemblyError('saved assembly manifest path mismatch')
    budget = Budget()
    manifest, manifest_sha = _verify_software(root, directory, budget, _sha(receipt.get('manifest_sha256')))
    run = receipt.get('source_run')
    if not isinstance(run, str) or not SAFE.fullmatch(run):
        raise AssemblyError('invalid source run ID')
    evidence_path = directory / 'evidence' / (run + '.json')
    if receipt.get('evidence_path') != str(evidence_path):
        raise AssemblyError('saved assembly evidence path mismatch')
    proof, _ = _json_source(_owned(root, evidence_path), _sha(receipt.get('evidence_sha256')), budget, LIMITS['evidence_bytes'])
    if (proof.get('software_identity') != software_identity or proof.get('owner_root') != str(root)
            or proof.get('state') != 'PASS' or proof.get('source_run') != run
            or proof.get('input_digest') != receipt.get('input_digest')):
        raise AssemblyError('saved success proof identity mismatch')
    expected = _receipt(root, directory, manifest, manifest_sha, evidence_path, receipt['evidence_sha256'], proof)
    if receipt != expected:
        raise AssemblyError('saved assembly receipt differs from immutable archive')
    return receipt


def save(candidate, owner_root, source_run, input_digest, evidence):
    """Promote a truly successful candidate's software to an owner-only archive.

    No historical/global software search or fallback is performed.  The same
    software content is idempotent; each successful run has separate immutable
    evidence.  Failure never publishes a partial software directory.
    """
    root = _root(owner_root)
    if not isinstance(candidate, dict) or candidate.get('owner_root') != str(root):
        raise AssemblyError('candidate belongs to another owner directory')
    if not isinstance(source_run, str) or not SAFE.fullmatch(source_run):
        raise AssemblyError('invalid source run ID')
    _sha(input_digest)
    budget = Budget()
    proof = _evidence(source_run, input_digest, evidence, budget, root, candidate)
    package = candidate.get('package') or {}
    replacements = candidate.get('replacements')
    if not isinstance(replacements, list):
        raise AssemblyError('candidate dependency inventory required')
    dependencies = {}
    for item in replacements:
        if not isinstance(item, dict):
            raise AssemblyError('invalid candidate replacement')
        if item.get('kind') != 'dependency':
            continue
        target = item.get('target')
        if target not in TARGETS or target in dependencies:
            raise AssemblyError('exactly the three approved dependency targets required')
        dependencies[target] = item
    if set(dependencies) != set(TARGETS):
        raise AssemblyError('three private dependencies required')
    with contextlib.ExitStack() as stack:
        source = stack.enter_context(contextlib.closing(Source(
            _owned(root, package.get('remote_path') or package.get('path')), budget,
            LIMITS['package_bytes'], _sha(package.get('sha256')))))
        sources = {}
        for target in TARGETS:
            item = dependencies[target]
            sources[target] = stack.enter_context(contextlib.closing(Source(
                _owned(root, item.get('path') or item.get('remote_path')), budget,
                LIMITS['dependency_bytes'], _sha(item.get('sha256')))))
        identity = {'package_source_sha256': _sha(candidate.get('package_source_sha256', source.sha256)),
                    'dependencies': [{'target': target, 'sha256': sources[target].sha256} for target in TARGETS]}
        software_identity = hashlib.sha256(canonical(identity)).hexdigest()
        proof.update(owner_root=str(root), software_identity=software_identity)
        proof_raw = canonical(proof)
        evidence_sha = hashlib.sha256(proof_raw).hexdigest()
        assemblies = _owned(root, root / 'assemblies', allow_missing=True)
        assemblies.mkdir(mode=0o700, exist_ok=True)
        _owned(root, assemblies)
        directory = assemblies / software_identity
        pending = None
        try:
            if directory.exists() or directory.is_symlink():
                manifest, manifest_sha = _verify_software(root, directory, budget)
            else:
                pending = assemblies / ('.pending-' + software_identity + '-' + uuid.uuid4().hex)
                pending.mkdir(mode=0o700)
                (pending / 'dependencies').mkdir(mode=0o700)
                records = []
                for target in TARGETS:
                    path = pending / 'dependencies' / PurePosixPath(target).name
                    _copy_source(sources[target], path)
                    records.append({'target': target, 'path': str(directory / 'dependencies' / path.name),
                                    'sha256': sources[target].sha256, 'bytes': sources[target].size})
                artifact = pending / 'assembled.app.tar.gz'
                source.stream.seek(0)
                with artifact.open('xb') as output:
                    with gzip.GzipFile(filename='', mode='wb', compresslevel=1, mtime=0, fileobj=output) as compressed:
                        with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                            with tarfile.open(fileobj=source.stream, mode='r:gz') as original:
                                inventory = _inventory(original, budget, sources, archive)
                    output.flush()
                    os.fsync(output.fileno())
                source.check()
                for item in sources.values():
                    item.check()
                artifact.chmod(0o440)
                with contextlib.closing(Source(artifact, budget, LIMITS['package_bytes'])) as assembled:
                    artifact_record = {'path': str(directory / artifact.name), 'sha256': assembled.sha256, 'bytes': assembled.size}
                manifest = {'schema_version': 1, 'kind': 'saved-assembly-v1', 'owner_root': str(root),
                            'software_identity': software_identity, **identity,
                            'configuration_policy': 'original-package-defaults',
                            'artifact': artifact_record, 'dependencies': records, 'inventory': inventory}
                manifest_raw = canonical(manifest)
                if len(manifest_raw) > LIMITS['evidence_bytes']:
                    raise AssemblyError('assembly inventory exceeds manifest budget')
                _write(pending / 'manifest.json', manifest_raw)
                manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
                (pending / 'evidence').mkdir(mode=0o700)
                _write(pending / 'evidence' / (source_run + '.json'), proof_raw)
                try:
                    pending.rename(directory)
                    pending = None
                except OSError:
                    if not directory.exists():
                        raise
                    manifest, manifest_sha = _verify_software(root, directory, budget)
            source.check()
            for item in sources.values():
                item.check()
            path = _owned(root, directory / 'evidence' / (source_run + '.json'), allow_missing=True)
            if path.exists() or path.is_symlink():
                with contextlib.closing(Source(_owned(root, path), budget, LIMITS['evidence_bytes'], evidence_sha)):
                    pass
            else:
                temporary = path.with_name('.pending-' + path.name + '-' + uuid.uuid4().hex)
                try:
                    _write(temporary, proof_raw)
                    # Hard-link creation is atomic and cannot replace another success proof.
                    os.link(str(temporary), str(path))
                except FileExistsError:
                    with contextlib.closing(Source(_owned(root, path), budget, LIMITS['evidence_bytes'], evidence_sha)):
                        pass
                finally:
                    if temporary.exists():
                        try:
                            temporary.unlink()
                        except PermissionError:
                            temporary.chmod(0o600)
                            temporary.unlink()
                            # On Windows the readonly bit belongs to the shared inode.
                            if path.exists() and not path.is_symlink():
                                path.chmod(0o440)
            return _receipt(root, directory, manifest, manifest_sha, path, evidence_sha, proof)
        finally:
            if pending is not None and pending.exists():
                for path in pending.rglob('*'):
                    if path.is_file() and not path.is_symlink():
                        path.chmod(0o600)
                shutil.rmtree(pending)
