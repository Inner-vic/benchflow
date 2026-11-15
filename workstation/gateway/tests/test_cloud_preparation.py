import copy,unittest
from unittest.mock import Mock,patch
import test_preparation as fixture
from bench_gateway.preparation import MaterialCheck
from bench_gateway.incremental import frozen_inputs
from bench_gateway.intake import Intake

class CloudPreparationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PreparationTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.assets=[]
        for role,target in [('package',None),('camera_so','lib/libxcamera.so'),('lidar_so','lib/libxlidar.so'),('m100_so','lib/liburoad_m100.so')]:
            self.assets.append({'id':role,'name':role,'owner':self.f.user,'kind':'package' if role=='package' else 'dependency',
                'version':1,'data':{'sha256':'a'*64,'remote_path':'/private/'+role,**({'target':target} if target else {})}})
        self.path='/lpai/volumes/team/my-data'
        self.dataset={'id':'ds-imported','owner':self.f.user,'kind':'dataset','origin':'dataset_catalog','version':1,
            'data':{'scene_count':2,'cloud_source':{'input_root':self.path,'source_scene_count':10,'source_indices':[2,5]}}}
        self.draft={a['id']:a['id'] for a in self.assets}
        self.draft.update(dataset=self.path,scene_scope='2,5',m100_mode='provided',replay_mode='frame_by_frame')
    def run_check(self,*,cached=False,static_failure=False):
        service,calls=self.f.material_service(self.assets,self.dataset)
        cloud=Mock();cloud.plan.return_value={'scenes':[{},{}]};cloud.prepare.return_value=self.dataset
        if static_failure:
            original=service.registry.ssh.python
            def inspect(code,args=(),**kwargs):
                result=fixture.json.loads(original(code,args,**kwargs))
                result.update(status='NEEDS_INPUT',issues=[{'field':'package','code':'ABI','message':'mismatch'}])
                return fixture.json.dumps(result)
            service.registry.ssh.python=inspect
        draft={**self.draft,'dataset':self.dataset['id'] if cached else self.path}
        with patch('bench_gateway.service.Service',return_value=service),patch('bench_gateway.cloud_datasets.CloudDatasets',return_value=cloud),patch('bench_gateway.package_assembly.prepare',return_value=(self.assets[0],None)):
            result=MaterialCheck(self.f.material_config(),self.f.store).run({'id':'operator-test','owner':self.f.user,'payload':draft})
        return result,cloud,calls
    def test_software_failure_prevents_bulk_download(self):
        result,cloud,calls=self.run_check(static_failure=True)
        self.assertEqual('NEEDS_INPUT',result['status']);cloud.prepare.assert_not_called()
    def test_passed_materials_download_selected_inputs_and_rebase_local_indices(self):
        result,cloud,calls=self.run_check()
        self.assertEqual('STATIC_PASSED',result['status']);cloud.prepare.assert_called_once()
        self.assertEqual([1,2],result['scenes']);self.assertEqual([2,5],result['dataset']['data']['cloud_source']['source_indices'])
    def test_same_imported_scope_reuses_snapshot_without_planning_or_download(self):
        result,cloud,calls=self.run_check(cached=True)
        self.assertEqual('STATIC_PASSED',result['status']);cloud.plan.assert_not_called();cloud.prepare.assert_not_called()
    def test_scope_change_uses_original_cloud_root(self):
        self.draft['scene_scope']='3'
        result,cloud,calls=self.run_check(cached=True)
        cloud.plan.assert_called_once_with(self.f.user,self.path,'3');cloud.prepare.assert_called_once()
    def test_frozen_memory_keeps_exact_imported_snapshot(self):
        from bench_gateway.core import Store
        draft={'dataset':self.path,'scene_scope':'2,5'}
        payload={'action':'replay','assets':{'dataset':self.dataset}}
        job,_=self.f.store.submit(self.f.user,self.f.session,'isolated-memory',payload)
        with self.f.store.connect() as c:current=frozen_inputs(c,self.f.user,{'job_id':job['id']},draft)
        self.assertEqual(self.dataset['id'],current['dataset']);self.assertEqual('2,5',current['scene_scope'])
    def test_replay_mode_survives_sensor_and_new_app_updates(self):
        Intake(self.f.store).update(self.f.user,{'replay_mode':'frame_by_frame'})
        Intake(self.f.store).update(self.f.user,{'camera_so':'new-camera'})
        self.assertEqual('frame_by_frame',Intake(self.f.store).get(self.f.user)['replay_mode'])
        Intake(self.f.store).update(self.f.user,{'package':'new-app'})
        self.assertEqual('frame_by_frame',Intake(self.f.store).get(self.f.user)['replay_mode'])

if __name__=='__main__':unittest.main()
