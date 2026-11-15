"""Publication boundaries; no business imports, credentials, network or hardware."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('checks',Path(__file__).parents[1]/'check_repository.py')
checks=importlib.util.module_from_spec(spec);spec.loader.exec_module(checks)

class RepositoryBoundaryTests(unittest.TestCase):
    def test_source_and_safe_schema_are_allowed(self):
        self.assertEqual([],checks.boundary_errors('workstation/gateway/config/intent-schema.json',b'{"app_secret":{"type":"string"}}'))

    def test_generated_build_and_debug_outputs_are_rejected(self):
        for name in ('cloud/evaluator/build/CMakeFiles/a.o','src/vc140.pdb','build/a.out'):
            with self.subTest(name=name):self.assertTrue(checks.boundary_errors(name,b'output'))

    def test_business_and_private_configuration_are_rejected(self):
        for name in ('state/gateway.sqlite3','config/gateway.json','runtime/lease.json','credential.key'):
            with self.subTest(name=name):self.assertTrue(checks.boundary_errors(name,b'{}'))

    def test_renamed_binary_is_rejected(self):
        self.assertTrue(checks.boundary_errors('source/disguised.py',b'\x7fELF\0binary'))

    def test_private_key_detection_does_not_echo_content(self):
        raw=b'-----BEGIN '+b'OPENSSH PRIVATE KEY-----\nfictional-content\n'
        errors=checks.boundary_errors('config/hidden.txt',raw)
        self.assertTrue(errors);self.assertNotIn('fictional-content',str(errors))

    def test_access_token_detection_does_not_echo_value(self):
        raw=b'glpat-'+b'A'*23
        errors=checks.boundary_errors('code.py',raw)
        self.assertTrue(errors);self.assertNotIn(raw.decode(),str(errors))

    def test_only_fixed_vendor_parser_is_allowed_under_runtime(self):
        prefix='workstation/gateway/vendor/offline_eval/scripts/runtime/'
        self.assertEqual([],checks.boundary_errors(prefix+'parse_eval_report.py',b'# fixed upstream source'))
        self.assertTrue(checks.boundary_errors(prefix+'credentials.json',b'{}'))

if __name__=='__main__':unittest.main()
