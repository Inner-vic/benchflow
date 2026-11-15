"""Real tiny APP archives and local receipt fixtures; no SSH or hardware."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from bench_gateway import assembly_remote as assembly


class AssemblyRemoteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'alice'
        self.root.mkdir()
        self.run = 'gw-assembled-001'
        self.input_digest = 'b' * 64
        self.package = self.root / 'package-source.bin'
        self.original_yaml = b'camera:\n  enabled: true\n'
        self.package_bytes = {'livc/etc/uroad/config/common_param.yaml': self.original_yaml,
                              'livc/bin/bench-replay-mode': b'original control',
                              'livc/bin/app': b'original application'}
        self.package_bytes.update({'livc/' + name: b'original dependency' for name in assembly.TARGETS})
        self.make_package()
        self.candidate = {'owner_root': str(self.root), 'package': {'remote_path': str(self.package), 'sha256': self.sha(self.package)},
                          'replacements': [], 'dataset': {'remote_path': '/shared/data'}, 'configuration': {}}
        for index, target in enumerate(assembly.TARGETS):
            path = self.root / Path(target).name
            path.write_bytes(('private assembled dependency %d' % index).encode())
            self.candidate['replacements'].append({'kind': 'dependency', 'target': target,
                'path': str(path), 'sha256': self.sha(path)})
        yaml = self.root / 'task-only.yaml'
        yaml.write_bytes(b'camera:\n  enabled: false\n')
        self.candidate['replacements'].append({'kind': 'yaml', 'target': 'etc/uroad/config/common_param.yaml',
                                              'path': str(yaml), 'sha256': self.sha(yaml)})
        control = self.root / 'new-control'
        control.write_bytes(b'shared replacement control')
        self.candidate['controls'] = [{'target': 'bin/bench-replay-mode', 'path': str(control), 'sha256': self.sha(control)}]
        self.evidence = self.make_evidence(self.run)

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def make_package(self, extra=()):
        with tarfile.open(self.package, 'w:gz', format=tarfile.PAX_FORMAT) as archive:
            for name in ('livc', 'livc/lib', 'livc/bin', 'livc/etc', 'livc/etc/uroad', 'livc/etc/uroad/config'):
                item = tarfile.TarInfo(name)
                item.type, item.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(item)
            for name, data in self.package_bytes.items():
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(data), 0o755 if name.startswith('livc/bin/') else 0o640
                archive.addfile(item, io.BytesIO(data))
            for item, data in extra:
                if isinstance(item, str):
                    item = tarfile.TarInfo(item)
                if data is not None:
                    item.size = len(data)
                archive.addfile(item, io.BytesIO(data) if data is not None else None)

    def make_evidence(self, run, input_digest=None, candidate=None):
        task = self.base / 'tasks' / run
        task.mkdir(parents=True)
        receipts = self.base / 'runs' / run
        receipts.mkdir(parents=True)
        values = {
            'runner_state': {'state': 'COMPLETED_TO_REQUESTED_GOAL', 'run_id': run},
            'restore': {'restore_ok': True, 'run_id': run},
            'product_receipt': {'state': 'SUCCEEDED', 'producer_run_id': run,
                                'inventory_sha256': '1' * 64, 'product_identity': '2' * 64},
            'task_index': {'state': 'PASS', 'task_id': run, 'run_id': run,
                           'inventory_sha256': '1' * 64, 'product_identity': '2' * 64},
            'smoke_state': {'state': 'COMPLETED_TO_REQUESTED_GOAL', 'run_id': run + '-smoke'},
            'smoke_restore': {'restore_ok': True, 'run_id': run + '-smoke'},
            'smoke_product_receipt': {'state': 'SUCCEEDED', 'producer_run_id': run + '-smoke'},
            'binding': {'state': 'READY_FOR_GATE', 'owner': 'ou_alice', 'preparation_id': 'prep-001',
                        'frozen': {'input_digest': input_digest or self.input_digest},
                        'binding_identity': {'input_digest': input_digest or self.input_digest},
                        'candidate': copy.deepcopy(candidate or self.candidate)}}
        evidence = {'state': 'PASS', 'run_id': run, 'task': str(task), 'smoke_run': run + '-smoke', 'files': []}
        for role, value in values.items():
            path = task / 'index.json' if role == 'task_index' else receipts / (role + '.json')
            path.write_text(json.dumps(value), encoding='utf-8')
            evidence['files'].append({'role': role, 'path': str(path), 'sha256': self.sha(path)})
        return evidence

    def save(self, **kwargs):
        return assembly.save(kwargs.get('candidate', self.candidate), kwargs.get('owner_root', str(self.root)),
                             kwargs.get('source_run', self.run), kwargs.get('input_digest', self.input_digest),
                             kwargs.get('evidence', self.evidence))

    def update_receipt(self, role, mutate, evidence=None):
        evidence = evidence or self.evidence
        item = next(item for item in evidence['files'] if item['role'] == role)
        path = Path(item['path'])
        value = json.loads(path.read_text(encoding='utf-8'))
        mutate(value)
        path.write_text(json.dumps(value), encoding='utf-8')
        item['sha256'] = self.sha(path)

    def sync_binding(self, candidate=None):
        self.update_receipt('binding', lambda value: value.update(candidate=copy.deepcopy(candidate or self.candidate)))

    def test_real_archive_materializes_three_sos_and_keeps_original_yaml_and_controls(self):
        receipt = self.save()
        self.assertEqual('SAVED', receipt['state'])
        self.assertEqual('saved-assembly-v1', receipt['kind'])
        self.assertEqual('original-package-defaults', receipt['configuration_policy'])
        self.assertIs(True, receipt['requires_current_precheck'])
        self.assertEqual('NOT_RUN', receipt['hardware_actions'])
        self.assertEqual(self.sha(self.package), receipt['package_source_sha256'])
        with tarfile.open(receipt['artifact']['path'], 'r:gz') as archive:
            self.assertEqual(self.original_yaml, archive.extractfile('livc/etc/uroad/config/common_param.yaml').read())
            self.assertEqual(b'original control', archive.extractfile('livc/bin/bench-replay-mode').read())
            for item in receipt['dependencies']:
                content = Path(item['path']).read_bytes()
                self.assertEqual(content, archive.extractfile('livc/' + item['target']).read())
                self.assertEqual(item['sha256'], hashlib.sha256(content).hexdigest())
                self.assertEqual(0o640, archive.getmember('livc/' + item['target']).mode)
            self.assertEqual(0o755, archive.getmember('livc/bin/app').mode)
        manifest = json.loads(Path(receipt['manifest_path']).read_text(encoding='utf-8'))
        self.assertEqual('directory', manifest['inventory']['livc']['kind'])
        self.assertEqual('file', manifest['inventory']['livc/bin/app']['kind'])
        self.assertEqual(self.sha(Path(receipt['artifact']['path'])), receipt['artifact']['sha256'])
        proof = json.loads(Path(receipt['evidence_path']).read_text(encoding='utf-8'))
        self.assertEqual(self.run, proof['source_run'])
        self.assertEqual(8, len(proof['files']))
        self.assertIn('snapshot', proof['files'][0])
        self.assertEqual(receipt, assembly.verify(receipt, str(self.root)))

    def test_yaml_and_control_input_paths_are_never_read_or_archived(self):
        self.candidate['replacements'][-1]['path'] = str(self.base / 'unrelated' / 'absent.yaml')
        self.candidate['controls'][0]['path'] = str(self.base / 'absent-shared-control')
        self.assertEqual('SAVED', self.save()['state'])

    def test_same_content_run_is_idempotent_without_replacing_any_file(self):
        first = self.save()
        paths = [Path(first[key]) for key in ('manifest_path', 'evidence_path')] + [Path(first['artifact']['path'])]
        before = [(path.stat().st_mtime_ns, path.stat().st_ino) for path in paths]
        second = self.save()
        self.assertEqual(first, second)
        self.assertEqual(before, [(path.stat().st_mtime_ns, path.stat().st_ino) for path in paths])

    def test_same_software_different_task_does_not_change_software_or_manifest(self):
        first = self.save()
        changed = copy.deepcopy(self.candidate)
        changed['dataset'] = {'remote_path': '/other/data', 'scenes': [3]}
        changed['replacements'][-1]['sha256'] = 'f' * 64
        other_digest, other_run = 'c' * 64, 'gw-assembled-002'
        second = self.save(candidate=changed, source_run=other_run, input_digest=other_digest,
                           evidence=self.make_evidence(other_run, other_digest, changed))
        self.assertEqual(first['software_identity'], second['software_identity'])
        self.assertEqual(first['artifact'], second['artifact'])
        self.assertEqual(first['manifest_sha256'], second['manifest_sha256'])
        self.assertNotEqual(first['evidence_path'], second['evidence_path'])
        self.assertEqual(first, assembly.verify(first))
        self.assertEqual(second, assembly.verify(second))

    def test_new_dependency_content_gets_new_immutable_software_identity(self):
        first = self.save()
        changed = copy.deepcopy(self.candidate)
        replacement = self.root / 'new-m100.so'
        replacement.write_bytes(b'new dependency version')
        changed['replacements'][0].update(path=str(replacement), sha256=self.sha(replacement))
        other_run = 'gw-assembled-003'
        second = self.save(candidate=changed, source_run=other_run, evidence=self.make_evidence(other_run, candidate=changed))
        self.assertNotEqual(first['software_identity'], second['software_identity'])
        self.assertNotEqual(first['artifact']['path'], second['artifact']['path'])
        self.assertEqual(first, assembly.verify(first))

    def test_using_saved_tar_as_input_keeps_original_software_identity(self):
        first = self.save()
        reused = copy.deepcopy(self.candidate)
        reused['package'] = {'remote_path': first['artifact']['path'], 'sha256': first['artifact']['sha256']}
        reused['package_source_sha256'] = first['package_source_sha256']
        reused['replacements'] = [{'kind': 'dependency', **item} for item in first['dependencies']]
        next_run = 'gw-saved-reuse-001'
        next_digest = 'd' * 64
        result = self.save(candidate=reused, source_run=next_run, input_digest=next_digest,
                           evidence=self.make_evidence(next_run, next_digest, reused))
        self.assertNotEqual(reused['package']['sha256'], first['package_source_sha256'])
        self.assertEqual(first['software_identity'], result['software_identity'])
        self.assertEqual(first['artifact'], result['artifact'])
        self.assertEqual(first['manifest_sha256'], result['manifest_sha256'])
        self.assertEqual(result, assembly.verify(result))

    def test_changing_dependency_of_saved_tar_preserves_original_package_lineage(self):
        first = self.save()
        changed = copy.deepcopy(self.candidate)
        changed['package'] = {'remote_path': first['artifact']['path'], 'sha256': first['artifact']['sha256']}
        changed['package_source_sha256'] = first['package_source_sha256']
        changed['replacements'] = [{'kind': 'dependency', **item} for item in first['dependencies']]
        replacement = self.root / 'different-private-m100.so'
        replacement.write_bytes(b'second M100 version')
        changed['replacements'][0].update(path=str(replacement), sha256=self.sha(replacement))
        next_run = 'gw-saved-derived-001'
        result = self.save(candidate=changed, source_run=next_run,
                           evidence=self.make_evidence(next_run, candidate=changed))
        self.assertNotEqual(first['software_identity'], result['software_identity'])
        self.assertEqual(first['package_source_sha256'], result['package_source_sha256'])
        with tarfile.open(result['artifact']['path'], 'r:gz') as archive:
            self.assertEqual(self.original_yaml, archive.extractfile('livc/etc/uroad/config/common_param.yaml').read())
            self.assertEqual(replacement.read_bytes(), archive.extractfile('livc/' + assembly.TARGETS[0]).read())

    def test_existing_manifest_cannot_claim_different_source_identity(self):
        receipt = self.save()
        path = Path(receipt['manifest_path'])
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['package_source_sha256'] = 'f' * 64
        path.chmod(0o600)
        path.write_bytes(assembly.canonical(manifest))
        with self.assertRaisesRegex(assembly.AssemblyError, 'software identity mismatch'):
            self.save()

    def test_archive_survives_removed_source_paths_and_mutated_original_receipts(self):
        receipt = self.save()
        self.package.unlink()
        for item in self.candidate['replacements']:
            Path(item['path']).unlink()
        for item in self.evidence['files']:
            Path(item['path']).write_text('{}', encoding='utf-8')
        self.assertEqual(receipt, assembly.verify(receipt))

    def test_cross_owner_candidate_or_receipt_is_rejected(self):
        other = self.base / 'bob'
        other.mkdir()
        with self.assertRaisesRegex(assembly.AssemblyError, 'another owner'):
            self.save(owner_root=str(other))
        receipt = self.save()
        with self.assertRaisesRegex(assembly.AssemblyError, 'another owner'):
            assembly.verify(receipt, str(other))

    def test_foreign_source_and_dependency_paths_are_rejected(self):
        for role in ('package', 'dependency'):
            with self.subTest(role=role):
                candidate = copy.deepcopy(self.candidate)
                foreign = self.base / ('foreign-' + role)
                foreign.write_bytes(self.package.read_bytes() if role == 'package' else Path(self.candidate['replacements'][0]['path']).read_bytes())
                if role == 'package':
                    candidate['package'].update(remote_path=str(foreign), sha256=self.sha(foreign))
                else:
                    candidate['replacements'][0].update(path=str(foreign), sha256=self.sha(foreign))
                with self.assertRaisesRegex(assembly.AssemblyError, 'outside this owner'):
                    self.save(candidate=candidate)

    def test_changed_package_or_dependency_hash_blocks_publication(self):
        for role in ('package', 'dependency'):
            with self.subTest(role=role):
                candidate = copy.deepcopy(self.candidate)
                item = candidate['package'] if role == 'package' else candidate['replacements'][0]
                item['sha256'] = '0' * 64
                self.sync_binding(candidate)
                with self.assertRaisesRegex(assembly.AssemblyError, 'SHA256 mismatch'):
                    self.save(candidate=candidate)
        self.assertFalse((self.root / 'assemblies').exists())

    def test_all_three_unique_dependency_roles_are_mandatory(self):
        for replacements in (self.candidate['replacements'][1:],
                             self.candidate['replacements'] + [self.candidate['replacements'][0]],
                             self.candidate['replacements'] + [{'kind': 'dependency', 'target': 'lib/unknown.so'}]):
            with self.subTest(replacements=replacements):
                candidate = {**self.candidate, 'replacements': replacements}
                with self.assertRaises(assembly.AssemblyError):
                    self.save(candidate=candidate)

    def test_missing_regular_dependency_target_in_app_fails_closed(self):
        del self.package_bytes['livc/' + assembly.TARGETS[0]]
        self.make_package()
        self.candidate['package']['sha256'] = self.sha(self.package)
        self.sync_binding()
        with self.assertRaisesRegex(assembly.AssemblyError, 'regular targets'):
            self.save()
        self.assertEqual([], list((self.root / 'assemblies').iterdir()))

    def test_unsafe_tar_members_and_links_are_rejected(self):
        outside = tarfile.TarInfo('livc/escape')
        outside.type, outside.linkname = tarfile.SYMTYPE, '../../outside'
        linked = tarfile.TarInfo('livc/linked')
        linked.type, linked.linkname = tarfile.SYMTYPE, 'lib'
        cases = [[('../escape', b'bad')], [(outside, None)], [(linked, None), ('livc/linked/child', b'bad')],
                 [('livc/bin/app', b'duplicate')], [('other-root/child', b'bad')]]
        for extra in cases:
            with self.subTest(extra=extra):
                self.make_package(extra)
                self.candidate['package']['sha256'] = self.sha(self.package)
                self.sync_binding()
                with self.assertRaises(assembly.AssemblyError):
                    self.save()
                self.assertEqual([], list((self.root / 'assemblies').iterdir()))

    def test_safe_internal_tar_link_is_preserved_in_full_inventory(self):
        link = tarfile.TarInfo('livc/lib/current-camera.so')
        link.type, link.linkname = tarfile.SYMTYPE, 'libxcamera.so'
        self.make_package([(link, None)])
        self.candidate['package']['sha256'] = self.sha(self.package)
        self.sync_binding()
        receipt = self.save()
        manifest = json.loads(Path(receipt['manifest_path']).read_text(encoding='utf-8'))
        self.assertEqual('libxcamera.so', manifest['inventory']['livc/lib/current-camera.so']['target'])
        self.assertEqual(receipt, assembly.verify(receipt))

    def test_symlink_source_or_owner_assembly_directory_is_rejected(self):
        link = self.root / 'linked-package'
        try:
            link.symlink_to(self.package)
        except OSError as exc:
            self.skipTest('OS cannot create test symlinks: ' + str(exc))
        candidate = copy.deepcopy(self.candidate)
        candidate['package']['remote_path'] = str(link)
        with self.assertRaisesRegex(assembly.AssemblyError, 'link'):
            self.save(candidate=candidate)
        foreign = self.base / 'foreign-assemblies'
        foreign.mkdir()
        (self.root / 'assemblies').symlink_to(foreign, target_is_directory=True)
        with self.assertRaisesRegex(assembly.AssemblyError, 'link'):
            self.save()

    def test_receipt_text_or_zero_exit_without_successful_json_is_not_success(self):
        self.update_receipt('runner_state', lambda value: value.update(state='RUNNING'))
        with self.assertRaisesRegex(assembly.AssemblyError, 'not successful'):
            self.save()
        self.assertFalse((self.root / 'assemblies').exists())

    def test_smoke_must_be_current_success_and_restore_verified(self):
        for role, change in (('smoke_product_receipt', {'producer_run_id': 'historical-smoke'}),
                             ('smoke_restore', {'restore_ok': False})):
            with self.subTest(role=role):
                original = copy.deepcopy(self.evidence)
                path = Path(next(item['path'] for item in self.evidence['files'] if item['role'] == role))
                raw = path.read_bytes()
                self.update_receipt(role, lambda value: value.update(change))
                with self.assertRaises(assembly.AssemblyError):
                    self.save()
                path.write_bytes(raw)
                self.evidence = original

    def test_binding_or_product_identity_mismatch_is_rejected(self):
        for role, mutate in (('binding', lambda value: value['frozen'].update(input_digest='f' * 64)),
                             ('task_index', lambda value: value.update(product_identity='f' * 64))):
            with self.subTest(role=role):
                path = Path(next(item['path'] for item in self.evidence['files'] if item['role'] == role))
                raw, original = path.read_bytes(), copy.deepcopy(self.evidence)
                self.update_receipt(role, mutate)
                with self.assertRaises(assembly.AssemblyError):
                    self.save()
                path.write_bytes(raw)
                self.evidence = original

    def test_missing_or_changed_evidence_file_is_rejected(self):
        item = self.evidence['files'][0]
        Path(item['path']).write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(assembly.AssemblyError, 'SHA256 mismatch'):
            self.save()
        Path(item['path']).unlink()
        with self.assertRaises(assembly.AssemblyError):
            self.save()

    def test_reusing_run_id_cannot_overwrite_a_different_success_proof(self):
        receipt = self.save()
        before = Path(receipt['evidence_path']).read_bytes()
        self.update_receipt('runner_state', lambda value: value.update(extra='changed after source registration'))
        with self.assertRaisesRegex(assembly.AssemblyError, 'SHA256 mismatch'):
            self.save()
        self.assertEqual(before, Path(receipt['evidence_path']).read_bytes())

    def test_archived_artifact_or_sidecar_or_manifest_tampering_is_rejected(self):
        receipt = self.save()
        paths = [Path(receipt['artifact']['path']), Path(receipt['dependencies'][0]['path']), Path(receipt['manifest_path'])]
        for path in paths:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.chmod(0o600)
                path.write_bytes(original + b'changed')
                with self.assertRaises(assembly.AssemblyError):
                    assembly.verify(receipt)
                path.write_bytes(original)
                path.chmod(0o440)

    def test_mid_assembly_source_mutation_never_publishes_saved_archive(self):
        original_inventory = assembly._inventory
        def mutate(*args, **kwargs):
            result = original_inventory(*args, **kwargs)
            self.package.write_bytes(self.package.read_bytes() + b'concurrent change')
            return result
        with patch.object(assembly, '_inventory', side_effect=mutate):
            with self.assertRaisesRegex(assembly.AssemblyError, 'changed during assembly'):
                self.save()
        self.assertEqual([], list((self.root / 'assemblies').iterdir()))

    def test_member_or_expansion_budget_failure_never_publishes_partial(self):
        for name, limit in (('members', 2), ('expanded_bytes', 4)):
            with self.subTest(limit=name):
                with patch.dict(assembly.LIMITS, {name: limit}):
                    with self.assertRaisesRegex(assembly.AssemblyError, 'budget exceeded'):
                        self.save()
                self.assertEqual([], list((self.root / 'assemblies').iterdir()))

    def test_publication_failure_removes_pending_directory(self):
        with patch.object(assembly, '_write', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.save()
        self.assertEqual([], list((self.root / 'assemblies').iterdir()))

    def test_module_has_no_transport_or_command_execution(self):
        import ast
        source = Path(assembly.__file__).read_text(encoding='utf-8')
        imports = {alias.name.split('.')[0] for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom))
                   for alias in (node.names if isinstance(node, ast.Import) else [])}
        self.assertFalse({'subprocess', 'socket', 'paramiko'} & imports)


if __name__ == '__main__':
    unittest.main()
