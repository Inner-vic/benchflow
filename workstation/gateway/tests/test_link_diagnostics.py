import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bench_gateway import link_diagnostics as links


class NetworkEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sys = self.root / 'net'
        self.sys.mkdir()
        self.config = 'hostname 192.0.2.15\nport 22\n'
        self.route = [{'dst': '192.0.2.15', 'dev': 'eno1.3', 'prefsrc': '192.0.2.16'}]
        self.neighbor = [{'dst': '192.0.2.15', 'dev': 'eno1.3',
                          'lladdr': '02:00:00:00:ce:81', 'state': ['REACHABLE']}]
        self.calls = []

    def interface(self, name, carrier='1', lowers=()):
        root = self.sys / name
        root.mkdir(exist_ok=True)
        if carrier is not None:
            (root / 'carrier').write_text(carrier, encoding='ascii')
        (root / 'operstate').write_text('up', encoding='ascii')
        (root / 'speed').write_text('1000', encoding='ascii')
        (root / 'duplex').write_text('full', encoding='ascii')
        for lower in lowers:
            (root / ('lower_' + lower)).mkdir(exist_ok=True)

    def command(self, argv, limit=32768):
        self.calls.append(argv)
        if argv[0] == 'ssh':
            return 0, self.config
        if argv[1:4] == ['-j', 'route', 'get']:
            return 0, json.dumps(self.route)
        if argv[1:4] == ['-j', 'neigh', 'show']:
            return 0, json.dumps(self.neighbor)
        self.fail('unexpected command: ' + repr(argv))

    def probe(self, responded=False):
        with patch.object(links, 'SYS_NET', self.sys), patch.object(links, '_command', side_effect=self.command):
            return links.inspect_network(self.root, responded)

    def test_carrier_down_without_response_requests_on_site_check(self):
        self.interface('eno1.3', '0')
        value = self.probe()
        self.assertEqual(value['state'], 'PHYSICAL_LINK_DOWN')
        self.assertEqual(value['physical_state'], 'DOWN')
        text = links.format_network(value)
        self.assertIn('检查连线、台架供电及网口', text)
        self.assertNotIn('物理断线', text)

    def test_carrier_up_does_not_imply_authenticated_device_or_readiness(self):
        self.interface('eno1.3')
        value = self.probe()
        self.assertEqual(value['state'], 'LINK_UP')
        self.assertEqual(value['neighbor']['lladdr'], '02:00:00:00:ce:81')
        self.assertFalse(value['identity_verified'])
        self.assertFalse(value['neighbor']['identity_evidence'])
        self.assertIn('不能证明台架设备身份', links.format_network(value))
        self.assertEqual(self.calls, [
            ['ssh', '-G', '-F', str(self.root / 'config/ssh_config'), 'bench-avm'],
            ['ip', '-j', 'route', 'get', '192.0.2.15'],
            ['ip', '-j', 'neigh', 'show', 'to', '192.0.2.15', 'dev', 'eno1.3']])

    def test_missing_carrier_is_unknown(self):
        self.interface('eno1.3', None)
        value = self.probe()
        self.assertEqual(value['state'], 'UNKNOWN')
        self.assertEqual(value['physical_state'], 'UNKNOWN')
        self.assertEqual(value['interfaces'][0]['carrier'], 'UNKNOWN')

    def test_vlan_lower_physical_interface_is_inspected(self):
        self.interface('eno1.3', '1', ('eno1',))
        self.interface('eno1', '1')
        value = self.probe()
        self.assertEqual([(x['name'], x['role']) for x in value['interfaces']],
                         [('eno1.3', 'route'), ('eno1', 'lower')])
        self.assertEqual(value['physical_state'], 'UP')
        self.assertIn('eno1（下层网口）有载波，1000 Mbps，全双工', links.format_network(value))

    def test_ssh_response_takes_precedence_over_down_carrier_snapshot(self):
        self.interface('eno1.3', '0')
        value = self.probe(True)
        self.assertEqual(value['state'], 'SSH_RESPONDED')
        self.assertEqual(value['physical_state'], 'INCONSISTENT')
        self.assertIn('SSH_CARRIER_SNAPSHOT_DISAGREEMENT', value['issues'])
        text = links.format_network(value)
        self.assertIn('不能据此判定物理断线', text)
        self.assertNotIn('请现场检查', text)

    def test_empty_route_is_reported_without_guessing_interface(self):
        self.route = []
        value = self.probe()
        self.assertEqual(value['state'], 'NO_ROUTE')
        self.assertEqual(value['route']['state'], 'MISSING')
        self.assertEqual(value['interfaces'], [])
        self.assertEqual(len(self.calls), 2)

    def test_no_route_does_not_erase_prior_ssh_response(self):
        self.route = []
        value = self.probe(True)
        self.assertEqual(value['state'], 'SSH_RESPONDED')
        self.assertIn('SSH_ROUTE_SNAPSHOT_DISAGREEMENT', value['issues'])

    def test_route_command_unreachable_has_explicit_missing_route(self):
        def command(argv, limit=32768):
            return (0, self.config) if argv[0] == 'ssh' else (2, 'RTNETLINK answers: Network is unreachable\n')
        with patch.object(links, '_command', side_effect=command):
            value = links.inspect_network(self.root)
        self.assertEqual(value['state'], 'NO_ROUTE')

    def test_proxyjump_and_proxycommand_stop_direct_link_inference(self):
        for setting in ('proxyjump jump.internal\n', 'proxycommand /usr/bin/nc proxy 22\n'):
            with self.subTest(setting=setting):
                self.calls = []
                self.config = 'hostname 192.0.2.15\nport 22\n' + setting
                value = self.probe(True)
                self.assertEqual(value['state'], 'CONFIG_UNSUPPORTED')
                self.assertEqual(value['issues'], ['PROXY_CONFIGURED'])
                self.assertEqual(len(self.calls), 1)
                self.assertTrue(value['ssh_responded'])

    def test_explicit_proxy_none_is_accepted(self):
        self.config += 'proxyjump none\nproxycommand none\n'
        self.interface('eno1.3')
        self.assertEqual(self.probe()['state'], 'LINK_UP')

    def test_dns_or_scoped_address_does_not_trigger_network_lookup(self):
        for address in ('bench.internal', 'fe80::1%eno1'):
            with self.subTest(address=address):
                self.calls = []
                self.config = 'hostname ' + address + '\nport 22\n'
                value = self.probe()
                self.assertEqual(value['state'], 'UNKNOWN')
                self.assertEqual(len(self.calls), 1)

    def test_invalid_interface_cannot_escape_sysfs_root(self):
        self.route[0]['dev'] = '../../private'
        value = self.probe()
        self.assertEqual(value['state'], 'UNKNOWN')
        self.assertEqual(value['interfaces'], [])

    def test_failed_optional_commands_do_not_raise_or_erase_response(self):
        with patch.object(links, '_command', return_value=None):
            value = links.inspect_network(self.root, True)
        self.assertTrue(value['ssh_responded'])
        self.assertEqual(value['state'], 'SSH_RESPONDED')
        self.assertEqual(value['issues'], ['SSH_CONFIG_UNAVAILABLE'])
        self.assertIn('已响应', links.format_network(value))

    def test_malformed_optional_display_data_keeps_ssh_response(self):
        value = {'route': None, 'interfaces': {}, 'ssh_responded': True}
        self.assertIn('已响应', links.format_network(value))

    def test_bounded_json_rejects_oversized_list(self):
        with patch.object(links, '_command', return_value=(0, json.dumps([{}] * 65))):
            self.assertEqual(links._json_command(['ip']), (None, 'INVALID_JSON'))

    def test_oversized_sysfs_field_is_unknown(self):
        self.interface('eno1.3', '1' * 257)
        self.assertEqual(self.probe()['interfaces'][0]['carrier'], 'UNKNOWN')

    def test_cyclic_lower_devices_are_bounded(self):
        self.interface('eno1.3', lowers=('eno1',))
        self.interface('eno1', lowers=('eno1.3',))
        value = self.probe()
        self.assertEqual(len(value['interfaces']), 2)
        self.assertEqual(value['physical_state'], 'UNKNOWN')

    def test_remote_code_is_python38_and_contains_the_native_functions(self):
        tree = ast.parse(links.REMOTE_CODE, feature_version=(3, 8))
        names = {item.name for item in tree.body if isinstance(item, ast.FunctionDef)}
        self.assertIn('inspect_network', names)
        self.assertIn('_command', names)
        self.assertNotIn('format_network', names)
        self.assertNotIn('socket', links.REMOTE_CODE)
        self.assertNotIn('ping', links.REMOTE_CODE)


if __name__ == '__main__':
    unittest.main()
