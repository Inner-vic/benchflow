from unittest.mock import patch
import test_material_inspector as fixture
from bench_gateway import material_inspector as inspector


class MaterialCacheTests(fixture.MaterialInspectorTests):
    def test_cache_hit_skips_decompression_but_detects_changed_material_and_rechecks_symbols(self):
        self.request['cache_version']='a'*64
        first=inspector.inspect(self.request);self.assertEqual('PASS',first['status'])
        with patch.object(inspector,'_archive',side_effect=AssertionError('must reuse archive scan')):
            second=inspector.inspect(self.request)
        self.assertEqual('PASS',second['status']);self.assertEqual('HIT',second['evidence']['archive_cache'])
        self.assertGreaterEqual(self.symbol_check.call_count,2)
        (self.root/'libxlidar.so').write_bytes(fixture.elf()+b'changed')
        third=inspector.inspect(self.request)
        self.assertIn('HASH_MISMATCH',self.codes(third))

    def test_changed_limits_and_corrupt_cache_force_real_scan(self):
        self.request['cache_version']='a'*64;inspector.inspect(self.request)
        self.request['limits']={'max_members':1}
        self.assertIn('ARCHIVE_MEMBER_LIMIT',self.codes(inspector.inspect(self.request)))
        self.request.pop('limits')
        for p in (self.root/'.material-cache').glob('*.json'):p.write_text('{}')
        self.assertEqual('MISS',inspector.inspect(self.request)['evidence']['archive_cache'])
