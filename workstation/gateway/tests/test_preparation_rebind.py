import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from bench_gateway.bindings import preparation_needs_rebind
from bench_gateway.core import GatewayError

class RebindTests(unittest.TestCase):
    def test_missing_unsubmitted_baseline_requires_new_preparation(self):
        store=SimpleNamespace(baseline=Mock(side_effect=GatewayError('missing baseline')))
        prep={'result':{'adapter_binding':{'state':'READY_FOR_GATE','baseline_name':'old'}}}
        self.assertTrue(preparation_needs_rebind({},store,'ou_test',prep))

    def test_current_implementation_does_not_recheck_materials(self):
        store=SimpleNamespace(baseline=Mock(return_value={'data':{'kind':'candidate-v1','binding':{'implementation_hashes':{'source':'fixed'}}}}))
        prep={'result':{'adapter_binding':{'state':'READY_FOR_GATE','baseline_name':'ready'}}}
        with patch('bench_gateway.bindings.implementation_snapshot',return_value={'source':'fixed'}):
            self.assertFalse(preparation_needs_rebind({'project_root':'/program'},store,'ou_test',prep))
