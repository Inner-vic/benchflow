"""Personal software aliases preserve the exact material identity and SO roles."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bench_gateway.core import Store
from bench_gateway.preparation import MaterialCheck, TARGETS, material_name
from bench_gateway.registry import Registry


class MaterialNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cfg={'state_root':str(self.root/'state'),'project_root':str(Path(__file__).resolve().parents[1]),
                  'mode':'simulate','live_enabled':False,'remote_users_root':'/data/users'}
        self.store=Store(self.cfg['state_root']);self.user='ou_names_alice';self.other='ou_names_bob'
        for user in (self.user,self.other):self.store.allow(user)
        self.records=[];self.inspections=[]
        def python(code,args=(),timeout=60,**kwargs):
            self.inspections.append(json.loads(base64.b64decode(args[0])))
            return json.dumps({'schema_version':1,'status':'PASS','checks':[],'issues':[]})
        ssh=SimpleNamespace(python=python)
        self.registry=Registry(self.cfg,self.store,ssh)
        # Exercise the real Registry.record name/owner-path/target validation;
        # this in-memory catalog replaces only SSH and content transport.
        def catalog(user,op,**kwargs):
            if op=='list':return self.store.list_assets(user)
            asset=kwargs['asset'];self.records.append((user,copy.deepcopy(asset)))
            previous=[a for a in self.store.list_assets(user)
                      if a['kind']==asset['kind'] and a['name']==asset['name'] and a['data']==asset['data']]
            if previous:return previous[-1]
            return self.store.add_asset(user,asset['kind'],asset['name'],asset['data'])
        self.registry.call=catalog
        dataset={'owner':'shared','origin':'dataset_catalog','kind':'dataset','name':'共享:18场景',
                 'data':{'scene_count':18}}
        self.service=SimpleNamespace(registry=self.registry,datasets=SimpleNamespace(ssh=ssh),asset=lambda *args:dataset)
        self.sources={field:self.source(self.user,field,'source-'+field)
                      for field in ('package',*TARGETS)}
        self.draft={**{field:asset['name'] for field,asset in self.sources.items()},
                    'dataset':'共享18场景','scene_scope':'1,3','goal':'replay','lpai_target':'none'}

    def tearDown(self):self.tmp.cleanup()

    def source(self,user,field,name,remote_path=None):
        content=('synthetic source bytes for '+field).encode()
        data={'remote_path':remote_path or self.store.remote_root(user,self.cfg['remote_users_root'])+'/'+field+'/asset.bin',
              'sha256':hashlib.sha256(content).hexdigest(),'size':len(content),'filename':field+'.bin'}
        if field in TARGETS:data['target']=TARGETS[field]
        return self.store.add_asset(user,'package' if field=='package' else 'dependency',name,data)

    def run_check(self,draft=None):
        with patch('bench_gateway.service.Service',return_value=self.service):
            return MaterialCheck(self.cfg,self.store).run({'id':'prep-names','owner':self.user,'payload':draft or self.draft})

    def test_one_software_name_has_fixed_distinct_role_aliases(self):
        expected={'package':'版本甲','camera_so':'版本甲-相机SO','lidar_so':'版本甲-雷达SO','m100_so':'版本甲-M100SO'}
        for field,name in expected.items():
            self.assertEqual(material_name({'software_name':'版本甲'},field,'https://ci.example/file'),name)
        self.assertEqual(material_name({'software_name':'名'*60},'m100_so','source'),'名'*60+'-M100SO')

    def test_without_software_name_original_source_hash_naming_is_retained(self):
        source='https://ci.example/package.app.tar.gz'
        suffix=hashlib.sha256(source.encode()).hexdigest()[:12]
        for field in ('package',*TARGETS):
            self.assertEqual(material_name({},field,source),'材料-'+field+'-'+suffix)
        self.assertNotEqual(material_name({},'package',source),material_name({},'package',source+'?v=2'))

    def test_named_registered_materials_alias_exact_existing_bytes_without_fetch(self):
        draft={**self.draft,'software_name':'软件包甲','build_reference':'build:2737 https://ci.example/job/2737'}
        with patch('bench_gateway.asset_fetch.fetch') as download,patch.object(self.registry,'remote_file') as remote_file:
            result=self.run_check(draft)
            download.assert_not_called();remote_file.assert_not_called()
        self.assertEqual(result['status'],'STATIC_PASSED')
        self.assertEqual(len(self.records),4)
        for field,original in self.sources.items():
            alias=result['assets'][field]
            self.assertEqual(alias['name'],material_name(draft,field,original['name']))
            self.assertEqual(alias['owner'],self.user)
            self.assertEqual(alias['data'],original['data'])
            self.assertNotEqual(alias['id'],original['id'])
            current=self.store.asset(self.user,original['kind'],original['name'])
            self.assertEqual(current,original)
        self.assertEqual(self.inspections[-1]['package'],self.sources['package']['data'])
        self.assertEqual(self.store.jobs(self.user),[])

    def test_alias_rechecking_same_material_does_not_create_new_versions(self):
        draft={**self.draft,'software_name':'可复用版本'}
        first=self.run_check(draft);second=self.run_check(draft)
        self.assertEqual(first['status'],'STATIC_PASSED');self.assertEqual(second['status'],'STATIC_PASSED')
        for field in ('package',*TARGETS):
            self.assertEqual(first['assets'][field]['id'],second['assets'][field]['id'])
            self.assertEqual(second['assets'][field]['version'],1)

    def test_other_users_same_source_name_is_not_a_fallback(self):
        self.source(self.other,'package','bob-only-package')
        result=self.run_check({**self.draft,'software_name':'我的版本','package':'bob-only-package'})
        self.assertEqual(result['status'],'NEEDS_INPUT')
        self.assertNotIn('package',result['assets'])
        self.assertTrue(any(i['field']=='package' and i['code']=='SOURCE_UNAVAILABLE' for i in result['issues']))
        self.assertFalse(any(a['name']=='我的版本' for a in self.store.list_assets(self.user)))

    def test_foreign_asset_in_a_corrupt_list_cannot_be_aliased(self):
        foreign=self.source(self.other,'package','foreign-package')
        with patch.object(self.registry,'list',return_value=[foreign,*self.sources.values()]):
            result=self.run_check({**self.draft,'software_name':'我的版本','package':foreign['name']})
        self.assertEqual(result['status'],'NEEDS_INPUT');self.assertNotIn('package',result['assets'])
        self.assertTrue(any('其他用户' in i['message'] for i in result['issues']))
        self.assertFalse(any(record[1]['name']=='我的版本' for record in self.records))

    def test_owner_label_cannot_alias_a_foreign_directory(self):
        foreign_root=self.store.remote_root(self.other,self.cfg['remote_users_root'])
        misleading=self.source(self.user,'package','misleading',foreign_root+'/package/asset.bin')
        result=self.run_check({**self.draft,'software_name':'我的版本','package':misleading['name']})
        self.assertEqual(result['status'],'NEEDS_INPUT');self.assertNotIn('package',result['assets'])
        self.assertTrue(any('本人资产目录' in i['message'] for i in result['issues']))
        self.assertFalse(any(record[1]['name']=='我的版本' for record in self.records))

    def test_aliasing_cannot_change_camera_to_a_lidar_role(self):
        draft={**self.draft,'software_name':'角色固定','camera_so':self.sources['lidar_so']['name']}
        result=self.run_check(draft)
        self.assertEqual(result['status'],'NEEDS_INPUT');self.assertNotIn('camera_so',result['assets'])
        self.assertFalse(any(record[1]['name']=='角色固定-相机SO' for record in self.records))


if __name__=='__main__':unittest.main()
