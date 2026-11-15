import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bench_gateway.core import GatewayError
from bench_gateway.recipes import load,select
from bench_gateway.package_assembly import prepare
from bench_gateway.package_assembly_remote import derive
from bench_gateway.m100_build import reusable
from bench_gateway.candidate_remote import cached_package_inventory
from unittest.mock import patch


class ReleaseRecipesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.recipe=load()[0]
        self.cfg={'project_root':str(Path(__file__).resolve().parents[1])}

    def test_raw_and_canonical_packages_select_the_same_reviewed_release(self):
        raw=self.recipe['assembly']['source_sha256'];canonical=self.recipe['assembly']['output_sha256']
        self.assertIsNone(select(self.cfg,raw))
        self.assertEqual(self.recipe,select(self.cfg,raw,raw=True))
        self.assertEqual(self.recipe,select(self.cfg,canonical))
        self.assertIsNone(select(self.cfg,'0'*64,raw=True))

    def test_new_recipe_is_data_and_invalid_commit_or_path_is_rejected(self):
        (self.root/'config').mkdir();path=self.root/'config/release-recipes.json'
        (self.root/'config/m100-conan-settings.yml').write_bytes((Path(self.cfg['project_root'])/'config/m100-conan-settings.yml').read_bytes())
        valid=copy.deepcopy(self.recipe);valid['id']='example-reviewed-release'
        path.write_text(json.dumps({'schema_version':1,'recipes':[valid]}))
        cfg={'project_root':str(self.root)}
        self.assertEqual('example-reviewed-release',load(cfg)[0]['id'])
        for key,value in (('commit','main'),('source','/tmp/../source')):
            bad=copy.deepcopy(valid);bad['build'][key]=value
            path.write_text(json.dumps({'schema_version':1,'recipes':[bad]}))
            with self.assertRaises(GatewayError):load(cfg)

    def test_raw_app_with_wrong_sensor_never_reaches_workstation(self):
        registry=SimpleNamespace(ssh=Mock());store=Mock();user='ou_me'
        app={'owner':user,'data':{'sha256':self.recipe['assembly']['source_sha256']}}
        with self.assertRaises(GatewayError):prepare(self.cfg,store,registry,user,app,{})
        registry.ssh.python.assert_not_called()

    def test_reuse_is_owner_and_sensor_scoped_and_caller_still_must_inspect(self):
        user='ou_me';package={'owner':user,'data':{'sha256':self.recipe['package_sha256'][0]}}
        sensors={role:{'owner':user,'data':{'sha256':sha}} for role,sha in self.recipe['sensors'].items()}
        asset={'owner':user,'kind':'dependency','data':{'target':'lib/liburoad_m100.so',
               'sha256':self.recipe['m100']['sha256'],'remote_path':'/data/my/file.so'}}
        found,receipt=reusable(self.cfg,user,package,sensors,[asset])
        self.assertIs(found,asset);self.assertEqual('REUSED',receipt['state'])
        foreign={**asset,'owner':'ou_other'}
        self.assertEqual((None,None),reusable(self.cfg,user,package,sensors,[foreign]))
        sensors['lidar_so']['data']['sha256']='0'*64
        self.assertEqual((None,None),reusable(self.cfg,user,package,sensors,[asset]))

    def test_assembly_is_deterministic_and_only_expected_files_change(self):
        old=b'key: old\n';new=b'key: new\n';dependency=self.root/'libvehicle_status.so';dependency.write_bytes(b'reviewed-library')
        source=self.root/'raw.tar.gz'
        with tarfile.open(source,'w:gz') as tar:
            for name,data in (('livc/config.yaml',old),('livc/lib/original.so',b'unchanged')):
                item=tarfile.TarInfo(name);item.size=len(data);item.mode=0o644;tar.addfile(item,io.BytesIO(data))
        assembly={'archive_prefix':'livc/','dependency':{'source':str(dependency),'target':'lib/libvehicle_status.so','sha256':hashlib.sha256(dependency.read_bytes()).hexdigest()},
                  'yaml_patches':{'config.yaml':{'before_sha256':hashlib.sha256(old).hexdigest(),'after_sha256':hashlib.sha256(new).hexdigest(),'old':old.decode(),'new':new.decode()}}}
        first=self.root/'one.tar.gz';second=self.root/'two.tar.gz'
        a=derive(source,first,assembly,time.monotonic()+10);b=derive(source,second,assembly,time.monotonic()+10)
        self.assertEqual(a['sha256'],b['sha256']);self.assertEqual(1,a['unchanged_members'])
        with tarfile.open(first) as tar:
            self.assertEqual(new,tar.extractfile('livc/config.yaml').read())
            self.assertEqual(b'unchanged',tar.extractfile('livc/lib/original.so').read())
        assembly['yaml_patches']['config.yaml']['before_sha256']='0'*64
        with self.assertRaises(AssertionError):derive(source,self.root/'bad.tar.gz',assembly,time.monotonic()+10)

    def test_binding_inventory_cache_invalidates_on_content_and_implementation(self):
        source=self.root/'app.tar.gz'
        with tarfile.open(source,'w:gz') as tar:
            data=b'key: value\n';item=tarfile.TarInfo('livc/etc/uroad/config/common_param.yaml');item.size=len(data)
            tar.addfile(item,io.BytesIO(data))
        cache={'root':str(self.root),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'version':'a'*64}
        first=cached_package_inventory(source,cache)
        with patch('bench_gateway.candidate_remote.package_inventory',side_effect=AssertionError('cache miss')):
            self.assertEqual(first,cached_package_inventory(source,cache))
            with self.assertRaises(AssertionError):cached_package_inventory(source,{**cache,'version':'b'*64})
        source.write_bytes(source.read_bytes()+b'changed')
        with self.assertRaises(ValueError):cached_package_inventory(source,cache)


if __name__=='__main__':unittest.main()
