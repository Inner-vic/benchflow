import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bench_gateway.registry import Registry,CATALOG_SCRIPT
from bench_gateway.core import GatewayError,Store,digest


class LocalSSH:
    """Run the exact catalog protocol against temporary directories, no network."""
    def python(self,code,args=(),timeout=60):
        r=subprocess.run([sys.executable,'-',*args],input=code,text=True,encoding='utf-8',capture_output=True,timeout=timeout)
        if r.returncode:raise GatewayError('catalog rejected request')
        return r.stdout


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.s=Store(self.root/'local');self.s.allow('ou_alice');self.s.allow('ou_bob')
        self.cfg={'remote_users_root':str(self.root/'remote')}
        self.registry=Registry(self.cfg,self.s,LocalSSH())

    def tearDown(self):self.tmp.cleanup()

    def register(self,user,name='A',value=b'package'):
        root=Path(self.s.remote_root(user,self.cfg['remote_users_root']));root.mkdir(parents=True,exist_ok=True)
        p=root/'package.bin';p.write_bytes(value)
        return self.registry.call(user,'register',asset={'kind':'package','name':name,'data':{'remote_path':str(p),'sha256':digest(p)}})

    def test_catalog_is_authoritative_on_remote_side(self):
        a=self.register('ou_alice');root=Path(self.s.remote_root('ou_alice',self.cfg['remote_users_root']))
        self.assertTrue((root/'catalog.json').is_file())
        self.assertNotIn('local_path',a['data'])
        self.assertEqual(self.registry.list('ou_alice')[0]['id'],a['id'])

    def test_catalog_owner_isolation(self):
        self.register('ou_bob',name='private-bob')
        self.assertEqual(self.registry.list('ou_alice'),[])
        with self.assertRaises(GatewayError):self.registry.get('ou_alice','package','private-bob')

    def test_catalog_rejects_foreign_path_even_if_content_matches(self):
        a=self.register('ou_bob')
        with self.assertRaises(GatewayError):self.registry.call('ou_alice','register',asset={'kind':'package','name':'A','data':a['data']})

    def test_remote_registration_rejects_wrong_digest(self):
        a=self.register('ou_alice')
        with self.assertRaises(GatewayError):self.registry.call('ou_alice','register',asset={'kind':'package','name':'wrong','data':{**a['data'],'sha256':'0'*64}})

    def test_remote_registration_reuses_same_content_version(self):
        a=self.register('ou_alice');b=self.register('ou_alice')
        self.assertEqual(a['id'],b['id']);self.assertEqual(b['version'],1)

    def test_remote_new_content_creates_version(self):
        a=self.register('ou_alice');b=self.register('ou_alice',value=b'new')
        self.assertEqual(b['version'],2);self.assertNotEqual(a['id'],b['id'])

    def test_catalog_owner_tampering_is_rejected(self):
        self.register('ou_alice');root=Path(self.s.remote_root('ou_alice',self.cfg['remote_users_root']))
        p=root/'catalog.json';v=json.loads(p.read_text());v['owner']='ou_bob';p.write_text(json.dumps(v))
        with self.assertRaises(GatewayError):self.registry.list('ou_alice')


if __name__=='__main__':unittest.main()
