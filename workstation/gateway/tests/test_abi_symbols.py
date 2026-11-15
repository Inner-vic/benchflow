import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from bench_gateway import abi_symbols as abi


CAMERA = '_ZN7XCamera5startEv'
LIDAR = '_ZN6XLidar8callbackEv'


def raw_table(*rows):
    return "Symbol table '.dynsym' contains %d entries:\n Num: Value Size Type Bind Vis Ndx Name\n%s\n" % (len(rows), '\n'.join(rows))


def table(*symbols):
    rows = [' 0: 0000000000000000 0 NOTYPE LOCAL DEFAULT UND']
    for index, (name, section, binding, visibility) in enumerate(symbols, 1):
        rows.append(' %d: 0000000000000000 0 FUNC %s %s %s %s' % (index, binding, visibility, section, name))
    return raw_table(*rows)


def symbol(name, section='1', binding='GLOBAL', visibility='DEFAULT'):
    return name, section, binding, visibility


class ABISymbolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = {}
        for name in abi.LIBRARIES:
            path = self.root / name
            path.write_bytes(b'local temporary SO fixture ' + name.encode())
            self.assets[name] = {'remote_path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def check(self, camera=None, lidar=None, consumer=None):
        outputs = [camera or table(symbol(CAMERA)), lidar or table(symbol(LIDAR)),
                   consumer or table(symbol(CAMERA, 'UND'), symbol(LIDAR, 'UND'))]
        with patch.object(abi, '_readelf', side_effect=outputs) as reader:
            result = abi.check_libraries(self.assets)
        self.assertEqual(3, reader.call_count)
        return result

    def test_original_provider_rule_matches_only_limited_requirements(self):
        result = self.check()
        self.assertEqual('LIMITED_PROVIDER_SYMBOLS_MATCH', result['status'])
        self.assertEqual(2, result['required_symbol_count'])
        self.assertEqual([], result['missing_symbols'])
        self.assertFalse(result['so_executed'])
        self.assertEqual('NOT_CHECKED', result['full_abi_compatibility'])
        self.assertEqual(set(abi.LIBRARIES), set(result['libraries']))

    def test_missing_symbol_identifies_exact_provider_and_name(self):
        result = self.check(lidar=table(symbol('_ZN6XLidar3oldEv')))
        self.assertEqual('LIMITED_PROVIDER_SYMBOLS_MISSING', result['status'])
        self.assertEqual([{'provider': 'libxlidar.so', 'symbol': LIDAR}], result['missing_symbols'])

    def test_strong_undefined_only_and_fixed_markers_do_not_claim_all_dependencies(self):
        result = self.check(consumer=table(symbol(CAMERA, 'UND', 'WEAK'), symbol('unrelated_runtime', 'UND')))
        self.assertEqual('LIMITED_PROVIDER_SYMBOLS_NO_REQUIREMENTS', result['status'])
        self.assertEqual(0, result['required_symbol_count'])
        self.assertEqual('NOT_CHECKED', result['full_abi_compatibility'])

    def test_versioned_requirement_accepts_same_default_or_nondefault_version(self):
        for export in (CAMERA + '@@CAM_2', CAMERA + '@CAM_2'):
            with self.subTest(export=export):
                result = self.check(camera=table(symbol(export)), consumer=table(symbol(CAMERA + '@CAM_2', 'UND')))
                self.assertEqual('LIMITED_PROVIDER_SYMBOLS_MATCH', result['status'])

    def test_version_mismatch_and_unversioned_export_cannot_satisfy_versioned_requirement(self):
        for export in (CAMERA + '@@CAM_1', CAMERA):
            with self.subTest(export=export):
                result = self.check(camera=table(symbol(export)), consumer=table(symbol(CAMERA + '@CAM_2', 'UND')))
                self.assertEqual([{'provider': 'libxcamera.so', 'symbol': CAMERA + '@CAM_2'}], result['missing_symbols'])

    def test_unversioned_requirement_accepts_default_but_not_nondefault_version(self):
        for suffix, expected in (('@@CAM_2', 'LIMITED_PROVIDER_SYMBOLS_MATCH'), ('@CAM_2', 'LIMITED_PROVIDER_SYMBOLS_MISSING')):
            with self.subTest(suffix=suffix):
                result = self.check(camera=table(symbol(CAMERA + suffix)), consumer=table(symbol(CAMERA, 'UND')))
                self.assertEqual(expected, result['status'])

    def test_hidden_symbols_are_not_external_exports(self):
        result = self.check(camera=table(symbol(CAMERA, visibility='HIDDEN')))
        self.assertEqual('LIMITED_PROVIDER_SYMBOLS_MISSING', result['status'])

    def test_empty_unrecognized_truncated_or_oversized_table_cannot_match(self):
        cases = ('', 'there are no symbols', table().replace('1 entries', '2 entries'),
                 table(symbol(CAMERA)).replace(CAMERA, CAMERA + '@'),
                 "Symbol table '.dynsym' contains 200001 entries:\n")
        for output in cases:
            with self.subTest(output=output), self.assertRaises(abi.SymbolCheckError) as error:
                abi.parse_dynamic_symbols(output)
            self.assertEqual('SYMBOL_TABLE_UNSUPPORTED', error.exception.code)

    def test_real_unnamed_local_section_rows_are_metadata_and_still_counted(self):
        output = raw_table(' 0: 0000000000000000 0 NOTYPE LOCAL DEFAULT UND',
                           ' 1: 000000000007afd0 0 SECTION LOCAL DEFAULT 9',
                           ' 2: 00000000007b0000 0 SECTION LOCAL DEFAULT 26',
                           ' 3: 0000000000000000 0 FUNC GLOBAL DEFAULT UND ' + CAMERA)
        exported, required = abi.parse_dynamic_symbols(output)
        self.assertEqual(set(), exported)
        self.assertEqual({CAMERA}, required)
        with self.assertRaises(abi.SymbolCheckError) as error:
            abi.parse_dynamic_symbols(output.replace('4 entries', '5 entries'))
        self.assertEqual('SYMBOL_TABLE_UNSUPPORTED', error.exception.code)

    def test_missing_names_remain_rejected_outside_null_and_defined_local_section(self):
        rows = (' 1: 000000000007afd0 0 SECTION GLOBAL DEFAULT 9',
                ' 1: 000000000007afd0 0 SECTION WEAK DEFAULT 9',
                ' 1: 0000000000000000 0 SECTION LOCAL DEFAULT UND',
                ' 1: 0000000000000000 0 SECTION LOCAL DEFAULT 0',
                ' 1: 0000000000000000 0 SECTION LOCAL DEFAULT ABS',
                ' 1: 0000000000000000 0 NOTYPE LOCAL DEFAULT UND',
                ' 1: 0000000000000000 0 FUNC GLOBAL DEFAULT UND',
                ' 1: 0000000000000000 0 FUNC WEAK DEFAULT UND',
                ' 0: 0000000000000000 0 NOTYPE GLOBAL DEFAULT UND',
                ' 0: 0000000000000000 0 SECTION LOCAL DEFAULT 9')
        for row in rows:
            with self.subTest(row=row), self.assertRaises(abi.SymbolCheckError) as error:
                abi.parse_dynamic_symbols(raw_table(row))
            self.assertEqual('SYMBOL_TABLE_UNSUPPORTED', error.exception.code)

    def test_hash_mismatch_is_checked_before_invoking_readelf(self):
        self.assets['libxcamera.so']['sha256'] = '0' * 64
        with patch.object(abi, '_readelf') as reader, self.assertRaises(abi.SymbolCheckError) as error:
            abi.check_libraries(self.assets)
        self.assertEqual('SYMBOL_FILE_CHANGED', error.exception.code)
        reader.assert_not_called()

    def test_same_size_mutation_during_tool_run_is_checked_after_readelf(self):
        path = Path(self.assets['libxcamera.so']['remote_path'])
        def mutate(*args):
            data = path.read_bytes()
            path.write_bytes(b'!' + data[1:])
            return table(symbol(CAMERA))
        with patch.object(abi, '_readelf', side_effect=mutate), self.assertRaises(abi.SymbolCheckError) as error:
            abi.check_libraries(self.assets)
        self.assertEqual('SYMBOL_FILE_CHANGED', error.exception.code)

    def test_shared_deadline_stops_hashing_without_tool_or_retries(self):
        with patch.object(abi.time, 'monotonic', side_effect=[0, 2]), patch.object(abi, '_readelf') as reader:
            with self.assertRaises(abi.SymbolCheckError) as error:
                abi.check_libraries(self.assets, duration_seconds=1)
        self.assertEqual('SYMBOL_CHECK_TIMEOUT', error.exception.code)
        reader.assert_not_called()

    def test_tool_unavailable_is_explicit_and_never_executes_so(self):
        with patch.object(abi, '_tool_supported', return_value=False), patch.object(abi.subprocess, 'Popen') as process:
            with self.assertRaises(abi.SymbolCheckError) as error:
                abi.check_libraries(self.assets)
        self.assertEqual('SYMBOL_TOOL_UNAVAILABLE', error.exception.code)
        process.assert_not_called()

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires POSIX FIFO support')
    def test_fifo_substituted_for_so_is_opened_nonblocking_and_rejected(self):
        path = Path(self.assets['libxcamera.so']['remote_path'])
        path.unlink()
        os.mkfifo(str(path), 0o600)
        with patch.object(abi, '_readelf') as reader, self.assertRaises(abi.SymbolCheckError) as error:
            abi.check_libraries(self.assets, duration_seconds=1)
        self.assertEqual('SYMBOL_FILE_CHANGED', error.exception.code)
        reader.assert_not_called()

    def test_only_fixed_three_libraries_and_tightened_limits_are_accepted(self):
        for assets, kwargs in (({**self.assets, 'extra.so': {}}, {}), (self.assets, {'duration_seconds': 61}),
                               (self.assets, {'maximum_output_bytes': abi.MAX_OUTPUT_BYTES + 1})):
            with self.subTest(kwargs=kwargs), self.assertRaises(abi.SymbolCheckError) as error:
                abi.check_libraries(assets, **kwargs)
            self.assertEqual('SYMBOL_REQUEST_INVALID', error.exception.code)


class BoundedReadelfTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'owner.so'
        self.path.write_bytes(b'fixed local fixture')

    def run_tool(self, stdout=b'fixture stdout', stderr=b'', maximum=1024, exit_code=0):
        pipes = [Mock(), Mock()]
        pipes[0].fileno.return_value = 100
        pipes[1].fileno.return_value = 101
        process = Mock(stdout=pipes[0], stderr=pipes[1])
        process.poll.return_value = None
        process.wait.return_value = exit_code
        registered = {}
        selector = Mock()
        selector.register.side_effect = lambda pipe, events: registered.update({pipe: SimpleNamespace(fileobj=pipe)})
        selector.unregister.side_effect = lambda pipe: registered.pop(pipe)
        selector.get_map.side_effect = lambda: registered
        selector.select.side_effect = lambda timeout: [(key, 1) for key in list(registered.values())]
        content = {100: [stdout, b''], 101: [stderr, b'']}
        def read(fd, amount):
            return content[fd].pop(0)
        with self.path.open('rb') as stream, patch.object(abi, '_tool_supported', return_value=True), \
                patch.object(abi.os, 'set_blocking'), patch.object(abi.os, 'read', side_effect=read), \
                patch.object(abi.selectors, 'DefaultSelector', return_value=selector), \
                patch.object(abi.subprocess, 'Popen', return_value=process) as spawn:
            try:
                result = abi._readelf(stream, abi.time.monotonic() + 5, maximum)
            except abi.SymbolCheckError as error:
                return error, process, spawn
        return result, process, spawn

    def test_fixed_readelf_reads_inherited_fd_and_never_executes_owner_path(self):
        result, process, spawn = self.run_tool()
        self.assertEqual('fixture stdout', result)
        command = spawn.call_args.args[0]
        self.assertEqual([abi.READELF, '--dyn-syms', '--wide', '--'], command[:4])
        self.assertTrue(command[4].startswith('/proc/self/fd/'))
        self.assertNotIn(str(self.path), command)
        self.assertEqual('C', spawn.call_args.kwargs['env']['LC_ALL'])
        self.assertFalse(spawn.call_args.kwargs.get('shell', False))
        self.assertTrue(spawn.call_args.kwargs['pass_fds'])

    def test_stdout_and_stderr_share_output_limit_and_process_is_killed(self):
        result, process, _ = self.run_tool(stdout=b'12345', stderr=b'67890', maximum=8)
        self.assertIsInstance(result, abi.SymbolCheckError)
        self.assertEqual('SYMBOL_OUTPUT_LIMIT', result.code)
        process.kill.assert_called_once()

    def test_nonzero_readelf_failure_is_not_a_match_and_raw_stderr_is_not_exposed(self):
        result, _, _ = self.run_tool(stderr=b'SECRET PATH OR INPUT', exit_code=1)
        self.assertEqual('SYMBOL_READELF_FAILED', result.code)
        self.assertNotIn('SECRET', str(result))

    def test_tool_timeout_kills_process_without_retries(self):
        with patch.object(abi.time, 'monotonic', side_effect=[0, 0, 6]):
            result, process, spawn = self.run_tool()
        self.assertEqual('SYMBOL_CHECK_TIMEOUT', result.code)
        process.kill.assert_called_once()
        self.assertEqual(1, spawn.call_count)


if __name__ == '__main__':
    unittest.main()
